# Configuration, models and cost

## Authentication with Claude

Locally, argus-qa uses your [Claude Code](https://docs.anthropic.com/en/docs/claude-code) login, so there's nothing to configure. On servers and in containers, set `ANTHROPIC_API_KEY`.

With a subscription login, usage counts against your plan instead of being billed, and the cost argus-qa shows is an estimate at API prices.

## Models

Each agent role uses a model suited to the job, set explicitly so a run behaves the same on every machine:

| Role | Default | Override |
|------|---------|----------|
| Tester (follows written steps) | `claude-sonnet-5` | `ARGUS_MODEL_TESTER` |
| Explorer (Discover and Explore sessions) | `claude-opus-5-5` | `ARGUS_MODEL_EXPLORER` |
| Writer (test plans from Discover, drafts, optional AI report) | `claude-sonnet-5` | `ARGUS_MODEL_WRITER` |

`ARGUS_MODEL` overrides every role at once. Each run records the models it used, shown on the run page.

## Keeping runs cheap

- Recorded [scripts](scripts.md) replay without AI. In Auto mode, a suite where nothing changed costs nothing.
- Testers read pages as text. Screenshots are saved as evidence but not sent to the model. The bug hunter (Explore) still sees them, so it can spot visual problems.
- One screenshot per step, plus one when a step fails.
- Testers batch routine work (fill a whole form at once) and only look at the page when they need to.
- Reports are built from the results unless you ask for an AI-written one (`--ai-report`, or `ai_report` in the API).

## Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `ANTHROPIC_API_KEY` | | API key, for servers and containers |
| `ARGUS_API_KEY` | | Require this Bearer token on the server's API |
| `ARGUS_MAX_RUN_COST` | `5` | Default cost limit (USD) for test runs |
| `ARGUS_MAX_DISCOVER_COST` | `3` | Default cost limit for Discover sessions |
| `ARGUS_MAX_EXPLORE_COST` | `2` | Default cost limit for Explore sessions |
| `ARGUS_MODEL`, `ARGUS_MODEL_<ROLE>` | see above | Model overrides |
| `ARGUS_PLAYWRIGHT_ARGS` | | Extra Playwright MCP flags, e.g. `--browser chrome` |
| `ARGUS_TESTER_SNAPSHOTS` | | `full` gives testers a page snapshot after every action (more tokens) |
| `ARGUS_RUNNER_DIR` | `~/.cache/argus-qa/runner` | Where the script replay runner is installed |
| `NO_COLOR` | | Disable colored terminal output |
