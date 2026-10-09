<h1 align="center">argus-qa</h1>

<p align="center">
  <strong>Write UI tests in plain English. AI agents run them in a real browser.</strong>
</p>

<p align="center">
  <a href="LICENSE"><img alt="License: MIT" src="https://img.shields.io/badge/license-MIT-blue.svg"></a>
  <img alt="Python 3.11+" src="https://img.shields.io/badge/python-3.11%2B-blue.svg">
  <img alt="Status: alpha" src="https://img.shields.io/badge/status-alpha-orange.svg">
</p>

<p align="center">
  <a href="#quick-start">Quick start</a> ·
  <a href="#how-it-works">How it works</a> ·
  <a href="#the-web-ui">Web UI</a> ·
  <a href="#use-it-in-ci">CI</a> ·
  <a href="docs/">Docs</a>
</p>

---

argus-qa is a QA tester that never gets bored. Point it at your web app and it will:

- **Explore** the app the way a new user would, and write a test plan for you.
- **Run** that plan in a real browser: clicking, typing, and checking each expected result, with a screenshot of every step.
- **Report** what passed, what broke, and *why*: a real bug, a test that's out of date, or a flaky environment.

Tests are plain Markdown. There are no selectors to maintain and no test code to write:

```markdown
### TC-004: Customer applies a discount code

**Steps:**
1. Log in as {{customer.username}}
2. Add any product to the cart and open checkout
3. Enter the discount code `WELCOME10`

**Expected result:**
- The order total drops by 10%
- The code is shown under "Applied discounts"
```

