"""Parse, merge, and export test results produced by tester agents."""

from __future__ import annotations

import json
import re
from xml.etree import ElementTree as ET

from argus_qa.plan_parser import TestCase, TestPlan, parse_test_plan

STATUSES = ("passed", "failed", "blocked", "skipped")
FAILING_STATUSES = ("failed", "blocked")


def extract_json(text: str) -> str:
    """Extract the first JSON object from text that may contain prose or code fences."""
    # Prefer a fenced block that actually contains an object
    for match in re.finditer(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL):
        block = match.group(1).strip()
        if block.startswith("{"):
            return block

    # Fall back to scanning for a balanced object, ignoring braces inside strings
    start = text.find("{")
    while start >= 0:
        depth = 0
        in_string = False
        escaped = False
        for i in range(start, len(text)):
            ch = text[i]
            if in_string:
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == '"':
                    in_string = False
            elif ch == '"':
                in_string = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    candidate = text[start : i + 1]
                    try:
                        json.loads(candidate)
                        return candidate
                    except json.JSONDecodeError:
                        break
        start = text.find("{", start + 1)
    return text


def merge_results(raw_outputs: list[str], plan: TestPlan) -> dict:
    """Merge raw agent outputs into one result set, reconciled against the plan.

    The summary is always computed from the per-test results rather than trusted
    from the agent. Test cases the agents didn't report on are marked blocked.
    """
    by_id: dict[str, dict] = {}
    bugs: list[dict] = []
    assessments: list[str] = []
    agent_errors: list[str] = []
    environment_errors: list[str] = []

    for raw in raw_outputs:
        try:
            data = json.loads(extract_json(raw))
        except (json.JSONDecodeError, ValueError):
            data = None
        if not isinstance(data, dict):
            agent_errors.append(raw)
            continue

        for r in data.get("results", []):
            if not isinstance(r, dict) or not r.get("id"):
                continue
            tc_id = str(r["id"]).upper()
            status = str(r.get("status", "")).lower()
            by_id[tc_id] = {**r, "id": tc_id, "status": status if status in STATUSES else "blocked"}
        bugs.extend(b for b in data.get("bugs", []) if isinstance(b, dict))
        if data.get("overall_assessment"):
            assessments.append(data["overall_assessment"])
        if data.get("environment_error"):
            environment_errors.append(str(data["environment_error"]))

    results = []
    for tc in plan.cases:
        result = by_id.pop(tc.id.upper(), None)
        if result is None:
            result = {
                "id": tc.id,
                "name": tc.name,
                "status": "blocked",
                "steps": [],
                "notes": "No result was reported for this test case.",
            }
        results.append(result)
    # Anything reported that wasn't in the plan is kept, but not counted
    extra = list(by_id.values())

    summary = {"total": len(results), **{s: 0 for s in STATUSES}}
    for r in results:
        summary[r["status"]] += 1

    merged = {
        "summary": summary,
        "results": results,
        "bugs": bugs,
        "overall_assessment": " | ".join(assessments),
    }
    if extra:
        merged["unplanned_results"] = extra
    if agent_errors:
        merged["agent_errors"] = agent_errors
    if environment_errors:
        merged["environment_errors"] = environment_errors
    return merged


def failed_ids(results: dict) -> list[str]:
    """IDs of failed/blocked tests, for re-runs."""
    return [
        r["id"]
        for r in results.get("results", [])
        if isinstance(r, dict) and r.get("status") in FAILING_STATUSES and r.get("id")
    ]


def to_junit(results: dict, suite_name: str = "argus-qa") -> str:
    """Render merged results as JUnit XML for CI systems."""
    summary = results.get("summary", {})
    suite = ET.Element(
        "testsuite",
        name=suite_name,
        tests=str(summary.get("total", 0)),
        failures=str(summary.get("failed", 0)),
        errors=str(summary.get("blocked", 0)),
        skipped=str(summary.get("skipped", 0)),
    )
    for r in results.get("results", []):
        case = ET.SubElement(
            suite, "testcase", classname=suite_name, name=f"{r['id']}: {r.get('name', '')}".rstrip(": ")
        )
        status = r.get("status")
        detail = _failure_detail(r)
        if status == "failed":
            ET.SubElement(case, "failure", message=_first_line(detail)).text = detail
        elif status == "blocked":
            ET.SubElement(case, "error", message=_first_line(detail)).text = detail
        elif status == "skipped":
            ET.SubElement(case, "skipped")
    ET.indent(suite)
    return ET.tostring(suite, encoding="unicode", xml_declaration=True) + "\n"


def _failure_detail(result: dict) -> str:
    lines = []
    for step in result.get("steps", []):
        if isinstance(step, dict) and step.get("status") == "failed":
            lines.append(f"Step: {step.get('step', '')}")
            lines.append(f"  Expected: {step.get('expected', '')}")
            lines.append(f"  Actual: {step.get('actual', '')}")
    if result.get("notes"):
        lines.append(str(result["notes"]))
    return "\n".join(lines) or result.get("status", "")


