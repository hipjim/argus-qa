import json

import pytest

from argus_qa.agents import orchestrator
from argus_qa.plan_parser import parse_test_plan
from argus_qa.project import (
    MASK,
    REDACTED,
    Project,
    Redactor,
    find_project_file,
    load_project_file,
    substitute,
)

TOML = """
name = "Looma Staging"
url = "https://staging.looma.test"
instructions = "Dismiss the cookie banner first."

[credentials.admin]
username = "qa-admin@looma.test"
password = "${LOOMA_ADMIN_PW}"
notes = "Full access"

[credentials.shopper]
username = "shopper@looma.test"
password = "shopper-pass-123"

[variables]
search_term = "running shoes"

[secrets]
test_card = "4242424242424242"
"""


@pytest.fixture
def project_file(tmp_path):
    path = tmp_path / "argus.toml"
    path.write_text(TOML)
    return path


@pytest.fixture
def project(project_file):
    return load_project_file(project_file).resolve_env({"LOOMA_ADMIN_PW": "admin-secret-9"})


def test_load_toml(project_file):
    p = load_project_file(project_file)
    assert p.name == "Looma Staging"
    assert p.slug == "looma-staging"
    assert p.url == "https://staging.looma.test"
    assert p.credentials["admin"].username == "qa-admin@looma.test"
    assert p.credentials["admin"].password == "${LOOMA_ADMIN_PW}"
    assert p.variables == {"search_term": "running shoes"}
    assert find_project_file(project_file.parent) == project_file


def test_resolve_env(project):
    assert project.credentials["admin"].password == "admin-secret-9"


def test_resolve_env_reports_missing_vars(project_file):
    with pytest.raises(ValueError, match="LOOMA_ADMIN_PW"):
        load_project_file(project_file).resolve_env({})


@pytest.mark.parametrize("data, message", [
    ({}, "needs a `name`"),
    ({"name": "x", "colour": "red"}, "Unknown project fields"),
    ({"name": "x", "slug": "Bad Slug"}, "Invalid project slug"),
    ({"name": "x", "credentials": {"admin": {"user": "a"}}}, "Unknown fields in credential"),
    ({"name": "x", "variables": {"a": "1"}, "secrets": {"a": "2"}}, "both variable and secret"),
    ({"name": "x", "variables": {"bad key": "1"}}, "Invalid variable name"),
])
def test_validation(data, message):
    with pytest.raises(ValueError, match=message):
        Project.from_dict(data)


def test_masking_and_keep_masked_secrets(project):
    masked = project.to_dict(mask_secrets=True)
    assert masked["credentials"]["admin"]["password"] == MASK
    assert masked["secrets"]["test_card"] == MASK
    assert masked["credentials"]["admin"]["username"] == "qa-admin@looma.test"

    # Client edits the masked copy and sends it back with a changed shopper password
    masked["credentials"]["shopper"]["password"] = "new-shopper-pass"
    updated = Project.from_dict(masked).keep_masked_secrets(project)
    assert updated.credentials["admin"].password == "admin-secret-9"
    assert updated.credentials["shopper"].password == "new-shopper-pass"
    assert updated.secrets["test_card"] == "4242424242424242"


def test_substitute(project):
    text = "Log in as {{admin.username}} / {{ admin.password }}, search {{search_term}}, pay {{test_card}}"
    assert substitute(text, project) == (
        "Log in as qa-admin@looma.test / admin-secret-9, search running shoes, pay 4242424242424242"
    )


def test_substitute_errors(project):
    with pytest.raises(ValueError, match=r"no value for: \{\{admin.email\}\}, \{\{nope\}\}"):
        substitute("{{nope}} {{admin.email}}", project)
    with pytest.raises(ValueError, match="no project"):
        substitute("{{admin.password}}", None)
    assert substitute("no placeholders", None) == "no placeholders"


def test_prompt_section(project):
    section = project.prompt_section()
    assert "## Project: Looma Staging" in section
    assert "**admin**: username `qa-admin@looma.test`, password `admin-secret-9` (Full access)" in section
    assert "**search_term**: `running shoes`" in section
    assert "Dismiss the cookie banner first." in section