Built on the [Claude Agent SDK](https://docs.anthropic.com/en/docs/agents/agent-sdk) and [Playwright MCP](https://github.com/microsoft/playwright-mcp).

## Why argus-qa?

| | Classic E2E tests | argus-qa |
|---|---|---|
| **Writing a test** | Code, selectors, waits | A few sentences anyone on the team can write |
| **When the UI changes** | Tests break, someone fixes selectors | The agent adapts. If the test's wording is now wrong, it proposes the fix |
| **When a test fails** | A stack trace | A short explanation, screenshots, console errors, and a suggested next step |
| **Cost per run** | Free | Free once a test is recorded as a script ([see below](#ai-when-it-helps-scripts-when-it-doesnt)), a few cents to dollars with AI |

argus-qa doesn't replace your unit tests. It's for the checks a person would otherwise click through before a release.

## Quick start

**You need:** Python 3.11+, Node.js 18+, and [Claude Code](https://docs.anthropic.com/en/docs/claude-code) installed and logged in. argus-qa uses that login, so there's no API key to set up locally.

```bash
git clone https://github.com/hipjim/argus-qa.git
cd argus-qa
python3 -m venv .venv && source .venv/bin/activate
pip install -e '.[server]'
argus-qa setup        # one-time: downloads the Chromium build argus-qa drives
```

Then pick how you want to work.

**From the terminal:**

```bash
argus-qa analyze https://staging.myapp.com -u admin -p secret   # explore → testplan.md
$EDITOR testplan.md                                             # review and tweak
argus-qa test testplan.md                                       # run it → reports/
```

**From the browser:**

```bash
argus-qa serve
# open http://localhost:8080
```

Create a project, let argus-qa **Discover** tests for you, save the ones you like to a suite, and run it. You can watch the agent work live.

> [!WARNING]
> Point argus-qa at a **staging** environment, not production. The agents follow safety rules (stay on your domain, never delete data, pay, or message real people), but they do click real buttons.

## How it works

```
   Discover                    Review                     Run                        Report
╭──────────────────╮      ╭──────────────╮      ╭────────────────────╮      ╭──────────────────╮
│ An agent explores│      │ You keep the │      │ Tester agents work │      │ Pass / fail per  │
│ your app and     │  ─▶  │ tests you    │  ─▶  │ through each test  │  ─▶  │ test, with cause,│
│ proposes tests   │      │ want, edit   │      │ in a real browser  │      │ screenshots, fix │
╰──────────────────╯      ╰──────────────╯      ╰────────────────────╯      ╰──────────────────╯
```

A few ideas make up the whole tool:

**Test plans** are Markdown files. Each test case is a `### TC-001: Title` section with steps and expected results. You can write them by hand, generate them with `analyze`, or edit them as forms in the web UI. → [Test plan format](docs/test-plans.md)

**Projects** hold everything about the app under test, so your tests don't have to: the URL, test accounts by role (`admin`, `customer`), test data, secrets, and standing instructions like "accept the cookie banner". Tests refer to them as `{{admin.password}}`. Secrets never appear in reports or logs. → [Projects](docs/projects.md)

**Suites** are a project's saved tests. Fill them three ways: write tests yourself (one sentence can be turned into a draft), let **Discover** map the app and propose tests, or let **Explore** hunt for bugs and turn each one into a regression test. → [Suites, Discover and Explore](docs/projects.md#suites)

**Results explain themselves.** Every failure has a cause:

| Cause | Meaning | What argus-qa offers |
|-------|---------|----------------------|
| `bug` | The app doesn't do what the test expects | Screenshots, console errors, failed requests |
| `outdated` | The app changed and the test's wording is stale | A corrected version of the test, applied with one click |
| `environment` | Login failed, server down, test data missing | A re-run once it's fixed |
| `not_reached` | The run was cancelled or hit its cost limit first | Continue with just those tests |

Each result is saved the moment a test finishes, so a run that's cut short keeps what it already did.

### AI when it helps, scripts when it doesn't

The first time a saved test passes, argus-qa records what the agent did as a plain Playwright script. Next time, the script replays in a couple of seconds for free. Only when it fails does the AI step in. If the AI then passes, the page changed but still works: the test is marked **healed** and the script is re-recorded.

```
replay script ──pass──▶ ✅ passed      ($0)
      │
     fail
      ▼
  AI runs it ──pass──▶ 🩹 healed      (script re-recorded)
      │
     fail
      ▼
  ❌ failed: a real problem, with the cause
```

On a 6-test suite, recording cost $0.81 with AI; the next replay took 12 seconds and cost nothing. → [Scripts](docs/scripts.md)

## The web UI

`argus-qa serve` starts a web app and an HTTP API on the same port.

- **Projects:** accounts, test data, secrets, instructions
- **Suites:** edit tests as fields (steps, acceptance criteria) or Markdown, drag to reorder, draft steps from a sentence
- **Runs:** watch every click and form fill live, then read step-by-step results with screenshots
- **Schedules:** run suites every night, on weekdays, or before the Friday release, with a webhook when something fails

Everything in the UI is also available over the API, documented at `/docs` on the running server. → [Server and API](docs/server.md) · [Schedules](docs/projects.md#schedules)

Run it with Docker:

```bash
docker build -t argus-qa .
docker run -p 8080:8080 \
  -e ANTHROPIC_API_KEY=sk-ant-... \
  -e ARGUS_API_KEY=choose-a-secret \
  -v argus-data:/data \
  argus-qa
```

## Use it in CI

`argus-qa test` exits `0` when all tests pass, `1` when any fail, and `2` when argus-qa itself errors. It also writes JUnit XML, which most CI systems display natively.

```yaml
# .github/workflows/ui-tests.yml
- run: argus-qa setup
- run: argus-qa test testplan.md --headless --parallel 3 --junit test-results/argus.xml
  env:
    ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
```

Or keep the tests on an argus-qa server and start a suite run from CI with one `curl`. → [CLI reference](docs/cli.md)

## What does it cost?

Every run has a cost limit (default $5), estimated at Anthropic API prices. When it's reached the agents stop, and unfinished tests are marked *not reached*. With a Claude subscription login, usage counts against your plan instead of being billed.

To keep runs cheap, testers read pages as text rather than images, reports are built from the results without an extra AI call, and recorded scripts replay for free. → [Models and cost](docs/configuration.md)

## Documentation

| | |
|---|---|
| [CLI reference](docs/cli.md) | `analyze`, `test`, `watch`, `serve` and every flag |
| [Test plan format](docs/test-plans.md) | How to write tests, with a full example |
| [Projects, suites and schedules](docs/projects.md) | Accounts, secrets, placeholders, Discover, Explore, schedules |
| [Scripts](docs/scripts.md) | Recording, replay, healing, and run modes |
| [Server and API](docs/server.md) | Web UI, HTTP endpoints, webhooks, Docker, authentication |
| [Configuration](docs/configuration.md) | Environment variables, models, and cost limits |

## Contributing

Contributions are welcome! See [CONTRIBUTING.md](CONTRIBUTING.md) for setup and guidelines. The test suite runs without a browser or model calls:

```bash
pip install -e '.[dev]'
pytest -q && ruff check .
```

## License

[MIT](LICENSE)
