"""Orchestrator — two modes: analyze (explore → scaffold) and test (execute plan → report)."""

from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from claude_agent_sdk import (
    ClaudeAgentOptions,
    ResultMessage,
    AssistantMessage,
    TextBlock,
    query,
)

from argus_qa.agents.definitions import SWARM_AGENTS
from argus_qa import colors as c
from argus_qa.plan_parser import TestPlan, parse_test_plan
from argus_qa.prompts.explorer import EXPLORER_PROMPT
from argus_qa.prompts.scaffold import SCAFFOLD_PROMPT
from argus_qa.prompts.tester import TESTER_PROMPT
from argus_qa.prompts.reporter import REPORTER_PROMPT

import random
import tempfile

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


def _pick_agent_name() -> str:
    return random.choice(AGENT_NAMES)


PLAYWRIGHT_MCP = {
    "playwright": {
        "command": "npx",
        "args": ["@playwright/mcp@latest"],
    }
}


def _make_playwright_mcp(agent_id: str | None = None) -> dict:
    """Create a Playwright MCP config, optionally isolated for parallel use.

    When agent_id is provided, each instance gets its own user-data-dir
    and runs headless so multiple browsers don't conflict.
    """
    if agent_id is None:
        return PLAYWRIGHT_MCP

    user_data_dir = Path(tempfile.gettempdir()) / f"argus-qa-{agent_id}"
    user_data_dir.mkdir(parents=True, exist_ok=True)
    return {
        "playwright": {
            "command": "npx",
            "args": [
                "@playwright/mcp@latest",
                "--headless",
                "--isolated",
                "--user-data-dir", str(user_data_dir),
            ],
        }
    }


def _browser_options(agent_id: str | None = None, **overrides) -> ClaudeAgentOptions:
    """Options for agents that need browser access."""
    opts = dict(
        mcp_servers=_make_playwright_mcp(agent_id),
        allowed_tools=["Agent", "mcp__playwright__*"],
        agents=SWARM_AGENTS,
    )
    opts.update(overrides)
    return ClaudeAgentOptions(**opts)


def _reasoning_options(**overrides) -> ClaudeAgentOptions:
    """Options for agents that only need to think and write."""
    opts = dict(allowed_tools=[])
    opts.update(overrides)
    return ClaudeAgentOptions(**opts)


async def _run_agent(prompt: str, options: ClaudeAgentOptions, label: str = "") -> str:
    """Run a single agent query and return the final text result."""
    result_text = ""
    async for message in query(prompt=prompt, options=options):
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock):
                    line = block.text.strip().replace("\n", " ")
                    if line:
                        # Highlight keywords in the output
                        line = _colorize_line(line)
                        if label:
                            print(c.agent_line(label, line))
                        else:
                            print(f"  {c.DIM}>{c.RESET} {line}")
        elif isinstance(message, ResultMessage):
            if message.subtype == "success":
                result_text = message.result or ""
            else:
                print(c.error(f"  Agent error: {message.subtype}"))
                result_text = f"Agent error: {message.subtype}"
    return result_text


def _colorize_line(line: str) -> str:
    """Add inline color hints to agent output for key events."""
    lower = line.lower()
    # Test results
    if "passed" in lower or "pass" in lower and "tc-" in lower:
        return f"{c.GREEN}{line}{c.RESET}"
    if "failed" in lower or "fail" in lower:
        return f"{c.RED}{line}{c.RESET}"
    if "blocked" in lower or "block" in lower:
        return f"{c.YELLOW}{line}{c.RESET}"
    if "skipped" in lower or "skip" in lower:
        return f"{c.DIM}{line}{c.RESET}"
    # Bug findings
    if "bug" in lower or "error" in lower:
        return f"{c.RED}{line}{c.RESET}"
    # Navigation / actions
    if line.startswith("TC-") or line.startswith("Now "):
        return f"{c.CYAN}{line}{c.RESET}"
    return line


# ── Analyze mode ────────────────────────────────────────────────────


async def run_analyze(
    url: str,
    output_file: str | None = None,
    credentials: dict[str, str] | None = None,
) -> str:
    """Explore a website and generate a test plan scaffold."""
    output = Path(output_file or "testplan.md")
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    if credentials:
        creds_text = (
            f"Use these to log in:\n"
            f"- Username: `{credentials['username']}`\n"
            f"- Password: `{credentials['password']}`"
        )
    else:
        creds_text = "No credentials provided. Explore only public/unauthenticated areas."

    # Phase 1: Explore
    explorer_name = _pick_agent_name()
    print(c.phase(1, 2, f"{explorer_name} is exploring the application..."))
    exploration_report = await _run_agent(
        prompt=EXPLORER_PROMPT.format(url=url, credentials=creds_text),
        options=_browser_options(),
        label=explorer_name,
    )
    print(c.success("\n  Exploration complete.\n"))

    # Phase 2: Generate scaffold
    print(c.phase(2, 2, "Generating test plan scaffold..."))
    scaffold = await _run_agent(
        prompt=SCAFFOLD_PROMPT.format(
            url=url,
            timestamp=timestamp,
            exploration_report=exploration_report,
        ),
        options=_reasoning_options(),
        label="scaffold",
    )

    output.write_text(scaffold)
    print(c.success(f"\n  Test plan saved to {output}\n"))
    return str(output)


