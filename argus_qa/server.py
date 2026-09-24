"""HTTP API: post a test plan or scenario, argus-qa runs it in a headless browser.

Runs are queued and executed in the background, up to max_concurrent at a time.
Each run's results, report, JUnit XML, and screenshots are stored under
<data_dir>/runs/<run_id>/ and survive restarts. There are three kinds of run:

- test: execute a plan, a scenario, or a saved suite
- discover: explore a project's app and propose test cases
- explore: hunt for bugs without a script, and turn them into regression tests

Proposed tests from discover/explore runs are reviewed and accepted into a
project's saved suites (<data_dir>/suites/<project>/<suite>.json).

Projects (target URL, test accounts, test data, secrets, standing instructions)
are stored under <data_dir>/projects/<slug>.json and referenced by runs. Secrets
are masked in API responses and redacted from results and reports.

Every run has a cost limit (estimated at API prices); agents stop when they
reach it. Defaults come from ARGUS_MAX_RUN_COST, ARGUS_MAX_DISCOVER_COST and
ARGUS_MAX_EXPLORE_COST.

Set ARGUS_API_KEY to require `Authorization: Bearer <key>` on every request.
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import shutil
import traceback
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import httpx
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request, Response, status
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, HttpUrl, model_validator

from argus_qa import colors as c
from argus_qa.agents.orchestrator import discover_app, draft_test_case, execute_plan, explore_app
from argus_qa.events import EventLog, capture
from argus_qa.plan_parser import (
    TestPlan,
    append_cases,
    compose_plan,
    parse_test_plan,
    plan_fields,
    plan_from_scenario,
    render_plan,
)
from argus_qa.project import MASK, Project, slugify, substitute

WEB_DIR = Path(__file__).parent / "web"

RunKind = Literal["test", "discover", "explore"]
RunStatus = Literal["queued", "running", "passed", "failed", "completed", "error", "cancelled"]
FINISHED: set[str] = {"passed", "failed", "completed", "error", "cancelled"}

DEFAULT_COSTS = {"test": 5.0, "discover": 3.0, "explore": 2.0}
_COST_ENV = {
    "test": "ARGUS_MAX_RUN_COST",
    "discover": "ARGUS_MAX_DISCOVER_COST",
    "explore": "ARGUS_MAX_EXPLORE_COST",
}


def default_costs() -> dict[str, float]:
    costs = {}
    for kind, default in DEFAULT_COSTS.items():
        try:
            costs[kind] = float(os.environ.get(_COST_ENV[kind], default))
        except ValueError:
            costs[kind] = default
    return costs


# ── Request / response models ───────────────────────────────────────


class CredentialModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: str = ""
    password: str = Field("", description=f"Returned as {MASK!r}; send {MASK!r} back to keep the stored one.")
    notes: str = ""


class ProjectModel(BaseModel):
    """Reusable configuration for testing one app. Values may use ${ENV_VAR} references."""

    model_config = ConfigDict(extra="forbid")

    name: str
    slug: str | None = Field(None, description="URL-safe ID; derived from the name if omitted.")
    url: str | None = Field(None, description="Default target URL for this project's runs.")
    description: str = ""
    instructions: str = Field("", description="Standing instructions the tester follows in every test.")
    credentials: dict[str, CredentialModel] = Field({}, description="Test accounts, keyed by role.")
    variables: dict[str, str] = Field({}, description="Test data, usable as {{name}} in plans.")
    secrets: dict[str, str] = Field({}, description="Secret test data; masked in responses and output.")


CostLimit = Field(None, gt=0, le=100, description="Stop once the estimated cost reaches this (USD).")


class RunRequest(BaseModel):
    """Submit either a full Markdown test plan or a single plain-English scenario."""

    project: str | None = Field(None, description="Project slug supplying URL, accounts, and test data.")
    plan: str | None = Field(None, description="Full Markdown test plan (### TC-NNN: sections).")
    scenario: str | None = Field(None, description="A quick test: plain-English steps and expected result.")
    name: str | None = Field(None, description="Name for the scenario (used with `scenario`).")
    url: HttpUrl | None = Field(None, description="Target URL. Overrides the project's and plan's URL.")
    only: list[str] | None = Field(None, description="Run only these test case IDs.")
    skip: list[str] | None = Field(None, description="Skip these test case IDs.")
    parallel: int = Field(1, ge=1, le=8, description="Parallel browser agents for this run.")
    max_cost_usd: float | None = CostLimit
    ai_report: bool = Field(False, description="Have an agent write the report (costs extra).")
    callback_url: HttpUrl | None = Field(None, description="POSTed the run record when the run finishes.")

    @model_validator(mode="after")
    def _one_source(self) -> RunRequest:
        if bool(self.plan) == bool(self.scenario):
            raise ValueError("Provide exactly one of `plan` or `scenario`.")
        return self


class SuiteModel(BaseModel):
    """A saved test suite: a Markdown plan of ### TC-NNN test cases."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    slug: str | None = Field(None, description="URL-safe ID; derived from the name if omitted.")
    plan: str = Field(description="Markdown test plan.")


