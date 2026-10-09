# Architecture

argus-qa is a small Python package. Agents are run with the [Claude Agent SDK](https://docs.anthropic.com/en/docs/agents/agent-sdk) and drive a browser through [Playwright MCP](https://github.com/microsoft/playwright-mcp). Everything else is plain Python: parsing plans, merging results, storing projects, serving the API.

## The pipeline

```
analyze / Discover:   Explorer agent (browser) ─▶ Scaffold agent (no tools) ─▶ test plan

test / suite run:     Plan parser ─▶ script replay ─▶ Tester agent(s) (browser) ─▶ merge results ─▶ report
                                      (auto/script)    (failures and new tests)

Explore:              Bug-hunting agent (browser) ─▶ bugs ─▶ proposed regression tests
```

Some design rules that hold everywhere:

- **Agents only get the tools they need.** Browser agents get Playwright MCP tools and nothing else; testers also get argus-qa's own `report_test` tool (and recording tools when recording). Agents that only write text get no tools.
- **Counts come from the results, never from the agent's summary.** Every test in the plan gets a result; one the agent didn't report on is `blocked`.
- **Results are saved as they come in.** A tester hands in each test's result as soon as it's done, and `results.json` is rewritten each time, so a run that's cut short keeps what it finished.
- **Secrets never leave the tester.** Placeholders are filled in only for tester agents, and every printed or stored output passes through a redactor.
- **Every run has a cost limit and turn limits.**

## Code map

```
argus_qa/
├── cli.py              # analyze, test, watch, serve, setup
├── agents/
│   └── orchestrator.py # runs agents, splits work across them, records scripts, writes outputs
├── prompts/            # one module per agent role: explorer, scaffold, tester, reporter,
│                       # bug_hunter, drafter, plus shared guardrails
├── plan_parser.py      # Markdown plan ⇄ TestPlan / TestCase, and ⇄ editor fields
├── results.py          # merging, causes and evidence, reports, JUnit
├── project.py          # projects, ${ENV} expansion, {{placeholders}}, redaction
├── scripts.py          # recording agent actions into Playwright scripts, replaying them
├── runner/replay.mjs   # the Node script runner
├── schedules.py        # schedule storage and next-firing calculation
├── server.py           # FastAPI app: run queue, projects, suites, schedules, API
├── events.py           # live activity feed for the web UI
├── colors.py           # terminal colors (respects NO_COLOR)
└── web/                # the web UI: plain HTML/CSS/JS, no build step
```

## Tests

```bash
pip install -e '.[dev]'
pytest -q && ruff check .
```

The tests cover the plan parser, result merging and JUnit output, script recording, schedules, and the HTTP API. Server tests replace the agent run with a fake, so no browser or model calls are made.

[`CLAUDE.md`](../CLAUDE.md) has a denser description of the internals, written for coding agents working on this repo.
