"""API tests. The browser run (execute_plan) is replaced with a fake."""

import asyncio
import json

import httpx
import pytest

from argus_qa import server
from argus_qa.agents.orchestrator import RunResult
from argus_qa.results import merge_results

PLAN = """# Plan

> URL: https://app.test

### TC-001: Home loads

1. Open the home page

### TC-002: Search works

1. Search for "shoes"
"""


@pytest.fixture
def fake_execute(monkeypatch):
    """Fake browser run: TC-002 fails, everything else passes. Records calls."""
    calls = []
    gate = asyncio.Event()
    gate.set()

    async def execute_plan(plan, url, run_dir, **kwargs):
        calls.append({"plan": plan, "url": url, **kwargs})
        await gate.wait()
        results = [
            {"id": tc.id, "status": "failed" if tc.id == "TC-002" else "passed"} for tc in plan.cases
        ]
        merged = merge_results([json.dumps({"results": results})], plan)
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "results.json").write_text(json.dumps(merged))
        (run_dir / "report.md").write_text("# Report\n")
        (run_dir / "junit.xml").write_text("<testsuite/>")
        (run_dir / "screenshots").mkdir(exist_ok=True)
        (run_dir / "screenshots" / "TC-001_step1.png").write_bytes(b"png")
        failed = [r["id"] for r in results if r["status"] == "failed"]
        return RunResult(run_dir, run_dir / "report.md", merged, failed, cost_usd=0.12)

    monkeypatch.setattr(server, "execute_plan", execute_plan)
    execute_plan.calls = calls
    execute_plan.gate = gate
    return execute_plan


@pytest.fixture
def make_client(tmp_path):
    def _make(**kwargs):
        app = server.create_app(tmp_path, **kwargs)
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
        client.app = app
        return client

    return _make


async def _wait_finished(client, run_id):
    for _ in range(200):
        run = (await client.get(f"/runs/{run_id}")).json()
        if run["status"] in server.FINISHED:
            return run
        await asyncio.sleep(0.01)
    raise AssertionError("run did not finish")


async def test_run_plan_end_to_end(make_client, fake_execute):
    async with make_client() as client:
        resp = await client.post("/runs", json={"plan": PLAN})
        assert resp.status_code == 202
        run = resp.json()
        assert run["status"] == "queued"
        assert run["url"] == "https://app.test"
        assert run["test_ids"] == ["TC-001", "TC-002"]

        run = await _wait_finished(client, run["id"])
        assert run["status"] == "failed"
        assert run["failed_ids"] == ["TC-002"]
        assert run["summary"]["passed"] == 1
        assert run["cost_usd"] == 0.12

        assert (await client.get(f"/runs/{run['id']}/results")).json()["summary"]["total"] == 2
        assert (await client.get(f"/runs/{run['id']}/report")).text == "# Report\n"
        assert (await client.get(f"/runs/{run['id']}/junit")).status_code == 200
        shots = (await client.get(f"/runs/{run['id']}/screenshots")).json()
        assert shots == ["TC-001_step1.png"]
        assert (await client.get(f"/runs/{run['id']}/screenshots/TC-001_step1.png")).content == b"png"

    assert fake_execute.calls[0]["headless"] is True
    assert fake_execute.calls[0]["isolated"] is True


async def test_run_scenario(make_client, fake_execute):
    async with make_client() as client:
        resp = await client.post("/runs", json={
            "scenario": "1. Open the page\n\nExpected: heading visible",
            "name": "Smoke",
            "url": "https://shop.test",
        })
        assert resp.status_code == 202
        run = await _wait_finished(client, resp.json()["id"])
        assert run["status"] == "passed"
    plan = fake_execute.calls[0]["plan"]
    assert [tc.name for tc in plan.cases] == ["Smoke"]


async def test_only_filter_and_url_override(make_client, fake_execute):
    async with make_client() as client:
        resp = await client.post("/runs", json={"plan": PLAN, "only": ["TC-001"], "url": "https://other.test"})
        run = await _wait_finished(client, resp.json()["id"])
    assert run["status"] == "passed"
    assert run["url"].startswith("https://other.test")
    assert [tc.id for tc in fake_execute.calls[0]["plan"].cases] == ["TC-001"]


