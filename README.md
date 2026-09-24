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
pip install -e .          # or: pip install -e '.[server]' for the HTTP API
argus-qa setup            # one-time: downloads the Chromium build argus-qa drives
```

argus-qa uses Playwright's bundled Chromium, so it behaves the same on every machine. To use a different browser, set `ARGUS_PLAYWRIGHT_ARGS`, for example `--browser chrome`.

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
| `--focus` | What to concentrate on, e.g. `"the checkout flow"` (default: the whole app) |
| `--max-cost USD` | Stop once the estimated cost reaches this |

The explorer follows fixed safety rules: it stays on the app's domain, never deletes data, pays, or messages real people, and names anything it creates `argus-test …`. Still, point it at a staging environment.

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
| `--headless` | Run the browser without a window (CI/servers) |
| `--junit PATH` | Also write JUnit XML to PATH (always written to the run dir) |
| `--ai-report` | Have an agent write the report (costs extra). By default it's built from the results |
| `-o, --output` | Report directory (default: `reports/`) |

Exit codes: `0` all tests passed, `1` one or more tests failed or were blocked, `2` argus-qa itself errored. This makes `argus-qa test` usable as a CI gate:

```bash
argus-qa test testplan.md --headless --junit test-results/argus.xml
```

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
| `--headless` | Run the browser without a window |
| `-o, --output` | Report directory (default: `reports/`) |

```bash
# Watch and re-test every 30 seconds
argus-qa watch testplan.md

# Faster polling with parallel agents
argus-qa watch testplan.md --interval 15 --parallel 2
```

### `serve` — Run tests over HTTP

```bash
pip install 'argus-qa[server]'
argus-qa serve --port 8080
```

Starts the web UI and HTTP API. Open `http://localhost:8080` to manage projects, start runs, and watch the agent work live: every click, form fill, and screenshot appears as it happens, followed by step-by-step results, screenshots, and the report. Everything in the UI is also available over the API: post a test plan (or a single plain-English scenario), get back a run ID, and poll for results. Runs execute in the background in isolated headless browsers, queued up to `--max-concurrent` at a time. Interactive API docs are at `/docs`.

| Flag | Description |
|------|-------------|
| `--host` / `--port` | Bind address (default: `127.0.0.1:8080`) |
| `--data-dir` | Where runs, reports, and screenshots are stored (default: `argus-data/`) |
| `--max-concurrent` | Runs executing at once; the rest wait in a queue (default: 2) |

Set `ARGUS_API_KEY` to require `Authorization: Bearer <key>` on every API request. Always set it when the server is reachable by anyone else. The web UI page itself loads without the key and asks for it, keeping it in that browser's local storage.

```bash
# Run a single scenario
curl -X POST localhost:8080/runs -H 'content-type: application/json' -d '{
  "url": "https://staging.myapp.com",
  "name": "Checkout with a saved card",
  "scenario": "1. Log in as user@myapp.com / user123!\n2. Add any product to the cart\n3. Check out with the saved card\n\nExpected: an order confirmation with an order number is shown",
  "callback_url": "https://hooks.myapp.com/argus"
}'
# → 202 {"id": "992a81d740eb", "status": "queued", ...}

# Or a whole plan (only/skip/parallel work too)
jq -Rs '{plan: ., only: ["TC-001", "TC-002"]}' testplan.md | curl -X POST localhost:8080/runs -H 'content-type: application/json' -d @-

curl localhost:8080/runs/992a81d740eb          # status: queued | running | passed | failed | completed | error | cancelled
```

Every run has a **cost limit**, estimated at Anthropic API prices. Agents stop when they reach it, and tests they didn't finish are marked blocked. Pass `max_cost_usd` per run, or set the server defaults with `ARGUS_MAX_RUN_COST` (default $5), `ARGUS_MAX_DISCOVER_COST` ($3) and `ARGUS_MAX_EXPLORE_COST` ($2). With a Claude subscription login rather than an API key, usage counts against your plan instead of being billed, and the cost is only an estimate.

