"""Recording scripts from AI runs, building them safely, and the ai / script / auto run modes."""

import json
import shutil

import pytest

from argus_qa import scripts
from argus_qa.agents import orchestrator
from argus_qa.plan_parser import parse_test_plan
from argus_qa.project import Project

PLAN = parse_test_plan("""# Test Plan: Login

### TC-001: Member logs in

**Preconditions:**
- On the login page

**Steps:**
1. Enter the member's credentials
2. Click Login

**Acceptance criteria:**
- The Secure Area is shown

### TC-002: Wrong password is rejected

**Steps:**
1. Log in with a wrong password

**Acceptance criteria:**
- An error is shown
""")
URL = "https://app.test/login"
PROJECT = Project.from_dict({
    "name": "App", "url": URL,
    "credentials": {"member": {"username": "tomsmith", "password": "Secret-Pass-1"}},
})


def _code(js: str) -> str:
    return f"### Ran Playwright code\n```js\n{js}\n```\n### Page\n- Page URL: {URL}"


def _feed(recorder: scripts.Recorder, calls: list[tuple]) -> None:
    for i, (name, args, result) in enumerate(calls):
        recorder.on_tool_use(f"t{i}", name, args)
        if result is not None:
            recorder.on_tool_result(f"t{i}", [{"type": "text", "text": result}], result.startswith("### Error"))


LOGIN_SESSION = [
    ("mcp__argus__start_test", {"test_id": "TC-001"}, None),
    ("mcp__playwright__browser_run_code_unsafe", {"code": "reset"}, _code("await (async (page) => { /* reset */ })(page);")),
    ("mcp__argus__start_step", {"test_id": "TC-001", "step": 0}, None),
    ("mcp__playwright__browser_navigate", {"url": URL}, _code(f"await page.goto('{URL}');")),
    ("mcp__argus__start_step", {"test_id": "TC-001", "step": 1}, None),
    ("mcp__playwright__browser_fill_form", {}, _code(
        "await page.getByRole('textbox', { name: 'Username' }).fill('tomsmith');\n"
        "await page.getByRole('textbox', { name: 'Password' }).fill('Secret-Pass-1');")),
    ("mcp__playwright__browser_take_screenshot", {"filename": "TC-001_step1.png"}, _code("await page.screenshot();")),
    ("mcp__playwright__browser_click", {"element": "Login"}, "### Error\nTimeout"),   # failed attempt
    ("mcp__argus__start_step", {"test_id": "TC-001", "step": 2}, None),
    ("mcp__playwright__browser_click", {"element": "Login"}, _code("await page.getByRole('button', { name: 'Login' }).click();")),
    ("mcp__argus__check", {"test_id": "TC-001", "criterion": "The Secure Area is shown", "kind": "text_visible", "text": "Secure Area"}, None),
    ("mcp__argus__check", {"test_id": "TC-001", "criterion": "Lands on the secure page", "kind": "url_contains", "value": "https://app.test/secure"}, None),
    ("mcp__argus__start_test", {"test_id": "TC-002"}, None),
    ("mcp__argus__start_step", {"test_id": "TC-002", "step": 1}, None),
    ("mcp__playwright__browser_navigate", {"url": URL}, _code(f"await page.goto('{URL}');")),
    ("mcp__argus__check", {"test_id": "TC-002", "criterion": "An error is shown", "kind": "manual"}, None),
]


def test_recorder_groups_actions_by_test_and_step():
    recorder = scripts.Recorder()
    _feed(recorder, LOGIN_SESSION)
    tests = recorder.tests()
    tc1 = tests["TC-001"]
    assert sorted(tc1.steps) == [0, 1, 2]
    assert tc1.steps[0] == [f"await page.goto('{URL}');"]
    assert "fill('tomsmith')" in tc1.steps[1][0]          # the reset before step 0 is left out
    assert tc1.steps[2] == ["await page.getByRole('button', { name: 'Login' }).click();"]  # failed click and screenshot skipped
    assert tc1.actions == 3
    assert [c["kind"] for c in tc1.checks] == ["text_visible", "url_contains"]


