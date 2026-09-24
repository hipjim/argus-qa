from pathlib import Path

from argus_qa.plan_parser import parse_test_plan, plan_from_scenario

EXAMPLE = Path(__file__).parent.parent / "examples" / "testplan_example.md"


def _plan(n_cases: int, preamble: str = "# Plan\n\n> URL: https://app.test\n") -> str:
    cases = "\n\n---\n\n".join(
        f"### TC-{i:03d}: Case {i}\n\n**Priority:** high\n**Category:** functional\n\nSteps..."
        for i in range(1, n_cases + 1)
    )
    return f"{preamble}\n## Test Cases\n\n{cases}\n"


def test_parses_example_plan():
    plan = parse_test_plan(EXAMPLE.read_text())
    assert plan.url == "https://staging.myapp.com"
    assert [tc.id for tc in plan.cases] == ["TC-001", "TC-002", "TC-003", "TC-004", "TC-005"]
    assert plan.cases[0].name == "Login with valid credentials"
    assert plan.cases[0].priority == "critical"
    assert plan.cases[0].category == "functional"
    assert "## Credentials" in plan.preamble


def test_case_body_excludes_trailing_separator():
    plan = parse_test_plan(_plan(2))
    assert not plan.cases[0].raw_markdown.rstrip().endswith("---")


def test_url_only_read_from_preamble():
    text = "# Plan\n\n## Test Cases\n\n### TC-001: X\n\n1. Check the page shows\nURL: https://wrong.test\n"
    assert parse_test_plan(text).url is None


def test_filter_only_and_skip_are_case_insensitive():
    plan = parse_test_plan(_plan(4))
    assert [tc.id for tc in plan.filter(only=["tc-001", "TC-003"]).cases] == ["TC-001", "TC-003"]
    assert [tc.id for tc in plan.filter(skip=["tc-002"]).cases] == ["TC-001", "TC-003", "TC-004"]


def test_unknown_ids():
    plan = parse_test_plan(_plan(2))
    assert plan.unknown_ids(["TC-001", "tc-002", "TC-009"]) == ["TC-009"]


def test_split_chunks_never_exceeds_requested_count():
    plan = parse_test_plan(_plan(5))
    for n in range(1, 8):
        chunks = plan.split_chunks(n)
        assert len(chunks) <= n
        assert [tc.id for ch in chunks for tc in ch.cases] == [tc.id for tc in plan.cases]


def test_split_chunks_are_balanced():
    plan = parse_test_plan(_plan(5))
    assert [len(ch.cases) for ch in plan.split_chunks(2)] == [3, 2]
    assert [len(ch.cases) for ch in plan.split_chunks(3)] == [2, 2, 1]


def test_to_markdown_round_trips():
    plan = parse_test_plan(_plan(3)).filter(only=["TC-002"])
    reparsed = parse_test_plan(plan.to_markdown())
    assert reparsed.url == "https://app.test"
    assert [tc.id for tc in reparsed.cases] == ["TC-002"]


def test_plan_from_scenario():
    scenario = "1. Open the home page\n\nExpected: a heading is shown"
    plan = plan_from_scenario(scenario, "https://x.test", "Home")
    assert plan.url == "https://x.test"
    assert len(plan.cases) == 1
    assert plan.cases[0].id == "TC-001"
    assert plan.cases[0].name == "Home"
    assert "Open the home page" in plan.cases[0].raw_markdown
