"""Record tests as Playwright scripts during AI runs, and replay them without AI.

While a tester agent works through a suite, Playwright MCP reports the code for
every browser action ("### Ran Playwright code"). The agent also calls two
argus tools: start_step (so actions are grouped by step) and check (one
machine-checkable assertion per acceptance criterion). The Recorder turns that
stream into one self-contained JavaScript module per test.

Scripts are replayed by runner/replay.mjs with plain Playwright, in a fresh
browser context per test. Project values never appear in scripts: typed values
become v('role.password') lookups, and URLs on the run's site become url('/path').
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

from argus_qa.events import CODE_BLOCK
from argus_qa.plan_parser import TestCase, case_fields

RUNNER_JS = Path(__file__).parent / "runner" / "replay.mjs"
PLAYWRIGHT_TOOL = "mcp__playwright__browser_"
ARGUS_TOOL = "mcp__argus__"

# Playwright MCP tools whose code isn't part of a test's behaviour
_NOT_REPLAYED = {
    "take_screenshot", "snapshot", "console_messages", "network_requests", "network_request",
    "find", "close", "install",
}
CHECK_KINDS = ("text_visible", "text_hidden", "element_visible", "url_contains", "field_value", "manual")
# What a check of each kind must say for a script to verify it
_CHECK_NEEDS = {
    "text_visible": "text", "text_hidden": "text", "element_visible": "role",
    "url_contains": "value", "field_value": "label",
}


class RunnerError(RuntimeError):
    """The script runner couldn't be installed or didn't run."""


def check_problem(check: dict) -> str | None:
    """Why a check can't be recorded, or None if it can. The check tool answers with this, and
    the Recorder leaves such a check out: the agent sees the error and calls again."""
    kind = check.get("kind")
    if kind not in CHECK_KINDS:
        return f"Unknown kind {kind!r}; use one of {', '.join(CHECK_KINDS)}."
    needs = _CHECK_NEEDS.get(kind)
    if needs and not check.get(needs):
        return f"A {kind} check needs `{needs}`."
    return None


def case_hash(case: TestCase) -> str:
    """Identifies the test's current wording; a script recorded for other wording is outdated."""
    return hashlib.sha256(case.raw_markdown.strip().encode()).hexdigest()[:16]


# ── Recording ────────────────────────────────────────────────────────


@dataclass
class RecordedTest:
    test_id: str
    steps: dict[int, list[str]] = field(default_factory=dict)  # step number -> code blocks
    checks: list[dict] = field(default_factory=list)
    actions: int = 0
    # Everything in the order it happened: ("step", n), ("code", js), ("check", {...}).
    # Checks must replay where they were verified: "the login form is shown" only holds
    # before logging in.
    items: list[tuple] = field(default_factory=list)


class Recorder:
    """Collects a tester agent's tool calls, in the order it made them, into per-test recordings."""

    def __init__(self) -> None:
        self._events: list[dict] = []
        self._pending: dict[str, int] = {}

    def on_tool_use(self, tool_id: str, name: str, tool_input: dict) -> None:
        args = tool_input if isinstance(tool_input, dict) else {}
        if name == ARGUS_TOOL + "start_test":
            self._events.append({"kind": "test", "test": _norm_id(args.get("test_id"))})
        elif name == ARGUS_TOOL + "start_step":
            test_id, step = _norm_id(args.get("test_id")), _int(args.get("step"))
            self._events.append({"kind": "step", "test": test_id, "step": step})
        elif name == ARGUS_TOOL + "check":
            if check_problem(args):
                return  # the tool turned it down
            check = {k: v for k, v in args.items() if k != "test_id"}
            self._events.append({"kind": "check", "test": _norm_id(args.get("test_id")), "check": check})
        elif name.startswith(PLAYWRIGHT_TOOL) and name[len(PLAYWRIGHT_TOOL):] not in _NOT_REPLAYED:
            self._pending[tool_id] = len(self._events)
            self._events.append({"kind": "action", "code": None})

    def on_tool_result(self, tool_id: str, content, is_error: bool | None) -> None:
        index = self._pending.pop(tool_id, None)
        if index is None:
            return
        text = content_text(content)
        if is_error or text.lstrip().startswith("### Error"):
            return  # failed attempts aren't part of the test
        match = CODE_BLOCK.search(text)
        if match:
            self._events[index]["code"] = match.group(1).strip()

    def tests(self) -> dict[str, RecordedTest]:
        """Group the recorded actions and checks by test and step.

        Actions between start_test and the first start_step (the browser reset)
        are left out: replays start each test in a fresh browser context.
        """
        recorded: dict[str, RecordedTest] = {}
        current: RecordedTest | None = None
        step: int | None = None
        for event in self._events:
            kind = event["kind"]
            if kind == "test":
                current = recorded.setdefault(event["test"], RecordedTest(event["test"]))
                step = None
            elif kind == "step":
                current = recorded.setdefault(event["test"], RecordedTest(event["test"]))
                step = event["step"]
                current.steps.setdefault(step, [])
                current.items.append(("step", step))
            elif kind == "check":
                target = recorded.setdefault(event["test"], RecordedTest(event["test"]))
                target.checks.append(event["check"])
                target.items.append(("check", event["check"]))
            elif kind == "action" and current is not None and step is not None and event["code"]:
                current.steps[step].append(event["code"])
                current.items.append(("code", event["code"]))
                current.actions += 1
        return recorded