| Endpoint | Description |
|----------|-------------|
| `POST /runs` | Start a run. Body: `plan` or `scenario` (+ `name`), optional `url`, `only`, `skip`, `parallel`, `callback_url` |
| `GET /runs` | Recent runs, newest first |
| `GET /runs/{id}` | Run status, summary, failed test IDs, cost |
| `POST /runs/{id}/cancel` | Cancel a queued or running run |
| `DELETE /runs/{id}` | Delete a finished run and its files (tests already saved to suites are kept) |
| `POST /runs/delete` | Delete several finished runs: `{"ids": [...]}`; active or unknown runs are skipped |
| `POST /runs/{id}/rerun` | Start a new run of the same plan; `{"failed_only": true}` (default) re-runs just the failures |
| `GET /runs/{id}/events?after=N` | Live agent activity (actions, narration, phases). Poll with `after` set to the returned `next` |
| `GET /runs/{id}/plan` | The plan that was run |
| `GET /runs/{id}/results` | Full structured results (JSON) |
| `GET /runs/{id}/report` | Markdown quality report |
| `GET /runs/{id}/junit` | JUnit XML |
| `GET /runs/{id}/screenshots` | Screenshot filenames; fetch one with `/screenshots/{name}` |
| `GET /runs/{id}/proposed` | Test cases proposed by a discover or explore run |
| `POST /runs/{id}/accept` | Save proposed tests to a suite: `{"case_ids": [...], "suite": "smoke"}` or `{"suite_name": "New suite"}` |
| `GET /runs/{id}/exploration` | What a discover run mapped: pages, user flows, forms, issues noticed |
| `GET /health` | Liveness, whether an API key is required, and the default cost limits |

If `callback_url` is set, the run record is POSTed there when the run finishes.

#### Docker

```bash
docker build -t argus-qa .
docker run -p 8080:8080 \
  -e ANTHROPIC_API_KEY=sk-ant-... \
  -e ARGUS_API_KEY=choose-a-secret \
  -v argus-data:/data \
  argus-qa
```

The image includes Chromium and everything Playwright MCP needs. Extra browser flags can be passed with `ARGUS_PLAYWRIGHT_ARGS`.

## Projects

A project stores everything about the app under test so plans and scenarios don't have to: the target URL, test accounts by role, test data, secrets, and standing instructions for the tester.

**CLI:** put an `argus.toml` next to your plans (see [`examples/argus.toml`](examples/argus.toml)). `analyze`, `test`, and `watch` pick it up automatically, or pass `--project path/to/file.toml`.

```toml
name = "My SaaS App"
url = "https://staging.myapp.com"
instructions = "Accept the cookie banner. Never click Delete account."

[credentials.admin]
username = "admin@myapp.com"
password = "${MYAPP_ADMIN_PASSWORD}"   # read from the environment at run time

[variables]
customer_name = "Acme Corp"

[secrets]
test_card = "${MYAPP_TEST_CARD}"
```

**Server:** manage projects over the API, then reference them from runs:

```bash
curl -X POST localhost:8080/projects -H 'content-type: application/json' -d '{
  "name": "Looma",
  "url": "https://staging.looma.example",
  "credentials": {"admin": {"username": "qa@looma.example", "password": "..."}},
  "instructions": "Dismiss the cookie banner."
}'

curl -X POST localhost:8080/runs -H 'content-type: application/json' -d '{
  "project": "looma",
  "scenario": "1. Log in as admin\n2. Create an invoice for {{customer_name}}\n\nExpected: the invoice appears in the list"
}'
```

| Endpoint | Description |
|----------|-------------|
| `POST /projects` | Create a project (`slug` is derived from `name` if omitted) |
| `GET /projects`, `GET /projects/{slug}` | Read projects (secrets masked) |
| `PUT /projects/{slug}` | Replace a project. Send `********` for a secret to keep the stored value |
| `DELETE /projects/{slug}` | Delete a project |
| `GET /runs?project={slug}` | A project's runs |

**How values are used:**
- Tests can refer to accounts by role ("Log in as admin"), or use placeholders: `{{admin.username}}`, `{{admin.password}}`, `{{customer_name}}`, `{{test_card}}`. Unknown placeholders are rejected before anything runs.
- The URL is taken from `--url`/`url` in the request first, then the project, then the plan's `> URL:` line.
- Passwords and `secrets` are given to the tester agent only. They are masked in API responses and replaced with `[redacted]` in results, reports, JUnit output, and logs; the report-writing agent never sees them.
- On the server, `${VAR}` references resolve against the server's environment, and project files are stored readable only by the server's user.

## Models and cost

Each agent role uses a model suited to the job, set explicitly so a run behaves the same on every machine:

| Role | Default | Override |
|------|---------|----------|
| Tester (follows written steps) | `claude-sonnet-5` | `ARGUS_MODEL_TESTER` |
| Explorer (Discover and Explore sessions) | `claude-opus-5-5` | `ARGUS_MODEL_EXPLORER` |
| Writer (test plans from Discover; optional AI report) | `claude-sonnet-5` | `ARGUS_MODEL_WRITER` |

`ARGUS_MODEL` overrides every role at once. Each run records the models it used, shown on the run page.