@pytest.mark.parametrize("body, fragment", [
    ({}, "exactly one"),
    ({"plan": PLAN, "scenario": "x", "url": "https://a.test"}, "exactly one"),
    ({"scenario": "do stuff"}, "No URL"),
    ({"plan": "# no cases here"}, "no '### TC-NNN"),
    ({"plan": PLAN, "only": ["TC-404"]}, "Unknown test case IDs"),
    ({"plan": PLAN.replace("> URL: https://app.test", "")}, "No URL"),
])
async def test_validation_errors(make_client, fake_execute, body, fragment):
    async with make_client() as client:
        resp = await client.post("/runs", json=body)
    assert resp.status_code == 422
    assert fragment in resp.text
    assert fake_execute.calls == []


async def test_api_key_required_when_configured(make_client, fake_execute, monkeypatch):
    monkeypatch.setenv("ARGUS_API_KEY", "s3cret")
    async with make_client() as client:
        assert (await client.get("/runs")).status_code == 401
        assert (await client.get("/runs", headers={"Authorization": "Bearer nope"})).status_code == 401
        assert (await client.get("/runs", headers={"Authorization": "Bearer s3cret"})).status_code == 200


async def test_cancel_running_run(make_client, fake_execute):
    fake_execute.gate.clear()
    async with make_client() as client:
        run_id = (await client.post("/runs", json={"plan": PLAN})).json()["id"]
        await asyncio.sleep(0.02)
        assert (await client.post(f"/runs/{run_id}/cancel")).status_code == 200
        run = await _wait_finished(client, run_id)
        assert run["status"] == "cancelled"
        assert (await client.post(f"/runs/{run_id}/cancel")).status_code == 409


async def test_concurrency_limit_queues_runs(make_client, fake_execute):
    fake_execute.gate.clear()
    async with make_client(max_concurrent=1) as client:
        a = (await client.post("/runs", json={"plan": PLAN})).json()["id"]
        b = (await client.post("/runs", json={"plan": PLAN})).json()["id"]
        await asyncio.sleep(0.02)
        assert (await client.get(f"/runs/{a}")).json()["status"] == "running"
        assert (await client.get(f"/runs/{b}")).json()["status"] == "queued"
        fake_execute.gate.set()
        await _wait_finished(client, a)
        await _wait_finished(client, b)


async def test_execution_error_is_recorded(make_client, monkeypatch):
    async def boom(*args, **kwargs):
        raise RuntimeError("browser crashed")

    monkeypatch.setattr(server, "execute_plan", boom)
    async with make_client() as client:
        run_id = (await client.post("/runs", json={"plan": PLAN})).json()["id"]
        run = await _wait_finished(client, run_id)
    assert run["status"] == "error"
    assert "browser crashed" in run["error"]


async def test_runs_persist_and_interrupted_runs_marked(tmp_path, fake_execute):
    fake_execute.gate.clear()
    app = server.create_app(tmp_path)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
        run_id = (await client.post("/runs", json={"plan": PLAN})).json()["id"]
        await asyncio.sleep(0.02)

    # Simulate a restart while the run is still in flight
    app2 = server.create_app(tmp_path)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app2), base_url="http://t") as client:
        run = (await client.get(f"/runs/{run_id}")).json()
    assert run["status"] == "error"
    assert "restarted" in run["error"]
    fake_execute.gate.set()


async def test_screenshot_path_traversal_rejected(make_client, fake_execute):
    async with make_client() as client:
        run_id = (await client.post("/runs", json={"plan": PLAN})).json()["id"]
        await _wait_finished(client, run_id)
        resp = await client.get(f"/runs/{run_id}/screenshots/..%2Frun.json")
    assert resp.status_code in (400, 404)


