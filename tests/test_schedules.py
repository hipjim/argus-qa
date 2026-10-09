"""Schedules: when they're due, and what firing one does (API level, fake browser run)."""

import asyncio
import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from argus_qa import server
from argus_qa.agents.orchestrator import RunResult
from argus_qa.results import merge_results
from argus_qa.schedules import next_fire

PROJECT = {"name": "Looma", "url": "https://looma.test", "variables": {"promo": "${LOOMA_PROMO}"}}

PLAN = """# Test Plan: {name}

### TC-001: Page loads

1. Open the page
"""


def utc(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=UTC)


# ── next_fire ────────────────────────────────────────────────


@pytest.mark.parametrize("days, at, tz, after, expected", [
    # Every day: later today, else tomorrow
    ([], "02:00", "UTC", "2026-10-02T01:00", "2026-10-02T02:00"),
    ([], "02:00", "UTC", "2026-10-02T02:00", "2026-10-03T02:00"),
    # End of the week: 2026-10-02 is a Friday
    (["fri"], "18:00", "UTC", "2026-10-02T17:59", "2026-10-02T18:00"),
    (["fri"], "18:00", "UTC", "2026-10-02T18:00", "2026-10-09T18:00"),
    (["mon", "thu"], "09:30", "UTC", "2026-10-02T12:00", "2026-10-05T09:30"),
    # The time is local: 02:00 in Bucharest is 23:00 UTC the day before in summer, 00:00 UTC in winter
    ([], "02:00", "Europe/Bucharest", "2026-10-02T12:00", "2026-10-02T23:00"),
    ([], "02:00", "Europe/Bucharest", "2026-12-01T12:00", "2026-12-02T00:00"),
    # The weekday is local too: Saturday 01:00 in Bucharest is still Friday in UTC
    (["sat"], "01:00", "Europe/Bucharest", "2026-10-02T12:00", "2026-10-02T22:00"),
])
def test_next_fire(days, at, tz, after, expected):
    assert next_fire(days, at, tz, utc(after)) == utc(expected)


def test_next_fire_rejects_unknown_timezone():
    with pytest.raises(ValueError, match="Unknown timezone"):
        next_fire([], "02:00", "Mars/Olympus", utc("2026-10-02T12:00"))


# ── API ──────────────────────────────────────────────────────


@pytest.fixture
def fake_execute(monkeypatch):
    """Fake browser run: every test passes, except in the suite named Checkout."""
    calls = []
    gate = asyncio.Event()
    gate.set()

    async def execute_plan(plan, url, run_dir, **kwargs):
        calls.append({"plan": plan, **kwargs})
        await gate.wait()
        status = "failed" if "Checkout" in plan.preamble else "passed"
        merged = merge_results([json.dumps({"results": [{"id": tc.id, "status": status} for tc in plan.cases]})], plan)
        failed = [tc.id for tc in plan.cases] if status == "failed" else []
        return RunResult(run_dir, run_dir / "report.md", merged, failed, cost_usd=0.1)

    monkeypatch.setattr(server, "execute_plan", execute_plan)
    execute_plan.calls = calls
    execute_plan.gate = gate
    return execute_plan


@pytest.fixture
async def client(tmp_path, monkeypatch):
    monkeypatch.setenv("LOOMA_PROMO", "Summer")
    app = server.create_app(tmp_path)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        client.scheduler = app.state.scheduler
        await client.post("/projects", json=PROJECT)
        for name in ("Smoke", "Checkout"):
            await client.post("/projects/looma/suites", json={"name": name, "plan": PLAN.format(name=name)})
        yield client


NIGHTLY = {"name": "Nightly", "suites": ["smoke", "checkout"], "time": "02:00"}


async def _finished(client, run_id):
    for _ in range(200):
        run = (await client.get(f"/runs/{run_id}")).json()
        if run["status"] in server.FINISHED:
            return run
        await asyncio.sleep(0.01)
    raise AssertionError("run did not finish")


async def _due(client, slug="nightly"):
    return datetime.fromisoformat((await client.get(f"/projects/looma/schedules/{slug}")).json()["next_run_at"])