def _first_line(text: str) -> str:
    return text.splitlines()[0] if text else ""


# ── Discovery and exploration sessions ──────────────────────────────

_PRIORITY_FOR_SEVERITY = {"critical": "critical", "high": "high", "medium": "medium", "low": "low"}


def bugs_to_cases(bugs: list[dict]) -> list[TestCase]:
    """Turn bugs found while exploring into regression test cases."""
    blocks = []
    for i, bug in enumerate(b for b in bugs if isinstance(b, dict)):
        title = str(bug.get("title") or "Untitled bug").strip()
        steps = [str(s).strip() for s in bug.get("steps_to_reproduce") or [] if str(s).strip()]
        if bug.get("url") and not steps:
            steps = [f"Go to {bug['url']}"]
        severity = str(bug.get("severity") or "").lower()
        steps = steps or ["Reproduce the scenario described below"]
        lines = [
            f"### TC-{i + 1:03d}: {title}",
            "",
            f"**Priority:** {_PRIORITY_FOR_SEVERITY.get(severity, 'medium')}",
            "**Category:** functional",
            "",
            "**Steps:**",
            *[f"{n}. {step}" for n, step in enumerate(steps, 1)],
            "",
            "**Expected result:**",
            f"- {bug.get('expected') or 'The app behaves correctly'}",
        ]
        if bug.get("actual"):
            lines += ["", f"_Regression check. When found, the app did this instead: {bug['actual']}_"]
        blocks.append("\n".join(lines))
    return parse_test_plan("\n\n".join(blocks)).cases if blocks else []


def discovery_report(exploration: dict | None, proposed: list[TestCase], url: str) -> str:
    """Markdown summary of a discovery session."""
    e = exploration or {}
    out = [f"# Discovery: {e.get('title') or url}", ""]
    if e.get("summary"):
        out += [str(e["summary"]), ""]
    out += [f"**{len(proposed)}** test cases proposed from **{len(e.get('pages') or [])}** pages "
            f"and **{len(e.get('user_flows') or [])}** user flows.", ""]
    if e.get("pages"):
        out += ["## Pages", "", "| Page | What it does |", "|---|---|"]
        out += [f"| `{_cell(p.get('url'))}` | {_cell(p.get('description') or p.get('title'))} |"
                for p in e["pages"] if isinstance(p, dict)]
        out.append("")
    if e.get("user_flows"):
        out += ["## User flows", ""]
        for flow in e["user_flows"]:
            if isinstance(flow, dict):
                steps = " → ".join(map(str, flow.get("steps") or []))
                out.append(f"- **{flow.get('name', 'Flow')}**: {steps}")
        out.append("")
    if e.get("issues_noticed"):
        out += ["## Issues noticed", ""]
        out += [f"- {_cell(i.get('description'))} (`{_cell(i.get('page'))}`)"
                for i in e["issues_noticed"] if isinstance(i, dict)]
        out.append("")
    if proposed:
        out += ["## Proposed tests", ""]
        out += [f"- **{c.id}** {c.name}" + (f" _({c.priority})_" if c.priority else "") for c in proposed]
    return "\n".join(out).rstrip() + "\n"


def exploration_report(data: dict, url: str) -> str:
    """Markdown summary of an exploratory (bug-hunting) session."""
    bugs = [b for b in data.get("bugs") or [] if isinstance(b, dict)]
    out = [f"# Exploratory testing: {url}", ""]
    if data.get("summary"):
        out += [str(data["summary"]), ""]
    if data.get("areas_covered"):
        out += ["**Areas covered:** " + ", ".join(map(str, data["areas_covered"])), ""]
    out += [f"## Bugs found ({len(bugs)})", ""]
    if not bugs:
        out += ["No bugs found in this session.", ""]
    for i, bug in enumerate(bugs, 1):
        where = f" · `{bug['url']}`" if bug.get("url") else ""
        out += [f"### {i}. {bug.get('title', 'Untitled')}", "",
                f"**Severity:** {bug.get('severity', 'unknown')}{where}", ""]
        steps = bug.get("steps_to_reproduce") or []
        if steps:
            out += ["**Steps to reproduce:**", *[f"{n}. {s}" for n, s in enumerate(steps, 1)], ""]
        if bug.get("expected"):
            out += [f"**Expected:** {bug['expected']}", ""]
        if bug.get("actual"):
            out += [f"**Actual:** {bug['actual']}", ""]
        if bug.get("screenshot"):
            out += [f"![{bug.get('title', 'screenshot')}](screenshots/{bug['screenshot']})", ""]
    if data.get("observations"):
        out += ["## Observations", "", *[f"- {o}" for o in data["observations"]], ""]
    return "\n".join(out).rstrip() + "\n"


def _cell(value) -> str:
    return str(value or "").replace("|", "\\|").replace("\n", " ")
