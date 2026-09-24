"""CLI entry point for argus-qa."""

import argparse
import asyncio
import subprocess
import sys
from pathlib import Path

from argus_qa import colors as c
from argus_qa.agents.orchestrator import install_browser, run_analyze, run_tests, run_watch
from argus_qa.project import PROJECT_FILE, Project, find_project_file, load_project_file

# Exit codes: 0 = all tests passed, 1 = test failures, 2 = argus-qa itself errored
EXIT_OK = 0
EXIT_TEST_FAILURES = 1
EXIT_ERROR = 2


def _add_headless(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--headless", action="store_true",
        help="Run the browser without a visible window (for CI/servers).",
    )


def _add_project(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--project", default=None, metavar="PATH",
        help=f"Project file with URL, test accounts, and data (default: ./{PROJECT_FILE} if present).",
    )


def _add_max_cost(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--max-cost", type=float, default=None, metavar="USD",
        help="Stop agents once the estimated cost (at API prices) reaches this many dollars.",
    )


def _load_project(path: str | None) -> Project | None:
    """Load the project file given, or ./argus.toml if it exists; expand ${ENV} references."""
    project_path = Path(path) if path else find_project_file()
    if project_path is None:
        return None
    if not project_path.is_file():
        raise FileNotFoundError(f"Project file not found: {project_path}")
    project = load_project_file(project_path).resolve_env()
    roles = ", ".join(project.credentials) or "none"
    print(c.dim(f"  Project: {project.name} ({project_path}) — test accounts: {roles}"))
    return project


