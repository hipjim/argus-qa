"""CLI entry point for ui-tester."""

import argparse
import asyncio
import sys

from ui_tester import colors as c
from ui_tester.agents.orchestrator import run_analyze, run_tests, run_watch


def main():
    parser = argparse.ArgumentParser(
        prog="ui-tester",
        description="AI-powered UI testing — explore apps and run test plans like a human.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # ── analyze ──────────────────────────────────────────────────
    analyze_parser = subparsers.add_parser(
        "analyze",
        help="Explore a website and generate a test plan scaffold.",
    )
    analyze_parser.add_argument("url", help="The URL to explore.")
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
        help="Directory for screenshots (default: reports/screenshots/<timestamp>/).",
    )

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

    args = parser.parse_args()

    try:
        if args.command == "analyze":
            print(c.header(f"\n  ui-tester analyze — targeting {args.url}"))
            print(f"  {c.DIM}1.{c.RESET} Explore the app with a browser agent")
            print(f"  {c.DIM}2.{c.RESET} Generate a test plan scaffold you can edit\n")
            credentials = None
            if args.username:
                credentials = {"username": args.username, "password": args.password or ""}
            result = asyncio.run(run_analyze(args.url, args.output, credentials))
            print(c.banner(f"Done! Test plan scaffold: {result}"))
            print(f"\n  Edit it, then run:")
            print(f"  {c.BOLD}ui-tester test {result}{c.RESET}\n")

        elif args.command == "test":
            only = [s.strip() for s in args.only.split(",")] if args.only else None
            skip = [s.strip() for s in args.skip.split(",")] if args.skip else None

            n_label = ""
            if only:
                n_label = f" ({len(only)} selected)"
            if skip:
                n_label = f" (skipping {len(skip)})"
            par_label = f", {args.parallel} agents" if args.parallel > 1 else ""

            print(c.header(f"\n  ui-tester test — running {args.plan}{n_label}{par_label}"))
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
            ))
            print(c.banner(f"Done! Report: {result}"))

        elif args.command == "watch":
            print(c.header(f"\n  ui-tester watch — monitoring {args.plan}"))
            print(f"  Polling every {c.BOLD}{args.interval}s{c.RESET} for changes")
            print(f"  Will re-run failed tests when the app changes")
            print(c.dim("  Press Ctrl+C to stop.\n"))

            asyncio.run(run_watch(
                args.plan,
                url=args.url,
                output_dir=args.output,
                interval=args.interval,
                parallel=args.parallel,
            ))

    except FileNotFoundError as e:
        print(c.error(f"\n  Error: {e}"), file=sys.stderr)
        sys.exit(1)
    except ValueError as e:
        print(c.error(f"\n  Error: {e}"), file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print(c.dim("\n\n  Interrupted."))
        sys.exit(1)


if __name__ == "__main__":
    main()
