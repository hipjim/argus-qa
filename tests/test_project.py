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
    result = await orchestrator.execute_plan(plan, project.url, tmp_path / "run", project=project)

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