class SuiteRunRequest(BaseModel):
    only: list[str] | None = None
    parallel: int = Field(1, ge=1, le=8)
    url: HttpUrl | None = None
    max_cost_usd: float | None = CostLimit
    ai_report: bool = False
    callback_url: HttpUrl | None = None


class DiscoverRequest(BaseModel):
    focus: str = Field("", description="What to concentrate on, e.g. 'promotions'. Empty = the whole app.")
    url: HttpUrl | None = Field(None, description="Where to start. Defaults to the project's URL.")
    max_cost_usd: float | None = CostLimit
    callback_url: HttpUrl | None = None


class ExploreRequest(BaseModel):
    charter: str = Field(min_length=3, description="What to explore and look for, in plain English.")
    url: HttpUrl | None = Field(None, description="Where to start. Defaults to the project's URL.")
    max_cost_usd: float | None = CostLimit
    callback_url: HttpUrl | None = None


class AcceptRequest(BaseModel):
    """Save proposed test cases from a discover/explore run into a suite."""

    case_ids: list[str] | None = Field(None, description="Proposed test IDs to keep. Omit to keep all.")
    suite: str | None = Field(None, description="Slug of an existing suite to append to.")
    suite_name: str | None = Field(None, description="Name of a new suite to create.")

    @model_validator(mode="after")
    def _one_target(self) -> AcceptRequest:
        if bool(self.suite) == bool(self.suite_name and self.suite_name.strip()):
            raise ValueError("Provide exactly one of `suite` or `suite_name`.")
        return self


class DeleteRunsRequest(BaseModel):
    ids: list[str] = Field(min_length=1, max_length=500, description="Run IDs to delete.")


class CaseFields(BaseModel):
    id: str | None = None
    title: str = ""
    priority: str = ""
    category: str = ""
    preconditions: list[str] = []
    steps: list[str] = []
    expected: list[str] = Field([], description="Acceptance criteria.")
    notes: str = ""


class PlanFields(BaseModel):
    title: str = ""
    url: str | None = None
    notes: str = Field("", description="Free-form text before the test cases (setup, credentials...).")
    cases: list[CaseFields] = []


class ParseRequest(BaseModel):
    plan: str


class DraftRequest(BaseModel):
    description: str = Field(min_length=3, max_length=2000)
    project: str | None = None


class RerunRequest(BaseModel):
    failed_only: bool = Field(True, description="Re-run only the tests that failed or were blocked.")


class Run(BaseModel):
    id: str
    kind: RunKind = "test"
    status: RunStatus
    title: str = ""
    project: str | None = None
    suite: str | None = None
    brief: str = Field("", description="Focus (discover) or charter (explore).")
    url: str
    test_ids: list[str] = []
    created_at: str
    started_at: str | None = None
    finished_at: str | None = None
    summary: dict | None = None
    failed_ids: list[str] = []
    accepted: list[str] = Field([], description="Proposed test IDs already saved to a suite.")
    cost_usd: float | None = None
    max_cost_usd: float | None = None
    models: list[str] = Field([], description="Models the agents used, e.g. claude-sonnet-5.")
    error: str | None = None
    callback_url: str | None = None
    parallel: int = 1


class Suite(BaseModel):
    slug: str
    name: str
    project: str
    plan: str
    created_at: str
    updated_at: str


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


# ── Storage ─────────────────────────────────────────────────────────