def test_redactor(project):
    redact = Redactor(project.secret_values() + ["abc"])
    assert redact("pw admin-secret-9 card 4242424242424242") == f"pw {REDACTED} card {REDACTED}"
    assert redact("abc stays: too short to redact safely") == "abc stays: too short to redact safely"
    data = {"steps": [{"actual": "typed shopper-pass-123"}], "n": 3}
    assert redact.data(data) == {"steps": [{"actual": f"typed {REDACTED}"}], "n": 3}


async def test_execute_plan_uses_project_and_never_saves_secrets(project, tmp_path, monkeypatch):
    """Tester gets real credentials; results, report, and the reporter's prompt never contain them."""
    prompts = []

    async def fake_agent(prompt, options, label="", redact=None):
        prompts.append((label, prompt))
        if label == "reporter":
            text = "# Report\nAdmin logged in with admin-secret-9."
        else:
            text = json.dumps({"results": [{
                "id": "TC-001", "status": "passed",
                "steps": [{"step": "log in", "actual": "typed admin-secret-9 and 4242424242424242"}],
            }]})
        return orchestrator.AgentRun(text=redact(text) if redact else text, ok=True)

    monkeypatch.setattr(orchestrator, "_run_agent", fake_agent)
    # The browser saves a page snapshot that shows the password on the page
    (tmp_path / "run" / "screenshots").mkdir(parents=True)
    (tmp_path / "run" / "screenshots" / "page-1.yml").write_text("- text: Use password admin-secret-9\n")
    plan = parse_test_plan(
        "### TC-001: Admin login\n\n1. Log in as admin with {{admin.password}}\n2. Search {{search_term}}\n"
    )
    result = await orchestrator.execute_plan(plan, project.url, tmp_path / "run", project=project, ai_report=True)

    tester_prompt = next(p for label, p in prompts if label != "reporter")
    assert "Log in as admin with admin-secret-9" in tester_prompt
    assert "Search running shoes" in tester_prompt
    assert "## Project: Looma Staging" in tester_prompt

    reporter_prompt = next(p for label, p in prompts if label == "reporter")
    assert "[redacted]" in (tmp_path / "run" / "screenshots" / "page-1.yml").read_text()
    for secret in ("admin-secret-9", "4242424242424242"):
        assert secret not in reporter_prompt
        for f in (tmp_path / "run").rglob("*"):
            if f.is_file():
                assert secret not in f.read_text(), f
    assert result.results["summary"]["passed"] == 1


async def test_execute_plan_rejects_unknown_placeholders_before_running(project, tmp_path, monkeypatch):
    async def fail(*args, **kwargs):
        raise AssertionError("agent should not run")

    monkeypatch.setattr(orchestrator, "_run_agent", fail)
    plan = parse_test_plan("### TC-001: X\n\n1. Use {{missing_value}}\n")
    with pytest.raises(ValueError, match="missing_value"):
        await orchestrator.execute_plan(plan, project.url, tmp_path / "run", project=project)


# ── discover / explore sessions (orchestrator level) ────────


EXPLORATION = {
    "title": "Looma Retailers", "summary": "Merchant platform",
    "pages": [{"url": "/promos", "description": "Promotions list"}],
    "user_flows": [{"name": "Create promo", "steps": ["Open", "Fill", "Save"]}],
}
SCAFFOLD = "# Test Plan: X\n\n### TC-001: Promotions list loads\n\n**Priority:** high\n\n1. Log in as admin\n"


