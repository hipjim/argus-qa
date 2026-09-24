"""Saved suites, discover/explore sessions, accepting proposed tests, and cost limits (API level)."""

import asyncio
import json

import httpx
import pytest

from argus_qa import server
from argus_qa.agents.orchestrator import RunResult, SessionResult
from argus_qa.plan_parser import parse_test_plan
from argus_qa.results import merge_results

PROJECT = {
    "name": "Looma",
    "url": "https://looma.test",
    "credentials": {"retailer": {"username": "r@looma.test", "password": "retail-pass-1"}},
    "variables": {"promo": "Summer"},
}

SUITE_PLAN = """# Test Plan: Smoke

### TC-001: Retailer logs in

1. Log in as retailer

### TC-002: Retailer creates promo {{promo}}

1. Create promo {{promo}}
"""

PROPOSED = """### TC-001: Promotions list loads

1. Open Promotions

### TC-002: Create a promotion

1. Create one

### TC-003: Delete confirmation appears

1. Click delete
"""


@pytest.fixture
def fakes(monkeypatch):
    calls = {"execute": [], "discover": [], "explore": []}
    gate = asyncio.Event()
    gate.set()

    async def execute_plan(plan, url, run_dir, **kwargs):
        calls["execute"].append({"plan": plan, "url": url, **kwargs})
        await gate.wait()
        merged = merge_results([json.dumps({"results": [{"id": tc.id, "status": "passed"} for tc in plan.cases]})], plan)
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "results.json").write_text(json.dumps(merged))
        return RunResult(run_dir, run_dir / "report.md", merged, [], cost_usd=0.1)

    async def discover_app(url, run_dir, **kwargs):
        calls["discover"].append({"url": url, **kwargs})
        await gate.wait()
        cases = parse_test_plan(PROPOSED).cases
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "proposed.md").write_text(PROPOSED)
        (run_dir / "exploration.json").write_text(json.dumps({"title": "Looma", "pages": [{"url": "/promos"}]}))
        return SessionResult(run_dir, {"proposed": 3, "pages": 1, "flows": 0}, cases, cost_usd=0.5)

    async def explore_app(url, run_dir, charter, **kwargs):
        calls["explore"].append({"url": url, "charter": charter, **kwargs})
        cases = parse_test_plan(PROPOSED).cases[:1]
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "proposed.md").write_text(PROPOSED.split("### TC-002")[0])
        return SessionResult(run_dir, {"bugs": 1, "proposed": 1}, cases, cost_usd=0.3)

    monkeypatch.setattr(server, "execute_plan", execute_plan)
    monkeypatch.setattr(server, "discover_app", discover_app)
    monkeypatch.setattr(server, "explore_app", explore_app)
    calls["gate"] = gate
    return calls


@pytest.fixture
def client(tmp_path):
    app = server.create_app(tmp_path)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def _finished(client, run_id):
    for _ in range(200):
        run = (await client.get(f"/runs/{run_id}")).json()
        if run["status"] in server.FINISHED:
            return run
        await asyncio.sleep(0.01)
    raise AssertionError("run did not finish")


# ── suites ───────────────────────────────────────────────────


async def test_suite_crud_and_run(client, fakes):
    async with client:
        await client.post("/projects", json=PROJECT)
        resp = await client.post("/projects/looma/suites", json={"name": "Smoke", "plan": SUITE_PLAN})
        assert resp.status_code == 201
        suite = resp.json()
        assert suite["slug"] == "smoke"
        assert suite["test_count"] == 2
        assert [t["name"] for t in suite["tests"]] == ["Retailer logs in", "Retailer creates promo {{promo}}"]
        assert (await client.post("/projects/looma/suites", json={"name": "Smoke", "plan": SUITE_PLAN})).status_code == 409

        run = (await client.post("/projects/looma/suites/smoke/run", json={"parallel": 2})).json()
        assert run["suite"] == "smoke" and run["title"] == "Smoke" and run["project"] == "looma"
        run = await _finished(client, run["id"])
        assert run["status"] == "passed"

        listed = (await client.get("/projects/looma/suites")).json()
        assert listed[0]["last_run"]["id"] == run["id"]
        assert [r["id"] for r in (await client.get("/runs?suite=smoke")).json()] == [run["id"]]

        edited = SUITE_PLAN + "\n### TC-003: Logout\n\n1. Log out\n"
        resp = await client.put("/projects/looma/suites/smoke", json={"name": "Smoke tests", "plan": edited})
        assert resp.json()["test_count"] == 3 and resp.json()["name"] == "Smoke tests"

        assert (await client.delete("/projects/looma/suites/smoke")).status_code == 204
        assert (await client.get("/projects/looma/suites/smoke")).status_code == 404
    assert fakes["execute"][0]["project"].credentials["retailer"].password == "retail-pass-1"
    assert fakes["execute"][0]["parallel"] == 2


