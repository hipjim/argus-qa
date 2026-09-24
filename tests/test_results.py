import json
from xml.etree import ElementTree as ET

from argus_qa.plan_parser import parse_test_plan
from argus_qa.results import extract_json, failed_ids, merge_results, to_junit

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
    assert merged["summary"] == {"total": 3, "passed": 2, "failed": 1, "blocked": 0, "skipped": 0}


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
