"""Orchestrator — two modes: analyze (explore → scaffold) and test (execute plan → report)."""

from __future__ import annotations

import asyncio
import functools
import hashlib
import json
import os
import random
import re
import shlex
import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

import httpx
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
    create_sdk_mcp_server,
    query,
    tool,
)

from argus_qa import colors as c
from argus_qa.events import describe_tool, emit
from argus_qa.plan_parser import TestCase, TestPlan, compose_plan, parse_test_plan
from argus_qa.project import Project, Redactor, substitute
from argus_qa.prompts.bug_hunter import BUG_HUNTER_PROMPT
from argus_qa.prompts.drafter import DRAFTER_PROMPT
from argus_qa.prompts.explorer import EXPLORER_PROMPT
from argus_qa.prompts.guardrails import GUARDRAILS
from argus_qa.prompts.reporter import REPORTER_PROMPT
from argus_qa.prompts.scaffold import SCAFFOLD_PROMPT
from argus_qa.prompts.tester import RECORDING_INSTRUCTIONS, TESTER_PROMPT
from argus_qa.results import (
    bugs_to_cases,
    discovery_report,
    exploration_report,
    extract_json,
    merge_results,
    script_result,
    summarize,
    test_report,
    to_junit,
)
from argus_qa.results import failed_ids as _failed_ids
from argus_qa.scripts import CHECK_KINDS, RecordedTest, Recorder, build_script, ensure_runner, replay

AGENT_NAMES = [
    "Chad the Clicker",
    "Button Basher Betty",
    "Tab-Smasher Todd",
    "Pixel Inspector Pam",
    "404 Hunter Frank",
    "Rage Clicker Rita",
    "Form Filler Phil",
    "Scroll Lord Steve",
    "Bug Sniper Brenda",
    "Chaos Monkey Carl",
    "Tooltip Tina",
    "Dropdown Dave",
    "Cookie Monster Claire",
    "Refresh Randy",
    "Screenshot Sally",
    "Lag Detector Larry",
]

# Pinned so upstream releases can't silently change browser behaviour between runs
PLAYWRIGHT_MCP_PACKAGE = "@playwright/mcp@0.0.82"

# Model per agent role. Following written steps and writing up results don't need the
# largest model; deciding what to explore and what counts as a bug does. Override with
# ARGUS_MODEL (every role) or ARGUS_MODEL_TESTER / _EXPLORER / _WRITER.
DEFAULT_MODELS = {"tester": "claude-sonnet-5", "writer": "claude-sonnet-5", "explorer": "claude-opus-5-5"}


def model_for(role: str) -> str:
    return (
        os.environ.get(f"ARGUS_MODEL_{role.upper()}")
        or os.environ.get("ARGUS_MODEL")
        or DEFAULT_MODELS[role]
    )


# Whether each tester action returns a snapshot of the page ("full") or the tester asks
# for one when it needs to look ("none", the default: measured cheaper with the same
# results). Set with ARGUS_TESTER_SNAPSHOTS.
def tester_snapshots() -> bool:
    return os.environ.get("ARGUS_TESTER_SNAPSHOTS", "none").lower() == "full"


_PAGE_READING = {
    True: "After each action you get a snapshot of the page; use it rather than asking for a new one.",
    False: (
        "Actions don't return the page. Call `browser_snapshot` when you need to see it (after "
        "navigating, or to check a result); element refs from an older snapshot may be stale."
    ),
}


# Turn limits per agent, so a confused agent can't loop forever
TESTER_MAX_TURNS = 300
EXPLORER_MAX_TURNS = 150
REPORTER_MAX_TURNS = 5

# Why an agent stopped early, in words (ResultMessage.subtype -> message)
_STOP_REASONS = {
    "error_max_turns": "reached its turn limit",
    "error_max_budget_usd": "reached its cost limit",
}