async def test_callback_posted_on_finish(make_client, fake_execute, monkeypatch):
    posted = []

    async def fake_notify(run):
        posted.append(run.model_dump())

    monkeypatch.setattr(server, "_notify", fake_notify)
    async with make_client() as client:
        resp = await client.post("/runs", json={"plan": PLAN, "callback_url": "https://hooks.test/argus"})
        await _wait_finished(client, resp.json()["id"])
        await asyncio.sleep(0.01)
    assert posted[0]["status"] == "failed"
    assert posted[0]["callback_url"] == "https://hooks.test/argus"


async def test_environment_error_makes_run_an_error(make_client, monkeypatch):
    async def broken_browser(plan, url, run_dir, **kwargs):
        output = json.dumps({"results": [], "environment_error": "browser not installed"})
        merged = merge_results([output], plan)
        return RunResult(run_dir, run_dir / "report.md", merged, [], cost_usd=0.01)

    monkeypatch.setattr(server, "execute_plan", broken_browser)
    async with make_client() as client:
        run_id = (await client.post("/runs", json={"plan": PLAN})).json()["id"]
        run = await _wait_finished(client, run_id)
    assert run["status"] == "error"
    assert "browser not installed" in run["error"]


# ── projects ─────────────────────────────────────────────────

PROJECT = {
    "name": "Looma",
    "url": "https://looma.test",
    "instructions": "Dismiss the cookie banner.",
    "credentials": {"admin": {"username": "admin@looma.test", "password": "admin-pass-1"}},
    "variables": {"search_term": "shoes"},
    "secrets": {"api_token": "tok-12345"},
}


async def test_project_crud_masks_secrets(make_client, tmp_path):
    async with make_client() as client:
        resp = await client.post("/projects", json=PROJECT)
        assert resp.status_code == 201
        body = resp.json()
        assert body["slug"] == "looma"
        assert body["credentials"]["admin"]["password"] == "********"
        assert body["secrets"]["api_token"] == "********"
        assert "admin-pass-1" not in resp.text

        assert (await client.post("/projects", json=PROJECT)).status_code == 409
        assert [p["slug"] for p in (await client.get("/projects")).json()] == ["looma"]

        # Round-trip the masked copy with one change: stored secrets are kept
        body["url"] = "https://staging.looma.test"
        resp = await client.put("/projects/looma", json=body)
        assert resp.status_code == 200
        stored = json.loads((tmp_path / "projects" / "looma.json").read_text())
        assert stored["url"] == "https://staging.looma.test"
        assert stored["credentials"]["admin"]["password"] == "admin-pass-1"
        assert stored["secrets"]["api_token"] == "tok-12345"
        assert (tmp_path / "projects" / "looma.json").stat().st_mode & 0o077 == 0

        assert (await client.delete("/projects/looma")).status_code == 204
        assert (await client.get("/projects/looma")).status_code == 404


async def test_project_validation(make_client):
    async with make_client() as client:
        assert (await client.post("/projects", json={"name": "x", "colour": "red"})).status_code == 422
        assert (await client.post("/projects", json={"name": "x", "slug": "Bad Slug"})).status_code == 422
        resp = await client.put("/projects/looma", json={**PROJECT, "slug": "other"})
        assert resp.status_code == 422


async def test_run_with_project(make_client, fake_execute):
    async with make_client() as client:
        await client.post("/projects", json=PROJECT)
        resp = await client.post("/runs", json={
            "project": "looma",
            "scenario": "1. Log in as admin\n2. Search for {{search_term}}\n\nExpected: results shown",
        })
        assert resp.status_code == 202
        run = await _wait_finished(client, resp.json()["id"])
        assert run["project"] == "looma"
        assert run["url"] == "https://looma.test"
        assert [r["id"] for r in (await client.get("/runs?project=looma")).json()] == [run["id"]]
        assert (await client.get("/runs?project=other")).json() == []

    project = fake_execute.calls[0]["project"]
    assert project.credentials["admin"].password == "admin-pass-1"


