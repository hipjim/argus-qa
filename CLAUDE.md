# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

AI-powered UI testing tool built on the Claude Agent SDK and Playwright MCP. AI agents explore web apps in a real browser and execute test plans written in plain Markdown. Three commands: `analyze` (explore + generate test plan), `test` (execute test plan), `watch` (re-run failures on app changes).

## Setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e .
```

Requires Python 3.11+, Node.js 18+ (for Playwright MCP via npx), and Claude Code CLI authenticated.

## Running

```bash
ui-tester analyze https://example.com -u user -p pass    # explore and generate testplan.md
ui-tester test testplan.md                                # run tests
ui-tester test testplan.md --only TC-001,TC-002           # run specific tests
ui-tester test testplan.md --parallel 3                   # parallel agents
ui-tester watch testplan.md --interval 15                 # watch mode
```

## Architecture

The pipeline has two modes, both orchestrated in `ui_tester/agents/orchestrator.py`:

**Analyze mode:** Explorer agent (browser) -> Scaffold generator (reasoning-only) -> writes `testplan.md`

**Test mode:** Plan parser -> Tester agent(s) (browser) -> Reporter agent (reasoning-only) -> writes report

Key design decisions:
- Agents are invoked via `claude_agent_sdk.query()` which streams `AssistantMessage` and `ResultMessage` objects. The orchestrator iterates these async and prints colorized output.
- Browser agents get Playwright MCP tools (`mcp__playwright__*`). Reasoning-only agents (scaffold, reporter) get no tools.
- Parallel execution gives each agent its own isolated headless browser via separate `--user-data-dir` and `--headless --isolated` flags. Sequential mode uses a headed browser.
- Agent definitions in `agents/definitions.py` are passed as `agents` kwarg to `ClaudeAgentOptions` (swarm-style sub-agents the main agent can delegate to).

**Prompt templates** (`prompts/`) use `str.format()` with named placeholders (double-brace `{{` for literal braces in JSON examples). Each prompt defines the agent's role, instructions, and expected JSON output format.

**Test plan parser** (`plan_parser.py`): Parses Markdown into `TestPlan` dataclass with `TestCase` list. Test cases are identified by `### TC-NNN: Name` headers. Supports `filter(only, skip)` and `split_chunks(n)` for parallel execution.

**Colors** (`colors.py`): ANSI terminal colors with `NO_COLOR` env var support. Each parallel agent gets a unique color via `agent_color()`.

## Test plan format

Test plans are Markdown with a `> URL:` blockquote line, optional credentials table, and test cases as `### TC-NNN: Title` sections containing Priority, Category, Preconditions, Steps, and Expected result.

## Dependencies

Only two runtime dependencies: `claude-agent-sdk` and `httpx` (used in watch mode for polling/change detection via content hashing).