# ── Turning a recording into a script ────────────────────────────────


def build_script(
    case: TestCase,
    recording: RecordedTest,
    base_url: str,
    values: dict[str, str],
    secret_names: set[str],
) -> tuple[str | None, str]:
    """JavaScript for one test, or (None, reason) when the recording can't be replayed safely."""
    if recording.actions == 0:
        return None, "no browser actions were recorded"
    if not recording.checks:
        return None, "no acceptance criteria were recorded as checks"
    manual = [c.get("criterion", "") for c in recording.checks if c.get("kind") not in CHECK_KINDS[:-1]]
    if manual:
        return None, f"a criterion needs judgement and can't be checked by a script: {manual[0]!r}"

    fields = case_fields(case)
    step_text = {0: "Before you start", **{n: _plain(s) for n, s in enumerate(fields["steps"], 1)}}
    lines = [
        f"// argus-qa script for {case.id}: {case.name}",
        f"// Recorded {datetime.now(UTC):%Y-%m-%d %H:%M} UTC from an AI run. Replayed without AI.",
        "// v('name') reads a project value; url('/path') is relative to the run's URL.",
        "export default async function run(page, { step, check, v, url }) {",
    ]
    # Replay in recorded order: a step's code in one block, checks where they were verified
    current: int | None = None
    block: list[str] = []

    def flush() -> None:
        if current is not None and block:
            label = step_text.get(current, f"Step {current}")
            lines.append(f"  await step({current}, {json.dumps(label)}, async () => {{")
            lines.extend(block)
            lines.append("  });")
        block.clear()

    for kind, value in recording.items:
        if kind == "step":
            flush()
            current = value
        elif kind == "code":
            for line in _portable(value, base_url, values).splitlines():
                if line.strip() and not line.strip().startswith("//"):
                    block.append("    " + line.rstrip())
        elif kind == "check":
            flush()
            lines.append("  " + _check_call(value, base_url, values))
    flush()
    lines.append("}")
    script = "\n".join(lines) + "\n"

    leaked = [n for n in secret_names if values.get(n) and len(values[n]) >= 4 and values[n] in script]
    if leaked:
        return None, "a secret value appears in the recorded code in a way that can't be replaced safely"
    return script, "ok"


def _plain(text: str) -> str:
    """Step text without Markdown emphasis or code marks, for labels."""
    return re.sub(r"[*`_]{1,2}([^*`_]+)[*`_]{1,2}", r"\1", text).strip()


def _check_call(c: dict, base_url: str, values: dict[str, str]) -> str:
    criterion = json.dumps(str(c.get("criterion", "")))

    def lit(key: str) -> str:
        return _value_expr(str(c.get(key, "")), values)

    kind = c.get("kind")
    if kind == "text_visible":
        return f"await check.textVisible({lit('text')}, {criterion});"
    if kind == "text_hidden":
        return f"await check.textHidden({lit('text')}, {criterion});"
    if kind == "element_visible":
        role = json.dumps(str(c.get("role", "")))
        return f"await check.elementVisible({role}, {lit('name')}, {criterion});"
    if kind == "url_contains":
        value = str(c.get("value") or c.get("text") or "")
        origin = _origin(base_url)
        if origin and value.startswith(origin):
            value = value[len(origin):] or "/"
        return f"await check.urlContains({_value_expr(value, values)}, {criterion});"
    if kind == "field_value":
        return f"await check.fieldValue({lit('label')}, {lit('value')}, {criterion});"
    raise ValueError(f"unknown check kind {kind!r}")


