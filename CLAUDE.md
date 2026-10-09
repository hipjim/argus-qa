# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

AI-powered UI testing tool built on the Claude Agent SDK and Playwright MCP. AI agents explore web apps in a real browser and execute test plans written in plain Markdown. Four commands: `analyze` (explore + generate test plan), `test` (execute test plan), `watch` (re-run failures on app changes), `serve` (HTTP API that queues and runs posted plans/scenarios).

## Setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'
pytest -q && ruff check .
```

Requires Python 3.11+, Node.js 18+ (for Playwright MCP via npx), and Claude Code CLI authenticated.

## Running

```bash
argus-qa analyze https://example.com -u user -p pass    # explore and generate testplan.md
argus-qa test testplan.md                                # run tests
argus-qa test testplan.md --only TC-001,TC-002           # run specific tests
argus-qa test testplan.md --parallel 3                   # parallel agents
argus-qa watch testplan.md --interval 15                 # watch mode
argus-qa serve --port 8080                               # HTTP API (needs the [server] extra)
```

## Architecture

The pipeline has two modes, both orchestrated in `argus_qa/agents/orchestrator.py`:

**Analyze mode:** Explorer agent (browser) -> Scaffold generator (reasoning-only) -> writes `testplan.md`

**Test mode:** Plan parser -> Tester agent(s) (browser) -> `results.merge_results` -> Reporter agent (reasoning-only) -> writes report, results.json, junit.xml. `run_tests` (file-based, CLI) wraps `execute_plan` (plan object + run dir), which the server calls directly.

Key design decisions:
- Agents are invoked via `claude_agent_sdk.query()` which streams `AssistantMessage` and `ResultMessage` objects. The orchestrator iterates these async and prints colorized output.
- Browser agents get only Playwright MCP tools (`tools=[]` disables built-ins, `allowed_tools=["mcp__playwright__*"]`); testers also get the in-process `argus` tools (`_browser_options(tester=True)`). Reasoning-only agents (scaffold, reporter) get no tools.
- Playwright MCP is pinned (`PLAYWRIGHT_MCP_PACKAGE`); keep `.mcp.json` and the Dockerfile's `PLAYWRIGHT_MCP_VERSION` in sync. Screenshots land in the run dir via `--output-dir`.
- Parallel execution (and every server run) uses `--headless --isolated` so each agent has its own in-memory browser profile. Sequential CLI mode is headed unless `--headless`.
- Result counts are always computed from per-test results and reconciled against the plan (`results.py`); test cases an agent didn't report on become `blocked`. Never trust the agent's own summary numbers.
- Testers hand in each test's result with the in-process `argus` tool `report_test` as soon as the test is finished; their final message carries only `overall_assessment` and `environment_error`. `_run_agent` passes each call to `on_report`, and `execute_plan` rewrites `results.json` after every result (`results.partial_results`, `"partial": true`). An agent that stops early keeps what it handed in (`AgentRun.reported`, `AgentRun.stop`), and its other tests are blocked with cause `not_reached`. A run that is cancelled, crashes or is cut off by a restart is completed by `orchestrator.close_run_results` (the server's `RunManager._keep_results`, the CLI on Ctrl-C). `merge_results` still accepts per-test results in the final JSON; a handed-in result wins.
- Every failed/blocked result has a `cause` (`results.CAUSES`: `bug`, `outdated`, `environment`, `unknown`, and `not_reached`, which only the orchestrator sets) and optional `evidence` (console errors, failed requests), normalized by `results.with_cause`. The tester agent decides the cause; a script-only failure is `unknown`. A result can carry `update`: the tester's corrected steps and criteria for a test whose wording no longer matches the app. `_proposed_update` keeps only what differs and puts the test's `{{placeholders}}` back (`project.restore_placeholders`); `POST /runs/{id}/tests/{test}/update` writes it into the suite with `plan_parser.replace_case`, which leaves every other case's text (and script) alone. The run page turns each cause into an action (`nextSteps`, `proposedUpdate` in `app.js`); `POST /runs/{id}/rerun` takes `only` and `mode` and uses the suite's current plan. Failed scripts are retried once before anything else, and a pass on the retry is `flaky`.
- CLI exit codes: 0 all passed, 1 test failures, 2 tool error.

**Prompt templates** (`prompts/`) use `str.format()` with named placeholders (double-brace `{{` for literal braces in JSON examples). Each prompt defines the agent's role, instructions, and expected JSON output format.

**Test plan parser** (`plan_parser.py`): Parses Markdown into `TestPlan` dataclass with `TestCase` list. Test cases are identified by `### TC-NNN: Name` headers. Supports `filter(only, skip)` and `split_chunks(n)` for parallel execution.

**Projects** (`project.py`): named config (URL, credentials by role, variables, secrets, instructions) loaded from `argus.toml` (CLI, auto-detected in cwd) or stored via the server's `/projects` API as `<data_dir>/projects/<slug>.json`. `resolve_env()` expands `${VAR}`; `substitute()` fills `{{role.username}}`/`{{name}}` placeholders in the plan for tester agents only; `Redactor` strips secret values from printed output, results.json, junit.xml and the report. The reporter gets the unsubstituted plan and redacted results, so it never sees secrets. URL precedence: explicit > project > plan.

**Runs have a kind** (`server.Run.kind`): `test` (plan, scenario, or saved suite → `execute_plan`), `discover` (`discover_app`: explorer agent + scaffold agent → `proposed.md`, `exploration.json`) and `explore` (`explore_app`: bug-hunting agent → `results.json` with bugs, and `bugs_to_cases` regression tests in `proposed.md`). Sessions finish as `completed`; `POST /runs/{id}/accept` copies proposed cases into a project suite (`SuiteStore`, `<data_dir>/suites/<project>/<suite>.json`), renumbering via `plan_parser.append_cases`/`compose_plan`. Browser-driving agents that act on their own judgement get `prompts/guardrails.py`. Every run has a cost limit (`max_budget_usd`, split across agents with `_share`) and turn limits (`*_MAX_TURNS`). Models are set per role with `model_for("tester"|"explorer"|"writer")` (defaults in `DEFAULT_MODELS`, env overrides); testers and discovery get `images=False` (`--image-responses omit`), testers also get `snapshots=False` (`--snapshot-mode none`, `ARGUS_TESTER_SNAPSHOTS`) plus the prompt's "Work efficiently" section, while the bug hunter sees screenshots. Test reports come from `results.test_report` unless `ai_report=True`.

**Schedules** (`schedules.py`, `server.Scheduler`): a project's schedule (`ScheduleStore`, `<data_dir>/schedules/<project>/<slug>.json`) runs a list of suites on chosen weekdays at a wall-clock time in a timezone (`schedules.next_fire`; no cron syntax, no extra dependency). `Scheduler.run_forever` is started by the app's lifespan and ticks every `SCHEDULE_TICK_SECONDS`; `fire` submits one run per suite through `RunManager.submit`, stamping `Run.schedule` and a shared `Run.batch`. A schedule is due at `next_fire` after the later of `updated_at` and `handled_at`; a due time more than `SCHEDULE_GRACE` old is skipped, as is one that overlaps the previous firing. Nobody is waiting on a scheduled firing, so problems are kept in `Schedule.last_error`. Tests call `scheduler.tick(now)` directly (httpx's ASGI transport doesn't run the lifespan).

**Structured editing**: the UI edits test cases as fields; `plan_parser.case_fields/render_case/plan_fields/render_plan` convert to and from Markdown (unrecognized text is kept in `notes`, existing TC IDs are preserved, new cases get the next number). `orchestrator.draft_test_case` drafts fields from a sentence with the writer model; `_draft_context` passes role and value *names* only. In `app.js`, `caseEditor()` is the reusable editor (suite page, New run scenario tab).

**Scripts** (`scripts.py`, `runner/replay.mjs`): suite runs with `record=True` add the recording tools to the tester's in-process `argus` MCP tools (`start_test`, `start_step`, `check`, next to `report_test`; built in `orchestrator._argus_tools`) and the prompt's `RECORDING_INSTRUCTIONS` (reset the browser per test, redo preconditions as step 0, call `check` when a criterion is confirmed). `Recorder` sees tool calls/results in `_run_agent`, keeps them in order (`RecordedTest.items`; checks must replay where they were verified) and takes action code from Playwright MCP's "### Ran Playwright code" blocks. `build_script` makes a JS module (project values -> `v()`, the run's origin -> `url()`, refuses on leaked secrets or `manual` checks); `execute_plan` keeps a script only if a replay passes. `execute_plan(mode=...)`: `ai`, `script`, `auto` (replay, then AI for missing/failed scripts; passes after a failed script are `healed`). The server stores scripts in `<suite>.scripts/<TC>.mjs` + `.json` (`case_hash` of the test's wording; a changed test is `outdated`). The runner needs `playwright-core` in `~/.cache/argus-qa/runner` (`ensure_runner`, `ARGUS_RUNNER_DIR`). A runner that can't be installed or started raises `RunnerError`: `auto` runs go to the AI, recording is skipped, and only `script` mode fails. Replay output is redacted as it arrives, before `script_result` shortens evidence.

**Web UI** (`web/`): a no-build single page (hash routing, plain ES module) served by the server at `/`, with assets at `/static`. Only `/`, `/static` and `/health` are public; all data endpoints sit on an `APIRouter` that requires `ARGUS_API_KEY` when set, so the UI sends the key as a Bearer header and loads screenshots as blobs. Reports are rendered with vendored `marked` and sanitized with vendored DOMPurify (report text is model output and can quote the site under test). Live activity comes from `events.py`: the orchestrator calls `emit()`; the server installs a per-run `EventLog` (`events.jsonl`) via a context variable.

**Colors** (`colors.py`): ANSI terminal colors with `NO_COLOR` env var support. Each parallel agent gets a unique color via `agent_color()`.

## Test plan format

Test plans are Markdown with a `> URL:` blockquote line, optional credentials table, and test cases as `### TC-NNN: Title` sections containing Priority, Category, Preconditions, Steps, and Expected result.

## Dependencies

Only two runtime dependencies: `claude-agent-sdk` and `httpx` (watch-mode polling, server webhooks). The server needs the optional `[server]` extra (FastAPI + uvicorn); `argus_qa.server` is only imported by the `serve` command.

## Tests

`tests/` covers the parser, result merging/JUnit, and the HTTP API. Server tests monkeypatch `server.execute_plan` with a fake, so no browser or model calls are made.