def test_build_script_is_portable_and_secret_free():
    recorder = scripts.Recorder()
    _feed(recorder, LOGIN_SESSION)
    case = PLAN.cases[0]
    js, reason = scripts.build_script(case, recorder.tests()["TC-001"], URL, PROJECT.placeholder_values(),
                                      PROJECT.secret_placeholders())
    assert reason == "ok"
    assert "Secret-Pass-1" not in js and "tomsmith" not in js
    assert 'fill(v("member.password"))' in js and 'fill(v("member.username"))' in js
    assert 'await page.goto(url("/login"));' in js
    assert 'await step(0, "Before you start"' in js
    assert 'await step(1, "Enter the member\'s credentials"' in js
    assert 'await check.textVisible("Secure Area", "The Secure Area is shown");' in js
    assert 'await check.urlContains("/secure", "Lands on the secure page");' in js


def _recording(code: str) -> scripts.RecordedTest:
    check = {"criterion": "c", "kind": "text_visible", "text": "ok"}
    return scripts.RecordedTest("TC-001", steps={1: [code]}, checks=[check], actions=1,
                                items=[("step", 1), ("code", code), ("check", check)])


def test_checks_replay_where_they_were_verified():
    recorder = scripts.Recorder()
    _feed(recorder, [
        ("mcp__argus__start_test", {"test_id": "TC-001"}, None),
        ("mcp__argus__start_step", {"test_id": "TC-001", "step": 1}, None),
        ("mcp__playwright__browser_navigate", {}, _code(f"await page.goto('{URL}');")),
        ("mcp__argus__check", {"test_id": "TC-001", "criterion": "Login form shown", "kind": "element_visible", "role": "textbox", "name": "Username"}, None),
        ("mcp__playwright__browser_click", {}, _code("await page.getByRole('button', { name: 'Login' }).click();")),
        ("mcp__argus__start_step", {"test_id": "TC-001", "step": 2}, None),
        ("mcp__playwright__browser_navigate", {}, _code("await page.goto('https://app.test/secure');")),
        ("mcp__argus__check", {"test_id": "TC-001", "criterion": "Secure page", "kind": "url_contains", "value": "/secure"}, None),
    ])
    js, _ = scripts.build_script(PLAN.cases[0], recorder.tests()["TC-001"], URL, {}, set())
    order = [js.index(s) for s in ('goto(url("/login"))', "Login form shown", "click()", 'goto(url("/secure"))', "Secure page")]
    assert order == sorted(order)
    assert js.count('await step(1, "Enter the member') == 2   # step 1 resumes after its check


def test_build_script_refuses_what_it_cannot_replay():
    recorder = scripts.Recorder()
    _feed(recorder, LOGIN_SESSION)
    js, reason = scripts.build_script(PLAN.cases[1], recorder.tests()["TC-002"], URL, {}, set())
    assert js is None and "judgement" in reason

    empty = scripts.RecordedTest("TC-001")
    assert scripts.build_script(PLAN.cases[0], empty, URL, {}, set())[1] == "no browser actions were recorded"

    # A separate literal is replaced safely...
    split = _recording("await page.fill('#p', 'x' + 'Secret-Pass-1')")
    js, _ = scripts.build_script(PLAN.cases[0], split, URL, PROJECT.placeholder_values(), PROJECT.secret_placeholders())
    assert js and 'v("member.password")' in js and "Secret-Pass-1" not in js
    # ...but a secret inside a longer string can't be, so there's no script
    leaky = _recording("await page.fill('#p', 'pw: Secret-Pass-1')")
    js, reason = scripts.build_script(PLAN.cases[0], leaky, URL, PROJECT.placeholder_values(), PROJECT.secret_placeholders())
    assert js is None and "secret" in reason


