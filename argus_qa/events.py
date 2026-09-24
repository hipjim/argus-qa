"""Run activity feed: a structured record of what agents are doing, for the web UI.

The orchestrator calls emit(); nothing is recorded unless a sink is installed
with capture(). The server installs an EventLog per run, which appends JSON
lines to <run dir>/events.jsonl. Context variables are copied into tasks, so
parallel agents started inside capture() report to the same sink.
"""

from __future__ import annotations

import json
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
    element = i.get("element") or ""
    extra: dict = {}

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
    elif tool == "take_screenshot":
        target = i.get("filename", "")
        if target:
            extra["file"] = Path(target).name
    else:
        target = element

    return tool, f"{verb}: {target}" if target else verb, extra


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