async def test_schedule_crud(client):
    resp = await client.post("/projects/looma/schedules", json={**NIGHTLY, "days": ["fri", "mon"]})
    assert resp.status_code == 201
    schedule = resp.json()
    assert schedule["slug"] == "nightly"
    assert schedule["days"] == ["mon", "fri"]
    assert schedule["suite_names"] == {"smoke": "Smoke", "checkout": "Checkout"}
    assert schedule["cost_limit_usd"] == 5.0
    assert datetime.fromisoformat(schedule["next_run_at"]) > datetime.now(UTC)
    assert (await client.post("/projects/looma/schedules", json=NIGHTLY)).status_code == 409

    resp = await client.put("/projects/looma/schedules/nightly", json={**NIGHTLY, "enabled": False, "max_cost_usd": 2})
    assert resp.json()["next_run_at"] is None
    assert resp.json()["cost_limit_usd"] == 2
    assert [s["slug"] for s in (await client.get("/projects/looma/schedules")).json()] == ["nightly"]

    assert (await client.delete("/projects/looma/schedules/nightly")).status_code == 204
    assert (await client.get("/projects/looma/schedules/nightly")).status_code == 404


@pytest.mark.parametrize("change, fragment", [
    ({"suites": []}, "suites"),
    ({"suites": ["nope"]}, "Unknown suites"),
    ({"time": "25:00"}, "time"),
    ({"days": ["someday"]}, "days"),
    ({"timezone": "Mars/Olympus"}, "Unknown timezone"),
    ({"slug": "../escape"}, "slug"),
])
async def test_schedule_validation(client, change, fragment):
    resp = await client.post("/projects/looma/schedules", json={**NIGHTLY, **change})
    assert resp.status_code == 422
    assert fragment in resp.text


async def test_due_schedule_runs_each_suite(client, fake_execute):
    await client.post("/projects/looma/schedules", json={**NIGHTLY, "mode": "script", "parallel": 2})
    due = await _due(client)

    client.scheduler.tick(due - timedelta(minutes=1))
    assert (await client.get("/runs")).json() == []

    client.scheduler.tick(due + timedelta(seconds=20))
    runs = (await client.get("/runs?schedule=nightly")).json()
    assert sorted(r["suite"] for r in runs) == ["checkout", "smoke"]
    assert len({r["batch"] for r in runs}) == 1
    assert all(r["mode"] == "script" and r["parallel"] == 2 for r in runs)
    statuses = {r["suite"]: (await _finished(client, r["id"]))["status"] for r in runs}
    assert statuses == {"smoke": "passed", "checkout": "failed"}

    schedule = (await client.get("/projects/looma/schedules/nightly")).json()
    assert [(r["suite"], r["status"]) for r in schedule["last_runs"]] == [("smoke", "passed"), ("checkout", "failed")]
    assert schedule["last_error"] is None
    assert await _due(client) == due + timedelta(days=1)

    # The same due time doesn't fire twice
    client.scheduler.tick(due + timedelta(seconds=50))
    assert len((await client.get("/runs")).json()) == 2
    assert (await client.get(f"/runs?batch={runs[0]['batch']}&suite=smoke")).json()[0]["id"] in {r["id"] for r in runs}


async def test_paused_schedule_does_not_fire_but_runs_on_demand(client, fake_execute):
    await client.post("/projects/looma/schedules", json=NIGHTLY)
    due = await _due(client)
    await client.put("/projects/looma/schedules/nightly", json={**NIGHTLY, "enabled": False})
    client.scheduler.tick(due + timedelta(seconds=20))
    assert (await client.get("/runs")).json() == []

    fake_execute.gate.clear()
    resp = await client.post("/projects/looma/schedules/nightly/run")
    assert resp.status_code == 202
    assert [r["suite"] for r in resp.json()] == ["smoke", "checkout"]
    assert (await client.post("/projects/looma/schedules/nightly/run")).status_code == 409
    fake_execute.gate.set()
    for run in resp.json():
        await _finished(client, run["id"])


async def test_overlapping_run_is_skipped(client, fake_execute):
    await client.post("/projects/looma/schedules", json=NIGHTLY)
    due = await _due(client)
    fake_execute.gate.clear()
    client.scheduler.tick(due)
    client.scheduler.tick(due + timedelta(days=1))
    runs = (await client.get("/runs")).json()
    assert len(runs) == 2
    schedule = (await client.get("/projects/looma/schedules/nightly")).json()
    assert "previous one hadn't finished" in schedule["last_error"]
    assert await _due(client) == due + timedelta(days=2)
    fake_execute.gate.set()
    for run in runs:
        await _finished(client, run["id"])


