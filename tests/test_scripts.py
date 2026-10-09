"""Recording scripts from AI runs, building them safely, and the ai / script / auto run modes."""

import json
import shutil

import pytest

from argus_qa import scripts
from argus_qa.agents import orchestrator
from argus_qa.plan_parser import parse_test_plan
from argus_qa.project import Project
from argus_qa.results import partial_results

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


def test_recorder_leaves_out_checks_the_tool_turned_down():
    criterion = "The Secure Area is shown"
    recorder = scripts.Recorder()
    _feed(recorder, [
        ("mcp__argus__start_step", {"test_id": "TC-001", "step": 1}, None),
        ("mcp__playwright__browser_navigate", {"url": URL}, _code(f"await page.goto('{URL}');")),
        # The check tool answers these two with an error, and the agent tries again
        ("mcp__argus__check", {"test_id": "TC-001", "criterion": criterion, "kind": "text_visible"}, None),
        ("mcp__argus__check", {"test_id": "TC-001", "criterion": criterion, "kind": "visible"}, None),
        ("mcp__argus__check", {"test_id": "TC-001", "criterion": criterion, "kind": "text_visible", "text": "Secure Area"}, None),
    ])
    recording = recorder.tests()["TC-001"]
    assert recording.checks == [{"criterion": criterion, "kind": "text_visible", "text": "Secure Area"}]
    js, _ = scripts.build_script(PLAN.cases[0], recording, URL, {}, set())
    assert js.count("check.textVisible(") == 1 and 'check.textVisible("Secure Area"' in js


def test_case_hash_changes_with_wording():
    edited = parse_test_plan(PLAN.to_markdown().replace("Click Login", "Click Sign in"))
    assert scripts.case_hash(PLAN.cases[0]) != scripts.case_hash(edited.cases[0])
    assert scripts.case_hash(PLAN.cases[1]) == scripts.case_hash(edited.cases[1])


# ── run modes (replay and agents faked) ──────────────────────


@pytest.fixture
def fakes(monkeypatch, tmp_path):
    # replay_results: outcomes for replays of recorded scripts, before the AI runs (a list gives
    # one outcome per try, the last repeating); later replays (verifying freshly recorded
    # scripts) pass unless listed in verify_results
    # reports: tests the agent hands in with report_test as it goes (ID -> the call's arguments);
    # stop: the agent stops early with this result subtype, after its reports and without a final report
    state = {"replayed": [], "agent_plans": [], "replay_results": {}, "verify_results": {}, "ai_statuses": {},
             "ai_results": {}, "reports": {}, "stop": None, "seen_on_disk": []}

    async def fake_replay(to_replay, url, values, ss_dir, runner, headless=True):
        overrides = state["verify_results"] if state["agent_plans"] else state["replay_results"]
        state["replayed"].append(sorted(to_replay))
        passed = {"status": "passed", "steps": [], "checks": [], "duration_ms": 900}

        def outcome(tid):
            o = overrides.get(tid, passed)
            return (o.pop(0) if len(o) > 1 else o[0]) if isinstance(o, list) else o
        return {tid: {"id": tid, **outcome(tid)} for tid in to_replay}

    async def fake_agent(prompt, options, label="", redact=None, recorder=None, on_report=None):
        ids = [tc.id for tc in parse_test_plan(prompt).cases] or ["TC-001", "TC-002"]
        ids = [i for i in ("TC-001", "TC-002") if f"### {i}:" in prompt]
        state["agent_plans"].append(ids)
        if recorder is not None:
            _feed(recorder, LOGIN_SESSION)
        run = orchestrator.AgentRun(text="", ok=True, cost_usd=0.1)
        for test_id, report in state["reports"].items():
            entry = on_report({"test_id": test_id, **report}, label)
            if entry:
                run.reported[entry["id"]] = entry
            state["seen_on_disk"].append(json.loads((tmp_path / "run" / "results.json").read_text()))
        if state["stop"]:
            run.ok, run.text, run.stop = False, f"Agent error: {state['stop']}", "reached its cost limit"
            return run
        results = [{"id": i, "name": i, "status": state["ai_statuses"].get(i, "passed"), **state["ai_results"].get(i, {})}
                   for i in ids if i not in state["reports"]]
        run.text = json.dumps({"results": results})
        return run

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