class ProjectStore:
    """Projects persisted as one JSON file each. Files are readable only by the server's user."""

    def __init__(self, data_dir: Path):
        self.dir = data_dir / "projects"
        self.dir.mkdir(parents=True, exist_ok=True)

    def _path(self, slug: str) -> Path:
        return self.dir / f"{slug}.json"

    def get(self, slug: str) -> Project:
        path = self._path(slug)
        if not path.is_file():
            raise HTTPException(404, f"Project {slug!r} not found.")
        return Project.from_dict(json.loads(path.read_text()))

    def exists(self, slug: str) -> bool:
        return self._path(slug).is_file()

    def list(self) -> list[Project]:
        return [Project.from_dict(json.loads(p.read_text())) for p in sorted(self.dir.glob("*.json"))]

    def save(self, project: Project) -> None:
        path = self._path(project.slug)
        path.touch(mode=0o600, exist_ok=True)
        path.write_text(json.dumps(project.to_dict(), indent=2))

    def delete(self, slug: str) -> None:
        self.get(slug)
        self._path(slug).unlink()


class SuiteStore:
    """Saved suites, one JSON file per suite under suites/<project>/."""

    def __init__(self, data_dir: Path):
        self.dir = data_dir / "suites"
        self.dir.mkdir(parents=True, exist_ok=True)

    def _path(self, project: str, slug: str) -> Path:
        return self.dir / project / f"{slug}.json"

    def get(self, project: str, slug: str) -> Suite:
        path = self._path(project, slug)
        if not path.is_file():
            raise HTTPException(404, f"Suite {slug!r} not found in project {project!r}.")
        return Suite.model_validate_json(path.read_text())

    def exists(self, project: str, slug: str) -> bool:
        return self._path(project, slug).is_file()

    def list(self, project: str) -> list[Suite]:
        return [Suite.model_validate_json(p.read_text()) for p in sorted((self.dir / project).glob("*.json"))]

    def save(self, suite: Suite) -> None:
        path = self._path(suite.project, suite.slug)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(suite.model_dump_json(indent=2))

    def delete(self, project: str, slug: str) -> None:
        self.get(project, slug)
        self._path(project, slug).unlink()

    def delete_project(self, project: str) -> None:
        shutil.rmtree(self.dir / project, ignore_errors=True)


def _project_from_model(model: ProjectModel) -> Project:
    try:
        return Project.from_dict(model.model_dump(exclude_none=True))
    except ValueError as e:
        raise HTTPException(422, str(e)) from e


def _check_suite_plan(plan_text: str, project: Project) -> TestPlan:
    plan = parse_test_plan(plan_text)
    if not plan.cases:
        raise HTTPException(422, "The suite has no '### TC-NNN: Title' test cases.")
    ids = [tc.id.upper() for tc in plan.cases]
    duplicates = sorted({i for i in ids if ids.count(i) > 1})
    if duplicates:
        raise HTTPException(422, f"Duplicate test case IDs: {', '.join(duplicates)}")
    try:
        substitute(plan_text, project)
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    return plan


def _case_summary(plan: TestPlan) -> list[dict]:
    return [
        {"id": tc.id, "name": tc.name, "priority": tc.priority, "category": tc.category} for tc in plan.cases
    ]


# ── Runs ────────────────────────────────────────────────────────────


