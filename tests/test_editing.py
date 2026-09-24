"""Structured editing of test cases: Markdown <-> fields, the parse/render API, and drafting."""

import json
from pathlib import Path

import httpx
import pytest

from argus_qa import server
from argus_qa.agents import orchestrator
from argus_qa.plan_parser import case_fields, parse_test_plan, plan_fields, render_case, render_plan
from argus_qa.project import Project

EXAMPLE = Path(__file__).parent.parent / "examples" / "testplan_example.md"


def _case(text: str) -> dict:
    return case_fields(parse_test_plan(text).cases[0])


def test_fields_from_the_standard_format():
    fields = plan_fields(EXAMPLE.read_text())
    tc1 = fields["cases"][0]
    assert tc1["id"] == "TC-001" and tc1["title"] == "Login with valid credentials"
    assert (tc1["priority"], tc1["category"]) == ("critical", "functional")
    assert tc1["preconditions"] == ["User is logged out", "On the home page"]
    assert tc1["steps"][0] == 'Go to the login page by clicking "Sign In" in the top nav'
    assert len(tc1["steps"]) == 4
    assert tc1["expected"][0] == "User is redirected to the dashboard"
    assert fields["title"] == "My SaaS App"
    assert fields["url"] == "https://staging.myapp.com"
    assert "## Credentials" in fields["notes"]


def test_round_trip_is_lossless():
    data = plan_fields(EXAMPLE.read_text())
    assert plan_fields(render_plan(data)) == data


@pytest.mark.parametrize("body, steps, expected", [
    ("1. Open the page\n2. Click Save\n\nExpected: a success message", ["Open the page", "Click Save"], ["a success message"]),
    ("**Steps:**\n- Open\n- Save\n\n**Acceptance criteria:**\n- Saved\n- Listed", ["Open", "Save"], ["Saved", "Listed"]),
    ("Steps:\n1. Open\n   the settings page\n\nExpected results:\n* Visible", ["Open the settings page"], ["Visible"]),
])
def test_loose_formats(body, steps, expected):
    fields = _case(f"### TC-001: X\n\n{body}\n")
    assert fields["steps"] == steps
    assert fields["expected"] == expected


def test_unrecognized_text_is_kept_as_notes():
    fields = _case("### TC-001: X\n\n1. Open\n\n_Regression check for bug #12._\nSee the design doc.\n")
    assert fields["steps"] == ["Open"]
    assert fields["notes"] == "_Regression check for bug #12._\nSee the design doc."
    assert "See the design doc." in render_case(fields, "TC-001")


def test_render_case_format():
    md = render_case({
        "title": "  Create   promo ", "priority": "high", "category": "",
        "preconditions": ["Logged in as retailer"], "steps": ["Open Promotions", " ", "Click New"],
        "expected": ["Promo is listed"], "notes": "",
    }, "TC-004")
    assert md == (
        "### TC-004: Create promo\n\n**Priority:** high\n\n**Preconditions:**\n- Logged in as retailer\n\n"
        "**Steps:**\n1. Open Promotions\n2. Click New\n\n**Acceptance criteria:**\n- Promo is listed\n"
    )


def test_render_plan_keeps_ids_and_numbers_new_cases():
    md = render_plan({"title": "Smoke", "url": "https://x.test", "notes": "", "cases": [
        {"id": "TC-007", "title": "Existing", "steps": ["a"]},
        {"id": None, "title": "New one", "steps": ["b"]},
        {"id": "TC-007", "title": "Duplicate id", "steps": ["c"]},
        {"id": "TC-002", "title": "Moved up", "steps": ["d"]},
    ]})
    plan = parse_test_plan(md)
    assert [(c.id, c.name) for c in plan.cases] == [
        ("TC-007", "Existing"), ("TC-008", "New one"), ("TC-009", "Duplicate id"), ("TC-002", "Moved up"),
    ]
    assert plan.url == "https://x.test"


# ── API ──────────────────────────────────────────────────────


@pytest.fixture
def client(tmp_path):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=server.create_app(tmp_path)), base_url="http://t")


async def test_parse_and_render_endpoints(client):
    async with client:
        parsed = (await client.post("/plans/parse", json={"plan": EXAMPLE.read_text()})).json()
        assert parsed["cases"][0]["expected"][0] == "User is redirected to the dashboard"
        parsed["cases"][0]["steps"].append("Refresh the page")
        rendered = (await client.post("/plans/render", json=parsed)).json()["plan"]
    assert "5. Refresh the page" in rendered
    assert [c.id for c in parse_test_plan(rendered).cases] == ["TC-001", "TC-002", "TC-003", "TC-004", "TC-005"]


async def test_draft_endpoint(client, monkeypatch):
    seen = {}

    async def fake_draft(description, project):
        seen["args"] = (description, project)
        return {"title": "Create promo", "priority": "high", "category": "functional", "preconditions": [],
                "steps": ["Log in as retailer"], "expected": ["Promo listed"], "notes": ""}, 0.01

    monkeypatch.setattr(server, "draft_test_case", fake_draft)
    async with client:
        await client.post("/projects", json={"name": "Looma", "credentials": {"retailer": {"username": "u", "password": "p"}}})
        resp = (await client.post("/drafts/test-case", json={"description": "Retailer creates a promo", "project": "looma"})).json()
        assert resp["case"]["steps"] == ["Log in as retailer"]
        assert resp["cost_usd"] == 0.01
        assert (await client.post("/drafts/test-case", json={"description": "x"})).status_code == 422
        assert (await client.post("/drafts/test-case", json={"description": "valid", "project": "nope"})).status_code == 404
    assert seen["args"][1].slug == "looma"


async def test_draft_test_case_never_sees_secret_values(monkeypatch):
    prompts = []

    async def fake_agent(prompt, options, label="", redact=None):
        prompts.append((prompt, options))
        return orchestrator.AgentRun(ok=True, cost_usd=0.004, text=json.dumps({
            "title": "Create promo", "priority": "HIGH", "category": "functional",
            "preconditions": "Logged in", "steps": ["Log in as retailer", "Open Promotions", ""],
            "expected": ["Promo listed"],
        }))

    monkeypatch.setattr(orchestrator, "_run_agent", fake_agent)
    project = Project.from_dict({
        "name": "Looma", "credentials": {"retailer": {"username": "r@x.test", "password": "hunter22"}},
        "variables": {"promo_code": "SUMMER"}, "secrets": {"api_token": "tok-999"},
    })
    fields, cost = await orchestrator.draft_test_case("Retailer creates a promo", project)
    prompt, options = prompts[0]
    assert "retailer" in prompt and "promo_code" in prompt and "api_token" in prompt
    for secret in ("hunter22", "tok-999", "r@x.test", "SUMMER"):
        assert secret not in prompt
    assert options.max_budget_usd == orchestrator.DRAFT_MAX_COST_USD
    assert fields["priority"] == "high"
    assert fields["preconditions"] == ["Logged in"]
    assert fields["steps"] == ["Log in as retailer", "Open Promotions"]
    assert cost == 0.004


async def test_draft_failure(monkeypatch):
    async def junk(prompt, options, label="", redact=None):
        return orchestrator.AgentRun(ok=True, text="Sorry, I can't.")

    monkeypatch.setattr(orchestrator, "_run_agent", junk)
    with pytest.raises(RuntimeError, match="Couldn't draft"):
        await orchestrator.draft_test_case("x y z")