@functools.cache
def playwright_core_version() -> str:
    """The Playwright version behind the pinned Playwright MCP (so the script runner matches it)."""
    return subprocess.run(
        ["npm", "view", PLAYWRIGHT_MCP_PACKAGE, "dependencies.playwright-core"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()


def install_browser() -> None:
    """Download the Chromium build that the pinned Playwright MCP version uses, and the script runner."""
    core_version = playwright_core_version()
    subprocess.run(["npx", "-y", f"playwright-core@{core_version}", "install", "chromium"], check=True)
    ensure_runner(core_version)


def _pick_agent_name() -> str:
    return random.choice(AGENT_NAMES)


def _make_playwright_mcp(
    headless: bool = False,
    isolated: bool = False,
    output_dir: Path | None = None,
    images: bool = True,
    snapshots: bool = True,
) -> dict:
    """Create a Playwright MCP config.

    Isolated instances keep their browser profile in memory, so multiple
    browsers can run side by side without conflicting. output_dir is where
    the browser saves screenshots. Without images, screenshots are still saved
    to disk but not sent back to the model, which saves a lot of tokens. Extra flags (e.g. "--browser chromium
    --no-sandbox" in containers) can be passed via ARGUS_PLAYWRIGHT_ARGS.
    """
    extra = shlex.split(os.environ.get("ARGUS_PLAYWRIGHT_ARGS", ""))
    if "--browser" not in extra:
        # Playwright's bundled Chromium (installed by `argus-qa setup`), rather than
        # whatever Chrome happens to be installed on the machine
        extra = ["--browser", "chromium", *extra]
    args = [PLAYWRIGHT_MCP_PACKAGE, *extra]
    if headless:
        args.append("--headless")
    if isolated:
        args.append("--isolated")
    if output_dir is not None:
        args.extend(["--output-dir", str(output_dir.resolve())])
    if not images:
        args.extend(["--image-responses", "omit"])
    if not snapshots:
        # Actions don't return the page; the agent asks for a snapshot when it needs one
        args.extend(["--snapshot-mode", "none"])
    return {"playwright": {"command": "npx", "args": args}}


def _tool_text(text: str, error: bool = False) -> dict:
    return {"content": [{"type": "text", "text": text}], **({"is_error": True} if error else {})}


_CHECK_SCHEMA = {
    "type": "object",
    "properties": {
        "test_id": {"type": "string", "description": "The test case ID, e.g. TC-003"},
        "criterion": {"type": "string", "description": "The acceptance criterion, as written in the test"},
        "kind": {"type": "string", "enum": list(CHECK_KINDS)},
        "text": {"type": "string", "description": "text_visible / text_hidden: exact text on the page"},
        "role": {"type": "string", "description": "element_visible: ARIA role, e.g. button, heading, link"},
        "name": {"type": "string", "description": "element_visible: the element's accessible name"},
        "label": {"type": "string", "description": "field_value: the field's label"},
        "value": {"type": "string", "description": "url_contains: part of the URL; field_value: the value"},
    },
    "required": ["test_id", "criterion", "kind"],
}


def _argus_tools():
    """In-process tools the tester uses to mark tests, steps, and checks while recording."""

    @tool("start_test", "Call at the very start of each test case, before resetting the browser.",
          {"test_id": str})
    async def start_test(args):
        return _tool_text(f"Recording {args.get('test_id', '')}. Now reset the browser as instructed.")

    @tool("start_step", "Call right before doing a step's actions. Step 0 is the test's preconditions.",
          {"test_id": str, "step": int})
    async def start_step(args):
        return _tool_text(f"Step {args.get('step')} of {args.get('test_id', '')}.")

    @tool("check", "Record one acceptance criterion that holds, as something a script can verify later.",
          _CHECK_SCHEMA)
    async def check(args):
        kind = args.get("kind")
        needs = {"text_visible": "text", "text_hidden": "text", "element_visible": "role",
                 "url_contains": "value", "field_value": "label"}.get(kind)
        if kind not in CHECK_KINDS:
            return _tool_text(f"Unknown kind {kind!r}; use one of {', '.join(CHECK_KINDS)}.", error=True)
        if needs and not args.get(needs):
            return _tool_text(f"A {kind} check needs `{needs}`.", error=True)
        return _tool_text("Recorded.")

    return create_sdk_mcp_server(name="argus", tools=[start_test, start_step, check])


def _browser_options(
    headless: bool = False,
    isolated: bool = False,
    output_dir: Path | None = None,
    max_budget_usd: float | None = None,
    max_turns: int | None = TESTER_MAX_TURNS,
    model: str | None = None,
    images: bool = True,
    snapshots: bool = True,
    record: bool = False,
) -> ClaudeAgentOptions:
    """Options for agents that need browser access.

    With an output_dir, the agent (and so its browser) also runs from that
    directory: Playwright MCP resolves explicit screenshot filenames against its
    working directory, while --output-dir only covers auto-named files.
    """
    servers = _make_playwright_mcp(
        headless=headless, isolated=isolated, output_dir=output_dir, images=images, snapshots=snapshots,
    )
    if record:
        servers["argus"] = _argus_tools()
    return ClaudeAgentOptions(
        mcp_servers=servers,
        model=model,
        tools=[],
        allowed_tools=["mcp__playwright__*", *(["mcp__argus__*"] if record else [])],
        cwd=str(output_dir.resolve()) if output_dir is not None else None,
        max_budget_usd=max_budget_usd,
        max_turns=max_turns,
    )


def _reasoning_options(max_budget_usd: float | None = None, model: str | None = None) -> ClaudeAgentOptions:
    """Options for agents that only need to think and write."""
    return ClaudeAgentOptions(
        tools=[], allowed_tools=[], max_budget_usd=max_budget_usd, max_turns=REPORTER_MAX_TURNS,
        model=model,
    )


def _share(budget: float | None, fraction: float) -> float | None:
    return round(budget * fraction, 4) if budget else None


@dataclass
class AgentRun:
    text: str
    ok: bool
    cost_usd: float = 0.0
    turns: int = 0
    models: list[str] = field(default_factory=list)
    recording: dict[str, RecordedTest] | None = None


def _models(runs: list[AgentRun]) -> list[str]:
    """Distinct models used across agents, e.g. ['claude-sonnet-5']."""
    return sorted({m for r in runs for m in r.models})


async def _run_agent(
    prompt: str,
    options: ClaudeAgentOptions,
    label: str = "",
    redact: Redactor | None = None,
    recorder: Recorder | None = None,
) -> AgentRun:
    """Run a single agent query and return its final result, with secrets redacted.

    A recorder, if given, sees every tool call and result (for recording scripts)."""
    redact = redact or Redactor()
    run = AgentRun(text="", ok=False)
    async for message in query(prompt=prompt, options=options):
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock):
                    line = redact(block.text.strip().replace("\n", " "))
                    if line:
                        spoken = _prose(redact(block.text.strip()))
                        if spoken:
                            emit("say", spoken, agent=label)
                        # Highlight keywords in the output
                        line = _colorize_line(line)
                        if label:
                            print(c.agent_line(label, line))
                        else:
                            print(f"  {c.DIM}>{c.RESET} {line}")
                elif isinstance(block, ToolUseBlock):
                    if recorder is not None:
                        recorder.on_tool_use(block.id, block.name, block.input)
                    if block.name.startswith("mcp__argus__"):
                        continue
                    tool_name, text, extra = describe_tool(block.name, block.input)
                    emit("action", redact(text), agent=label, tool=tool_name, **extra)
        elif isinstance(message, UserMessage) and recorder is not None:
            for block in message.content if isinstance(message.content, list) else []:
                if isinstance(block, ToolResultBlock):
                    recorder.on_tool_result(block.tool_use_id, block.content, block.is_error)
        elif isinstance(message, ResultMessage):
            run.cost_usd = message.total_cost_usd or 0.0
            run.turns = message.num_turns
            run.models = sorted(m.split("[")[0] for m in (message.model_usage or {}))
            if message.subtype == "success" and not message.is_error:
                run.ok = True
                run.text = redact(message.result or "")
            else:
                reason = _STOP_REASONS.get(message.subtype, message.subtype)
                print(c.error(f"  Agent stopped: {reason}"))
                emit("error", f"Stopped early: {reason}", agent=label)
                run.text = f"Agent error: {message.subtype}"
    return run


def _prose(text: str) -> str:
    """The narration part of an agent message, without the JSON report it may end with."""
    for marker in ("```json", "```\n{", "\n{"):
        if marker in text:
            text = text.split(marker, 1)[0]
    text = text.strip()
    return "" if text.startswith("{") else text


_STATUS_WORDS = [
    (re.compile(r"\b(passed|passes)\b", re.I), c.GREEN),
    (re.compile(r"\b(failed|fails|failure)\b", re.I), c.RED),
    (re.compile(r"\bblocked\b", re.I), c.YELLOW),
    (re.compile(r"\bskipped\b", re.I), c.DIM),
    (re.compile(r"\b(bug|error)s?\b", re.I), c.RED),
]


def _colorize_line(line: str) -> str:
    """Add inline color hints to agent output for key events."""
    for pattern, color in _STATUS_WORDS:
        if pattern.search(line):
            return f"{color}{line}{c.RESET}"
    if line.startswith("TC-") or line.startswith("Now "):
        return f"{c.CYAN}{line}{c.RESET}"
    return line


# ── Analyze mode ────────────────────────────────────────────────────


async def run_analyze(
    url: str | None,
    output_file: str | None = None,
    credentials: dict[str, str] | None = None,
    headless: bool = False,
    project: Project | None = None,
    focus: str = "",
    max_cost_usd: float | None = None,
    output_dir: str = "reports",
) -> str:
    """Explore a website and write a test plan scaffold (CLI)."""
    url = url or (project.url if project else None)
    if not url:
        raise ValueError("No URL: pass one, or set `url` in the project.")
    output = Path(output_file or "testplan.md")
    stamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    run_dir = Path(output_dir) / site_dir_name(url) / f"{stamp}_discover"

    result = await discover_app(
        url, run_dir, project=project, credentials=credentials, focus=focus,
        headless=headless, max_cost_usd=max_cost_usd,
    )
    if result.environment_error:
        raise RuntimeError(
            f"The browser couldn't explore the app: {result.environment_error}\n"
            "  If the browser isn't installed, run: argus-qa setup"
        )
    output.write_text(result.raw_plan)
    print(c.success(f"\n  Test plan saved to {output}\n"))
    print(c.dim(f"  Exploration notes: {run_dir}   Cost: ${result.cost_usd:.2f}"))
    return str(output)


# ── Drafting test cases ─────────────────────────────────────────────

DRAFT_MAX_COST_USD = 0.10


def _draft_context(project: Project | None) -> str:
    """What the drafter may know about a project: role and value names, never their values."""
    if project is None:
        return "No project: don't use logins or {{placeholders}}."
    lines = [f"Name: {project.name}"]
    if project.description:
        lines.append(f"About: {project.description}")
    if project.credentials:
        lines.append("Test account roles: " + ", ".join(project.credentials))
    names = [*project.variables, *project.secrets]
    if names:
        lines.append("Project values: " + ", ".join(names))
    return "\n".join(lines)


async def draft_test_case(description: str, project: Project | None = None) -> tuple[dict, float]:
    """Draft one test case's fields from a plain-English sentence. Returns (fields, cost)."""
    run = await _run_agent(
        prompt=DRAFTER_PROMPT.format(
            description=description.strip(), project_context=_draft_context(project)
        ),
        options=_reasoning_options(max_budget_usd=DRAFT_MAX_COST_USD, model=model_for("writer")),
        label="drafter",
    )
    data = _parse_json(run.text) if run.ok else None
    if not data:
        raise RuntimeError("Couldn't draft a test case from that description. Try rephrasing it.")

    def items(key: str) -> list[str]:
        value = data.get(key) or []
        return [str(v).strip() for v in (value if isinstance(value, list) else [value]) if str(v).strip()]

    fields = {
        "title": str(data.get("title") or description).strip()[:120],
        "priority": str(data.get("priority") or "").lower(),
        "category": str(data.get("category") or "").lower(),
        "preconditions": items("preconditions"),
        "steps": items("steps"),
        "expected": items("expected"),
        "notes": "",
    }
    return fields, run.cost_usd


# ── Discovery & exploration sessions ────────────────────────────────


@dataclass
class SessionResult:
    """Outcome of a discover or explore session."""

    run_dir: Path
    summary: dict
    proposed: list[TestCase]
    cost_usd: float = 0.0
    environment_error: str | None = None
    raw_plan: str = ""
    models: list[str] = field(default_factory=list)


def _login_text(project: Project | None, credentials: dict[str, str] | None) -> str:
    if project:
        text = project.prompt_section()
        if project.credentials:
            text += "\nLog in with the test accounts above to explore authenticated areas."
        return text
    if credentials:
        return (
            f"Use these to log in:\n- Username: `{credentials['username']}`\n"
            f"- Password: `{credentials['password']}`"
        )
    return "No credentials provided. Explore only public/unauthenticated areas."


def _guardrails(url: str) -> str:
    return GUARDRAILS.format(host=urlparse(url).hostname or url)


def _parse_json(text: str) -> dict | None:
    try:
        data = json.loads(extract_json(text))
    except (json.JSONDecodeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


async def discover_app(
    url: str,
    run_dir: Path,
    project: Project | None = None,
    credentials: dict[str, str] | None = None,
    focus: str = "",
    existing_tests: list[str] | None = None,
    headless: bool = False,
    isolated: bool = False,
    max_cost_usd: float | None = None,
) -> SessionResult:
    """Explore an app and propose test cases. Writes exploration.json, proposed.md, report.md."""
    ss_dir = run_dir / "screenshots"
    ss_dir.mkdir(parents=True, exist_ok=True)
    secrets = project.secret_values() if project else []
    if credentials and credentials.get("password"):
        secrets.append(credentials["password"])
    redact = Redactor(secrets)

    explorer_name = _pick_agent_name()
    print(c.phase(1, 2, f"{explorer_name} is exploring the application..."))
    emit("phase", f"{explorer_name} is exploring the application")
    exploration = await _run_agent(
        prompt=EXPLORER_PROMPT.format(
            url=url,
            credentials=_login_text(project, credentials),
            focus=focus.strip() or "The whole application: its main pages, features, and user flows.",
            guardrails=_guardrails(url),
        ),
        options=_browser_options(
            headless=headless, isolated=isolated, output_dir=ss_dir,
            max_budget_usd=_share(max_cost_usd, 0.8), max_turns=EXPLORER_MAX_TURNS,
            model=model_for("explorer"), images=False,
        ),
        label=explorer_name,
        redact=redact,
    )
    _redact_text_files(ss_dir, redact)
    if not exploration.ok:
        raise RuntimeError(f"Exploration failed: {exploration.text}")
    data = _parse_json(exploration.text)
    (run_dir / "exploration.json").write_text(json.dumps(data or {"raw": exploration.text}, indent=2))
    if data and data.get("environment_error"):
        emit("error", f"The browser couldn't explore the app: {data['environment_error']}")
        return SessionResult(run_dir, {}, [], exploration.cost_usd, str(data["environment_error"]),
                             models=exploration.models)
    print(c.success("\n  Exploration complete.\n"))

    guidance = ""
    if project and project.credentials:
        roles = ", ".join(project.credentials)
        guidance += (
            f"Test accounts are configured in the project (roles: {roles}). Do NOT include a "
            "Credentials section or any usernames/passwords in the plan. Write login steps as "
            "'Log in as <role>'.\n"
        )
    if existing_tests:
        guidance += (
            "These tests already exist; don't propose duplicates of them:\n"
            + "".join(f"- {t}\n" for t in existing_tests)
        )
    if focus.strip():
        guidance += f"Concentrate the test cases on this focus: {focus.strip()}\n"

    print(c.phase(2, 2, "Proposing test cases..."))
    emit("phase", "Writing test cases from what was found")
    scaffold = await _run_agent(
        prompt=SCAFFOLD_PROMPT.format(
            url=url,
            timestamp=datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC"),
            exploration_report=exploration.text,
            project_guidance=guidance,
        ),
        options=_reasoning_options(max_budget_usd=_share(max_cost_usd, 0.2), model=model_for("writer")),
        label="scaffold",
        redact=redact,
    )
    cost = exploration.cost_usd + scaffold.cost_usd
    if not scaffold.ok:
        raise RuntimeError(f"Test plan generation failed: {scaffold.text}")
    cases = parse_test_plan(scaffold.text).cases
    if not cases:
        (run_dir / "proposed.rejected.md").write_text(scaffold.text)
        raise RuntimeError("The proposed plan contains no '### TC-NNN:' test cases.")

    title = (data or {}).get("title") or (project.name if project else urlparse(url).hostname or url)
    (run_dir / "proposed.md").write_text(compose_plan(str(title), url, cases))
    (run_dir / "report.md").write_text(discovery_report(data, cases, url))
    summary = {
        "proposed": len(cases),
        "pages": len((data or {}).get("pages") or []),
        "flows": len((data or {}).get("user_flows") or []),
    }
    emit("phase", f"Proposed {len(cases)} test cases")
    return SessionResult(run_dir, summary, cases, cost, raw_plan=scaffold.text,
                         models=_models([exploration, scaffold]))


async def explore_app(
    url: str,
    run_dir: Path,
    charter: str,
    project: Project | None = None,
    headless: bool = False,
    isolated: bool = False,
    max_cost_usd: float | None = None,
) -> SessionResult:
    """Hunt for bugs without a script. Writes results.json, proposed.md (regression tests), report.md."""
    ss_dir = run_dir / "screenshots"
    ss_dir.mkdir(parents=True, exist_ok=True)
    redact = Redactor(project.secret_values() if project else [])

    name = _pick_agent_name()
    print(c.phase(1, 1, f"{name} is hunting for bugs..."))
    emit("phase", f"{name} is hunting for bugs")
    run = await _run_agent(
        prompt=BUG_HUNTER_PROMPT.format(
            url=url,
            project_context=project.prompt_section() if project else "",
            charter=charter.strip(),
            guardrails=_guardrails(url),
        ),
        options=_browser_options(
            headless=headless, isolated=isolated, output_dir=ss_dir,
            max_budget_usd=max_cost_usd, max_turns=EXPLORER_MAX_TURNS,
            model=model_for("explorer"), images=True,  # sees the page, to spot visual bugs
        ),
        label=name,
        redact=redact,
    )
    _redact_text_files(ss_dir, redact)
    data = _parse_json(run.text) if run.ok else None
    if data is None:
        raise RuntimeError(f"The exploratory session produced no report: {run.text[:300]}")
    if data.get("environment_error"):
        emit("error", f"The browser couldn't explore the app: {data['environment_error']}")
        return SessionResult(run_dir, {}, [], run.cost_usd, str(data["environment_error"]), models=run.models)

    bugs = [b for b in data.get("bugs") or [] if isinstance(b, dict)]
    cases = bugs_to_cases(bugs)
    (run_dir / "results.json").write_text(json.dumps(data, indent=2))
    (run_dir / "report.md").write_text(exploration_report(data, url))
    if cases:
        (run_dir / "proposed.md").write_text(compose_plan("Regression tests from exploration", url, cases))
    summary = {"bugs": len(bugs), "proposed": len(cases)}
    emit("phase", f"Found {len(bugs)} bug{'s' if len(bugs) != 1 else ''}")
    return SessionResult(run_dir, summary, cases, run.cost_usd, models=run.models)


# ── Test mode ───────────────────────────────────────────────────────


@dataclass
class RunResult:
    run_dir: Path
    report_file: Path
    results: dict
    failed_ids: list[str] = field(default_factory=list)
    cost_usd: float = 0.0
    models: list[str] = field(default_factory=list)
    recorded: dict[str, str] = field(default_factory=dict)  # test ID -> verified script

    @property
    def environment_error(self) -> str | None:
        """Set when the browser/tools broke, meaning the results say nothing about the app."""
        errors = self.results.get("environment_errors")
        return "; ".join(dict.fromkeys(errors)) if errors else None


def site_dir_name(url: str) -> str:
    """Filesystem-safe name for the site under test, e.g. 'localhost-3000'."""
    parsed = urlparse(url)
    host = parsed.hostname or "unknown"
    site = f"{host}-{parsed.port}" if parsed.port else host
    return re.sub(r"[^\w\-.]", "_", site)


async def run_tests(
    test_plan_file: str,
    url: str | None = None,
    output_dir: str = "reports",
    only: list[str] | None = None,
    skip: list[str] | None = None,
    parallel: int = 1,
    screenshot_dir: str | None = None,
    headless: bool = False,
    junit_file: str | None = None,
    project: Project | None = None,
    max_cost_usd: float | None = None,
    ai_report: bool = False,
) -> RunResult:
    """Execute a test plan from a Markdown file.

    Args:
        test_plan_file: Path to the Markdown test plan.
        url: Override URL (otherwise the project's URL, then the plan's).
        output_dir: Directory to write reports to.
        only: Run only these test case IDs.
        skip: Skip these test case IDs.
        parallel: Number of parallel browser agents.
        screenshot_dir: Directory to save screenshots. Defaults to <run dir>/screenshots/.
        headless: Run the browser headless (parallel runs are always headless).
        junit_file: Extra path to write JUnit XML to, in addition to the run dir.
        project: Project supplying the URL, test accounts, data, and instructions.
        max_cost_usd: Stop agents once the run's estimated cost reaches this.
        ai_report: Have an agent write the report, instead of building it from the results.
    """
    plan_path = Path(test_plan_file)
    if not plan_path.exists():
        raise FileNotFoundError(f"Test plan not found: {plan_path}")

    full_plan = parse_test_plan(plan_path.read_text())

    url = url or (project.url if project else None) or full_plan.url
    if url is None:
        raise ValueError(
            "Could not find a URL. Pass --url, set `url` in the project, "
            "or add a '> URL: ...' line to the plan."
        )

    for flag, ids in (("--only", only), ("--skip", skip)):
        unknown = full_plan.unknown_ids(ids or [])
        if unknown:
            print(c.warn(f"  Warning: {flag} IDs not found in plan: {', '.join(unknown)}"))

    # Apply filters
    plan = full_plan.filter(only=only, skip=skip)
    if not plan.cases:
        raise ValueError("No test cases to run after applying --only/--skip filters.")

    timestamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    run_dir = Path(output_dir) / site_dir_name(url) / timestamp

    result = await execute_plan(
        plan,
        url,
        run_dir,
        parallel=parallel,
        headless=headless,
        screenshot_dir=Path(screenshot_dir) if screenshot_dir else None,
        project=project,
        max_cost_usd=max_cost_usd,
        ai_report=ai_report,
    )
    if result.environment_error:
        raise RuntimeError(
            f"The browser couldn't run the tests: {result.environment_error}\n"
            "  If the browser isn't installed, run: argus-qa setup"
        )

    if junit_file:
        Path(junit_file).parent.mkdir(parents=True, exist_ok=True)
        Path(junit_file).write_text(to_junit(result.results))

    if result.failed_ids:
        rerun = f"argus-qa test {test_plan_file} --only {','.join(result.failed_ids)}"
        print(f"  Re-run just failures: {c.BOLD}{rerun}{c.RESET}")

    return result


RUN_MODES = ("ai", "script", "auto")


def _n(count: int, word: str) -> str:
    return f"{count} {word}{'' if count == 1 else 's'}"


async def execute_plan(
    plan: TestPlan,
    url: str,
    run_dir: Path,
    parallel: int = 1,
    headless: bool = False,
    screenshot_dir: Path | None = None,
    isolated: bool = False,
    project: Project | None = None,
    max_cost_usd: float | None = None,
    ai_report: bool = False,
    mode: str = "ai",
    scripts: dict[str, Path] | None = None,
    record: bool = False,
) -> RunResult:
    """Run the given test cases, write results/report/JUnit to run_dir, and return the outcome.

    mode "ai" runs every test with a tester agent. "script" replays recorded
    scripts (test ID -> .mjs in `scripts`) without AI; tests without one are
    blocked. "auto" replays scripts and hands any test whose script failed, or
    that has none, to the AI: if the AI passes it, the test is "healed" (the
    site changed but still works). With record, tests the AI passes get a new
    script, kept only if it replays successfully (RunResult.recorded).

    Set isolated when other runs may be using a browser at the same time. The
    project's secrets are given to tester agents but redacted from everything
    printed or saved, and never shown to the reporter. The report is built from the
    results unless ai_report is set, in which case an agent writes it and gets 10% of
    max_cost_usd; the tester agents share the rest.
    """
    if mode not in RUN_MODES:
        raise ValueError(f"Unknown run mode {mode!r}; use one of {', '.join(RUN_MODES)}.")
    tester_share = 0.9 if ai_report else 1.0
    substitute(plan.to_markdown(), project)  # fail fast on unknown {{placeholders}}
    redact = Redactor(project.secret_values() if project else [])
    values = project.placeholder_values() if project else {}
    run_dir.mkdir(parents=True, exist_ok=True)
    ss_dir = screenshot_dir or run_dir / "screenshots"
    ss_dir.mkdir(parents=True, exist_ok=True)

    total = len(plan.cases)
    case_ids = ", ".join(tc.id for tc in plan.cases)
    print(f"\n  {c.BOLD}{total}{c.RESET} test cases selected: {c.DIM}{case_ids}{c.RESET}")
    print(f"  Screenshots {c.DIM}→{c.RESET} {c.info(str(ss_dir))}")

    # ── Scripts first (no AI) ───────────────────────────────────
    script_passed: dict[str, dict] = {}
    script_failed: dict[str, dict] = {}
    scripts = scripts or {}
    if mode in ("script", "auto"):
        to_replay = {tc.id: scripts[tc.id] for tc in plan.cases if tc.id in scripts}
        if to_replay:
            print(c.phase(1, 2, f"Replaying {len(to_replay)} recorded script(s) without AI..."))
            emit("phase", f"Replaying {_n(len(to_replay), 'recorded script')} without AI")
            runner = await asyncio.to_thread(ensure_runner)
            replayed = await replay(to_replay, url, values, ss_dir, runner)
            for tc in plan.cases:
                if tc.id not in replayed:
                    continue
                entry = script_result(tc, replayed[tc.id])
                seconds = replayed[tc.id].get("duration_ms", 0) / 1000
                if entry["status"] == "passed":
                    script_passed[tc.id] = entry
                    emit("phase", f"{tc.id} passed by script in {seconds:.1f}s")
                else:
                    script_failed[tc.id] = entry
                    emit("error", f"{tc.id} script failed: {entry['notes']}")

    if mode == "script":
        ai_cases: list[TestCase] = []
    elif mode == "auto":
        ai_cases = [tc for tc in plan.cases if tc.id not in script_passed]
    else:
        ai_cases = list(plan.cases)

    # ── AI for the rest ─────────────────────────────────────────
    runs: list[AgentRun] = []
    ai_merged: dict = {"results": [], "bugs": [], "overall_assessment": ""}
    if ai_cases:
        ai_plan = TestPlan(raw=plan.raw, url=plan.url, preamble=plan.preamble, cases=ai_cases)
        if script_failed and mode == "auto":
            emit("phase", f"Healing {_n(len(script_failed), 'test')} with AI")
        runs = await _run_testers(
            ai_plan, url, ss_dir, parallel=parallel, headless=headless, isolated=isolated, project=project,
            redact=redact, budget=_share(max_cost_usd, tester_share), record=record,
        )
        ai_merged = merge_results([r.text for r in runs], ai_plan)

    # Page snapshots and other text the browser saved can contain secrets shown or typed on the page
    _redact_text_files(ss_dir, redact)

    ai_by_id = {r["id"]: r for r in ai_merged["results"]}
    results = []
    for tc in plan.cases:
        if tc.id in script_passed:
            results.append(script_passed[tc.id])
        elif tc.id in ai_by_id:
            entry = {**ai_by_id[tc.id], "mode": "ai"}
            failure = script_failed.get(tc.id)
            if failure and entry["status"] == "passed":
                entry["healed"] = True
                entry["notes"] = (
                    f"Healed: the recorded script failed ({failure['notes']}), but the AI completed "
                    f"the test, so the page probably changed. {entry.get('notes') or ''}"
                ).strip()
            elif failure:
                entry["notes"] = (
                    f"The recorded script failed too: {failure['notes']}. {entry.get('notes') or ''}"
                ).strip()
            results.append(entry)
        elif tc.id in script_failed:
            results.append(script_failed[tc.id])
        else:
            results.append({
                "id": tc.id, "name": tc.name, "status": "blocked", "mode": "script", "steps": [],
                "notes": "No script is recorded for this test yet. Run it with AI or Auto to record one.",
            })

    merged = {
        **{k: v for k, v in ai_merged.items() if k != "results"},
        "summary": summarize(results),
        "results": results,
    }

    # ── Record scripts from what the AI just did ────────────────
    recorded: dict[str, str] = {}
    if record and runs and not merged.get("environment_errors"):
        recorded = await _record_scripts(plan, runs, results, url, project, run_dir)

    merged = redact.data(merged)
    (run_dir / "results.json").write_text(json.dumps(merged, indent=2))
    (run_dir / "junit.xml").write_text(to_junit(merged))
    print(c.success("\n  Tests complete.\n"))

    runs_cost = sum(r.cost_usd for r in runs)
    if merged.get("environment_errors"):
        # Nothing about the app was learned; skip the report
        problem = "; ".join(merged["environment_errors"])
        print(c.error(f"  Browser/environment problem: {problem}"))
        emit("error", f"The browser couldn't run the tests: {problem}")
        return RunResult(run_dir=run_dir, report_file=run_dir / "report.md", results=merged,
                         failed_ids=[], cost_usd=runs_cost, models=_models(runs))

    summary = merged["summary"]
    print(c.result_bar(summary["passed"], summary["failed"], summary["blocked"], summary["skipped"]))
    if summary.get("by_script"):
        how = f"{summary['by_script']} by script, {summary['by_ai']} by AI, {summary['healed']} healed"
        print(c.dim(f"  {how}"))

    # ── Report ──────────────────────────────────────────────────
    agents = list(runs)
    report_file = run_dir / "report.md"
    if ai_report:
        print(c.phase(2, 2, "Generating report..."))
        emit("phase", "Writing the report")
        report = await _run_agent(
            prompt=REPORTER_PROMPT.format(
                exploration_report="(not available — ran from existing test plan)",
                test_plan=plan.to_markdown(),
                test_results=json.dumps(merged, indent=2),
                screenshot_dir=str(ss_dir),
            ),
            options=_reasoning_options(max_budget_usd=_share(max_cost_usd, 0.1), model=model_for("writer")),
            label="reporter",
            redact=redact,
        )
        agents.append(report)
        report_file.write_text(report.text)
    else:
        report_file.write_text(test_report(merged, plan, url))
    print(c.success(f"\n  Report saved to {report_file}\n"))

    # Save failed test IDs for re-runs
    failed = _failed_ids(merged)
    if failed:
        failures_file = run_dir / "failures.txt"
        failures_file.write_text("\n".join(failed) + "\n")
        print(c.warn(f"  Failed tests saved to {failures_file}"))

    cost = sum(a.cost_usd for a in agents)
    print(c.dim(f"  Cost: ${cost:.2f}   Models: {', '.join(_models(agents)) or 'none (scripts only)'}"))

    return RunResult(
        run_dir=run_dir,
        report_file=report_file,
        results=merged,
        failed_ids=failed,
        cost_usd=cost,
        models=_models(agents),
        recorded=recorded,
    )


async def _run_testers(
    plan: TestPlan,
    url: str,
    ss_dir: Path,
    parallel: int,
    headless: bool,
    isolated: bool,
    project: Project | None,
    redact: Redactor,
    budget: float | None,
    record: bool,
) -> list[AgentRun]:
    """Run tester agents over the plan: one agent, or parallel agents on chunks of it."""
    used_names: set[str] = set()

    def _unique_name() -> str:
        available = [n for n in AGENT_NAMES if n not in used_names] or AGENT_NAMES
        name = random.choice(available)
        used_names.add(name)
        return name

    total = len(plan.cases)
    if parallel <= 1 or total <= 1:
        tester_name = _unique_name()
        print(c.phase(1, 2, f"{tester_name} is running {total} tests..."))
        emit("phase", f"{tester_name} is running {total} test{'s' if total != 1 else ''}")
        return [await _run_test_chunk(
            url, plan, ss_dir, label=tester_name, headless=headless, isolated=isolated,
            project=project, redact=redact, max_budget_usd=budget, record=record,
        )]

    chunks = plan.split_chunks(min(parallel, total))
    print(c.phase(1, 2, f"Assembling the testing squad ({len(chunks)} agents)..."))
    emit("phase", f"{len(chunks)} agents are splitting {total} tests")
    tasks = []
    for chunk in chunks:
        chunk_ids = ", ".join(tc.id for tc in chunk.cases)
        name = _unique_name()
        color = c.agent_color(name)
        print(f"  {color}{c.BOLD}{name}{c.RESET}: {c.DIM}{chunk_ids}{c.RESET}")
        emit("phase", f"{name} takes {chunk_ids}", agent=name)
        tasks.append(
            _run_test_chunk(
                url, chunk, ss_dir, label=name, headless=True, isolated=True,
                project=project, redact=redact, record=record,
                max_budget_usd=_share(budget, 1 / len(chunks)) if budget else None,
            )
        )
    print()
    return list(await asyncio.gather(*tasks))


async def _record_scripts(
    plan: TestPlan,
    runs: list[AgentRun],
    results: list[dict],
    url: str,
    project: Project | None,
    run_dir: Path,
) -> dict[str, str]:
    """Build scripts for tests the AI passed, and keep the ones that replay successfully."""
    recordings = {}
    for run in runs:
        recordings.update(run.recording or {})
    values = project.placeholder_values() if project else {}
    secrets = project.secret_placeholders() if project else set()
    status = {r["id"]: r for r in results}
    drafts: dict[str, Path] = {}
    draft_dir = run_dir / "scripts"
    for tc in plan.cases:
        entry = status.get(tc.id)
        if not entry or entry.get("mode") != "ai" or entry.get("status") != "passed":
            continue
        recording = recordings.get(tc.id.upper())
        if recording is None:
            entry["script"] = "not recorded: the tester didn't mark this test's steps"
            continue
        script, reason = build_script(tc, recording, url, values, secrets)
        if script is None:
            entry["script"] = f"not recorded: {reason}"
            continue
        draft_dir.mkdir(parents=True, exist_ok=True)
        drafts[tc.id] = draft_dir / f"{tc.id}.mjs"
        drafts[tc.id].write_text(script)
    if not drafts:
        return {}

    emit("phase", f"Checking {_n(len(drafts), 'recorded script')} by replaying them")
    runner = await asyncio.to_thread(ensure_runner)
    verified = await replay(drafts, url, values, run_dir / "verify", runner)
    recorded = {}
    for tc_id, path in drafts.items():
        result = verified.get(tc_id, {})
        if result.get("status") == "passed":
            recorded[tc_id] = path.read_text()
            status[tc_id]["script"] = "recorded"
        else:
            problem = result.get("error") or "no result"
            status[tc_id]["script"] = f"not recorded: the script didn't replay ({problem})"
    emit("phase", f"Recorded {_n(len(recorded), 'script')} for replay without AI")
    return recorded


_TEXT_ARTIFACTS = {".yml", ".yaml", ".md", ".txt", ".json", ".log", ".html"}


def _redact_text_files(directory: Path, redact: Redactor) -> None:
    if not redact.values:
        return
    for path in directory.rglob("*"):
        if path.is_file() and path.suffix.lower() in _TEXT_ARTIFACTS:
            text = path.read_text(errors="replace")
            cleaned = redact(text)
            if cleaned != text:
                path.write_text(cleaned)


async def _run_test_chunk(
    url: str,
    plan: TestPlan,
    screenshot_dir: Path,
    label: str = "",
    headless: bool = False,
    isolated: bool = False,
    project: Project | None = None,
    redact: Redactor | None = None,
    max_budget_usd: float | None = None,
    record: bool = False,
) -> AgentRun:
    """Run a chunk of test cases in one agent.

    Isolated agents get their own in-memory browser profile so multiple chunks
    can run in parallel without conflicts. With record, the agent marks steps and
    checks with the argus tools, and the run carries per-test recordings.
    """
    snapshots = tester_snapshots()
    prompt = TESTER_PROMPT.format(
        url=url,
        test_plan=substitute(plan.to_markdown(), project),
        screenshot_dir=str(screenshot_dir),
        project_context=project.prompt_section() if project else "",
        page_reading=_PAGE_READING[snapshots],
        recording=RECORDING_INSTRUCTIONS if record else "",
    )
    options = _browser_options(
        headless=headless, isolated=isolated, output_dir=screenshot_dir, max_budget_usd=max_budget_usd,
        model=model_for("tester"),
        images=False,  # it reads the page as text; screenshots are evidence for people
        snapshots=snapshots,
        record=record,
    )
    recorder = Recorder() if record else None
    run = await _run_agent(prompt, options, label=label, redact=redact, recorder=recorder)
    if recorder is not None:
        run.recording = recorder.tests()
    return run


# ── Watch mode ──────────────────────────────────────────────────────


async def run_watch(
    test_plan_file: str,
    url: str | None = None,
    output_dir: str = "reports",
    interval: int = 30,
    parallel: int = 1,
    screenshot_dir: str | None = None,
    headless: bool = False,
    project: Project | None = None,
    max_cost_usd: float | None = None,
) -> None:
    """Watch mode: re-run failed tests when the app changes.

    Polls the target URL at the given interval. When a change is detected
    (different response content), re-runs only the previously failed tests.
    """
    plan_path = Path(test_plan_file)
    if not plan_path.exists():
        raise FileNotFoundError(f"Test plan not found: {plan_path}")
    full_plan = parse_test_plan(plan_path.read_text())
    target_url = url or (project.url if project else None) or full_plan.url
    if not target_url:
        raise ValueError("No URL found. Pass --url, set it in the project, or add '> URL: ...' to the plan.")

    print(c.info(f"\n  Watch mode — polling {target_url} every {interval}s"))
    print(c.dim("  Press Ctrl+C to stop.\n"))

    # Initial run
    print(c.header("  Running initial test pass...\n"))
    result = await run_tests(
        test_plan_file, url=target_url, output_dir=output_dir,
        parallel=parallel, screenshot_dir=screenshot_dir, headless=headless, project=project,
        max_cost_usd=max_cost_usd,
    )
    failed_ids = result.failed_ids
    last_hash = await _fetch_hash(target_url)

    if not failed_ids:
        print(c.success("\n  All tests passed! Watching for regressions...\n"))

    while True:
        await asyncio.sleep(interval)
        current_hash = await _fetch_hash(target_url)

        if current_hash is None or current_hash == last_hash:
            continue

        ts = datetime.now().strftime("%H:%M:%S")
        print(c.warn(f"\n  Change detected at {ts}!"))
        last_hash = current_hash

        if failed_ids:
            print(c.warn(f"  Re-running {len(failed_ids)} failed tests: {', '.join(failed_ids)}\n"))
            run_only = failed_ids
        else:
            print(c.info("  No previous failures — running full suite to check for regressions.\n"))
            run_only = None

        result = await run_tests(
            test_plan_file, url=target_url, output_dir=output_dir,
            only=run_only, parallel=parallel, screenshot_dir=screenshot_dir, headless=headless,
            project=project, max_cost_usd=max_cost_usd,
        )
        failed_ids = result.failed_ids

        if not failed_ids:
            print(c.success("\n  All tests passing now! Continuing to watch...\n"))


async def _fetch_hash(url: str) -> str | None:
    """Fetch a URL and return a hash of the response body, or None if unreachable."""
    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
            resp = await client.get(url)
            return hashlib.sha256(resp.content).hexdigest()
    except httpx.HTTPError as e:
        print(c.dim(f"  Could not reach {url}: {e}"))
        return None
