# CLI reference

```bash
argus-qa setup                                         # one-time: install the browser
argus-qa analyze <url> [options]                       # explore and write a test plan
argus-qa test <plan.md> [options]                      # run a test plan
argus-qa watch <plan.md> [options]                     # re-run failures when the app changes
argus-qa serve [options]                               # web UI and HTTP API
```

All commands pick up an `argus.toml` [project file](projects.md) in the current directory, or take `--project path/to/file.toml`.

## `setup`

Downloads the Chromium build that argus-qa drives. Run it once per machine. argus-qa uses Playwright's bundled Chromium so it behaves the same everywhere; to use another browser, set `ARGUS_PLAYWRIGHT_ARGS`, for example `--browser chrome`.

## `analyze`: explore and generate a test plan

An agent opens a browser, logs in, visits every page, maps forms and flows, and writes a Markdown test plan you can edit.

```bash
argus-qa analyze https://example.com                                          # public site
argus-qa analyze https://staging.myapp.com -u admin -p secret -o myapp.md     # with a login
argus-qa analyze https://staging.myapp.com --focus "the checkout flow"
```

| Flag | Description |
|------|-------------|
| `-u, --username` | Username to log in with |
| `-p, --password` | Password to log in with |
| `-o, --output` | Output path (default: `testplan.md`) |
| `--focus` | What to concentrate on (default: the whole app) |
| `--max-cost USD` | Stop once the estimated cost reaches this |

The explorer stays on the app's domain, never deletes data, pays, or messages real people, and names anything it creates `argus-test …`. Still, point it at staging.

## `test`: run a test plan

Agents follow each test case step by step, compare what they see to the expected result, and take screenshots.

```bash
argus-qa test testplan.md                              # everything
argus-qa test testplan.md --only TC-001,TC-002         # chosen tests
argus-qa test testplan.md --skip TC-042,TC-043         # all but these
argus-qa test testplan.md --parallel 3                 # 3 agents, each with its own browser
argus-qa test testplan.md --headless --junit out.xml   # CI
```

| Flag | Description |
|------|-------------|
| `--url` | Override the target URL |
| `--only` | Run only these tests: `--only TC-001,TC-005` |
| `--skip` | Skip these tests |
| `--parallel N` | Split the plan across N agents, each in its own isolated headless browser |
| `--headless` | Run without a browser window |
| `--junit PATH` | Also write JUnit XML to PATH (it's always written to the run directory) |
| `--ai-report` | Have an agent write the report (costs extra). By default it's built from the results |
| `--screenshots` | Custom screenshot directory |
| `-o, --output` | Report directory (default: `reports/`) |

**Exit codes:** `0` all tests passed, `1` one or more failed or were blocked, `2` argus-qa itself errored.

Sequential runs open a visible browser so you can watch, unless you pass `--headless`. Parallel runs are always headless. Press Ctrl-C to stop a run; the tests that finished are kept.

### What a run produces

Each run gets its own directory:

```
reports/staging.myapp.com/20260331_143612/
├── report.md        # summary, bugs by severity, recommendations
├── results.json     # per-test results with cause and evidence
├── junit.xml        # for CI systems
├── failures.txt     # failed test IDs, ready for --only
└── screenshots/
```

```
Results Summary
═══════════════
  PASSED:  18 / 25  ═══════════════════════════════  72%
  FAILED:   4 / 25  ════════                         16%
  BLOCKED:  3 / 25  ══════                           12%

Bugs Found: 7
  HIGH:   2 — notification dismiss fails, event history errors
  MEDIUM: 4 — no 404 page, missing validation, confusing save UX
  LOW:    1 — missing autocomplete attributes
```

## `watch`: re-run failures when the app changes

Polls the app and re-runs failed tests when it detects a change. Once everything passes, it keeps watching for regressions.

```bash
argus-qa watch testplan.md --interval 15 --parallel 2
```

| Flag | Description |
|------|-------------|
| `--url` | Override the target URL |
| `--interval` | Seconds between checks (default: 30) |
| `--parallel N` | Number of parallel agents |
| `--headless` | Run without a browser window |
| `-o, --output` | Report directory (default: `reports/`) |

## `serve`: web UI and HTTP API

Needs the `server` extra: `pip install -e '.[server]'`. See [Server and API](server.md).

| Flag | Description |
|------|-------------|
| `--host` / `--port` | Bind address (default: `127.0.0.1:8080`) |
| `--data-dir` | Where projects, suites, runs and screenshots are stored (default: `argus-data/`) |
| `--max-concurrent` | Runs executing at once; the rest wait in a queue (default: 2) |
