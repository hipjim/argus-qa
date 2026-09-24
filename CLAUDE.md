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
- Browser agents get only Playwright MCP tools (`tools=[]` disables built-ins, `allowed_tools=["mcp__playwright__*"]`). Reasoning-only agents (scaffold, reporter) get no tools.
- Playwright MCP is pinned (`PLAYWRIGHT_MCP_PACKAGE`); keep `.mcp.json` and the Dockerfile's `PLAYWRIGHT_MCP_VERSION` in sync. Screenshots land in the run dir via `--output-dir`.
- Parallel execution (and every server run) uses `--headless --isolated` so each agent has its own in-memory browser profile. Sequential CLI mode is headed unless `--headless`.
- Result counts are always computed from per-test results and reconciled against the plan (`results.py`); test cases an agent didn't report on become `blocked`. Never trust the agent's own summary numbers.
- CLI exit codes: 0 all passed, 1 test failures, 2 tool error.

**Prompt templates** (`prompts/`) use `str.format()` with named placeholders (double-brace `{{` for literal braces in JSON examples). Each prompt defines the agent's role, instructions, and expected JSON output format.

**Test plan parser** (`plan_parser.py`): Parses Markdown into `TestPlan` dataclass with `TestCase` list. Test cases are identified by `### TC-NNN: Name` headers. Supports `filter(only, skip)` and `split_chunks(n)` for parallel execution.

**Projects** (`project.py`): named config (URL, credentials by role, variables, secrets, instructions) loaded from `argus.toml` (CLI, auto-detected in cwd) or stored via the server's `/projects` API as `<data_dir>/projects/<slug>.json`. `resolve_env()` expands `${VAR}`; `substitute()` fills `{{role.username}}`/`{{name}}` placeholders in the plan for tester agents only; `Redactor` strips secret values from printed output, results.json, junit.xml and the report. The reporter gets the unsubstituted plan and redacted results, so it never sees secrets. URL precedence: explicit > project > plan.

**Runs have a kind** (`server.Run.kind`): `test` (plan, scenario, or saved suite → `execute_plan`), `discover` (`discover_app`: explorer agent + scaffold agent → `proposed.md`, `exploration.json`) and `explore` (`explore_app`: bug-hunting agent → `results.json` with bugs, and `bugs_to_cases` regression tests in `proposed.md`). Sessions finish as `completed`; `POST /runs/{id}/accept` copies proposed cases into a project suite (`SuiteStore`, `<data_dir>/suites/<project>/<suite>.json`), renumbering via `plan_parser.append_cases`/`compose_plan`. Browser-driving agents that act on their own judgement get `prompts/guardrails.py`. Every run has a cost limit (`max_budget_usd`, split across agents with `_share`) and turn limits (`*_MAX_TURNS`). Models are set per role with `model_for("tester"|"explorer"|"writer")` (defaults in `DEFAULT_MODELS`, env overrides); testers and discovery get `images=False` (`--image-responses omit`) while the bug hunter sees screenshots. Test reports come from `results.test_report` unless `ai_report=True`.

**Web UI** (`web/`): a no-build single page (hash routing, plain ES module) served by the server at `/`, with assets at `/static`. Only `/`, `/static` and `/health` are public; all data endpoints sit on an `APIRouter` that requires `ARGUS_API_KEY` when set, so the UI sends the key as a Bearer header and loads screenshots as blobs. Reports are rendered with vendored `marked` and sanitized with vendored DOMPurify (report text is model output and can quote the site under test). Live activity comes from `events.py`: the orchestrator calls `emit()`; the server installs a per-run `EventLog` (`events.jsonl`) via a context variable.

**Colors** (`colors.py`): ANSI terminal colors with `NO_COLOR` env var support. Each parallel agent gets a unique color via `agent_color()`.

## Test plan format

Test plans are Markdown with a `> URL:` blockquote line, optional credentials table, and test cases as `### TC-NNN: Title` sections containing Priority, Category, Preconditions, Steps, and Expected result.

## Dependencies

Only two runtime dependencies: `claude-agent-sdk` and `httpx` (watch-mode polling, server webhooks). The server needs the optional `[server]` extra (FastAPI + uvicorn); `argus_qa.server` is only imported by the `serve` command.

## Tests

`tests/` covers the parser, result merging/JUnit, and the HTTP API. Server tests monkeypatch `server.execute_plan` with a fake, so no browser or model calls are made.