async def test_failed_scripts_get_one_retry_and_passing_then_is_flaky(fakes, tmp_path):
    failure = {"status": "failed", "failed_step": 1, "error": "Timeout", "steps": [], "checks": []}
    fakes["replay_results"]["TC-001"] = [failure, {"status": "passed", "steps": [], "checks": []}]
    result = await orchestrator.execute_plan(
        PLAN, URL, tmp_path / "run", project=PROJECT, mode="auto", scripts=_scripts(tmp_path, "TC-001", "TC-002"))
    tc1 = next(r for r in result.results["results"] if r["id"] == "TC-001")
    assert fakes["replayed"] == [["TC-001", "TC-002"], ["TC-001"]]  # only the failure is retried
    assert fakes["agent_plans"] == []                               # and the AI isn't needed
    assert tc1["status"] == "passed" and tc1["flaky"] is True and "cause" not in tc1
    assert "The first try failed at step 1: Timeout" in tc1["notes"]
    assert result.results["summary"]["flaky"] == 1


async def test_script_only_failures_have_unknown_cause_and_evidence(fakes, tmp_path):
    fakes["replay_results"]["TC-001"] = {
        "status": "failed", "error": "Text not visible", "steps": [], "checks": [],
        "console_errors": ["Uncaught TypeError: x is undefined"], "failed_requests": ["POST http://x/api: 500"]}
    result = await orchestrator.execute_plan(
        PLAN, URL, tmp_path / "run", project=PROJECT, mode="script", scripts=_scripts(tmp_path, "TC-001"))
    by_id = {r["id"]: r for r in result.results["results"]}
    assert by_id["TC-001"]["cause"] == "unknown"
    assert by_id["TC-001"]["evidence"] == {"console": ["Uncaught TypeError: x is undefined"],
                                           "network": ["POST http://x/api: 500"]}
    assert by_id["TC-002"]["cause"] == "outdated"                   # no script recorded yet
    assert result.results["summary"]["causes"] == {"outdated": 1, "unknown": 1}
    assert 'type="unknown"' in (tmp_path / "run" / "junit.xml").read_text()


async def test_auto_mode_takes_the_cause_from_the_ai(fakes, tmp_path):
    fakes["replay_results"]["TC-001"] = {"status": "failed", "error": "Text not visible", "steps": [], "checks": [],
                                         "failed_requests": ["POST http://x/api/login: 500"]}
    fakes["ai_statuses"]["TC-001"] = "failed"
    fakes["ai_results"]["TC-001"] = {"cause": "Bug"}
    result = await orchestrator.execute_plan(
        PLAN, URL, tmp_path / "run", project=PROJECT, mode="auto", scripts=_scripts(tmp_path, "TC-001", "TC-002"))
    tc1 = next(r for r in result.results["results"] if r["id"] == "TC-001")
    assert tc1["cause"] == "bug"
    assert tc1["evidence"] == {"network": ["POST http://x/api/login: 500"]}  # kept from the script
    assert "1 app bug" in result.report_file.read_text()


async def test_ai_mode_records_verified_scripts(fakes, tmp_path):
    result = await orchestrator.execute_plan(PLAN, URL, tmp_path / "run", project=PROJECT, mode="ai", record=True)
    assert fakes["agent_plans"] == [["TC-001", "TC-002"]]
    assert set(result.recorded) == {"TC-001"}                      # TC-002 has a judgement-only criterion
    assert fakes["replayed"] == [["TC-001"]]                       # verified by replaying before keeping it
    tc2 = next(r for r in result.results["results"] if r["id"] == "TC-002")
    assert tc2["script"].startswith("not recorded") and "judgement" in tc2["script"]
    assert "Secret-Pass-1" not in result.recorded["TC-001"]


async def test_unverified_scripts_are_not_kept(fakes, tmp_path):
    fakes["verify_results"]["TC-001"] = {"status": "failed", "error": "flaky", "steps": [], "checks": []}
    result = await orchestrator.execute_plan(PLAN, URL, tmp_path / "run", project=PROJECT, mode="ai", record=True)
    assert result.recorded == {}
    tc1 = next(r for r in result.results["results"] if r["id"] == "TC-001")
    assert "didn't replay" in tc1["script"]


