"""Orchestrator — two modes: analyze (explore → scaffold) and test (execute plan → report)."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import random
import re
import shlex
import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

import httpx
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
    query,
)

from argus_qa import colors as c
from argus_qa.events import describe_tool, emit
from argus_qa.plan_parser import TestPlan, parse_test_plan
from argus_qa.project import Project, Redactor, substitute
from argus_qa.prompts.explorer import EXPLORER_PROMPT
from argus_qa.prompts.reporter import REPORTER_PROMPT
from argus_qa.prompts.scaffold import SCAFFOLD_PROMPT
from argus_qa.prompts.tester import TESTER_PROMPT
from argus_qa.results import failed_ids as _failed_ids
from argus_qa.results import merge_results, to_junit

AGENT_NAMES = [
    "Chad the Clicker",
    "Button Basher Betty",
    "Tab-Smasher Todd",
    "Pixel Inspector Pam",
    "404 Hunter Frank",
    "Rage Clicker Rita",
    "Form Filler Phil",
    "Scroll Lord Steve",
    "Bug Sniper Brenda",
    "Chaos Monkey Carl",
    "Tooltip Tina",
    "Dropdown Dave",
    "Cookie Monster Claire",
    "Refresh Randy",
    "Screenshot Sally",
    "Lag Detector Larry",
]

# Pinned so upstream releases can't silently change browser behaviour between runs
PLAYWRIGHT_MCP_PACKAGE = "@playwright/mcp@0.0.82"


def install_browser() -> None:
    """Download the Chromium build that the pinned Playwright MCP version uses."""
    core_version = subprocess.run(
        ["npm", "view", PLAYWRIGHT_MCP_PACKAGE, "dependencies.playwright-core"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    subprocess.run(["npx", "-y", f"playwright-core@{core_version}", "install", "chromium"], check=True)


def _pick_agent_name() -> str:
    return random.choice(AGENT_NAMES)


def _make_playwright_mcp(
    headless: bool = False,
    isolated: bool = False,
    output_dir: Path | None = None,
) -> dict:
    """Create a Playwright MCP config.

    Isolated instances keep their browser profile in memory, so multiple
    browsers can run side by side without conflicting. output_dir is where
    the browser saves screenshots. Extra flags (e.g. "--browser chromium
    --no-sandbox" in containers) can be passed via ARGUS_PLAYWRIGHT_ARGS.
    """
    extra = shlex.split(os.environ.get("ARGUS_PLAYWRIGHT_ARGS", ""))
    if "--browser" not in extra:
        # Playwright's bundled Chromium (installed by `argus-qa setup`), rather than
        # whatever Chrome happens to be installed on the machine
        extra = ["--browser", "chromium", *extra]
    args = [PLAYWRIGHT_MCP_PACKAGE, *extra]
    if headless:
        args.append("--headless")
    if isolated:
        args.append("--isolated")
    if output_dir is not None:
        args.extend(["--output-dir", str(output_dir.resolve())])
    return {"playwright": {"command": "npx", "args": args}}


def _browser_options(
    headless: bool = False,
    isolated: bool = False,
    output_dir: Path | None = None,
) -> ClaudeAgentOptions:
    """Options for agents that need browser access.

    With an output_dir, the agent (and so its browser) also runs from that
    directory: Playwright MCP resolves explicit screenshot filenames against its
    working directory, while --output-dir only covers auto-named files.
    """
    return ClaudeAgentOptions(
        mcp_servers=_make_playwright_mcp(headless=headless, isolated=isolated, output_dir=output_dir),
        tools=[],
        allowed_tools=["mcp__playwright__*"],
        cwd=str(output_dir.resolve()) if output_dir is not None else None,
    )


def _reasoning_options() -> ClaudeAgentOptions:
    """Options for agents that only need to think and write."""
    return ClaudeAgentOptions(tools=[], allowed_tools=[])


@dataclass
class AgentRun:
    text: str
    ok: bool
    cost_usd: float = 0.0
    turns: int = 0


async def _run_agent(
    prompt: str,
    options: ClaudeAgentOptions,
    label: str = "",
    redact: Redactor | None = None,
) -> AgentRun:
    """Run a single agent query and return its final result, with secrets redacted."""
    redact = redact or Redactor()
    run = AgentRun(text="", ok=False)
    async for message in query(prompt=prompt, options=options):
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock):
                    line = redact(block.text.strip().replace("\n", " "))
                    if line:
                        emit("say", redact(block.text.strip()), agent=label)
                        # Highlight keywords in the output
                        line = _colorize_line(line)
                        if label:
                            print(c.agent_line(label, line))
                        else:
                            print(f"  {c.DIM}>{c.RESET} {line}")
                elif isinstance(block, ToolUseBlock):
                    tool, text, extra = describe_tool(block.name, block.input)
                    emit("action", redact(text), agent=label, tool=tool, **extra)
        elif isinstance(message, ResultMessage):
            run.cost_usd = message.total_cost_usd or 0.0
            run.turns = message.num_turns
            if message.subtype == "success" and not message.is_error:
                run.ok = True
                run.text = redact(message.result or "")
            else:
                print(c.error(f"  Agent error: {message.subtype}"))
                emit("error", f"Agent stopped: {message.subtype}", agent=label)
                run.text = f"Agent error: {message.subtype}"
    return run


_STATUS_WORDS = [
    (re.compile(r"\b(passed|passes)\b", re.I), c.GREEN),
    (re.compile(r"\b(failed|fails|failure)\b", re.I), c.RED),
    (re.compile(r"\bblocked\b", re.I), c.YELLOW),
    (re.compile(r"\bskipped\b", re.I), c.DIM),
    (re.compile(r"\b(bug|error)s?\b", re.I), c.RED),
]


def _colorize_line(line: str) -> str:
    """Add inline color hints to agent output for key events."""
    for pattern, color in _STATUS_WORDS:
        if pattern.search(line):
            return f"{color}{line}{c.RESET}"
    if line.startswith("TC-") or line.startswith("Now "):
        return f"{c.CYAN}{line}{c.RESET}"
    return line


# ── Analyze mode ────────────────────────────────────────────────────


async def run_analyze(
    url: str | None,
    output_file: str | None = None,
    credentials: dict[str, str] | None = None,
    headless: bool = False,
    project: Project | None = None,
) -> str:
    """Explore a website and generate a test plan scaffold."""
    url = url or (project.url if project else None)
    if not url:
        raise ValueError("No URL: pass one, or set `url` in the project.")
    output = Path(output_file or "testplan.md")
    timestamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")

    secrets = project.secret_values() if project else []
    project_guidance = ""
    if project:
        creds_text = project.prompt_section()
        if project.credentials:
            creds_text += "\nLog in with the test accounts above to explore authenticated areas."
            roles = ", ".join(project.credentials)
            project_guidance = (
                f"Test accounts are configured in the project (roles: {roles}). Do NOT include a "
                "Credentials section or any usernames/passwords in the plan. Write login steps as "
                "'Log in as <role>'.\n"
            )
    elif credentials:
        secrets.append(credentials["password"])
        creds_text = (
            f"Use these to log in:\n"
            f"- Username: `{credentials['username']}`\n"
            f"- Password: `{credentials['password']}`"
        )
    else:
        creds_text = "No credentials provided. Explore only public/unauthenticated areas."
    redact = Redactor(secrets)

    # Phase 1: Explore
    explorer_name = _pick_agent_name()
    print(c.phase(1, 2, f"{explorer_name} is exploring the application..."))
    emit("phase", f"{explorer_name} is exploring the application")
    exploration = await _run_agent(
        prompt=EXPLORER_PROMPT.format(url=url, credentials=creds_text),
        options=_browser_options(headless=headless),
        label=explorer_name,
        redact=redact,
    )
    if not exploration.ok:
        raise RuntimeError(f"Exploration failed: {exploration.text}")
    print(c.success("\n  Exploration complete.\n"))

    # Phase 2: Generate scaffold
    print(c.phase(2, 2, "Generating test plan scaffold..."))
    emit("phase", "Writing the test plan")
    scaffold = await _run_agent(
        prompt=SCAFFOLD_PROMPT.format(
            url=url,
            timestamp=timestamp,
            exploration_report=exploration.text,
            project_guidance=project_guidance,
        ),
        options=_reasoning_options(),
        label="scaffold",
        redact=redact,
    )
    if not scaffold.ok:
        raise RuntimeError(f"Test plan generation failed: {scaffold.text}")
    if not parse_test_plan(scaffold.text).cases:
        rejected = output.with_suffix(".rejected.md")
        rejected.write_text(scaffold.text)
        raise RuntimeError(
            f"Generated test plan contains no '### TC-NNN:' test cases. Raw output saved to {rejected}"
        )

    output.write_text(scaffold.text)
    print(c.success(f"\n  Test plan saved to {output}\n"))
    print(c.dim(f"  Cost: ${exploration.cost_usd + scaffold.cost_usd:.2f}"))
    return str(output)


# ── Test mode ───────────────────────────────────────────────────────


@dataclass
class RunResult:
    run_dir: Path
    report_file: Path
    results: dict
    failed_ids: list[str] = field(default_factory=list)
    cost_usd: float = 0.0

    @property
    def environment_error(self) -> str | None:
        """Set when the browser/tools broke, meaning the results say nothing about the app."""
        errors = self.results.get("environment_errors")
        return "; ".join(dict.fromkeys(errors)) if errors else None


def site_dir_name(url: str) -> str:
    """Filesystem-safe name for the site under test, e.g. 'localhost-3000'."""
    parsed = urlparse(url)
    host = parsed.hostname or "unknown"
    site = f"{host}-{parsed.port}" if parsed.port else host
    return re.sub(r"[^\w\-.]", "_", site)


async def run_tests(
    test_plan_file: str,
    url: str | None = None,
    output_dir: str = "reports",
    only: list[str] | None = None,
    skip: list[str] | None = None,
    parallel: int = 1,
    screenshot_dir: str | None = None,
    headless: bool = False,
    junit_file: str | None = None,
    project: Project | None = None,
) -> RunResult:
    """Execute a test plan from a Markdown file.

    Args:
        test_plan_file: Path to the Markdown test plan.
        url: Override URL (otherwise the project's URL, then the plan's).
        output_dir: Directory to write reports to.
        only: Run only these test case IDs.
        skip: Skip these test case IDs.
        parallel: Number of parallel browser agents.
        screenshot_dir: Directory to save screenshots. Defaults to <run dir>/screenshots/.
        headless: Run the browser headless (parallel runs are always headless).
        junit_file: Extra path to write JUnit XML to, in addition to the run dir.
        project: Project supplying the URL, test accounts, data, and instructions.
    """
    plan_path = Path(test_plan_file)
    if not plan_path.exists():
        raise FileNotFoundError(f"Test plan not found: {plan_path}")

    full_plan = parse_test_plan(plan_path.read_text())

    url = url or (project.url if project else None) or full_plan.url
    if url is None:
        raise ValueError(
            "Could not find a URL. Pass --url, set `url` in the project, "
            "or add a '> URL: ...' line to the plan."
        )

    for flag, ids in (("--only", only), ("--skip", skip)):
        unknown = full_plan.unknown_ids(ids or [])
        if unknown:
            print(c.warn(f"  Warning: {flag} IDs not found in plan: {', '.join(unknown)}"))

    # Apply filters
    plan = full_plan.filter(only=only, skip=skip)
    if not plan.cases:
        raise ValueError("No test cases to run after applying --only/--skip filters.")

    timestamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    run_dir = Path(output_dir) / site_dir_name(url) / timestamp

    result = await execute_plan(
        plan,
        url,
        run_dir,
        parallel=parallel,
        headless=headless,
        screenshot_dir=Path(screenshot_dir) if screenshot_dir else None,
        project=project,
    )
    if result.environment_error:
        raise RuntimeError(
            f"The browser couldn't run the tests: {result.environment_error}\n"
            "  If the browser isn't installed, run: argus-qa setup"
        )

    if junit_file:
        Path(junit_file).parent.mkdir(parents=True, exist_ok=True)
        Path(junit_file).write_text(to_junit(result.results))

    if result.failed_ids:
        rerun = f"argus-qa test {test_plan_file} --only {','.join(result.failed_ids)}"
        print(f"  Re-run just failures: {c.BOLD}{rerun}{c.RESET}")

    return result


async def execute_plan(
    plan: TestPlan,
    url: str,
    run_dir: Path,
    parallel: int = 1,
    headless: bool = False,
    screenshot_dir: Path | None = None,
    isolated: bool = False,
    project: Project | None = None,
) -> RunResult:
    """Run the given test cases, write results/report/JUnit to run_dir, and return the outcome.

    Set isolated when other runs may be using a browser at the same time. The
    project's secrets are given to tester agents but redacted from everything
    printed or saved, and never shown to the reporter.
    """
    substitute(plan.to_markdown(), project)  # fail fast on unknown {{placeholders}}
    redact = Redactor(project.secret_values() if project else [])
    run_dir.mkdir(parents=True, exist_ok=True)
    ss_dir = screenshot_dir or run_dir / "screenshots"
    ss_dir.mkdir(parents=True, exist_ok=True)

    total = len(plan.cases)
    case_ids = ", ".join(tc.id for tc in plan.cases)
    print(f"\n  {c.BOLD}{total}{c.RESET} test cases selected: {c.DIM}{case_ids}{c.RESET}")
    print(f"  Screenshots {c.DIM}→{c.RESET} {c.info(str(ss_dir))}")

    # ── Phase 1: Execute tests (sequential or parallel) ─────────
    used_names: set[str] = set()

    def _unique_name() -> str:
        available = [n for n in AGENT_NAMES if n not in used_names]
        if not available:
            available = AGENT_NAMES
        name = random.choice(available)
        used_names.add(name)
        return name

    if parallel <= 1 or total <= 1:
        tester_name = _unique_name()
        print(c.phase(1, 2, f"{tester_name} is running {total} tests..."))
        emit("phase", f"{tester_name} is running {total} test{'s' if total != 1 else ''}")
        runs = [await _run_test_chunk(
            url, plan, ss_dir, label=tester_name, headless=headless, isolated=isolated,
            project=project, redact=redact,
        )]
    else:
        chunks = plan.split_chunks(min(parallel, total))
        print(c.phase(1, 2, f"Assembling the testing squad ({len(chunks)} agents)..."))
        emit("phase", f"{len(chunks)} agents are splitting {total} tests")
        tasks = []
        for chunk in chunks:
            chunk_ids = ", ".join(tc.id for tc in chunk.cases)
            name = _unique_name()
            color = c.agent_color(name)
            print(f"  {color}{c.BOLD}{name}{c.RESET}: {c.DIM}{chunk_ids}{c.RESET}")
            emit("phase", f"{name} takes {chunk_ids}", agent=name)
            tasks.append(
                _run_test_chunk(
                    url, chunk, ss_dir, label=name, headless=True, isolated=True,
                    project=project, redact=redact,
                )
            )
        print()
        runs = await asyncio.gather(*tasks)

    # Page snapshots and other text the browser saved can contain secrets shown or typed on the page
    _redact_text_files(ss_dir, redact)

    # Merge results
    merged_results = redact.data(merge_results([r.text for r in runs], plan))
    (run_dir / "results.json").write_text(json.dumps(merged_results, indent=2))
    (run_dir / "junit.xml").write_text(to_junit(merged_results))
    print(c.success("\n  Tests complete.\n"))

    runs_cost = sum(r.cost_usd for r in runs)
    if merged_results.get("environment_errors"):
        # Nothing about the app was learned; skip the report
        problem = "; ".join(merged_results["environment_errors"])
        print(c.error(f"  Browser/environment problem: {problem}"))
        emit("error", f"The browser couldn't run the tests: {problem}")
        return RunResult(run_dir=run_dir, report_file=run_dir / "report.md", results=merged_results,
                         failed_ids=[], cost_usd=runs_cost)

    # Print quick summary
    summary = merged_results["summary"]
    print(c.result_bar(
        summary["passed"],
        summary["failed"],
        summary["blocked"],
        summary["skipped"],
    ))

    # ── Phase 2: Generate report ────────────────────────────────
    print(c.phase(2, 2, "Generating report..."))
    emit("phase", "Writing the report")
    report = await _run_agent(
        prompt=REPORTER_PROMPT.format(
            exploration_report="(not available — ran from existing test plan)",
            test_plan=plan.to_markdown(),
            test_results=json.dumps(merged_results, indent=2),
            screenshot_dir=str(ss_dir),
        ),
        options=_reasoning_options(),
        label="reporter",
        redact=redact,
    )

    report_file = run_dir / "report.md"
    report_file.write_text(report.text)
    print(c.success(f"\n  Report saved to {report_file}\n"))

    # Save failed test IDs for re-runs
    failed = _failed_ids(merged_results)
    if failed:
        failures_file = run_dir / "failures.txt"
        failures_file.write_text("\n".join(failed) + "\n")
        print(c.warn(f"  Failed tests saved to {failures_file}"))

    cost = runs_cost + report.cost_usd
    print(c.dim(f"  Cost: ${cost:.2f}"))

    return RunResult(
        run_dir=run_dir,
        report_file=report_file,
        results=merged_results,
        failed_ids=failed,
        cost_usd=cost,
    )


_TEXT_ARTIFACTS = {".yml", ".yaml", ".md", ".txt", ".json", ".log", ".html"}


def _redact_text_files(directory: Path, redact: Redactor) -> None:
    if not redact.values:
        return
    for path in directory.rglob("*"):
        if path.is_file() and path.suffix.lower() in _TEXT_ARTIFACTS:
            text = path.read_text(errors="replace")
            cleaned = redact(text)
            if cleaned != text:
                path.write_text(cleaned)


async def _run_test_chunk(
    url: str,
    plan: TestPlan,
    screenshot_dir: Path,
    label: str = "",
    headless: bool = False,
    isolated: bool = False,
    project: Project | None = None,
    redact: Redactor | None = None,
) -> AgentRun:
    """Run a chunk of test cases in one agent.

    Isolated agents get their own in-memory browser profile so multiple chunks
    can run in parallel without conflicts.
    """
    prompt = TESTER_PROMPT.format(
        url=url,
        test_plan=substitute(plan.to_markdown(), project),
        screenshot_dir=str(screenshot_dir),
        project_context=project.prompt_section() if project else "",
    )
    options = _browser_options(headless=headless, isolated=isolated, output_dir=screenshot_dir)
    return await _run_agent(prompt, options, label=label, redact=redact)


# ── Watch mode ──────────────────────────────────────────────────────


async def run_watch(
    test_plan_file: str,
    url: str | None = None,
    output_dir: str = "reports",
    interval: int = 30,
    parallel: int = 1,
    screenshot_dir: str | None = None,
    headless: bool = False,
    project: Project | None = None,
) -> None:
    """Watch mode: re-run failed tests when the app changes.

    Polls the target URL at the given interval. When a change is detected
    (different response content), re-runs only the previously failed tests.
    """
    plan_path = Path(test_plan_file)
    if not plan_path.exists():
        raise FileNotFoundError(f"Test plan not found: {plan_path}")
    full_plan = parse_test_plan(plan_path.read_text())
    target_url = url or (project.url if project else None) or full_plan.url
    if not target_url:
        raise ValueError("No URL found. Pass --url, set it in the project, or add '> URL: ...' to the plan.")

    print(c.info(f"\n  Watch mode — polling {target_url} every {interval}s"))
    print(c.dim("  Press Ctrl+C to stop.\n"))

    # Initial run
    print(c.header("  Running initial test pass...\n"))
    result = await run_tests(
        test_plan_file, url=target_url, output_dir=output_dir,
        parallel=parallel, screenshot_dir=screenshot_dir, headless=headless, project=project,
    )
    failed_ids = result.failed_ids
    last_hash = await _fetch_hash(target_url)

    if not failed_ids:
        print(c.success("\n  All tests passed! Watching for regressions...\n"))

    while True:
        await asyncio.sleep(interval)
        current_hash = await _fetch_hash(target_url)

        if current_hash is None or current_hash == last_hash:
            continue

        ts = datetime.now().strftime("%H:%M:%S")
        print(c.warn(f"\n  Change detected at {ts}!"))
        last_hash = current_hash

        if failed_ids:
            print(c.warn(f"  Re-running {len(failed_ids)} failed tests: {', '.join(failed_ids)}\n"))
            run_only = failed_ids
        else:
            print(c.info("  No previous failures — running full suite to check for regressions.\n"))
            run_only = None

        result = await run_tests(
            test_plan_file, url=target_url, output_dir=output_dir,
            only=run_only, parallel=parallel, screenshot_dir=screenshot_dir, headless=headless,
            project=project,
        )
        failed_ids = result.failed_ids

        if not failed_ids:
            print(c.success("\n  All tests passing now! Continuing to watch...\n"))


async def _fetch_hash(url: str) -> str | None:
    """Fetch a URL and return a hash of the response body, or None if unreachable."""
    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
            resp = await client.get(url)
            return hashlib.sha256(resp.content).hexdigest()
    except httpx.HTTPError as e:
        print(c.dim(f"  Could not reach {url}: {e}"))
        return None