def main():
    parser = argparse.ArgumentParser(
        prog="argus-qa",
        description="AI-powered UI testing — explore apps and run test plans like a human.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # ── analyze ──────────────────────────────────────────────────
    analyze_parser = subparsers.add_parser(
        "analyze",
        help="Explore a website and generate a test plan scaffold.",
    )
    analyze_parser.add_argument("url", nargs="?", help="The URL to explore (default: the project's URL).")
    analyze_parser.add_argument(
        "-u", "--username", default=None,
        help="Username/email to log in during exploration.",
    )
    analyze_parser.add_argument(
        "-p", "--password", default=None,
        help="Password to log in during exploration.",
    )
    analyze_parser.add_argument(
        "-o", "--output", default="testplan.md",
        help="Output path for the test plan (default: testplan.md).",
    )
    analyze_parser.add_argument(
        "--focus", default="",
        help="What to concentrate on, e.g. 'the checkout flow' (default: the whole app).",
    )
    _add_headless(analyze_parser)
    _add_project(analyze_parser)
    _add_max_cost(analyze_parser)

    # ── test ─────────────────────────────────────────────────────
    test_parser = subparsers.add_parser(
        "test",
        help="Execute a test plan from a Markdown file.",
    )
    test_parser.add_argument("plan", help="Path to the Markdown test plan file.")
    test_parser.add_argument(
        "--url", default=None,
        help="Override the target URL (otherwise read from the test plan).",
    )
    test_parser.add_argument(
        "-o", "--output", default="reports",
        help="Directory for reports (default: reports/).",
    )
    test_parser.add_argument(
        "--only", default=None,
        help="Run only these test case IDs (comma-separated, e.g., TC-001,TC-005).",
    )
    test_parser.add_argument(
        "--skip", default=None,
        help="Skip these test case IDs (comma-separated, e.g., TC-003,TC-010).",
    )
    test_parser.add_argument(
        "--parallel", type=int, default=1,
        help="Number of parallel browser agents (default: 1).",
    )
    test_parser.add_argument(
        "--screenshots", default=None,
        help="Directory for screenshots (default: <run dir>/screenshots/).",
    )
    test_parser.add_argument(
        "--junit", default=None,
        help="Also write JUnit XML results to this path (always written to the run dir).",
    )
    _add_headless(test_parser)
    _add_project(test_parser)
    _add_max_cost(test_parser)

    # ── watch ────────────────────────────────────────────────────
    watch_parser = subparsers.add_parser(
        "watch",
        help="Watch for app changes and re-run failed tests automatically.",
    )
    watch_parser.add_argument("plan", help="Path to the Markdown test plan file.")
    watch_parser.add_argument(
        "--url", default=None,
        help="Override the target URL.",
    )
    watch_parser.add_argument(
        "-o", "--output", default="reports",
        help="Directory for reports (default: reports/).",
    )
    watch_parser.add_argument(
        "--interval", type=int, default=30,
        help="Polling interval in seconds (default: 30).",
    )
    watch_parser.add_argument(
        "--parallel", type=int, default=1,
        help="Number of parallel browser agents (default: 1).",
    )
    _add_headless(watch_parser)
    _add_project(watch_parser)
    _add_max_cost(watch_parser)

    # ── setup ────────────────────────────────────────────────────
    subparsers.add_parser(
        "setup",
        help="Install the browser argus-qa uses (run once per machine).",
    )

    # ── serve ────────────────────────────────────────────────────
    serve_parser = subparsers.add_parser(
        "serve",
        help="Run an HTTP API that accepts test plans/scenarios and runs them.",
    )
    serve_parser.add_argument("--host", default="127.0.0.1", help="Bind address (default: 127.0.0.1).")
    serve_parser.add_argument("--port", type=int, default=8080, help="Port (default: 8080).")
    serve_parser.add_argument(
        "--data-dir", default="argus-data",
        help="Where runs, reports, and screenshots are stored (default: argus-data/).",
    )
    serve_parser.add_argument(
        "--max-concurrent", type=int, default=2,
        help="Maximum runs executing at once; others wait in a queue (default: 2).",
    )

    args = parser.parse_args()

    try:
        if args.command == "analyze":
            project = _load_project(args.project)
            print(c.header(f"\n  argus-qa analyze — targeting {args.url or (project and project.url)}"))
            print(f"  {c.DIM}1.{c.RESET} Explore the app with a browser agent")
            print(f"  {c.DIM}2.{c.RESET} Generate a test plan scaffold you can edit\n")
            credentials = None
            if args.username:
                credentials = {"username": args.username, "password": args.password or ""}
            result = asyncio.run(run_analyze(
                args.url, args.output, credentials, headless=args.headless, project=project,
                focus=args.focus, max_cost_usd=args.max_cost,
            ))
            print(c.banner(f"Done! Test plan scaffold: {result}"))
            print("\n  Edit it, then run:")
            print(f"  {c.BOLD}argus-qa test {result}{c.RESET}\n")

        elif args.command == "test":
            project = _load_project(args.project)
            only = [s.strip() for s in args.only.split(",")] if args.only else None
            skip = [s.strip() for s in args.skip.split(",")] if args.skip else None

            n_label = ""
            if only:
                n_label = f" ({len(only)} selected)"
            if skip:
                n_label = f" (skipping {len(skip)})"
            par_label = f", {args.parallel} agents" if args.parallel > 1 else ""

            print(c.header(f"\n  argus-qa test — running {args.plan}{n_label}{par_label}"))
            print(f"  {c.DIM}1.{c.RESET} Execute test cases in the browser")
            print(f"  {c.DIM}2.{c.RESET} Capture screenshots")
            print(f"  {c.DIM}3.{c.RESET} Generate a quality report\n")

            result = asyncio.run(run_tests(
                args.plan,
                url=args.url,
                output_dir=args.output,
                only=only,
                skip=skip,
                parallel=args.parallel,
                screenshot_dir=args.screenshots,
                headless=args.headless,
                junit_file=args.junit,
                project=project,
                max_cost_usd=args.max_cost,
            ))
            print(c.banner(f"Done! Report: {result.report_file}"))
            sys.exit(EXIT_TEST_FAILURES if result.failed_ids else EXIT_OK)

        elif args.command == "watch":
            project = _load_project(args.project)
            print(c.header(f"\n  argus-qa watch — monitoring {args.plan}"))
            print(f"  Polling every {c.BOLD}{args.interval}s{c.RESET} for changes")
            print("  Will re-run failed tests when the app changes")
            print(c.dim("  Press Ctrl+C to stop.\n"))

            asyncio.run(run_watch(
                args.plan,
                url=args.url,
                output_dir=args.output,
                interval=args.interval,
                parallel=args.parallel,
                headless=args.headless,
                project=project,
                max_cost_usd=args.max_cost,
            ))

        elif args.command == "setup":
            print(c.header("\n  Installing Chromium for Playwright MCP...\n"))
            install_browser()
            print(c.success("\n  Browser installed.\n"))

        elif args.command == "serve":
            try:
                from argus_qa.server import serve
            except ImportError:
                print(c.error(
                    "\n  The server needs extra dependencies: pip install 'argus-qa[server]'"
                ), file=sys.stderr)
                sys.exit(EXIT_ERROR)
            serve(
                host=args.host,
                port=args.port,
                data_dir=args.data_dir,
                max_concurrent=args.max_concurrent,
            )

    except subprocess.CalledProcessError as e:
        print(c.error(f"\n  Error: {' '.join(e.cmd)} failed (exit {e.returncode})"), file=sys.stderr)
        sys.exit(EXIT_ERROR)
    except (FileNotFoundError, ValueError, RuntimeError) as e:
        print(c.error(f"\n  Error: {e}"), file=sys.stderr)
        sys.exit(EXIT_ERROR)
    except KeyboardInterrupt:
        print(c.dim("\n\n  Interrupted."))
        sys.exit(EXIT_ERROR)


if __name__ == "__main__":
    main()