async def test_auto_mode_uses_ai_when_the_script_runner_is_unavailable(fakes, tmp_path, monkeypatch):
    def no_runner(*_):
        raise scripts.RunnerError("The script runner couldn't be installed: npm is offline")

    monkeypatch.setattr(orchestrator, "ensure_runner", no_runner)
    result = await orchestrator.execute_plan(
        PLAN, URL, tmp_path / "run", project=PROJECT, mode="auto", scripts=_scripts(tmp_path, "TC-001", "TC-002"),
        record=True)
    by_id = {r["id"]: r for r in result.results["results"]}
    assert fakes["agent_plans"] == [["TC-001", "TC-002"]]
    assert {r["status"] for r in by_id.values()} == {"passed"} and not by_id["TC-001"].get("healed")
    # Recording needs the runner too: the results are kept, the script isn't
    assert result.recorded == {}
    assert by_id["TC-001"]["script"].startswith("not recorded") and "npm is offline" in by_id["TC-001"]["script"]
    assert json.loads((tmp_path / "run" / "results.json").read_text())["summary"]["passed"] == 2

    # Script mode has nothing to fall back on
    with pytest.raises(scripts.RunnerError, match="npm is offline"):
        await orchestrator.execute_plan(
            PLAN, URL, tmp_path / "run2", project=PROJECT, mode="script", scripts=_scripts(tmp_path, "TC-001"))


async def test_first_results_stand_when_the_retry_does_not_run(fakes, tmp_path, monkeypatch):
    tries = []

    async def replay(to_replay, *_, **__):
        tries.append(sorted(to_replay))
        if len(tries) > 1:
            raise scripts.RunnerError("The script runner failed: crashed")
        return {tid: {"id": tid, "status": "failed", "failed_step": 1, "error": "Timeout", "steps": [], "checks": []}
                for tid in to_replay}

    monkeypatch.setattr(orchestrator, "replay", replay)
    result = await orchestrator.execute_plan(
        PLAN, URL, tmp_path / "run", project=PROJECT, mode="script", scripts=_scripts(tmp_path, "TC-001"))
    tc1 = next(r for r in result.results["results"] if r["id"] == "TC-001")
    assert tries == [["TC-001"], ["TC-001"]]
    assert tc1["status"] == "failed" and "Timeout" in tc1["notes"]


async def test_secrets_in_script_evidence_are_removed_before_it_is_shortened(fakes, tmp_path):
    project = Project.from_dict({
        "name": "App", "url": URL, "credentials": {"member": {"username": "tomsmith", "password": "p@ss/word+1"}},
    })
    fakes["replay_results"]["TC-001"] = {
        "status": "failed", "error": 'Field "Password" doesn\'t have the value "p@ss/word+1"', "steps": [], "checks": [],
        "failed_requests": [
            "GET https://app.test/api?pw=p%40ss%2Fword%2B1: 401",               # as a URL carries it
            "GET https://app.test/" + "a" * 275 + "p@ss/word+1: 500",           # across the 300-character cut
        ]}
    await orchestrator.execute_plan(
        PLAN, URL, tmp_path / "run", project=project, mode="script", scripts=_scripts(tmp_path, "TC-001"))
    saved = (tmp_path / "run" / "results.json").read_text()
    assert "p@ss" not in saved and "p%40ss" not in saved
    network = json.loads(saved)["results"][0]["evidence"]["network"]
    assert network[0] == "GET https://app.test/api?pw=[redacted]: 401"
    assert len(network[1]) == 300


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
    page.write_text("<script>console.error('boom')</script><img src=missing.png alt=''><h1>Welcome</h1><label>Name <input id=n></label><button onclick=\"document.body.insertAdjacentHTML('beforeend','<p>Hi '+n.value+'</p>')\">Greet</button>")
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
    assert results["TC-002"]["console_errors"] == ["boom"]
    assert any("missing.png: 404" in r for r in results["TC-002"]["failed_requests"])
    assert (tmp_path / "shots" / "TC-001_step2.png").is_file()
    httpd.shutdown()


# ── results handed in test by test ───────────────────────────