class RunManager:
    """Queues runs, executes them with bounded concurrency, and persists their state."""

    def __init__(self, data_dir: Path, max_concurrent: int, projects: ProjectStore, suites: SuiteStore):
        self.projects = projects
        self.suites = suites
        self.costs = default_costs()
        self.runs_dir = data_dir / "runs"
        self.runs_dir.mkdir(parents=True, exist_ok=True)
        self.semaphore = asyncio.Semaphore(max_concurrent)
        self.runs: dict[str, Run] = {}
        self.tasks: dict[str, asyncio.Task] = {}
        self._load()

    def _load(self) -> None:
        for record in self.runs_dir.glob("*/run.json"):
            run = Run.model_validate_json(record.read_text())
            if run.status not in FINISHED:
                # The server stopped while this run was in flight
                run.status = "error"
                run.error = "Server restarted before the run finished."
                run.finished_at = _now()
                self.save(run)
            self.runs[run.id] = run

    def run_dir(self, run_id: str) -> Path:
        return self.runs_dir / run_id

    def save(self, run: Run) -> None:
        path = self.run_dir(run.id) / "run.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(run.model_dump_json(indent=2))

    def _project(self, slug: str | None) -> Project | None:
        if not slug:
            return None
        try:
            return self.projects.get(slug).resolve_env()
        except ValueError as e:
            raise HTTPException(422, str(e)) from e

    def _new_run(self, kind: RunKind, **fields) -> Run:
        run = Run(id=uuid.uuid4().hex[:12], kind=kind, status="queued", created_at=_now(), **fields)
        self.runs[run.id] = run
        self.save(run)
        return run

    def _start(self, run: Run, job: Callable[[], Awaitable[None]]) -> Run:
        task = asyncio.create_task(self._run_job(run, job))
        self.tasks[run.id] = task
        task.add_done_callback(lambda _: self.tasks.pop(run.id, None))
        return run

    async def _run_job(self, run: Run, job: Callable[[], Awaitable[None]]) -> None:
        try:
            async with self.semaphore:
                run.status = "running"
                run.started_at = _now()
                self.save(run)
                print(c.header(f"\n  [run {run.id}] {run.kind} started — {run.title} ({run.url})"))
                with capture(EventLog(self.run_dir(run.id) / "events.jsonl")):
                    await job()
        except asyncio.CancelledError:
            run.status = "cancelled"
        except HTTPException:
            raise
        except Exception as e:  # noqa: BLE001 — any failure must be recorded on the run
            run.status = "error"
            run.error = f"{type(e).__name__}: {e}"
            traceback.print_exc()
        finally:
            run.finished_at = _now()
            self.save(run)
            print(c.header(f"  [run {run.id}] {run.status}"))
            if run.callback_url:
                await _notify(run)

    # test runs

    def submit(self, req: RunRequest, *, suite: str | None = None, title: str | None = None) -> Run:
        project = self._project(req.project)
        url = str(req.url) if req.url else (project.url if project else None)
        plan = _build_plan(req, url)
        url = url or plan.url
        if not url:
            raise HTTPException(
                422, "No URL: pass `url`, set one on the project, or include a '> URL: ...' line in the plan."
            )
        try:
            substitute(plan.to_markdown(), project)
        except ValueError as e:
            raise HTTPException(422, str(e)) from e

        max_cost = req.max_cost_usd or self.costs["test"]
        run = self._new_run(
            "test",
            title=title or req.name or plan_title(plan),
            project=project.slug if project else None,
            suite=suite,
            url=url,
            test_ids=[tc.id for tc in plan.cases],
            max_cost_usd=max_cost,
            callback_url=str(req.callback_url) if req.callback_url else None,
            parallel=req.parallel,
        )
        (self.run_dir(run.id) / "plan.md").write_text(plan.to_markdown())

        async def job() -> None:
            result = await execute_plan(
                plan, run.url, self.run_dir(run.id), parallel=req.parallel, headless=True,
                isolated=True, project=project, max_cost_usd=max_cost, ai_report=req.ai_report,
            )
            run.summary = result.results["summary"]
            run.failed_ids = result.failed_ids
            run.cost_usd = round(result.cost_usd, 4)
            run.models = result.models
            if result.environment_error:
                run.status = "error"
                run.error = f"Browser/environment problem: {result.environment_error}"
            else:
                run.status = "failed" if result.failed_ids else "passed"
                stopped = [e for e in result.results.get("agent_errors", []) if "error_max" in e]
                if stopped:
                    run.error = (
                        f"{len(stopped)} agent(s) stopped early at the cost or turn limit "
                        f"(${max_cost:.2f}); their unfinished tests are marked blocked."
                    )

        return self._start(run, job)

    # discover / explore sessions

    def submit_session(self, kind: RunKind, project_slug: str, req: DiscoverRequest | ExploreRequest) -> Run:
        project = self._project(project_slug)
        url = str(req.url) if req.url else project.url
        if not url:
            raise HTTPException(422, "No URL: pass `url` or set one on the project.")
        brief = req.focus if isinstance(req, DiscoverRequest) else req.charter
        max_cost = req.max_cost_usd or self.costs[kind]
        title = ("Discover: " if kind == "discover" else "Explore: ") + (brief.strip() or project.name)
        run = self._new_run(
            kind,
            title=_shorten(title, 90),
            project=project.slug,
            brief=brief,
            url=url,
            max_cost_usd=max_cost,
            callback_url=str(req.callback_url) if req.callback_url else None,
        )
        existing = [tc.name for s in self.suites.list(project.slug) for tc in parse_test_plan(s.plan).cases]

        async def job() -> None:
            if kind == "discover":
                result = await discover_app(
                    url, self.run_dir(run.id), project=project, focus=brief, existing_tests=existing,
                    headless=True, isolated=True, max_cost_usd=max_cost,
                )
            else:
                result = await explore_app(
                    url, self.run_dir(run.id), charter=brief, project=project,
                    headless=True, isolated=True, max_cost_usd=max_cost,
                )
            run.summary = result.summary
            run.test_ids = [tc.id for tc in result.proposed]
            run.cost_usd = round(result.cost_usd, 4)
            run.models = result.models
            if result.environment_error:
                run.status = "error"
                run.error = f"Browser/environment problem: {result.environment_error}"
            else:
                run.status = "completed"

        return self._start(run, job)

    def delete(self, run_id: str) -> None:
        """Remove a finished run and everything it saved (results, report, screenshots, activity)."""
        run = self.runs.get(run_id)
        if run is None:
            raise HTTPException(404, f"Run {run_id} not found.")
        if run.status not in FINISHED:
            raise HTTPException(409, f"Run {run_id} is {run.status}; cancel it before deleting it.")
        shutil.rmtree(self.run_dir(run_id), ignore_errors=True)
        del self.runs[run_id]

    def cancel(self, run_id: str) -> bool:
        task = self.tasks.get(run_id)
        if task is None:
            return False
        task.cancel()
        return True