@pytest.mark.parametrize("plan, message", [
    ("# nothing here", "no '### TC-NNN"),
    ("### TC-001: A\n\nx\n\n### TC-001: B\n\ny\n", "Duplicate test case IDs: TC-001"),
    ("### TC-001: A\n\nUse {{nope}}\n", "nope"),
])
async def test_suite_validation(client, plan, message):
    async with client:
        await client.post("/projects", json=PROJECT)
        resp = await client.post("/projects/looma/suites", json={"name": "Bad", "plan": plan})
    assert resp.status_code == 422
    assert message in resp.text


async def test_deleting_project_deletes_its_suites(client, fakes, tmp_path):
    async with client:
        await client.post("/projects", json=PROJECT)
        await client.post("/projects/looma/suites", json={"name": "Smoke", "plan": SUITE_PLAN})
        await client.delete("/projects/looma")
        await client.post("/projects", json=PROJECT)
        assert (await client.get("/projects/looma/suites")).json() == []


# ── discover / explore ──────────────────────────────────────


async def test_discover_then_accept_into_new_and_existing_suites(client, fakes):
    async with client:
        await client.post("/projects", json=PROJECT)
        await client.post("/projects/looma/suites", json={"name": "Smoke", "plan": SUITE_PLAN})
        resp = await client.post("/projects/looma/discover", json={"focus": "promotions"})
        assert resp.status_code == 202
        run = await _finished(client, resp.json()["id"])
        assert run["kind"] == "discover"
        assert run["status"] == "completed"
        assert run["title"] == "Discover: promotions"
        assert run["summary"] == {"proposed": 3, "pages": 1, "flows": 0}
        assert run["max_cost_usd"] == 3.0

        proposed = (await client.get(f"/runs/{run['id']}/proposed")).json()["cases"]
        assert [c["name"] for c in proposed] == ["Promotions list loads", "Create a promotion", "Delete confirmation appears"]
        assert (await client.get(f"/runs/{run['id']}/exploration")).json()["title"] == "Looma"

        # New suite with two of them
        resp = await client.post(f"/runs/{run['id']}/accept", json={"case_ids": ["TC-001", "tc-003"], "suite_name": "Promotions"})
        assert resp.status_code == 200
        assert [t["name"] for t in resp.json()["tests"]] == ["Promotions list loads", "Delete confirmation appears"]
        assert [t["id"] for t in resp.json()["tests"]] == ["TC-001", "TC-002"]

        # The remaining one appended to Smoke, renumbered after its tests
        resp = await client.post(f"/runs/{run['id']}/accept", json={"case_ids": ["TC-002"], "suite": "smoke"})
        assert [(t["id"], t["name"]) for t in resp.json()["tests"]][-1] == ("TC-003", "Create a promotion")

        run = (await client.get(f"/runs/{run['id']}")).json()
        assert run["accepted"] == ["TC-001", "TC-002", "TC-003"]
        assert all(c["accepted"] for c in (await client.get(f"/runs/{run['id']}/proposed")).json()["cases"])

        resp = await client.post(f"/runs/{run['id']}/accept", json={"suite_name": "Promotions"})
        assert resp.status_code == 409  # name already taken

    discover_call = fakes["discover"][0]
    assert discover_call["url"] == "https://looma.test"
    assert discover_call["focus"] == "promotions"
    assert discover_call["existing_tests"] == ["Retailer logs in", "Retailer creates promo {{promo}}"]
    assert discover_call["max_cost_usd"] == 3.0
    assert discover_call["isolated"] and discover_call["headless"]