def test_case_hash_changes_with_wording():
    edited = parse_test_plan(PLAN.to_markdown().replace("Click Login", "Click Sign in"))
    assert scripts.case_hash(PLAN.cases[0]) != scripts.case_hash(edited.cases[0])
    assert scripts.case_hash(PLAN.cases[1]) == scripts.case_hash(edited.cases[1])


# ── run modes (replay and agents faked) ──────────────────────


@pytest.fixture
def fakes(monkeypatch, tmp_path):
    # replay_results: outcomes for the first replay (recorded scripts); later replays
    # (verifying freshly recorded scripts) pass unless listed in verify_results
    state = {"replayed": [], "agent_plans": [], "replay_results": {}, "verify_results": {}, "ai_statuses": {}}

    async def fake_replay(to_replay, url, values, ss_dir, runner, headless=True):
        overrides = state["replay_results"] if not state["replayed"] else state["verify_results"]
        state["replayed"].append(sorted(to_replay))
        passed = {"status": "passed", "steps": [], "checks": [], "duration_ms": 900}
        return {tid: {"id": tid, **overrides.get(tid, passed)} for tid in to_replay}

    async def fake_agent(prompt, options, label="", redact=None, recorder=None):
        ids = [tc.id for tc in parse_test_plan(prompt).cases] or ["TC-001", "TC-002"]
        ids = [i for i in ("TC-001", "TC-002") if f"### {i}:" in prompt]
        state["agent_plans"].append(ids)
        if recorder is not None:
            _feed(recorder, LOGIN_SESSION)
        results = [{"id": i, "name": i, "status": state["ai_statuses"].get(i, "passed")} for i in ids]
        return orchestrator.AgentRun(text=json.dumps({"results": results}), ok=True, cost_usd=0.1)

    monkeypatch.setattr(orchestrator, "replay", fake_replay)
    monkeypatch.setattr(orchestrator, "ensure_runner", lambda *a: tmp_path)
    monkeypatch.setattr(orchestrator, "_run_agent", fake_agent)
    return state


def _scripts(tmp_path, *ids):
    out = {}
    for i in ids:
        out[i] = tmp_path / f"{i}.mjs"
        out[i].write_text("export default async () => {}")
    return out


async def test_script_mode_replays_without_ai(fakes, tmp_path):
    result = await orchestrator.execute_plan(
        PLAN, URL, tmp_path / "run", project=PROJECT, mode="script", scripts=_scripts(tmp_path, "TC-001"))
    statuses = {r["id"]: (r["status"], r["mode"]) for r in result.results["results"]}
    assert statuses == {"TC-001": ("passed", "script"), "TC-002": ("blocked", "script")}
    assert fakes["agent_plans"] == []
    assert result.cost_usd == 0
    assert result.results["summary"]["by_script"] == 2


async def test_auto_mode_heals_failed_scripts_with_ai(fakes, tmp_path):
    fakes["replay_results"]["TC-001"] = {"id": "TC-001", "status": "failed", "failed_step": 2,
                                         "error": "No button named Login", "steps": [], "checks": []}
    result = await orchestrator.execute_plan(
        PLAN, URL, tmp_path / "run", project=PROJECT, mode="auto", scripts=_scripts(tmp_path, "TC-001", "TC-002"),
        record=True)
    by_id = {r["id"]: r for r in result.results["results"]}
    assert fakes["agent_plans"] == [["TC-001"]]                    # only the failed script goes to the AI
    assert by_id["TC-001"]["mode"] == "ai" and by_id["TC-001"]["healed"] is True
    assert "No button named Login" in by_id["TC-001"]["notes"]
    assert by_id["TC-002"]["mode"] == "script"
    assert result.results["summary"]["healed"] == 1
    assert set(result.recorded) == {"TC-001"}                      # re-recorded after healing
    assert by_id["TC-001"]["script"] == "recorded"


