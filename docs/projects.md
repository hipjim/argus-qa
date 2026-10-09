# Projects, suites and schedules

## Projects

A project holds everything about the app under test, so plans and scenarios don't have to: the URL, test accounts by role, test data, secrets, and standing instructions for the tester.

### In the CLI: `argus.toml`

Put an `argus.toml` next to your plans (see [`examples/argus.toml`](../examples/argus.toml)). `analyze`, `test` and `watch` pick it up automatically, or pass `--project path/to/file.toml`.

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

### On the server

Create projects in the web UI, or over the API:

```bash
curl -X POST localhost:8080/projects -H 'content-type: application/json' -d '{
  "name": "Looma",
  "url": "https://staging.looma.example",
  "credentials": {"admin": {"username": "qa@looma.example", "password": "..."}},
  "instructions": "Dismiss the cookie banner."
}'
```

### How values are used

- Tests can refer to accounts by role ("Log in as admin"), or use placeholders: `{{admin.username}}`, `{{admin.password}}`, `{{customer_name}}`, `{{test_card}}`. Unknown placeholders are rejected before anything runs.
- The URL comes from `--url` (or `url` in the request) first, then the project, then the plan's `> URL:` line.
- Passwords and `secrets` are given only to the tester agent. They're masked in API responses and replaced with `[redacted]` in results, reports, JUnit output and logs. The agent that writes reports never sees them.
- On the server, `${VAR}` references resolve against the server's environment, and project files are readable only by the server's user.

## Suites

A suite is a project's saved set of tests that you run with one click. There are three ways to fill one.

### Write tests yourself

In the web UI, tests are edited as fields: a title, priority and category, *Before you start* (preconditions), numbered *Steps*, and *Acceptance criteria*.

- Enter adds the next step, and Backspace on an empty one removes it.
- Pasting a list from a ticket splits it into steps.
- Steps and tests can be dragged to reorder.
- Project values (`{{retailer.password}}`, …) insert with one click, and unknown ones are flagged.
- **Draft steps** turns a sentence like "Retailer creates a 10% promotion and sees it listed as active" into a first draft. It costs about a cent, and the model sees role and value *names*, never the values.

There's also a Markdown tab, and **Import Markdown…** turns a `testplan.md` (for example from `argus-qa analyze`) into suite tests.

### Discover

An agent explores the app like a new user: it logs in with the project's accounts, maps the pages and flows, and proposes test cases. It knows your existing suites and avoids duplicates. Give it a `focus` ("the billing pages") to narrow it down.

### Explore

An agent hunts for bugs without a script, trying unusual input, double submits, refreshing mid-flow, and narrow windows. It reproduces each bug before reporting it, with steps and screenshots, and **each bug becomes a proposed regression test.**

For both Discover and Explore, you review the proposals, untick the ones you don't want, and save the rest to a new or existing suite. Both sessions follow the same safety rules as `analyze` and have their own cost limits.

### When a test is out of date

If the app changed and a test's wording no longer matches it (a button was renamed, a step moved), the tester marks the failure `outdated` and proposes corrected steps. The run page shows the proposal next to the original, and one click writes it into the suite without touching any other test.

## Schedules

A project can run its suites on a timer: every night, on weekdays, or on Friday evening. Create one on the project's **Schedules** tab, or with the API:

```bash
curl -X POST localhost:8080/projects/looma/schedules -H 'content-type: application/json' -d '{
  "name": "Nightly",
  "suites": ["smoke", "checkout"],
  "days": [],
  "time": "02:00",
  "timezone": "Europe/Bucharest",
  "callback_url": "https://hooks.myapp.com/argus",
  "notify_on": "failure"
}'
```

- `days` is a list of `mon`…`sun`; leave it empty for every day. `time` is `HH:MM` in `timezone` (an IANA name, default `UTC`).
- Each suite gets its own run. Runs started together share a `batch` ID and carry the schedule's slug (`GET /runs?schedule=nightly`).
- `mode` defaults to `auto`, so recorded [scripts](scripts.md) replay without AI and the AI only steps in for tests that fail. `max_cost_usd` is the limit for each suite's run, so one firing can cost up to that times the number of suites.
- `notify_on: "failure"` calls `callback_url` only for runs that don't pass.
- Schedules fire while the server is running. A run that came due while the server was down is started if the server is back within an hour, and skipped otherwise. If the previous firing is still running, the new one is skipped. Either way the reason is shown on the schedule (`last_error`), as is any suite that couldn't start.

The endpoints for projects, suites and schedules are listed in [Server and API](server.md#projects).
