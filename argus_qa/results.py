"""Parse, merge, and export test results produced by tester agents."""

from __future__ import annotations

import json
import re
from xml.etree import ElementTree as ET

from argus_qa.plan_parser import TestCase, TestPlan, parse_test_plan

STATUSES = ("passed", "failed", "blocked", "skipped")
FAILING_STATUSES = ("failed", "blocked")

# Why a test failed or was blocked. The tester agent decides, except for script-only failures,
# which stay "unknown" until an AI run looks at them.
CAUSES = {
    "bug": "App bug",
    "outdated": "Test needs updating",
    "environment": "Environment problem",
    "unknown": "Cause unclear",
    # Set by the orchestrator, never by the tester: the run stopped before the test finished
    "not_reached": "Not reached",
}
# The causes a tester agent chooses from
TESTER_CAUSES = ("bug", "outdated", "environment", "unknown")
EVIDENCE_LIMIT = 10
EVIDENCE_CHARS = 300
NO_RESULT = "No result was reported for this test case."


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


def report_problem(report: dict) -> str | None:
    """Why a `report_test` call can't be kept, or None if it can. The tool answers with this."""
    if not isinstance(report, dict) or not str(report.get("test_id") or report.get("id") or "").strip():
        return "`test_id` is required, e.g. TC-003."
    if str(report.get("status") or "").lower() not in STATUSES:
        return f"`status` must be one of {', '.join(STATUSES)}."
    return None


def reported_result(report: dict) -> dict | None:
    """A results entry from one `report_test` call, or None if the call was turned down.

    Besides the usual fields it can carry `bug` (the bug behind a failure) and `update` (the
    test's steps and acceptance criteria as they should read now).
    """
    if report_problem(report):
        return None
    entry = {
        "id": str(report.get("test_id") or report.get("id")).strip().upper(),
        "name": str(report.get("name") or ""),
        "status": str(report["status"]).lower(),
        "steps": [s for s in report.get("steps") or [] if isinstance(s, dict)],
        "notes": str(report.get("notes") or ""),
        "cause": report.get("cause"),
        "evidence": report.get("evidence"),
    }
    if isinstance(report.get("bug"), dict) and report["bug"].get("title"):
        entry["bug"] = report["bug"]
    update = report.get("update")
    if isinstance(update, dict):
        lines = {k: [" ".join(str(i).split()) for i in update.get(k) or [] if str(i).strip()]
                 for k in ("steps", "expected")}
        if any(lines.values()):
            entry["update"] = {k: v for k, v in lines.items() if v}
    return with_cause(entry)


def _split_bugs(results: list[dict]) -> tuple[list[dict], list[dict]]:
    """Take the `bug` out of each reported result: bugs are listed on their own."""
    bugs = [{**r["bug"], "test_case": r["id"]} for r in results if isinstance(r.get("bug"), dict)]
    return [{k: v for k, v in r.items() if k != "bug"} for r in results], bugs