async def test_auto_mode_real_failure_when_ai_also_fails(fakes, tmp_path):
    fakes["replay_results"]["TC-001"] = {"id": "TC-001", "status": "failed", "error": "Text not visible", "steps": [], "checks": []}
    fakes["ai_statuses"]["TC-001"] = "failed"
    result = await orchestrator.execute_plan(
        PLAN, URL, tmp_path / "run", project=PROJECT, mode="auto", scripts=_scripts(tmp_path, "TC-001"), record=True)
    tc1 = next(r for r in result.results["results"] if r["id"] == "TC-001")
    assert tc1["status"] == "failed" and not tc1.get("healed")
    assert "The recorded script failed too" in tc1["notes"]
    assert "TC-001" not in result.recorded


async def test_ai_mode_records_verified_scripts(fakes, tmp_path):
    result = await orchestrator.execute_plan(PLAN, URL, tmp_path / "run", project=PROJECT, mode="ai", record=True)
    assert fakes["agent_plans"] == [["TC-001", "TC-002"]]
    assert set(result.recorded) == {"TC-001"}                      # TC-002 has a judgement-only criterion
    assert fakes["replayed"] == [["TC-001"]]                       # verified by replaying before keeping it
    tc2 = next(r for r in result.results["results"] if r["id"] == "TC-002")
    assert tc2["script"].startswith("not recorded") and "judgement" in tc2["script"]
    assert "Secret-Pass-1" not in result.recorded["TC-001"]


async def test_unverified_scripts_are_not_kept(fakes, tmp_path):
    fakes["replayed"].append("pretend an earlier replay happened")
    fakes["verify_results"]["TC-001"] = {"status": "failed", "error": "flaky", "steps": [], "checks": []}
    result = await orchestrator.execute_plan(PLAN, URL, tmp_path / "run", project=PROJECT, mode="ai", record=True)
    assert result.recorded == {}
    tc1 = next(r for r in result.results["results"] if r["id"] == "TC-001")
    assert "didn't replay" in tc1["script"]


async def test_unknown_mode(tmp_path):
    with pytest.raises(ValueError, match="Unknown run mode"):
        await orchestrator.execute_plan(PLAN, URL, tmp_path, mode="turbo")


# ── the real runner (needs Node and the installed runner) ────


@pytest.mark.skipif(
    not shutil.which("node") or not (scripts.runner_dir() / "node_modules" / "playwright-core").is_dir(),
    reason="script runner not installed (argus-qa setup)",
)
async def test_real_runner_against_a_local_page(tmp_path):
    page = tmp_path / "site" / "index.html"
    page.parent.mkdir()
    page.write_text("<h1>Welcome</h1><label>Name <input id=n></label><button onclick=\"document.body.insertAdjacentHTML('beforeend','<p>Hi '+n.value+'</p>')\">Greet</button>")
    good = tmp_path / "TC-001.mjs"
    good.write_text("""export default async function run(page, { step, check, v, url }) {
  await step(1, "Open", async () => { await page.goto(url('/index.html')); });
  await step(2, "Greet", async () => {
    await page.getByLabel('Name').fill(v("name"));
    await page.getByRole('button', { name: 'Greet' }).click();
  });
  await check.textVisible("Hi Ada", "Greeting shown");
  await check.fieldValue("Name", "Ada", "Name kept");
}""")
    bad = tmp_path / "TC-002.mjs"
    bad.write_text("""export default async function run(page, { step, check, url }) {
  await step(1, "Open", async () => { await page.goto(url('/index.html')); });
  await check.elementVisible("button", "Delete", "Delete button shown");
}""")
    import functools
    import http.server
    import threading

    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(page.parent))
    handler.log_message = lambda *a: None
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}/"
    results = await scripts.replay({"TC-001": good, "TC-002": bad}, base, {"name": "Ada"}, tmp_path / "shots", scripts.runner_dir())
    assert results["TC-001"]["status"] == "passed"
    assert [c["status"] for c in results["TC-001"]["checks"]] == ["passed", "passed"]
    assert results["TC-002"]["status"] == "failed"
    assert 'No visible button named "Delete"' in results["TC-002"]["error"]
    assert (tmp_path / "shots" / "TC-001_step2.png").is_file()
    httpd.shutdown()
