"""Run activity feed: a structured record of what agents are doing, for the web UI.

The orchestrator calls emit(); nothing is recorded unless a sink is installed
with capture(). The server installs an EventLog per run, which appends JSON
lines to <run dir>/events.jsonl. Context variables are copied into tasks, so
parallel agents started inside capture() report to the same sink.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from pathlib import Path

Sink = Callable[[dict], None]
_sink: ContextVar[Sink | None] = ContextVar("argus_event_sink", default=None)

# Friendly verbs for Playwright MCP tools (tool name minus the "mcp__playwright__browser_" prefix)
_VERBS = {
    "navigate": "Open",
    "navigate_back": "Go back",
    "click": "Click",
    "hover": "Hover",
    "type": "Type",
    "fill_form": "Fill form",
    "select_option": "Select",
    "press_key": "Press",
    "take_screenshot": "Screenshot",
    "snapshot": "Read page",
    "wait_for": "Wait for",
    "evaluate": "Run script",
    "run_code_unsafe": "Run script",
    "console_messages": "Read console",
    "network_requests": "Read network log",
    "network_request": "Read network request",
    "tabs": "Tabs",
    "resize": "Resize window",
    "handle_dialog": "Answer dialog",
    "file_upload": "Upload file",
    "drag": "Drag",
    "drop": "Drop",
    "close": "Close browser",
    "find": "Find",
}

# Tools that only look (read the page, logs, wait); the feed plays them down next to real actions
QUIET_TOOLS = {"snapshot", "wait_for", "console_messages", "network_requests", "network_request", "evaluate",
               "run_code_unsafe", "find", "tabs"}

# Tools that act on one element: without the agent's description, the result's Playwright code names it
ELEMENT_TOOLS = {"click", "hover", "type", "select_option", "drag", "file_upload"}

_REF = re.compile(r"^[a-z]*\d*e\d+$")  # snapshot refs like "e12" or "f3e46" mean nothing to a reader
_QUOTED = r"'((?:[^'\\]|\\.)*)'"
_GET_BY = re.compile(
    rf"\.getBy(Role|Text|Label|Placeholder|TestId|AltText|Title)\(\s*{_QUOTED}"
    rf"(?:\s*,\s*\{{[^}}]*?name:\s*{_QUOTED})?"
)
_LOCATOR = re.compile(rf"\.locator\(\s*{_QUOTED}")
# The code Playwright MCP reports for each browser action
CODE_BLOCK = re.compile(r"### Ran Playwright code\s*```(?:js|javascript|ts|typescript)?\n(.*?)```", re.DOTALL)


def emit(kind: str, text: str = "", agent: str = "", **extra) -> None:
    """Record an event if a sink is installed. kind: phase | say | action | error."""
    sink = _sink.get()
    if sink is None:
        return
    sink({
        "ts": datetime.now(UTC).isoformat(timespec="milliseconds"),
        "kind": kind,
        "agent": agent,
        "text": text,
        **extra,
    })


@contextmanager
def capture(sink: Sink) -> Iterator[None]:
    token = _sink.set(sink)
    try:
        yield
    finally:
        _sink.reset(token)


def describe_tool(name: str, tool_input: dict) -> tuple[str, str, dict]:
    """Turn a tool call into (tool, human-readable description, extra event fields)."""
    # "mcp__<server>__<tool>" -> "<tool>", minus Playwright's "browser_" prefix
    tool = (name.split("__", 2)[-1] if name.startswith("mcp__") else name).removeprefix("browser_")
    verb = _VERBS.get(tool, tool.replace("_", " ").capitalize())
    i = tool_input if isinstance(tool_input, dict) else {}
    target_ref = str(i.get("target") or i.get("ref") or "")
    element = i.get("element") or ("" if _REF.match(target_ref) else target_ref)
    extra: dict = {"quiet": True} if tool in QUIET_TOOLS else {}

    if tool == "navigate":
        target = i.get("url", "")
    elif tool == "type":
        target = f"{element} ← \"{i.get('text', '')}\"" if element else f"\"{i.get('text', '')}\""
    elif tool == "fill_form":
        fields = i.get("fields") or []
        target = ", ".join(
            f"{f.get('name', '?')} ← \"{f.get('value', '')}\"" for f in fields if isinstance(f, dict)
        )
    elif tool == "select_option":
        target = f"{element} ← {', '.join(map(str, i.get('values') or []))}"
    elif tool == "press_key":
        target = i.get("key", "")
    elif tool == "wait_for":
        target = i.get("text") or i.get("textGone") or (f"{i['time']}s" if "time" in i else "")
    elif tool in ("evaluate", "run_code_unsafe"):
        target = _short_code(i.get("function") or i.get("code") or "")
    elif tool == "take_screenshot":
        target = i.get("filename", "")
        if target:
            extra["file"] = Path(target).name
    else:
        target = element

    if tool in ELEMENT_TOOLS and not element:
        extra["unnamed"] = True
    return tool, f"{verb}: {target}" if target else verb, extra


def _short_code(code: str, limit: int = 72) -> str:
    """A one-line glimpse of a script the agent ran, e.g. "document.title"."""
    code = re.sub(r"^\s*(async\s*)?\(\s*\)\s*=>\s*", "", " ".join(code.split()))
    code = code.removeprefix("{").removesuffix("}").strip().removeprefix("return ").rstrip(";")
    return code if len(code) <= limit else code[: limit - 1] + "…"


def element_from_result(result_text: str) -> str:
    """Names the element an action used, from the Playwright code in the tool's result:
    getByRole('button', { name: 'Sign in' }) -> '"Sign in" button'. "" if there's none."""
    block = CODE_BLOCK.search(result_text or "")
    if not block:
        return ""
    code = block.group(1)
    found = list(_GET_BY.finditer(code))
    if found:
        kind, value, name = found[-1].groups()
        value, name = (v.replace("\\'", "'") for v in (value, name or ""))
        return {
            "Role": f'"{name}" {value}' if name else value,
            "Label": f'"{value}" field',
            "Placeholder": f'"{value}" field',
            "TestId": f'[{value}]',
        }.get(kind, f'"{value}"')
    locator = _LOCATOR.search(code)
    return locator.group(1) if locator else ""


def name_element(text: str, element: str) -> str:
    """Adds the element to an action's description: 'Click' -> 'Click: "Sign in" button',
    'Type: "abc"' -> 'Type: "Search" field ← "abc"'."""
    verb, _, rest = text.partition(": ")
    if not rest:
        return f"{verb}: {element}"
    return f"{verb}: {element} ← {rest}" if verb in ("Type", "Select") else f"{verb}: {element} {rest}"


class EventLog:
    """Appends events as JSON lines to a file."""

    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def __call__(self, event: dict) -> None:
        with self.path.open("a") as f:
            f.write(json.dumps(event) + "\n")

    @staticmethod
    def read(path: Path, after: int = 0) -> tuple[list[dict], int]:
        """Events after the first `after` lines, and the new cursor."""
        if not path.is_file():
            return [], after
        lines = path.read_text().splitlines()
        events = []
        for line in lines[after:]:
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                break  # partially written line; pick it up next time
        return events, after + len(events)
