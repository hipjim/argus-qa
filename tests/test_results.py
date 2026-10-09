import json
from xml.etree import ElementTree as ET

from argus_qa.plan_parser import parse_test_plan
from argus_qa.results import (
    close_partial,
    extract_json,
    failed_ids,
    merge_results,
    not_reached_ids,
    partial_results,
    report_problem,
    reported_result,
    to_junit,
)
from argus_qa.results import test_report as build_report

PLAN = parse_test_plan(
    "> URL: https://app.test\n\n"
    + "\n\n".join(f"### TC-00{i}: Case {i}\n\nSteps" for i in range(1, 4))
)


def _agent_output(results, summary=None, **extra) -> str:
    payload = {"summary": summary or {}, "results": results, **extra}
    return f"Done testing.\n\n```json\n{json.dumps(payload)}\n```\n"


# ── extract_json ─────────────────────────────────────────────


def test_extract_json_from_fence():
    assert json.loads(extract_json('text\n```json\n{"a": 1}\n```')) == {"a": 1}


def test_extract_json_skips_non_json_fence():
    text = '```bash\nargus-qa test\n```\nthen {"a": 1}'
    assert json.loads(extract_json(text)) == {"a": 1}


def test_extract_json_ignores_braces_inside_strings():
    text = 'Result: {"note": "clicked the } button", "n": 2} trailing'
    assert json.loads(extract_json(text)) == {"note": "clicked the } button", "n": 2}


def test_extract_json_skips_invalid_candidates():
    text = 'set {x} then {"ok": true}'
    assert json.loads(extract_json(text)) == {"ok": True}


# ── merge_results ────────────────────────────────────────────


def test_summary_is_computed_not_trusted():
    raw = _agent_output(
        [{"id": "TC-001", "status": "passed"}, {"id": "TC-002", "status": "failed"},
         {"id": "TC-003", "status": "passed"}],
        summary={"total": 99, "passed": 99},
    )
    merged = merge_results([raw], PLAN)
    assert merged["summary"] == {"total": 3, "passed": 2, "failed": 1, "blocked": 0, "skipped": 0,
                                 "causes": {"unknown": 1}}


def test_missing_cases_are_blocked():
    merged = merge_results([_agent_output([{"id": "TC-001", "status": "passed"}])], PLAN)
    statuses = {r["id"]: r["status"] for r in merged["results"]}
    assert statuses == {"TC-001": "passed", "TC-002": "blocked", "TC-003": "blocked"}
    assert merged["summary"]["blocked"] == 2


def test_unparseable_chunk_blocks_its_cases_and_is_recorded():
    good = _agent_output([{"id": "TC-001", "status": "passed"}])
    merged = merge_results([good, "Agent error: error_max_turns"], PLAN)
    assert merged["summary"]["passed"] == 1
    assert merged["summary"]["blocked"] == 2
    assert merged["agent_errors"] == ["Agent error: error_max_turns"]


def test_statuses_and_ids_are_normalized():
    raw = _agent_output([
        {"id": "tc-001", "status": "PASSED"},
        {"id": "TC-002", "status": "kinda-worked"},
        {"id": "TC-003", "status": "Skipped"},
    ])
    statuses = [r["status"] for r in merge_results([raw], PLAN)["results"]]
    assert statuses == ["passed", "blocked", "skipped"]


def test_results_follow_plan_order_and_merge_across_chunks():
    a = _agent_output([{"id": "TC-003", "status": "passed"}], bugs=[{"title": "b1"}])
    b = _agent_output([{"id": "TC-001", "status": "failed"}, {"id": "TC-002", "status": "passed"}],
                      bugs=[{"title": "b2"}])
    merged = merge_results([a, b], PLAN)
    assert [r["id"] for r in merged["results"]] == ["TC-001", "TC-002", "TC-003"]
    assert [b["title"] for b in merged["bugs"]] == ["b1", "b2"]


def test_unplanned_results_kept_separately():
    raw = _agent_output([{"id": "TC-001", "status": "passed"}, {"id": "TC-042", "status": "failed"}])
    merged = merge_results([raw], PLAN)
    assert merged["summary"]["total"] == 3
    assert merged["unplanned_results"][0]["id"] == "TC-042"


def test_failed_ids_include_blocked():
    merged = merge_results([_agent_output([{"id": "TC-002", "status": "failed"}])], PLAN)
    assert failed_ids(merged) == ["TC-001", "TC-002", "TC-003"]


# ── failure causes ───────────────────────────────────────────


def _cause_results():
    raw = _agent_output([
        {"id": "TC-001", "status": "passed", "cause": "bug", "evidence": {"console": ["noise"]}},
        {"id": "TC-002", "status": "failed", "cause": "BUG",
         "evidence": {"console": [f"err {i}" for i in range(15)], "network": [], "other": ["x"]}},
        {"id": "TC-003", "status": "blocked", "cause": "the moon"},
    ])
    return merge_results([raw], PLAN)


def test_causes_are_normalized_and_counted():
    merged = _cause_results()
    tc1, tc2, tc3 = merged["results"]
    assert "cause" not in tc1 and "evidence" not in tc1              # passed tests have neither
    assert tc2["cause"] == "bug"
    assert tc2["evidence"] == {"console": [f"err {i}" for i in range(10)]}  # capped, unknown kinds dropped
    assert tc3["cause"] == "unknown"
    assert merged["summary"]["causes"] == {"bug": 1, "unknown": 1}