# ── Test mode ───────────────────────────────────────────────────────


async def run_tests(
    test_plan_file: str,
    url: str | None = None,
    output_dir: str = "reports",
    only: list[str] | None = None,
    skip: list[str] | None = None,
    parallel: int = 1,
    screenshot_dir: str | None = None,
) -> str:
    """Execute a test plan from a Markdown file.

    Args:
        test_plan_file: Path to the Markdown test plan.
        url: Override URL (otherwise extracted from the test plan).
        output_dir: Directory to write reports to.
        only: Run only these test case IDs.
        skip: Skip these test case IDs.
        parallel: Number of parallel browser agents.
        screenshot_dir: Directory to save screenshots. Defaults to reports/screenshots/.
    """
    plan_path = Path(test_plan_file)
    if not plan_path.exists():
        raise FileNotFoundError(f"Test plan not found: {plan_path}")

    full_plan = parse_test_plan(plan_path.read_text())

    if url is None:
        url = full_plan.url
    if url is None:
        raise ValueError(
            "Could not find URL in test plan. "
            "Pass it explicitly with --url or add a '> URL: ...' line to the plan."
        )

    # Apply filters
    plan = full_plan.filter(only=only, skip=skip)
    if not plan.cases:
        raise ValueError("No test cases to run after applying --only/--skip filters.")

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    parsed = urlparse(url)
    host = parsed.hostname or "unknown"
    site = f"{host}-{parsed.port}" if parsed.port else host
    site = re.sub(r"[^\w\-.]", "_", site)
    run_dir = Path(output_dir) / site / timestamp
    run_dir.mkdir(parents=True, exist_ok=True)

    ss_dir = Path(screenshot_dir or run_dir / "screenshots")
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
        test_results = await _run_test_chunk(url, plan, ss_dir, label=tester_name)
        all_results = [test_results]
    else:
        chunks = plan.split_chunks(min(parallel, total))
        actual_parallel = len(chunks)
        print(c.phase(1, 2, f"Assembling the testing squad ({actual_parallel} agents)..."))
        tasks = []
        for i, chunk in enumerate(chunks):
            chunk_ids = ", ".join(tc.id for tc in chunk.cases)
            name = _unique_name()
            color = c.agent_color(name)
            print(f"  {color}{c.BOLD}{name}{c.RESET}: {c.DIM}{chunk_ids}{c.RESET}")
            tasks.append(
                _run_test_chunk(url, chunk, ss_dir, label=name, agent_id=f"agent-{i+1}")
            )
        print()
        all_results = await asyncio.gather(*tasks)

    # Merge results
    merged_results = _merge_results(all_results)
    results_file = run_dir / "results.json"
    results_file.write_text(json.dumps(merged_results, indent=2))
    print(c.success("\n  Tests complete.\n"))

    # Print quick summary
    summary = merged_results.get("summary", {})
    print(c.result_bar(
        summary.get("passed", 0),
        summary.get("failed", 0),
        summary.get("blocked", 0),
        summary.get("skipped", 0),
    ))

    # ── Phase 2: Generate report ────────────────────────────────
    print(c.phase(2, 2, "Generating report..."))
    report = await _run_agent(
        prompt=REPORTER_PROMPT.format(
            exploration_report="(not available — ran from existing test plan)",
            test_plan=plan.to_markdown(),
            test_results=json.dumps(merged_results, indent=2),
            screenshot_dir=str(ss_dir),
        ),
        options=_reasoning_options(),
        label="reporter",
    )

    report_file = run_dir / "report.md"
    report_file.write_text(report)
    print(c.success(f"\n  Report saved to {report_file}\n"))

    # Save failed test IDs for watch mode / re-run
    failed_ids = _extract_failed_ids(merged_results)
    if failed_ids:
        failures_file = run_dir / "failures.txt"
        failures_file.write_text("\n".join(failed_ids) + "\n")
        print(c.warn(f"  Failed tests saved to {failures_file}"))
        print(f"  Re-run just failures: {c.BOLD}argus-qa test {test_plan_file} --only {','.join(failed_ids)}{c.RESET}")

    return str(report_file)