def merge_results(
    raw_outputs: list[str],
    plan: TestPlan,
    reported: dict[str, dict] | None = None,
    not_reached: dict[str, str] | None = None,
) -> dict:
    """Merge raw agent outputs into one result set, reconciled against the plan.

    The summary is always computed from the per-test results rather than trusted
    from the agent. `reported` holds the results agents handed in test by test
    (reported_result, by test ID); they stand even when an agent never got to its
    final report. Test cases without any result are marked blocked: as not reached,
    with the note from `not_reached` (test ID -> why), when their agent stopped early.
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
            status = status if status in STATUSES else "blocked"
            by_id[tc_id] = with_cause({**r, "id": tc_id, "status": status})
        bugs.extend(b for b in data.get("bugs", []) if isinstance(b, dict))
        if data.get("overall_assessment"):
            assessments.append(data["overall_assessment"])
        if data.get("environment_error"):
            environment_errors.append(str(data["environment_error"]))

    # What an agent reported as it went is what people saw live, so it wins over its final report
    by_id.update({tc_id.upper(): r for tc_id, r in (reported or {}).items()})
    stopped = {tc_id.upper(): why for tc_id, why in (not_reached or {}).items()}

    results = []
    for tc in plan.cases:
        result = by_id.pop(tc.id.upper(), None)
        if result is None:
            result = _no_result(tc, stopped.get(tc.id.upper()))
        results.append({**result, "id": tc.id, "name": result.get("name") or tc.name})
    # Anything reported that wasn't in the plan is kept, but not counted
    extra = list(by_id.values())

    results, reported_bugs = _split_bugs(results)
    known = {(b.get("test_case"), b.get("title")) for b in bugs}
    bugs += [b for b in reported_bugs if (b["test_case"], b.get("title")) not in known]
    summary = summarize(results)

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


def _no_result(case: TestCase, stopped: str | None = None) -> dict:
    return {
        "id": case.id,
        "name": case.name,
        "status": "blocked",
        "cause": "not_reached" if stopped else "unknown",
        "steps": [],
        "notes": stopped or NO_RESULT,
    }


def partial_results(plan: TestPlan, done: dict[str, dict]) -> dict:
    """The results of a run that is still going: only the tests finished so far, in plan order."""
    results, bugs = _split_bugs([done[tc.id] for tc in plan.cases if tc.id in done])
    return {"partial": True, "summary": summarize(results), "results": results, "bugs": bugs}


def close_partial(partial: dict, plan: TestPlan, why: str) -> dict:
    """Complete the results of a run that stopped part-way: the tests it finished keep their
    results, the rest are blocked as not reached, with `why` as their note."""
    done = {r["id"]: r for r in partial.get("results", []) if isinstance(r, dict) and r.get("id")}
    results = [done.get(tc.id) or _no_result(tc, why) for tc in plan.cases]
    closed = {k: v for k, v in partial.items() if k != "partial"}
    return {**closed, "summary": summarize(results), "results": results}


def not_reached_ids(results: dict) -> list[str]:
    """IDs of the tests a run stopped before finishing."""
    return [
        r["id"] for r in results.get("results", []) if isinstance(r, dict) and r.get("cause") == "not_reached"
    ]


def with_cause(result: dict) -> dict:
    """Normalize a result's cause and evidence: failed and blocked tests always have a cause
    (one of CAUSES), other tests have neither."""
    out = {k: v for k, v in result.items() if k not in ("cause", "evidence")}
    if result.get("status") not in FAILING_STATUSES:
        return out
    out["cause"] = _cause(result.get("cause"))
    evidence = result.get("evidence")
    if isinstance(evidence, dict):
        lists = {k: evidence.get(k) for k in ("console", "network")}
        kept = {k: [str(x) for x in v][:EVIDENCE_LIMIT]
                for k, v in lists.items() if isinstance(v, list) and v}
        if kept:
            out["evidence"] = kept
    return out


def _cause(value) -> str:
    value = str(value or "").strip().lower()
    return value if value in CAUSES else "unknown"


def summarize(results: list[dict]) -> dict:
    """Counts by status and by failure cause, and (for runs that replayed scripts) how each test was run."""
    summary = {"total": len(results), **{s: 0 for s in STATUSES}}
    for r in results:
        summary[r["status"]] += 1
    causes = [r.get("cause") or "unknown" for r in results if r["status"] in FAILING_STATUSES]
    if causes:
        summary["causes"] = {k: causes.count(k) for k in CAUSES if k in causes}
    if any(r.get("mode") for r in results):
        summary["by_script"] = sum(1 for r in results if r.get("mode") == "script")
        summary["by_ai"] = sum(1 for r in results if r.get("mode") == "ai")
        summary["healed"] = sum(1 for r in results if r.get("healed"))
        summary["flaky"] = sum(1 for r in results if r.get("flaky"))
    return summary


def causes_line(summary: dict) -> str:
    """E.g. "2 app bugs, 1 test needs updating", or "" when nothing failed."""
    counts = (summary.get("causes") or {}).items()
    return ", ".join(f"{n} {_CAUSE_COUNTS[k][n != 1]}" for k, n in counts if k in _CAUSE_COUNTS)


_CAUSE_COUNTS = {
    "bug": ("app bug", "app bugs"),
    "outdated": ("test needs updating", "tests need updating"),
    "environment": ("environment problem", "environment problems"),
    "unknown": ("unclear", "unclear"),
    "not_reached": ("not reached", "not reached"),
}


def script_result(case: TestCase, replayed: dict) -> dict:
    """A results entry for a test replayed from its script.

    `replayed` must already be redacted: evidence is shortened here, and a secret cut in two
    can no longer be recognised.
    """

    def short(items) -> list[str]:
        return [str(x)[:EVIDENCE_CHARS] for x in items or []]

    steps = [
        {"step": s.get("text") or f"Step {s.get('step')}", "expected": "",
         "actual": "Done" if s.get("status") == "passed" else s.get("error", "Failed"),
         "status": s.get("status"), "screenshot": s.get("screenshot")}
        for s in replayed.get("steps", [])
    ]
    steps += [
        {"step": "Check", "expected": c.get("criterion", ""),
         "actual": "As expected" if c.get("status") == "passed" else c.get("error", "Not as expected"),
         "status": c.get("status"), "screenshot": c.get("screenshot")}
        for c in replayed.get("checks", [])
    ]
    passed = replayed.get("status") == "passed"
    where = f"step {replayed['failed_step']}" if replayed.get("failed_step") is not None else "a check"
    return with_cause({
        "id": case.id,
        "name": case.name,
        "status": "passed" if passed else "failed",
        "mode": "script",
        "duration_ms": replayed.get("duration_ms"),
        "steps": steps,
        "notes": "" if passed else f"Failed at {where}: {replayed.get('error') or 'unknown error'}",
        # A script can't tell a bug from a changed page; an AI run (Auto mode) can
        "cause": "unknown",
        "evidence": None if passed else {
            "console": short(replayed.get("console_errors")),
            "network": short(replayed.get("failed_requests")),
        },
    })


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
        if status in FAILING_STATUSES:
            cause = _cause(r.get("cause"))
            message = f"[{CAUSES[cause]}] {_first_line(detail)}"
            tag = "failure" if status == "failed" else "error"
            ET.SubElement(case, tag, message=message, type=cause).text = detail
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
    for kind, items in (result.get("evidence") or {}).items():
        lines += [f"{kind.capitalize()}: {item}" for item in items]
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


# ── Test run report ──────────────────────────────────────────────────

_STATUS_LABEL = {
    "passed": "✅ Passed", "failed": "❌ Failed", "blocked": "⛔ Blocked", "skipped": "⏭️ Skipped",
}


def test_report(results: dict, plan: TestPlan, url: str) -> str:
    """Markdown report built from the results, with no model call. Failures come first."""
    headings = [line[2:] for line in plan.preamble.splitlines() if line.startswith("# ")]
    title = (headings[0].removeprefix("Test Plan:").strip() if headings else "") or (
        plan.cases[0].name if len(plan.cases) == 1 else "Test run"
    )
    summary = results.get("summary", {})
    tests = results.get("results", [])
    problems = [r for r in tests if r.get("status") in FAILING_STATUSES]
    others = [r for r in tests if r.get("status") not in FAILING_STATUSES]

    out = [f"# Test report: {title}", "", f"**Target:** {url}", ""]
    out += ["| Status | Count |", "|---|---:|"]
    out += [f"| {_STATUS_LABEL[s]} | {summary.get(s, 0)} |" for s in STATUSES]
    out += [f"| **Total** | **{summary.get('total', len(tests))}** |", ""]
    if causes_line(summary):
        out += [f"**Why tests failed:** {causes_line(summary)}", ""]

    if results.get("overall_assessment"):
        out += [str(results["overall_assessment"]), ""]

    bugs = [b for b in results.get("bugs", []) if isinstance(b, dict)]
    if bugs:
        out += ["## Bugs found", ""]
        for b in bugs:
            where = f" ({b['test_case']})" if b.get("test_case") else ""
            out += [f"### {b.get('title', 'Untitled')}{where}", "",
                    f"**Severity:** {b.get('severity', 'unknown')}", ""]
            steps = b.get("steps_to_reproduce") or []
            if steps:
                out += ["**Steps to reproduce:**", *[f"{n}. {s}" for n, s in enumerate(steps, 1)], ""]
            if b.get("expected"):
                out += [f"**Expected:** {b['expected']}", ""]
            if b.get("actual"):
                out += [f"**Actual:** {b['actual']}", ""]
            if b.get("screenshot"):
                out += [f"![{b.get('title', 'screenshot')}](screenshots/{b['screenshot']})", ""]

    if problems:
        out += ["## Failed and blocked tests", ""]
        for r in problems:
            out += _case_section(r)
    if others:
        out += ["## Other tests", ""]
        out += [f"- {_STATUS_LABEL.get(r.get('status'), r.get('status'))} **{r['id']}** {r.get('name', '')}"
                for r in others]
        out.append("")
    if results.get("agent_errors"):
        out += ["## Agent problems", "", *[f"- {_cell(e)[:300]}" for e in results["agent_errors"]], ""]
    if problems:
        ids = ",".join(r["id"] for r in problems)
        out += ["## Re-run the failures", "", f"`argus-qa test <plan.md> --only {ids}`", ""]
    return "\n".join(out).rstrip() + "\n"


def _case_section(result: dict) -> list[str]:
    label = _STATUS_LABEL.get(result.get("status"), "")
    cause = f" · {CAUSES[_cause(result['cause'])]}" if result.get("cause") else ""
    out = [f"### {result['id']}: {result.get('name', '')} — {label}{cause}", ""]
    if result.get("notes"):
        out += [str(result["notes"]), ""]
    evidence = result.get("evidence") or {}
    for kind, title in (("console", "Console errors"), ("network", "Failed requests")):
        if evidence.get(kind):
            out += [f"**{title}:**", *[f"- `{_cell(e)[:300]}`" for e in evidence[kind]], ""]
    steps = [s for s in result.get("steps", []) if isinstance(s, dict)]
    if steps:
        out += ["| # | Step | Expected | Actual | |", "|---|---|---|---|---|"]
        for n, s in enumerate(steps, 1):
            mark = "❌" if s.get("status") == "failed" else "✅"
            cells = " | ".join(_cell(s.get(k)) for k in ("step", "expected", "actual"))
            out.append(f"| {n} | {cells} | {mark} |")
        out.append("")
        for s in steps:
            if s.get("status") == "failed" and s.get("screenshot"):
                out += [f"![{_cell(s.get('step'))}](screenshots/{s['screenshot']})", ""]
    return out