async def test_discover_app(project, tmp_path, monkeypatch):
    seen = []

    async def fake_agent(prompt, options, label="", redact=None):
        seen.append((label, prompt, options))
        if label == "scaffold":
            return orchestrator.AgentRun(text=SCAFFOLD, ok=True, cost_usd=0.1)
        return orchestrator.AgentRun(text=json.dumps(EXPLORATION), ok=True, cost_usd=0.9)

    monkeypatch.setattr(orchestrator, "_run_agent", fake_agent)
    result = await orchestrator.discover_app(
        project.url, tmp_path / "d", project=project, focus="promotions",
        existing_tests=["Old test"], max_cost_usd=2.0,
    )
    assert [c.name for c in result.proposed] == ["Promotions list loads"]
    assert result.summary == {"proposed": 1, "pages": 1, "flows": 1}
    assert result.cost_usd == pytest.approx(1.0)
    assert "Promotions list loads" in (tmp_path / "d" / "proposed.md").read_text()
    assert "Looma Retailers" in (tmp_path / "d" / "report.md").read_text()

    (explorer_label, explorer_prompt, explorer_opts), (_, scaffold_prompt, scaffold_opts) = seen
    assert "promotions" in explorer_prompt
    assert "Stay on staging.looma.test" in explorer_prompt       # safety rules
    assert "admin-secret-9" in explorer_prompt                    # explorer may log in
    assert explorer_opts.max_budget_usd == pytest.approx(1.6)
    assert explorer_opts.max_turns == orchestrator.EXPLORER_MAX_TURNS
    assert scaffold_opts.max_budget_usd == pytest.approx(0.4)
    assert "Old test" in scaffold_prompt and "Do NOT include a Credentials section" in scaffold_prompt


async def test_discover_app_reports_browser_failure(project, tmp_path, monkeypatch):
    async def broken(prompt, options, label="", redact=None):
        return orchestrator.AgentRun(text=json.dumps({"environment_error": "no browser"}), ok=True)

    monkeypatch.setattr(orchestrator, "_run_agent", broken)
    result = await orchestrator.discover_app(project.url, tmp_path / "d", project=project)
    assert result.environment_error == "no browser"
    assert result.proposed == []


async def test_explore_app_turns_bugs_into_tests(project, tmp_path, monkeypatch):
    report = {
        "summary": "Poked at promos",
        "areas_covered": ["Promotions"],
        "bugs": [{"title": "Save does nothing", "severity": "high", "steps_to_reproduce": ["Open /promos", "Click Save"],
                  "expected": "Saved", "actual": "Nothing (typed admin-secret-9)"}],
        "observations": ["Wording is unclear"],
    }
    prompts = []

    async def fake_agent(prompt, options, label="", redact=None):
        prompts.append((prompt, options))
        text = json.dumps(report)
        return orchestrator.AgentRun(text=redact(text) if redact else text, ok=True, cost_usd=0.4)

    monkeypatch.setattr(orchestrator, "_run_agent", fake_agent)
    result = await orchestrator.explore_app(
        project.url, tmp_path / "e", charter="Break promos", project=project, max_cost_usd=1.0
    )
    assert result.summary == {"bugs": 1, "proposed": 1}
    assert result.proposed[0].name == "Save does nothing" and result.proposed[0].priority == "high"
    assert "Break promos" in prompts[0][0] and prompts[0][1].max_budget_usd == 1.0
    for f in (tmp_path / "e").rglob("*.*"):
        assert "admin-secret-9" not in f.read_text(), f
    assert "Save does nothing" in (tmp_path / "e" / "report.md").read_text()


async def test_explore_app_without_report_is_an_error(project, tmp_path, monkeypatch):
    async def stopped(prompt, options, label="", redact=None):
        return orchestrator.AgentRun(text="Agent error: error_max_budget_usd", ok=False)

    monkeypatch.setattr(orchestrator, "_run_agent", stopped)
    with pytest.raises(RuntimeError, match="no report"):
        await orchestrator.explore_app(project.url, tmp_path / "e", charter="x", project=project)


def test_budget_split_for_test_runs():
    opts = orchestrator._browser_options(max_budget_usd=orchestrator._share(5.0, 0.9 / 3))
    assert opts.max_budget_usd == pytest.approx(1.5)
    assert orchestrator._browser_options().max_budget_usd is None
    assert orchestrator._reasoning_options(max_budget_usd=0.5).max_turns == orchestrator.REPORTER_MAX_TURNS



# ── cost controls: models, screenshots, report ──────────────