async def test_run_missed_while_server_was_down_is_not_caught_up(client, fake_execute):
    await client.post("/projects/looma/schedules", json=NIGHTLY)
    due = await _due(client)
    client.scheduler.tick(due + timedelta(hours=7))
    assert (await client.get("/runs")).json() == []
    schedule = (await client.get("/projects/looma/schedules/nightly")).json()
    assert "Missed the run" in schedule["last_error"]
    assert await _due(client) == due + timedelta(days=1)


async def test_problems_starting_a_run_are_kept_on_the_schedule(client, fake_execute, monkeypatch):
    await client.post("/projects/looma/schedules", json=NIGHTLY)
    due = await _due(client)
    await client.delete("/projects/looma/suites/checkout")
    client.scheduler.tick(due)
    schedule = (await client.get("/projects/looma/schedules/nightly")).json()
    assert [r["suite"] for r in schedule["last_runs"]] == ["smoke"]
    assert "checkout" in schedule["last_error"] and "not found" in schedule["last_error"]
    assert schedule["suite_names"] == {"smoke": "Smoke", "checkout": None}
    await _finished(client, schedule["last_runs"][0]["id"])

    # The project needs an environment variable the server no longer has: nothing can start
    monkeypatch.delenv("LOOMA_PROMO")
    client.scheduler.tick(due + timedelta(days=1))
    schedule = (await client.get("/projects/looma/schedules/nightly")).json()
    assert schedule["last_runs"] == []
    assert "LOOMA_PROMO" in schedule["last_error"]
    resp = await client.post("/projects/looma/schedules/nightly/run")
    assert resp.status_code == 422 and "LOOMA_PROMO" in resp.text


async def test_unexpected_problem_starting_a_run_is_not_retried_every_tick(client, fake_execute, monkeypatch):
    await client.post("/projects/looma/schedules", json=NIGHTLY)
    due = await _due(client)

    def broken(*_, **__):
        raise RuntimeError("the disk is full")

    monkeypatch.setattr(client.scheduler.manager, "submit", broken)
    client.scheduler.tick(due)
    schedule = (await client.get("/projects/looma/schedules/nightly")).json()
    assert "Couldn't start the run" in schedule["last_error"] and "the disk is full" in schedule["last_error"]
    assert await _due(client) == due + timedelta(days=1)


async def test_damaged_schedule_file_does_not_stop_the_others(client, fake_execute, tmp_path):
    await client.post("/projects/looma/schedules", json=NIGHTLY)
    await client.post("/projects/looma/schedules", json={**NIGHTLY, "name": "Broken"})
    (tmp_path / "schedules" / "looma" / "broken.json").write_text('{"name": "Bro')   # cut off mid-write
    due = await _due(client)

    client.scheduler.tick(due)
    runs = (await client.get("/runs?schedule=nightly")).json()
    assert sorted(r["suite"] for r in runs) == ["checkout", "smoke"]
    for run in runs:
        await _finished(client, run["id"])

    assert [s["slug"] for s in (await client.get("/projects/looma/schedules")).json()] == ["nightly"]
    resp = await client.get("/projects/looma/schedules/broken")
    assert resp.status_code == 500 and "can't be read" in resp.text
    assert (await client.delete("/projects/looma/schedules/broken")).status_code < 300
    assert not (tmp_path / "schedules" / "looma" / "broken.json").exists()


async def test_notify_on_failure_only(client, fake_execute, monkeypatch):
    posted = []

    async def fake_notify(run):
        posted.append(run.suite)

    monkeypatch.setattr(server, "_notify", fake_notify)
    body = {**NIGHTLY, "callback_url": "https://hooks.test/argus", "notify_on": "failure"}
    await client.post("/projects/looma/schedules", json=body)
    for run in (await client.post("/projects/looma/schedules/nightly/run")).json():
        await _finished(client, run["id"])
    await asyncio.sleep(0.01)
    assert posted == ["checkout"]


async def test_deleting_project_deletes_its_schedules(client, tmp_path):
    await client.post("/projects/looma/schedules", json=NIGHTLY)
    assert (tmp_path / "schedules" / "looma" / "nightly.json").is_file()
    await client.delete("/projects/looma")
    assert not (tmp_path / "schedules" / "looma").exists()
