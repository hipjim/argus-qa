# argus-qa

AI agents that test your web app like real humans. They explore, learn, click around, and write you a report.

Built on the [Claude Agent SDK](https://docs.anthropic.com/en/docs/agents/agent-sdk) and [Playwright MCP](https://github.com/microsoft/playwright-mcp).

## How it works

```
        analyze                                test
╭────────────────────────────╮   ╭────────╮   ╭──────────────────╮
│ Explorer  →  Scaffold      │   │  You   │   │ Tester(s) run in │
│ Agent        Generator     │ → │  edit  │ → │ the browser      │ → Report
│ (browser)    (writes plan) │   │  plan  │   │ (screenshots)    │
╰────────────────────────────╯   ╰────────╯   ╰──────────────────╯
```

1. **`analyze`** — an AI agent opens a real browser, logs in with your credentials, clicks through every page, and generates a Markdown test plan
2. **You review it** — fill in credentials, tweak test cases, add your own
3. **`test`** — agents execute the test plan step by step, take screenshots, and produce a quality report with bugs, severity ratings, and a score

No test code to write. No selectors to maintain. Just plain English.

## Install

```bash
# Prerequisites
# - Python 3.11+
# - Node.js (for Playwright MCP)
# - Claude Code CLI (authenticated with your Claude subscription)

git clone https://github.com/hipjim/argus-qa.git
cd argus-qa
python3 -m venv .venv && source .venv/bin/activate
pip install -e .
```

## Quick start

```bash
# Step 1: Explore your app and generate a test plan
argus-qa analyze https://myapp.com -u admin -p admin

# Step 2: Review and edit the generated test plan
$EDITOR testplan.md

# Step 3: Run the tests
argus-qa test testplan.md
```

The report lands in `reports/`.

## Commands

### `analyze` — Explore and generate a test plan

```bash
argus-qa analyze <url> [options]
```

An AI agent opens a browser, logs in, navigates every page, maps forms and flows, and writes a Markdown test plan you can edit.

| Flag | Description |
|------|-------------|
| `-u, --username` | Username to log in during exploration |
| `-p, --password` | Password to log in during exploration |
| `-o, --output` | Output path (default: `testplan.md`) |

```bash
# Public site (no login)
argus-qa analyze https://example.com

# Authenticated app
argus-qa analyze https://staging.myapp.com -u admin -p secret -o myapp-tests.md
```

### `test` — Execute a test plan

```bash
argus-qa test <plan.md> [options]
```

Agents open a browser and follow each test case step by step — clicking, typing, navigating, and comparing actual results to expected outcomes.

| Flag | Description |
|------|-------------|
| `--url` | Override the target URL |
| `--only` | Run specific tests: `--only TC-001,TC-005` |
| `--skip` | Skip specific tests: `--skip TC-042,TC-043` |
| `--parallel N` | Run N agents in parallel, each with its own browser |
| `--screenshots` | Custom screenshot directory |
| `-o, --output` | Report directory (default: `reports/`) |

```bash
# Run everything
argus-qa test testplan.md

# Run just login tests
argus-qa test testplan.md --only TC-001,TC-002,TC-003

# Skip slow tests
argus-qa test testplan.md --skip TC-042,TC-043

# 3 parallel agents (each gets its own isolated headless browser)
argus-qa test testplan.md --parallel 3

# Re-run only failures from last run
argus-qa test testplan.md --only TC-003,TC-018,TC-034
```

### `watch` — Re-run failed tests on app changes

```bash
argus-qa watch <plan.md> [options]
```

Polls your app and re-runs failed tests automatically when it detects a change. Once all tests pass, it watches for regressions.

| Flag | Description |
|------|-------------|
| `--url` | Override the target URL |
| `--interval` | Polling interval in seconds (default: 30) |
| `--parallel N` | Number of parallel agents |
| `-o, --output` | Report directory (default: `reports/`) |

```bash
# Watch and re-test every 30 seconds
argus-qa watch testplan.md

# Faster polling with parallel agents
argus-qa watch testplan.md --interval 15 --parallel 2
```

## The test plan format

Test plans are plain Markdown files. You can write them from scratch or generate one with `analyze`. Here's the structure:

```markdown
# Test Plan: My App

> URL: https://myapp.com
> Generated: 2026-03-24

## Credentials

| Role    | Username         | Password  | Notes       |
|---------|------------------|-----------|-------------|
| Admin   | admin@myapp.com  | admin123  | Full access |
| User    | user@myapp.com   | user123   | Limited     |

## Setup

- [ ] Staging environment is running
- [ ] Test data has been seeded

## Test Cases

### TC-001: Login with valid credentials

**Priority:** critical
**Category:** functional

**Preconditions:**
- User is logged out

**Steps:**
1. Go to the login page
2. Enter `admin@myapp.com` in the email field
3. Enter `admin123` in the password field
4. Click "Sign In"

**Expected result:**
- User is redirected to the dashboard
- Welcome message is visible
```

See [`examples/testplan_example.md`](examples/testplan_example.md) for a full example.

## What you get

### Reports

Each test run produces:
- **`<timestamp>_report.md`** — full quality report with executive summary, bug list, detailed results, and a quality score
- **`<timestamp>_results.json`** — raw structured test results
- **`<timestamp>_failures.txt`** — failed test IDs, ready for `--only` re-runs
- **`screenshots/`** — screenshots captured during test execution

### Sample report output

```
Results Summary
═══════════════
  PASSED:  18 / 25  ═══════════════════════════════  72%
  FAILED:   4 / 25  ════════                         16%
  BLOCKED:  3 / 25  ══════                           12%

Quality Score: 6.0 / 10

Bugs Found: 7
  HIGH:   2 — notification dismiss fails, event history errors
  MEDIUM: 4 — no 404 page, missing validation, confusing save UX
  LOW:    1 — missing autocomplete attributes
```

## Parallel execution

When you use `--parallel N`, the test plan is split into N chunks. Each chunk runs in its own **isolated headless browser** — separate user data directory, separate process, no tab conflicts.

```bash
# 45 tests ÷ 3 agents = ~15 tests each
argus-qa test testplan.md --parallel 3
```

```
Assembling the testing squad (3 agents)...

  Bug Sniper Brenda: TC-001 .. TC-015
  Chaos Monkey Carl: TC-016 .. TC-030
  Screenshot Sally:  TC-031 .. TC-045
```

Sequential mode runs with a headed browser so you can watch. Parallel mode runs headless for speed.

## Architecture

```
argus_qa/
├── cli.py                  # Entry point — analyze, test, watch subcommands
├── plan_parser.py          # Parses Markdown test plans into structured objects
├── agents/
│   ├── definitions.py      # Explorer + Tester agent definitions
│   └── orchestrator.py     # Pipeline coordination, parallel execution
└── prompts/
    ├── explorer.py         # "Browse the app and map everything"
    ├── scaffold.py         # "Generate an editable test plan"
    ├── tester.py           # "Execute these test cases in the browser"
    └── reporter.py         # "Compile a quality report"
```

**Stack:**
- [Claude Agent SDK](https://docs.anthropic.com/en/docs/agents/agent-sdk) — orchestrates AI agents
- [Playwright MCP](https://github.com/microsoft/playwright-mcp) — browser automation via MCP
- Claude subscription — no API keys needed, uses your Claude Code auth

## Requirements

- Python 3.11+
- Node.js 18+ (for Playwright MCP)
- [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) installed and authenticated

## License

MIT