def _shorten(text: str, limit: int) -> str:
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[:limit].rsplit(" ", 1)[0].rstrip(",;:") + "…"


def plan_title(plan: TestPlan) -> str:
    """A display title: the plan's '# ' heading, else its only test, else a count."""
    for line in plan.preamble.splitlines():
        if line.startswith("# "):
            return line[2:].removeprefix("Test Plan:").strip()
    if len(plan.cases) == 1:
        return plan.cases[0].name
    return f"{len(plan.cases)} tests"


def _build_plan(req: RunRequest, url: str | None) -> TestPlan:
    if req.scenario:
        if not url:
            raise HTTPException(422, "No URL: pass `url` or use a project that has one.")
        plan = plan_from_scenario(req.scenario, url, name=req.name or "Quick test")
    else:
        plan = parse_test_plan(req.plan or "")
        if not plan.cases:
            raise HTTPException(422, "The plan contains no '### TC-NNN: Title' test cases.")
        unknown = plan.unknown_ids((req.only or []) + (req.skip or []))
        if unknown:
            raise HTTPException(422, f"Unknown test case IDs: {', '.join(unknown)}")
        plan = plan.filter(only=req.only, skip=req.skip)
        if not plan.cases:
            raise HTTPException(422, "No test cases left after applying only/skip.")
    return plan


async def _notify(run: Run) -> None:
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            await client.post(run.callback_url, json=run.model_dump())
    except httpx.HTTPError as e:
        print(c.warn(f"  [run {run.id}] callback to {run.callback_url} failed: {e}"))


# ── App ─────────────────────────────────────────────────────────────