def test_causes_in_junit_and_report():
    merged = _cause_results()
    cases = {tc.attrib["name"].split(":")[0]: tc for tc in ET.fromstring(to_junit(merged)).iter("testcase")}
    failure = cases["TC-002"].find("failure")
    assert failure.attrib["type"] == "bug" and failure.attrib["message"].startswith("[App bug]")
    assert "Console: err 0" in failure.text
    assert cases["TC-003"].find("error").attrib["type"] == "unknown"
    report = build_report(merged, PLAN, "https://app.test")
    assert "**Why tests failed:** 1 app bug, 1 unclear" in report
    assert "— ❌ Failed · App bug" in report
    assert "**Console errors:**" in report


# ── JUnit ────────────────────────────────────────────────────


def test_junit_xml():
    raw = _agent_output([
        {"id": "TC-001", "name": "Login", "status": "passed"},
        {"id": "TC-002", "name": "Logout", "status": "failed", "steps": [
            {"step": "Click logout", "expected": "Signed out", "actual": "500 error", "status": "failed"}
        ]},
        {"id": "TC-003", "name": "Skip me", "status": "skipped"},
    ])
    suite = ET.fromstring(to_junit(merge_results([raw], PLAN)))
    assert suite.attrib["tests"] == "3"
    assert suite.attrib["failures"] == "1"
    assert suite.attrib["skipped"] == "1"
    cases = {tc.attrib["name"]: tc for tc in suite.iter("testcase")}
    failure = cases["TC-002: Logout"].find("failure")
    assert failure is not None
    assert "500 error" in failure.text
    assert cases["TC-003: Skip me"].find("skipped") is not None
    assert len(list(cases["TC-001: Login"])) == 0


def test_environment_errors_collected():
    raw = _agent_output([], environment_error="Chromium distribution 'chrome' is not found")
    merged = merge_results([raw, _agent_output([], environment_error=None)], PLAN)
    assert merged["environment_errors"] == ["Chromium distribution 'chrome' is not found"]
    assert "environment_errors" not in merge_results([_agent_output([])], PLAN)


# ── results handed in test by test ───────────────────────────


def test_reported_results_stand_without_a_final_report():
    reported = {"TC-001": reported_result({"test_id": "tc-001", "status": "Passed"})}
    stopped = {"TC-002": "The run stopped before this test finished: the agent reached its cost limit after TC-001."}
    merged = merge_results(["Agent error: error_max_budget_usd"], PLAN, reported=reported, not_reached=stopped)
    by_id = {r["id"]: r for r in merged["results"]}
    assert by_id["TC-001"]["status"] == "passed" and by_id["TC-001"]["name"] == "Case 1"
    assert (by_id["TC-002"]["status"], by_id["TC-002"]["cause"]) == ("blocked", "not_reached")
    assert "cost limit after TC-001" in by_id["TC-002"]["notes"]
    # Nothing explains TC-003's missing result, so its cause stays unclear
    assert by_id["TC-003"]["cause"] == "unknown"
    assert merged["summary"]["causes"] == {"unknown": 1, "not_reached": 1}
    assert not_reached_ids(merged) == ["TC-002"]
    assert failed_ids(merged) == ["TC-002", "TC-003"]


def test_a_reported_result_wins_over_the_final_report_and_carries_its_bug():
    reported = {"TC-001": reported_result({
        "test_id": "TC-001", "status": "failed", "cause": "bug",
        "bug": {"title": "Save does nothing", "severity": "high"},
    })}
    final = _agent_output([{"id": "TC-001", "status": "passed"}, {"id": "TC-002", "status": "passed"}],
                          bugs=[{"title": "Save does nothing", "test_case": "TC-001"}])
    merged = merge_results([final], PLAN, reported=reported)
    assert merged["results"][0]["status"] == "failed" and "bug" not in merged["results"][0]
    assert merged["results"][1]["status"] == "passed"          # the final report fills the gaps
    assert [b["title"] for b in merged["bugs"]] == ["Save does nothing"]   # listed once


def test_report_test_calls_are_checked():
    assert "test_id" in report_problem({"status": "passed"})
    assert "status" in report_problem({"test_id": "TC-001", "status": "done"})
    assert reported_result({"test_id": "TC-001", "status": "done"}) is None
    entry = reported_result({"test_id": "TC-001", "status": "passed", "cause": "bug",
                             "update": {"steps": ["  Click   Sign in ", ""], "expected": []}})
    assert "cause" not in entry                                  # passed tests have no cause
    assert entry["update"] == {"steps": ["Click Sign in"]}


def test_partial_results_are_completed_when_a_run_stops():
    done = {"TC-002": {"id": "TC-002", "name": "Case 2", "status": "failed", "cause": "bug",
                       "bug": {"title": "Broken"}}}
    partial = partial_results(PLAN, done)
    assert partial["partial"] is True and [r["id"] for r in partial["results"]] == ["TC-002"]
    assert partial["bugs"] == [{"title": "Broken", "test_case": "TC-002"}]

    closed = close_partial(partial, PLAN, "The run was cancelled before this test finished.")
    assert "partial" not in closed and closed["bugs"] == partial["bugs"]
    assert [(r["id"], r["status"], r["cause"]) for r in closed["results"]] == [
        ("TC-001", "blocked", "not_reached"), ("TC-002", "failed", "bug"), ("TC-003", "blocked", "not_reached")]
    assert closed["summary"]["causes"] == {"bug": 1, "not_reached": 2}
    assert "2 not reached" in build_report(closed, PLAN, "https://app.test")
    assert 'type="not_reached"' in to_junit(closed)