Other things that keep runs cheap:
- Testers read pages as text. Screenshots are saved as evidence but not sent to the model (Playwright MCP `--image-responses omit`). The bug hunter still sees them, so it can spot visual problems.
- One screenshot per step, plus one when a step fails.
- Testers are told to batch routine work (fill a whole form at once, log in with one short script) and to look at the page only when they need to: actions don't return a page snapshot (`ARGUS_TESTER_SNAPSHOTS=full` turns that back on).
- Reports are built from the results unless you ask for an AI-written one (`--ai-report`, or `ai_report` in the API).

## Discover, explore, and saved suites

A project can hold **test suites**: saved plans you run with one click (`POST /projects/{slug}/suites/{suite}/run`). You can write them yourself, but the quickest way to get one is to let argus-qa find the tests:

- **Discover** (`POST /projects/{slug}/discover`, optional `focus`): an agent explores the app like a new user, logs in with the project's accounts, maps its pages and flows, and proposes test cases. It knows your existing suites and avoids duplicates.
- **Explore** (`POST /projects/{slug}/explore` with a `charter`): an agent hunts for bugs without a script, trying unusual input, double submits, refreshes mid-flow, and narrow windows. It reproduces each bug before reporting it, with steps and screenshots. **Each bug becomes a proposed regression test.**

**Writing tests in the web UI.** New run has two kinds of run: a **Quick test** (one test, run now, not saved) and a **Suite** (a project's saved tests; tick the ones to run). Suites are edited as fields rather than Markdown: a title, priority and category, *Before you start* (preconditions), numbered *Steps*, and *Acceptance criteria*. Enter adds the next step, Backspace on an empty one removes it, pasting a list from a ticket splits it into items, and steps and test cases can be dragged to reorder. Project values (`{{retailer.password}}`, …) insert with one click, and unknown ones are flagged. **Draft steps** turns a sentence like “Retailer creates a 10% promotion and sees it listed as active” into a first draft (Sonnet, about a cent; it sees role and value names, never their values). Markdown is still the storage format, with a Markdown tab for editing it directly and **Import Markdown…** for turning a `testplan.md` (e.g. from `argus-qa analyze`) into suite tests; `POST /plans/parse` and `POST /plans/render` convert between the two, and `POST /drafts/test-case` does the drafting.

In the web UI you review the proposals, untick the ones you don't want, and save the rest to a new or existing suite. New tests are numbered after the suite's existing ones. Both kinds of session follow the safety rules above and have their own cost limits.

| Endpoint | Description |
|----------|-------------|
| `GET/POST /projects/{slug}/suites` | List or create suites (`{"name": ..., "plan": "### TC-001: ..."}`) |
| `GET/PUT/DELETE /projects/{slug}/suites/{suite}` | Read, replace, or delete a suite |
| `POST /projects/{slug}/suites/{suite}/run` | Run a suite (`only`, `parallel`, `url`, `max_cost_usd` optional) |
| `POST /projects/{slug}/discover` | Start a discovery session |
| `POST /projects/{slug}/explore` | Start an exploratory bug hunt |

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

Each test run creates its own directory under `reports/`:

```
reports/
├── staging.myapp.com/
│   ├── 20260331_143612/
│   │   ├── report.md        # quality report with bugs, scores, and recommendations
│   │   ├── results.json     # structured test results (counts computed from per-test results)
│   │   ├── junit.xml        # JUnit XML for CI systems
│   │   ├── failures.txt     # failed test IDs, ready for --only re-runs
│   │   └── screenshots/     # screenshots captured during execution
│   └── 20260331_160045/
│       └── ...
└── example.com/
    └── 20260331_150000/
        └── ...
```

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

When you use `--parallel N`, the test plan is split into at most N chunks. Each chunk runs in its own **isolated headless browser**: a separate process with an in-memory profile, so no tab or cookie conflicts.

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

Sequential mode runs with a headed browser so you can watch (unless `--headless`). Parallel mode always runs headless.

## Architecture

```
argus_qa/
├── cli.py                  # Entry point — analyze, test, watch, serve subcommands
├── plan_parser.py          # Parses Markdown test plans into structured objects
├── results.py              # Merges agent output, reconciles against the plan, JUnit export
├── project.py              # Projects: config, ${ENV} expansion, {{placeholders}}, redaction
├── server.py               # HTTP API: queue, run, and serve results
├── events.py               # Live activity feed of what agents are doing
├── web/                    # Web UI: plain HTML/CSS/JS, no build step (marked + DOMPurify vendored)
├── agents/
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
- Claude subscription — no API keys needed locally, uses your Claude Code auth (servers/containers use `ANTHROPIC_API_KEY`)

## Requirements

- Python 3.11+
- Node.js 18+ (for Playwright MCP)
- [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) installed and authenticated

## License

MIT