async def test_explore_run(client, fakes):
    async with client:
        await client.post("/projects", json=PROJECT)
        resp = await client.post("/projects/looma/explore", json={"charter": "Break the promo form", "max_cost_usd": 1.5})
        run = await _finished(client, resp.json()["id"])
        assert run["kind"] == "explore" and run["status"] == "completed"
        assert run["summary"] == {"bugs": 1, "proposed": 1}
        assert run["test_ids"] == ["TC-001"]
        assert (await client.post(f"/runs/{run['id']}/rerun")).status_code == 409
    assert fakes["explore"][0]["charter"] == "Break the promo form"
    assert fakes["explore"][0]["max_cost_usd"] == 1.5


async def test_session_validation(client, fakes):
    async with client:
        assert (await client.post("/projects/nope/discover")).status_code == 404
        await client.post("/projects", json={"name": "No URL"})
        resp = await client.post("/projects/no-url/discover")
        assert resp.status_code == 422 and "No URL" in resp.text
        await client.post("/projects", json=PROJECT)
        assert (await client.post("/projects/looma/explore", json={"charter": ""})).status_code == 422
        assert (await client.post("/projects/looma/discover", json={"max_cost_usd": 500})).status_code == 422


async def test_accept_requires_completed_session(client, fakes):
    fakes["gate"].clear()
    async with client:
        await client.post("/projects", json=PROJECT)
        test_run = (await client.post("/runs", json={"project": "looma", "scenario": "x"})).json()
        assert (await client.post(f"/runs/{test_run['id']}/accept", json={"suite_name": "X"})).status_code == 409
        pending = (await client.post("/projects/looma/discover")).json()
        assert (await client.post(f"/runs/{pending['id']}/accept", json={"suite_name": "X"})).status_code == 409
        resp = await client.post(f"/runs/{pending['id']}/accept", json={"suite_name": "X", "suite": "y"})
        assert resp.status_code == 422
        fakes["gate"].set()
        await _finished(client, pending["id"])


# ── cost limits ──────────────────────────────────────────────


async def test_cost_limit_defaults_and_overrides(client, fakes, monkeypatch):
    async with client:
        await client.post("/projects", json=PROJECT)
        default = (await client.post("/runs", json={"project": "looma", "scenario": "x"})).json()
        custom = (await client.post("/runs", json={"project": "looma", "scenario": "x", "max_cost_usd": 0.75})).json()
        await _finished(client, default["id"])
        await _finished(client, custom["id"])
    assert default["max_cost_usd"] == 5.0
    assert custom["max_cost_usd"] == 0.75
    assert [c["max_cost_usd"] for c in fakes["execute"]] == [5.0, 0.75]


async def test_cost_limit_env(tmp_path, fakes, monkeypatch):
    monkeypatch.setenv("ARGUS_MAX_RUN_COST", "1.25")
    monkeypatch.setenv("ARGUS_MAX_DISCOVER_COST", "not-a-number")
    app = server.create_app(tmp_path)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
        health = (await client.get("/health")).json()
    assert health["default_max_cost_usd"] == {"test": 1.25, "discover": 3.0, "explore": 2.0}


async def test_agents_stopped_at_limit_are_explained(client, monkeypatch):
    async def stopped(plan, url, run_dir, **kwargs):
        merged = merge_results(["Agent error: error_max_budget_usd"], plan)
        return RunResult(run_dir, run_dir / "report.md", merged, [tc.id for tc in plan.cases], cost_usd=5.0)

    monkeypatch.setattr(server, "execute_plan", stopped)
    async with client:
        await client.post("/projects", json=PROJECT)
        run = (await client.post("/runs", json={"project": "looma", "scenario": "x"})).json()
        run = await _finished(client, run["id"])
    assert run["status"] == "failed"
    assert "stopped early at the cost or turn limit ($5.00)" in run["error"]