async def _run_test_chunk(
    url: str,
    plan: TestPlan,
    screenshot_dir: Path,
    label: str = "",
    agent_id: str | None = None,
) -> str:
    """Run a chunk of test cases in one agent.

    When agent_id is set, the agent gets its own isolated headless browser
    so multiple chunks can run in parallel without tab conflicts.
    """
    prompt = TESTER_PROMPT.format(
        url=url,
        test_plan=plan.to_markdown(),
        screenshot_dir=str(screenshot_dir),
    )
    return await _run_agent(prompt, _browser_options(agent_id=agent_id), label=label)


def _merge_results(results: list[str]) -> dict:
    """Merge JSON results from multiple parallel agents."""
    merged = {
        "summary": {"total": 0, "passed": 0, "failed": 0, "blocked": 0, "skipped": 0},
        "results": [],
        "bugs": [],
        "overall_assessment": "",
    }
    assessments = []
    for raw in results:
        try:
            # Try to extract JSON from the response (it might have surrounding text)
            json_str = _extract_json(raw)
            data = json.loads(json_str)
        except (json.JSONDecodeError, ValueError):
            # If parsing fails, store raw text
            merged["results"].append({"raw_output": raw})
            continue

        if "summary" in data:
            for key in ("total", "passed", "failed", "blocked", "skipped"):
                merged["summary"][key] += data["summary"].get(key, 0)
        if "results" in data:
            merged["results"].extend(data["results"])
        if "bugs" in data:
            merged["bugs"].extend(data["bugs"])
        if "overall_assessment" in data:
            assessments.append(data["overall_assessment"])

    if assessments:
        merged["overall_assessment"] = " | ".join(assessments)

    return merged


def _extract_json(text: str) -> str:
    """Extract JSON from text that might have markdown code fences."""
    # Try to find ```json ... ``` blocks
    match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL)
    if match:
        return match.group(1)
    # Try to find raw JSON object
    start = text.find("{")
    if start >= 0:
        depth = 0
        for i in range(start, len(text)):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    return text[start : i + 1]
    return text


def _extract_failed_ids(results: dict) -> list[str]:
    """Pull out IDs of failed/blocked tests for re-run."""
    failed = []
    for r in results.get("results", []):
        if isinstance(r, dict) and r.get("status") in ("failed", "blocked"):
            tc_id = r.get("id", "")
            if tc_id:
                failed.append(tc_id)
    return failed


# ── Watch mode ──────────────────────────────────────────────────────


async def run_watch(
    test_plan_file: str,
    url: str | None = None,
    output_dir: str = "reports",
    interval: int = 30,
    parallel: int = 1,
    screenshot_dir: str | None = None,
) -> None:
    """Watch mode: re-run failed tests when the app changes.

    Polls the target URL at the given interval. When a change is detected
    (different response content), re-runs only the previously failed tests.
    """
    import hashlib
    import httpx

    plan_path = Path(test_plan_file)
    full_plan = parse_test_plan(plan_path.read_text())
    target_url = url or full_plan.url
    if not target_url:
        raise ValueError("No URL found. Pass --url or add '> URL: ...' to the plan.")

    failed_ids: list[str] = []
    last_hash = ""

    print(c.info(f"\n  Watch mode — polling {target_url} every {interval}s"))
    print(c.dim("  Press Ctrl+C to stop.\n"))

    # Initial run
    print(c.header("  Running initial test pass...\n"))
    report = await run_tests(
        test_plan_file, url=target_url, output_dir=output_dir,
        parallel=parallel, screenshot_dir=screenshot_dir,
    )

    # Load failures from the latest results
    failed_ids = _load_latest_failures(Path(output_dir))
    last_hash = await _fetch_hash(target_url)

    if not failed_ids:
        print(c.success("\n  All tests passed! Watching for regressions...\n"))

    while True:
        await asyncio.sleep(interval)
        current_hash = await _fetch_hash(target_url)

        if current_hash == last_hash:
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

        report = await run_tests(
            test_plan_file, url=target_url, output_dir=output_dir,
            only=run_only, parallel=parallel, screenshot_dir=screenshot_dir,
        )
        failed_ids = _load_latest_failures(Path(output_dir))

        if not failed_ids:
            print(c.success("\n  All tests passing now! Continuing to watch...\n"))


async def _fetch_hash(url: str) -> str:
    """Fetch a URL and return a hash of the response body."""
    import hashlib
    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
            resp = await client.get(url)
            return hashlib.md5(resp.content).hexdigest()
    except Exception:
        return ""


def _load_latest_failures(report_dir: Path) -> list[str]:
    """Find the most recent failures file and load the IDs."""
    failure_files = sorted(report_dir.glob("**/failures.txt"), reverse=True)
    if not failure_files:
        return []
    return [
        line.strip()
        for line in failure_files[0].read_text().splitlines()
        if line.strip()
    ]
