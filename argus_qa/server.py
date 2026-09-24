"""HTTP API: post a test plan or scenario, argus-qa runs it in a headless browser.

Runs are queued and executed in the background, up to max_concurrent at a time.
Each run's results, report, JUnit XML, and screenshots are stored under
<data_dir>/runs/<run_id>/ and survive restarts.

Projects (target URL, test accounts, test data, secrets, standing instructions)
are stored under <data_dir>/projects/<slug>.json and referenced by runs. Secrets
are masked in API responses and redacted from results and reports.

Set ARGUS_API_KEY to require `Authorization: Bearer <key>` on every request.
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import traceback
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import httpx
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request, Response, status
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, HttpUrl, model_validator

from argus_qa import colors as c
from argus_qa.agents.orchestrator import execute_plan
from argus_qa.events import EventLog, capture
from argus_qa.plan_parser import TestPlan, parse_test_plan, plan_from_scenario
from argus_qa.project import MASK, Project, substitute

WEB_DIR = Path(__file__).parent / "web"

RunStatus = Literal["queued", "running", "passed", "failed", "error", "cancelled"]
FINISHED: set[str] = {"passed", "failed", "error", "cancelled"}


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


class RunRequest(BaseModel):
    """Submit either a full Markdown test plan or a single plain-English scenario."""

    project: str | None = Field(None, description="Project slug supplying URL, accounts, and test data.")
    plan: str | None = Field(None, description="Full Markdown test plan (### TC-NNN: sections).")
    scenario: str | None = Field(None, description="Plain-English steps and expected result for one test.")
    name: str | None = Field(None, description="Name for the scenario (used with `scenario`).")
    url: HttpUrl | None = Field(None, description="Target URL. Overrides the project's and plan's URL.")
    only: list[str] | None = Field(None, description="Run only these test case IDs.")
    skip: list[str] | None = Field(None, description="Skip these test case IDs.")
    parallel: int = Field(1, ge=1, le=8, description="Parallel browser agents for this run.")
    callback_url: HttpUrl | None = Field(None, description="POSTed the run record when the run finishes.")

    @model_validator(mode="after")
    def _one_source(self) -> RunRequest:
        if bool(self.plan) == bool(self.scenario):
            raise ValueError("Provide exactly one of `plan` or `scenario`.")
        return self


class Run(BaseModel):
    id: str
    status: RunStatus
    title: str = ""
    project: str | None = None
    url: str
    test_ids: list[str]
    created_at: str
    started_at: str | None = None
    finished_at: str | None = None
    summary: dict | None = None
    failed_ids: list[str] = []
    cost_usd: float | None = None
    error: str | None = None
    callback_url: str | None = None
    parallel: int = 1


class RerunRequest(BaseModel):
    failed_only: bool = Field(True, description="Re-run only the tests that failed or were blocked.")


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


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


def _project_from_model(model: ProjectModel) -> Project:
    try:
        return Project.from_dict(model.model_dump(exclude_none=True))
    except ValueError as e:
        raise HTTPException(422, str(e)) from e


class RunManager:
    """Queues runs, executes them with bounded concurrency, and persists their state."""

    def __init__(self, data_dir: Path, max_concurrent: int, projects: ProjectStore):
        self.projects = projects
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
                self._save(run)
            self.runs[run.id] = run

    def run_dir(self, run_id: str) -> Path:
        return self.runs_dir / run_id

    def _save(self, run: Run) -> None:
        path = self.run_dir(run.id) / "run.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(run.model_dump_json(indent=2))

    def submit(self, req: RunRequest) -> Run:
        project = None
        if req.project:
            try:
                project = self.projects.get(req.project).resolve_env()
            except ValueError as e:
                raise HTTPException(422, str(e)) from e

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

        run = Run(
            id=uuid.uuid4().hex[:12],
            status="queued",
            title=req.name or plan_title(plan),
            project=project.slug if project else None,
            url=url,
            test_ids=[tc.id for tc in plan.cases],
            created_at=_now(),
            callback_url=str(req.callback_url) if req.callback_url else None,
            parallel=req.parallel,
        )
        self.runs[run.id] = run
        self._save(run)
        (self.run_dir(run.id) / "plan.md").write_text(plan.to_markdown())

        task = asyncio.create_task(self._execute(run, plan, req.parallel, project))
        self.tasks[run.id] = task
        task.add_done_callback(lambda _: self.tasks.pop(run.id, None))
        return run

    async def _execute(self, run: Run, plan: TestPlan, parallel: int, project: Project | None) -> None:
        try:
            async with self.semaphore:
                run.status = "running"
                run.started_at = _now()
                self._save(run)
                print(c.header(f"\n  [run {run.id}] started — {len(plan.cases)} tests against {run.url}"))

                with capture(EventLog(self.run_dir(run.id) / "events.jsonl")):
                    result = await execute_plan(
                        plan,
                        run.url,
                        self.run_dir(run.id),
                        parallel=parallel,
                        headless=True,
                        isolated=True,
                        project=project,
                    )
                run.summary = result.results["summary"]
                run.failed_ids = result.failed_ids
                run.cost_usd = round(result.cost_usd, 4)
                if result.environment_error:
                    run.status = "error"
                    run.error = f"Browser/environment problem: {result.environment_error}"
                else:
                    run.status = "failed" if result.failed_ids else "passed"
        except asyncio.CancelledError:
            run.status = "cancelled"
        except Exception as e:  # noqa: BLE001 — any failure must be recorded on the run
            run.status = "error"
            run.error = f"{type(e).__name__}: {e}"
            traceback.print_exc()
        finally:
            run.finished_at = _now()
            self._save(run)
            print(c.header(f"  [run {run.id}] {run.status}"))
            if run.callback_url:
                await _notify(run)

    def cancel(self, run_id: str) -> bool:
        task = self.tasks.get(run_id)
        if task is None:
            return False
        task.cancel()
        return True


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
        plan = plan_from_scenario(req.scenario, url, name=req.name or "Scenario")
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
    manager = RunManager(Path(data_dir), max_concurrent, projects)
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

    @app.get("/health")
    async def health() -> dict:
        return {"status": "ok", "auth_required": bool(api_key)}

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
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    # ── runs ─────────────────────────────────────────────────

    @api.post("/runs", status_code=status.HTTP_202_ACCEPTED)
    async def create_run(req: RunRequest) -> Run:
        return manager.submit(req)

    @api.get("/runs")
    async def list_runs(limit: int = 50, project: str | None = None) -> list[Run]:
        runs = [r for r in manager.runs.values() if project is None or r.project == project]
        runs.sort(key=lambda r: r.created_at, reverse=True)
        return runs[:limit]

    @api.get("/runs/{run_id}")
    async def read_run(run_id: str) -> Run:
        return get_run(run_id)

    @api.post("/runs/{run_id}/rerun", status_code=status.HTTP_202_ACCEPTED)
    async def rerun(run_id: str, body: RerunRequest | None = None) -> Run:
        run = get_run(run_id)
        failed_only = body.failed_only if body else True
        if failed_only and not run.failed_ids:
            raise HTTPException(409, f"Run {run_id} has no failed tests to re-run.")
        return manager.submit(RunRequest(
            plan=run_file(run_id, "plan.md").read_text(),
            name=run.title,
            project=run.project,
            url=run.url,
            only=run.failed_ids if failed_only else None,
            parallel=run.parallel,
        ))

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
    print(c.header(f"\n  argus-qa server — http://{host}:{port}  (web UI at /, API docs at /docs)"))
    print(c.dim(f"  Data: {Path(data_dir).resolve()}   Max concurrent runs: {max_concurrent}\n"))
    uvicorn.run(create_app(data_dir, max_concurrent), host=host, port=port)