async def test_run_with_project_errors(make_client, fake_execute, monkeypatch):
    async with make_client() as client:
        resp = await client.post("/runs", json={"project": "nope", "scenario": "x"})
        assert resp.status_code == 404

        await client.post("/projects", json={**PROJECT, "slug": "envy",
                                             "secrets": {"api_token": "${ARGUS_TEST_UNSET_VAR}"}})
        monkeypatch.delenv("ARGUS_TEST_UNSET_VAR", raising=False)
        resp = await client.post("/runs", json={"project": "envy", "scenario": "x"})
        assert resp.status_code == 422
        assert "ARGUS_TEST_UNSET_VAR" in resp.text

        await client.post("/projects", json=PROJECT)
        resp = await client.post("/runs", json={"project": "looma", "scenario": "Use {{unknown}}"})
        assert resp.status_code == 422
        assert "unknown" in resp.text
    assert fake_execute.calls == []


# ── web UI support ───────────────────────────────────────────


async def test_ui_and_health_are_public_but_data_needs_key(make_client, monkeypatch):
    monkeypatch.setenv("ARGUS_API_KEY", "s3cret")
    async with make_client() as client:
        page = await client.get("/")
        assert page.status_code == 200
        assert "argus" in page.text
        assert (await client.get("/static/app.js")).status_code == 200
        assert (await client.get("/static/vendor/purify.min.js")).status_code == 200
        assert (await client.get("/health")).json() == {"status": "ok", "auth_required": True}
        assert (await client.get("/runs")).status_code == 401
        assert (await client.get("/projects")).status_code == 401


async def test_run_title(make_client, fake_execute):
    async with make_client() as client:
        titled = (await client.post("/runs", json={"plan": PLAN})).json()
        named = (await client.post("/runs", json={"scenario": "x", "name": "Checkout", "url": "https://a.test"})).json()
    assert titled["title"] == "Plan"
    assert named["title"] == "Checkout"


async def test_events_feed(make_client, monkeypatch):
    from argus_qa.events import emit

    async def chatty(plan, url, run_dir, **kwargs):
        emit("phase", "Dave is running 2 tests")
        emit("action", "Open: https://app.test", agent="Dave", tool="navigate")
        merged = merge_results([json.dumps({"results": []})], plan)
        return RunResult(run_dir, run_dir / "report.md", merged, [], cost_usd=0)

    monkeypatch.setattr(server, "execute_plan", chatty)
    async with make_client() as client:
        run_id = (await client.post("/runs", json={"plan": PLAN})).json()["id"]
        await _wait_finished(client, run_id)
        first = (await client.get(f"/runs/{run_id}/events")).json()
        assert [e["kind"] for e in first["events"]] == ["phase", "action"]
        assert first["events"][1]["agent"] == "Dave"
        assert first["next"] == 2
        assert (await client.get(f"/runs/{run_id}/events?after=2")).json() == {"events": [], "next": 2}
        assert "### TC-001" in (await client.get(f"/runs/{run_id}/plan")).text


async def test_rerun_failed_and_all(make_client, fake_execute):
    async with make_client() as client:
        await client.post("/projects", json=PROJECT)
        first = (await client.post("/runs", json={"plan": PLAN, "project": "looma", "parallel": 2})).json()
        first = await _wait_finished(client, first["id"])
        assert first["failed_ids"] == ["TC-002"]

        failed = (await client.post(f"/runs/{first['id']}/rerun", json={"failed_only": True})).json()
        assert failed["test_ids"] == ["TC-002"]
        assert failed["project"] == "looma"
        assert failed["parallel"] == 2
        everything = (await client.post(f"/runs/{first['id']}/rerun", json={"failed_only": False})).json()
        assert everything["test_ids"] == ["TC-001", "TC-002"]

        passed = await _wait_finished(client, failed["id"])
        passed = await _wait_finished(client, everything["id"])
        only_passing = (await client.post("/runs", json={"plan": PLAN, "only": ["TC-001"]})).json()
        await _wait_finished(client, only_passing["id"])
        resp = await client.post(f"/runs/{only_passing['id']}/rerun", json={"failed_only": True})
        assert resp.status_code == 409
    assert passed