def create_app(data_dir: str | Path = "argus-data", max_concurrent: int = 2) -> FastAPI:
    api_key = os.environ.get("ARGUS_API_KEY")

    async def require_key(request: Request) -> None:
        if not api_key:
            return
        header = request.headers.get("authorization", "")
        if not secrets.compare_digest(header, f"Bearer {api_key}"):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing or invalid API key.")

    app = FastAPI(
        title="argus-qa",
        description="AI QA tester: post a test plan or scenario, get results, a report, and screenshots.",
    )
    # Everything except the web UI's static files and /health needs the API key
    api = APIRouter(dependencies=[Depends(require_key)])
    projects = ProjectStore(Path(data_dir))
    suites = SuiteStore(Path(data_dir))
    manager = RunManager(Path(data_dir), max_concurrent, projects, suites)
    app.state.manager = manager

    def get_run(run_id: str) -> Run:
        run = manager.runs.get(run_id)
        if run is None:
            raise HTTPException(404, f"Run {run_id} not found.")
        return run

    def run_file(run_id: str, name: str) -> Path:
        get_run(run_id)
        path = manager.run_dir(run_id) / name
        if not path.is_file():
            raise HTTPException(404, f"{name} is not available (yet) for run {run_id}.")
        return path

    def suite_view(suite: Suite, with_plan: bool = False) -> dict:
        plan = parse_test_plan(suite.plan)
        runs = sorted(
            (r for r in manager.runs.values() if r.project == suite.project and r.suite == suite.slug),
            key=lambda r: r.created_at, reverse=True,
        )
        last = runs[0] if runs else None
        view = {
            **suite.model_dump(exclude={"plan"}),
            "test_count": len(plan.cases),
            "tests": _case_summary(plan),
            "last_run": {"id": last.id, "status": last.status, "summary": last.summary,
                         "created_at": last.created_at} if last else None,
        }
        if with_plan:
            view["plan"] = suite.plan
        return view

    @app.get("/health")
    async def health() -> dict:
        return {"status": "ok", "auth_required": bool(api_key), "default_max_cost_usd": manager.costs}

    @app.get("/", include_in_schema=False)
    async def web_ui() -> FileResponse:
        return FileResponse(WEB_DIR / "index.html")

    app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")

    # ── projects ─────────────────────────────────────────────

    @api.post("/projects", status_code=status.HTTP_201_CREATED)
    async def create_project(body: ProjectModel) -> dict:
        project = _project_from_model(body)
        if projects.exists(project.slug):
            raise HTTPException(409, f"Project {project.slug!r} already exists; use PUT to update it.")
        projects.save(project)
        return project.to_dict(mask_secrets=True)

    @api.get("/projects")
    async def list_projects() -> list[dict]:
        return [p.to_dict(mask_secrets=True) for p in projects.list()]

    @api.get("/projects/{slug}")
    async def read_project(slug: str) -> dict:
        return projects.get(slug).to_dict(mask_secrets=True)

    @api.put("/projects/{slug}")
    async def update_project(slug: str, body: ProjectModel) -> dict:
        if body.slug and body.slug != slug:
            raise HTTPException(422, "The slug in the body doesn't match the URL.")
        project = _project_from_model(body.model_copy(update={"slug": slug}))
        if projects.exists(slug):
            project = project.keep_masked_secrets(projects.get(slug))
        projects.save(project)
        return project.to_dict(mask_secrets=True)

    @api.delete("/projects/{slug}", status_code=status.HTTP_204_NO_CONTENT)
    async def delete_project(slug: str) -> Response:
        projects.delete(slug)
        suites.delete_project(slug)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @api.post("/projects/{slug}/discover", status_code=status.HTTP_202_ACCEPTED)
    async def discover(slug: str, body: DiscoverRequest | None = None) -> Run:
        """Explore the project's app and propose test cases to review."""
        return manager.submit_session("discover", slug, body or DiscoverRequest())

    @api.post("/projects/{slug}/explore", status_code=status.HTTP_202_ACCEPTED)
    async def explore(slug: str, body: ExploreRequest) -> Run:
        """Hunt for bugs without a script; bugs found become proposed regression tests."""
        return manager.submit_session("explore", slug, body)

    # ── suites ───────────────────────────────────────────────

    @api.get("/projects/{slug}/suites")
    async def list_suites(slug: str) -> list[dict]:
        projects.get(slug)
        return [suite_view(s) for s in suites.list(slug)]

    @api.post("/projects/{slug}/suites", status_code=status.HTTP_201_CREATED)
    async def create_suite(slug: str, body: SuiteModel) -> dict:
        project = projects.get(slug)
        suite_slug = body.slug or slugify(body.name)
        if suites.exists(slug, suite_slug):
            raise HTTPException(409, f"Suite {suite_slug!r} already exists; use PUT to update it.")
        _check_suite_plan(body.plan, project)
        suite = Suite(slug=suite_slug, name=body.name.strip(), project=slug, plan=body.plan,
                      created_at=_now(), updated_at=_now())
        suites.save(suite)
        return suite_view(suite, with_plan=True)

    @api.get("/projects/{slug}/suites/{suite_slug}")
    async def read_suite(slug: str, suite_slug: str) -> dict:
        return suite_view(suites.get(slug, suite_slug), with_plan=True)

    @api.put("/projects/{slug}/suites/{suite_slug}")
    async def update_suite(slug: str, suite_slug: str, body: SuiteModel) -> dict:
        project = projects.get(slug)
        existing = suites.get(slug, suite_slug)
        _check_suite_plan(body.plan, project)
        changes = {"name": body.name.strip(), "plan": body.plan, "updated_at": _now()}
        suite = existing.model_copy(update=changes)
        suites.save(suite)
        return suite_view(suite, with_plan=True)

    @api.delete("/projects/{slug}/suites/{suite_slug}", status_code=status.HTTP_204_NO_CONTENT)
    async def delete_suite(slug: str, suite_slug: str) -> Response:
        suites.delete(slug, suite_slug)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @api.post("/projects/{slug}/suites/{suite_slug}/run", status_code=status.HTTP_202_ACCEPTED)
    async def run_suite(slug: str, suite_slug: str, body: SuiteRunRequest | None = None) -> Run:
        suite = suites.get(slug, suite_slug)
        body = body or SuiteRunRequest()
        return manager.submit(
            RunRequest(plan=suite.plan, project=slug, only=body.only, parallel=body.parallel,
                       url=body.url, max_cost_usd=body.max_cost_usd, ai_report=body.ai_report,
                       callback_url=body.callback_url),
            suite=suite.slug, title=suite.name,
        )

    # ── editing helpers ──────────────────────────────────────

    @api.post("/plans/parse")
    async def parse_plan(body: ParseRequest) -> PlanFields:
        """Split a Markdown plan into editable fields (test cases, steps, acceptance criteria)."""
        return PlanFields.model_validate(plan_fields(body.plan))

    @api.post("/plans/render")
    async def render(body: PlanFields) -> dict:
        """Markdown for a plan from editable fields. Keeps test IDs; numbers new cases."""
        return {"plan": render_plan(body.model_dump())}

    @api.post("/drafts/test-case")
    async def draft(body: DraftRequest) -> dict:
        """Draft a test case's steps and acceptance criteria from a sentence (uses the writer model)."""
        project = projects.get(body.project) if body.project else None
        try:
            fields, cost_usd = await draft_test_case(body.description, project)
        except RuntimeError as e:
            raise HTTPException(422, str(e)) from e
        return {"case": fields, "cost_usd": round(cost_usd, 4)}

    # ── runs ─────────────────────────────────────────────────

    @api.post("/runs", status_code=status.HTTP_202_ACCEPTED)
    async def create_run(req: RunRequest) -> Run:
        return manager.submit(req)

    @api.get("/runs")
    async def list_runs(
        limit: int = 50, project: str | None = None, suite: str | None = None, kind: RunKind | None = None
    ) -> list[Run]:
        runs = [
            r for r in manager.runs.values()
            if (project is None or r.project == project)
            and (suite is None or r.suite == suite)
            and (kind is None or r.kind == kind)
        ]
        runs.sort(key=lambda r: r.created_at, reverse=True)
        return runs[:limit]

    @api.get("/runs/{run_id}")
    async def read_run(run_id: str) -> Run:
        return get_run(run_id)

    @api.delete("/runs/{run_id}", status_code=status.HTTP_204_NO_CONTENT)
    async def delete_run(run_id: str) -> Response:
        """Delete a finished run and its files. Tests already saved to suites are kept."""
        manager.delete(run_id)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @api.post("/runs/delete")
    async def delete_runs(body: DeleteRunsRequest) -> dict:
        """Delete several finished runs. Runs that are missing or still active are skipped."""
        deleted, skipped = [], []
        for run_id in dict.fromkeys(body.ids):
            try:
                manager.delete(run_id)
                deleted.append(run_id)
            except HTTPException as e:
                skipped.append({"id": run_id, "reason": e.detail})
        return {"deleted": deleted, "skipped": skipped}

    @api.post("/runs/{run_id}/rerun", status_code=status.HTTP_202_ACCEPTED)
    async def rerun(run_id: str, body: RerunRequest | None = None) -> Run:
        run = get_run(run_id)
        if run.kind != "test":
            raise HTTPException(409, "Only test runs can be re-run.")
        failed_only = body.failed_only if body else True
        if failed_only and not run.failed_ids:
            raise HTTPException(409, f"Run {run_id} has no failed tests to re-run.")
        return manager.submit(
            RunRequest(
                plan=run_file(run_id, "plan.md").read_text(),
                project=run.project,
                url=run.url,
                only=run.failed_ids if failed_only else None,
                parallel=run.parallel,
                max_cost_usd=run.max_cost_usd,
            ),
            suite=run.suite, title=run.title,
        )

    @api.get("/runs/{run_id}/proposed")
    async def read_proposed(run_id: str) -> dict:
        """Test cases proposed by a discover or explore run."""
        run = get_run(run_id)
        path = manager.run_dir(run_id) / "proposed.md"
        plan = parse_test_plan(path.read_text()) if path.is_file() else TestPlan("", None, "", [])
        return {
            "cases": [{**summary, "markdown": tc.raw_markdown, "accepted": tc.id in run.accepted}
                      for summary, tc in zip(_case_summary(plan), plan.cases, strict=True)],
        }

    @api.post("/runs/{run_id}/accept")
    async def accept_proposed(run_id: str, body: AcceptRequest) -> dict:
        """Save proposed test cases into a new or existing suite of the run's project."""
        run = get_run(run_id)
        if run.kind == "test" or run.status != "completed" or not run.project:
            raise HTTPException(409, "Only completed discover/explore runs have tests to accept.")
        plan = parse_test_plan(run_file(run_id, "proposed.md").read_text())
        wanted = {i.upper() for i in body.case_ids} if body.case_ids is not None else None
        chosen = [tc for tc in plan.cases if wanted is None or tc.id.upper() in wanted]
        if not chosen:
            raise HTTPException(422, "No matching proposed test cases selected.")
        project = projects.get(run.project)

        if body.suite:
            suite = suites.get(run.project, body.suite)
            suite = suite.model_copy(update={"plan": append_cases(suite.plan, chosen), "updated_at": _now()})
        else:
            name = body.suite_name.strip()
            suite_slug = slugify(name)
            if suites.exists(run.project, suite_slug):
                raise HTTPException(409, f"A suite named {name!r} already exists; add to it instead.")
            suite = Suite(slug=suite_slug, name=name, project=run.project, created_at=_now(),
                          updated_at=_now(), plan=compose_plan(name, project.url or run.url, chosen))
        _check_suite_plan(suite.plan, project)
        suites.save(suite)

        run.accepted = sorted(set(run.accepted) | {tc.id for tc in chosen})
        manager.save(run)
        return suite_view(suite)

    @api.get("/runs/{run_id}/exploration")
    async def read_exploration(run_id: str) -> dict:
        """What a discover run mapped: pages, user flows, forms, issues noticed."""
        return json.loads(run_file(run_id, "exploration.json").read_text())

    @api.get("/runs/{run_id}/events")
    async def read_events(run_id: str, after: int = 0) -> dict:
        """Agent activity, oldest first. Poll with `after` set to the returned `next`."""
        get_run(run_id)
        events, cursor = EventLog.read(manager.run_dir(run_id) / "events.jsonl", after)
        return {"events": events, "next": cursor}

    @api.get("/runs/{run_id}/plan", response_class=PlainTextResponse)
    async def read_plan(run_id: str) -> PlainTextResponse:
        return PlainTextResponse(run_file(run_id, "plan.md").read_text(), media_type="text/markdown")

    @api.post("/runs/{run_id}/cancel")
    async def cancel_run(run_id: str) -> Run:
        run = get_run(run_id)
        if run.status in FINISHED or not manager.cancel(run_id):
            raise HTTPException(409, f"Run {run_id} is already {run.status}.")
        return run

    @api.get("/runs/{run_id}/results")
    async def read_results(run_id: str) -> dict:
        return json.loads(run_file(run_id, "results.json").read_text())

    @api.get("/runs/{run_id}/report", response_class=PlainTextResponse)
    async def read_report(run_id: str) -> PlainTextResponse:
        text = run_file(run_id, "report.md").read_text()
        return PlainTextResponse(text, media_type="text/markdown")

    @api.get("/runs/{run_id}/junit")
    async def read_junit(run_id: str) -> FileResponse:
        return FileResponse(run_file(run_id, "junit.xml"), media_type="application/xml")

    @api.get("/runs/{run_id}/screenshots")
    async def list_screenshots(run_id: str) -> list[str]:
        get_run(run_id)
        ss_dir = manager.run_dir(run_id) / "screenshots"
        if not ss_dir.is_dir():
            return []
        return sorted(p.name for p in ss_dir.iterdir() if p.is_file())

    @api.get("/runs/{run_id}/screenshots/{name}")
    async def read_screenshot(run_id: str, name: str) -> FileResponse:
        if "/" in name or "\\" in name or name.startswith("."):
            raise HTTPException(400, "Invalid screenshot name.")
        return FileResponse(run_file(run_id, f"screenshots/{name}"))

    app.include_router(api)
    return app


def serve(host: str, port: int, data_dir: str, max_concurrent: int) -> None:
    import uvicorn

    if not os.environ.get("ARGUS_API_KEY") and host not in ("127.0.0.1", "localhost"):
        print(c.warn(
            f"  Warning: serving on {host} without ARGUS_API_KEY — anyone who can reach "
            "this port can drive a browser from this machine."
        ))
    costs = default_costs()
    print(c.header(f"\n  argus-qa server — http://{host}:{port}  (web UI at /, API docs at /docs)"))
    print(c.dim(f"  Data: {Path(data_dir).resolve()}   Max concurrent runs: {max_concurrent}"))
    print(c.dim(
        f"  Cost limits per run: test ${costs['test']:.2f}, discover ${costs['discover']:.2f}, "
        f"explore ${costs['explore']:.2f} (estimated at API prices)\n"
    ))
    uvicorn.run(create_app(data_dir, max_concurrent), host=host, port=port)