async def test_default_report_needs_no_model_call(tmp_path, monkeypatch):
    calls = []

    async def fake_agent(prompt, options, label="", redact=None):
        calls.append((label, options))
        text = json.dumps({"results": [
            {"id": "TC-001", "name": "Login", "status": "passed"},
            {"id": "TC-002", "name": "Save", "status": "failed",
             "steps": [{"step": "Click Save", "expected": "Saved", "actual": "Error 500", "status": "failed",
                        "screenshot": "TC-002_step1_fail.png"}]},
        ]})
        return orchestrator.AgentRun(text=text, ok=True, cost_usd=0.2, models=["claude-sonnet-5"])

    monkeypatch.setattr(orchestrator, "_run_agent", fake_agent)
    plan = parse_test_plan("# Test Plan: Smoke\n\n### TC-001: Login\n\nx\n\n### TC-002: Save\n\ny\n")
    result = await orchestrator.execute_plan(plan, "https://app.test", tmp_path / "run", max_cost_usd=2.0)

    assert [label for label, _ in calls if label == "reporter"] == []
    assert calls[0][1].max_budget_usd == pytest.approx(2.0)  # no reporter share held back
    report = (tmp_path / "run" / "report.md").read_text()
    assert report.startswith("# Test report: Smoke")
    assert report.index("TC-002") < report.index("TC-001")  # failures first
    assert "Error 500" in report and "screenshots/TC-002_step1_fail.png" in report
    assert "--only TC-002" in report
    assert result.models == ["claude-sonnet-5"]
    assert result.cost_usd == pytest.approx(0.2)


def test_tester_uses_sonnet_and_gets_no_images(monkeypatch):
    for var in ("ARGUS_MODEL", "ARGUS_MODEL_TESTER", "ARGUS_MODEL_EXPLORER", "ARGUS_MODEL_WRITER"):
        monkeypatch.delenv(var, raising=False)
    assert orchestrator.model_for("tester") == "claude-sonnet-5"
    assert orchestrator.model_for("writer") == "claude-sonnet-5"
    assert orchestrator.model_for("explorer") == "claude-opus-5-5"

    no_images = orchestrator._browser_options(images=False, model="claude-sonnet-5")
    args = no_images.mcp_servers["playwright"]["args"]
    assert args[args.index("--image-responses") + 1] == "omit"
    assert no_images.model == "claude-sonnet-5"
    assert "--image-responses" not in orchestrator._browser_options().mcp_servers["playwright"]["args"]


def test_model_overrides(monkeypatch):
    monkeypatch.setenv("ARGUS_MODEL", "claude-opus-5-5")
    monkeypatch.setenv("ARGUS_MODEL_WRITER", "claude-haiku-4-5-20251001")
    assert orchestrator.model_for("tester") == "claude-opus-5-5"
    assert orchestrator.model_for("writer") == "claude-haiku-4-5-20251001"


async def test_session_agents_get_role_models(project, tmp_path, monkeypatch):
    for var in ("ARGUS_MODEL", "ARGUS_MODEL_TESTER", "ARGUS_MODEL_EXPLORER", "ARGUS_MODEL_WRITER"):
        monkeypatch.delenv(var, raising=False)
    seen = {}

    async def fake_agent(prompt, options, label="", redact=None):
        seen[label] = options
        if label == "scaffold":
            return orchestrator.AgentRun(text=SCAFFOLD, ok=True, models=["claude-sonnet-5"])
        return orchestrator.AgentRun(text=json.dumps({**EXPLORATION, "bugs": []}), ok=True, models=["claude-opus-5-5"])

    monkeypatch.setattr(orchestrator, "_run_agent", fake_agent)
    result = await orchestrator.discover_app(project.url, tmp_path / "d", project=project)
    explorer = next(o for label, o in seen.items() if label != "scaffold")
    assert explorer.model == "claude-opus-5-5"
    assert "omit" in explorer.mcp_servers["playwright"]["args"]      # discovery maps pages as text
    assert seen["scaffold"].model == "claude-sonnet-5"
    assert result.models == ["claude-opus-5-5", "claude-sonnet-5"]

    seen.clear()
    await orchestrator.explore_app(project.url, tmp_path / "e", charter="x", project=project)
    hunter = next(iter(seen.values()))
    assert hunter.model == "claude-opus-5-5"
    assert "--image-responses" not in hunter.mcp_servers["playwright"]["args"]  # sees pages to spot visual bugs