async def test_results_handed_in_survive_an_agent_that_stops_early(fakes, tmp_path):
    fakes["reports"]["TC-001"] = {"status": "failed", "cause": "bug", "notes": "Login does nothing",
                                  "bug": {"title": "Login button is dead", "severity": "high"}}
    fakes["stop"] = "error_max_budget_usd"
    result = await orchestrator.execute_plan(PLAN, URL, tmp_path / "run", project=PROJECT)
    by_id = {r["id"]: r for r in result.results["results"]}
    # While the agent was still working, the file already held the finished test
    assert fakes["seen_on_disk"][0]["partial"] is True
    assert [r["id"] for r in fakes["seen_on_disk"][0]["results"]] == ["TC-001"]
    assert (by_id["TC-001"]["status"], by_id["TC-001"]["cause"], by_id["TC-001"]["mode"]) == ("failed", "bug", "ai")
    assert (by_id["TC-002"]["status"], by_id["TC-002"]["cause"]) == ("blocked", "not_reached")
    assert "the agent reached its cost limit after TC-001" in by_id["TC-002"]["notes"]
    assert result.results["bugs"] == [{"title": "Login button is dead", "severity": "high", "test_case": "TC-001"}]
    assert result.failed_ids == ["TC-001", "TC-002"]
    saved = json.loads((tmp_path / "run" / "results.json").read_text())
    assert "partial" not in saved and saved["summary"]["causes"] == {"bug": 1, "not_reached": 1}


async def test_a_healed_test_is_marked_healed_as_soon_as_it_is_handed_in(fakes, tmp_path):
    fakes["replay_results"]["TC-001"] = {"status": "failed", "failed_step": 2, "error": "No button named Login",
                                         "steps": [], "checks": []}
    fakes["reports"]["TC-001"] = {"status": "passed"}
    await orchestrator.execute_plan(
        PLAN, URL, tmp_path / "run", project=PROJECT, mode="auto", scripts=_scripts(tmp_path, "TC-001", "TC-002"))
    live = {r["id"]: r for r in fakes["seen_on_disk"][0]["results"]}
    assert live["TC-002"]["mode"] == "script"                      # replayed before the AI started
    assert live["TC-001"]["healed"] is True and "No button named Login" in live["TC-001"]["notes"]


async def test_a_proposed_update_gets_the_tests_placeholders_back(fakes, tmp_path):
    plan = parse_test_plan("""### TC-001: Member logs in

**Steps:**
1. Enter {{member.username}} and {{member.password}}
2. Click Login

**Acceptance criteria:**
- The Secure Area is shown
""")
    fakes["reports"]["TC-001"] = {"status": "failed", "cause": "outdated", "update": {
        "steps": ["Enter tomsmith and Secret-Pass-1", "Click Sign in"],
        "expected": ["The Secure Area is shown"]}}
    result = await orchestrator.execute_plan(plan, URL, tmp_path / "run", project=PROJECT)
    tc1 = result.results["results"][0]
    # Only what changed is kept, and the project's values are placeholders again
    assert tc1["update"] == {"steps": ["Enter {{member.username}} and {{member.password}}", "Click Sign in"]}
    assert "Secret-Pass-1" not in json.dumps(result.results)


def test_a_run_that_stopped_part_way_keeps_its_finished_tests(tmp_path):
    done = {"TC-001": {"id": "TC-001", "name": "Member logs in", "status": "passed", "mode": "ai"}}
    (tmp_path / "results.json").write_text(json.dumps(partial_results(PLAN, done)))
    closed = orchestrator.close_run_results(tmp_path, PLAN, URL, "The run was cancelled before this test finished.")
    assert [(r["id"], r["status"], r.get("cause")) for r in closed["results"]] == [
        ("TC-001", "passed", None), ("TC-002", "blocked", "not_reached")]
    assert json.loads((tmp_path / "results.json").read_text()) == closed
    assert "Not reached" in (tmp_path / "report.md").read_text()
    assert (tmp_path / "junit.xml").is_file()
    # A run whose results are complete is left alone
    assert orchestrator.close_run_results(tmp_path, PLAN, URL, "again") is None
    assert orchestrator.close_run_results(tmp_path / "missing", PLAN, URL, "nothing saved") is None


def test_testers_always_get_the_report_tool():
    plain = orchestrator._browser_options()
    assert "argus" not in plain.mcp_servers and plain.allowed_tools == ["mcp__playwright__*"]
    tester = orchestrator._browser_options(tester=True)
    assert "argus" in tester.mcp_servers and "mcp__argus__*" in tester.allowed_tools