def _portable(code: str, base_url: str, values: dict[str, str]) -> str:
    """Replace project values with v('name') and the run's own URLs with url('/path')."""
    origin = _origin(base_url)

    def literal(m: re.Match) -> str:
        raw = _unescape(m.group(2))
        if origin and raw.startswith(origin) and (len(raw) == len(origin) or raw[len(origin)] in "/?#"):
            return f"url({json.dumps(raw[len(origin):] or '/')})"
        return _value_expr(raw, values, fallback=m.group(0))

    return re.sub(r"(['\"])((?:\\.|(?!\1).)*)\1", literal, code)


def _value_expr(raw: str, values: dict[str, str], fallback: str | None = None) -> str:
    for name, value in sorted(values.items(), key=lambda kv: -len(kv[1] or "")):
        if value and len(value) >= 3 and raw == value:
            return f"v({json.dumps(name)})"
    return fallback if fallback is not None else json.dumps(raw)


def _unescape(text: str) -> str:
    try:
        return json.loads('"' + text.replace('"', '\\"').replace("\\'", "'") + '"')
    except json.JSONDecodeError:
        return text


def _origin(url: str) -> str:
    parsed = urlparse(url or "")
    return f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme and parsed.netloc else ""


def _norm_id(value) -> str:
    return str(value or "").strip().upper()


def _int(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def content_text(content) -> str:
    if isinstance(content, str):
        return content
    parts = []
    for item in content or []:
        if isinstance(item, dict) and item.get("type") == "text":
            parts.append(item.get("text", ""))
        elif hasattr(item, "text"):
            parts.append(str(item.text))
    return "\n".join(parts)


# ── Replaying ────────────────────────────────────────────────────────


def runner_dir() -> Path:
    return Path(os.environ.get("ARGUS_RUNNER_DIR") or Path.home() / ".cache" / "argus-qa" / "runner")


def ensure_runner(playwright_core_version: str | None = None) -> Path:
    """Install the Playwright library the replay runner needs: the same version the AI's browser
    uses, so both drive the same Chromium build. Without a version, any installed one will do."""
    directory = runner_dir()
    marker = directory / "node_modules" / "playwright-core" / "package.json"
    if marker.is_file():
        installed = json.loads(marker.read_text()).get("version")
        if playwright_core_version is None or installed == playwright_core_version:
            return directory
    try:
        if playwright_core_version is None:
            from argus_qa.agents.orchestrator import playwright_core_version as lookup

            playwright_core_version = lookup()
        directory.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["npm", "install", "--prefix", str(directory), "--no-save", "--no-audit", "--no-fund",
             f"playwright-core@{playwright_core_version}"],
            check=True, capture_output=True, text=True,
        )
    except (OSError, subprocess.CalledProcessError) as e:
        detail = (getattr(e, "stderr", None) or str(e)).strip()[-500:]
        raise RunnerError(f"The script runner couldn't be installed: {detail}") from e
    return directory


async def replay(
    scripts: dict[str, Path],
    base_url: str,
    values: dict[str, str],
    screenshot_dir: Path,
    runner: Path,
    headless: bool = True,
) -> dict[str, dict]:
    """Replay test scripts (test ID -> .mjs file). Returns test ID -> result from the runner."""
    if not scripts:
        return {}
    job = {
        "base_url": base_url,
        "values": values,
        "screenshot_dir": str(screenshot_dir.resolve()),
        "headless": headless,
        "no_sandbox": "--no-sandbox" in os.environ.get("ARGUS_PLAYWRIGHT_ARGS", ""),
        "tests": [{"id": tid, "file": str(path.resolve())} for tid, path in scripts.items()],
    }
    screenshot_dir.mkdir(parents=True, exist_ok=True)
    try:
        proc = await asyncio.create_subprocess_exec(
            "node", str(RUNNER_JS),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            env={**os.environ, "ARGUS_RUNNER_DIR": str(runner)},
        )
    except OSError as e:
        raise RunnerError(f"The script runner couldn't start: {e}") from e
    out, err = await proc.communicate(json.dumps(job).encode())
    try:
        results = json.loads(out.decode() or "{}").get("results", [])
    except json.JSONDecodeError:
        results = []
    if proc.returncode != 0 and not results:
        raise RunnerError(f"The script runner failed: {err.decode().strip()[-500:] or 'no output'}")
    return {r["id"]: r for r in results}
