# Server and API

```bash
pip install -e '.[server]'
argus-qa serve --port 8080
```

This starts the web UI at `http://localhost:8080` and an HTTP API on the same port. In the UI you manage projects and suites, start runs, and watch the agent work live: every click, form fill and screenshot appears as it happens, followed by step-by-step results and the report.

Everything in the UI is also available over the API. Runs execute in the background in isolated headless browsers, up to `--max-concurrent` at a time, and the rest wait in a queue. Interactive API docs are at `/docs`.

## Authentication

Set `ARGUS_API_KEY` to require `Authorization: Bearer <key>` on every API request. **Always set it when anyone else can reach the server.** The UI page itself loads without the key, asks for it, and keeps it in that browser's local storage.

## Running a test

```bash
# A single plain-English scenario
curl -X POST localhost:8080/runs -H 'content-type: application/json' -d '{
  "url": "https://staging.myapp.com",
  "name": "Checkout with a saved card",
  "scenario": "1. Log in as user@myapp.com / user123!\n2. Add any product to the cart\n3. Check out with the saved card\n\nExpected: an order confirmation with an order number is shown",
  "callback_url": "https://hooks.myapp.com/argus"
}'
# → 202 {"id": "992a81d740eb", "status": "queued", ...}

# A whole plan (only, skip and parallel work too)
jq -Rs '{plan: ., only: ["TC-001", "TC-002"]}' testplan.md \
  | curl -X POST localhost:8080/runs -H 'content-type: application/json' -d @-

# A saved suite
curl -X POST localhost:8080/projects/looma/suites/smoke/run -H 'content-type: application/json' -d '{"mode": "auto"}'

# Check on it
curl localhost:8080/runs/992a81d740eb
```

A run's status is one of `queued`, `running`, `passed`, `failed`, `completed` (Discover and Explore sessions), `error`, or `cancelled`. If `callback_url` is set, the run record is POSTed there when the run finishes.

## Cost limits

Every run has a cost limit, estimated at Anthropic API prices, and the agents stop when they reach it. Each test's result is saved the moment it finishes, so a run that hits its limit, is cancelled, or is cut short by a server restart keeps the tests it finished. The rest are blocked as **not reached**, and the run page offers to continue with just those.

Pass `max_cost_usd` per run, or change the defaults with environment variables (see [Configuration](configuration.md)).

## Endpoints

### Runs

| Endpoint | Description |
|----------|-------------|
| `POST /runs` | Start a run. Body: `plan` or `scenario` (+ `name`), optional `project`, `url`, `only`, `skip`, `parallel`, `max_cost_usd`, `callback_url` |
| `GET /runs` | Recent runs, newest first. Filter with `?project=` or `?schedule=` |
| `GET /runs/{id}` | Status, summary, failed test IDs, cost |
| `POST /runs/{id}/cancel` | Cancel a queued or running run |
| `DELETE /runs/{id}` | Delete a finished run and its files (tests already saved to suites are kept) |
| `POST /runs/delete` | Delete several finished runs: `{"ids": [...]}`. Active or unknown runs are skipped |
| `POST /runs/{id}/rerun` | Start a new run of the same tests. `{"failed_only": true}` (default) re-runs just the failures, `{"only": [...]}` chosen tests, and `{"mode": "auto"}` changes a suite run's mode. Suite runs use the suite's current wording |
| `POST /runs/{id}/tests/{test}/update` | Apply the rewording the tester proposed for an outdated test to the run's suite |
| `GET /runs/{id}/events?after=N` | Live agent activity. Poll with `after` set to the returned `next` |
| `GET /runs/{id}/plan` | The plan that was run |
| `GET /runs/{id}/results` | Structured results (JSON). While a run is going, the tests finished so far, with `"partial": true` |
| `GET /runs/{id}/report` | Markdown report |
| `GET /runs/{id}/junit` | JUnit XML |
| `GET /runs/{id}/screenshots` | Screenshot filenames; fetch one with `/screenshots/{name}` |
| `GET /runs/{id}/proposed` | Test cases proposed by a Discover or Explore session |
| `POST /runs/{id}/accept` | Save proposed tests to a suite: `{"case_ids": [...], "suite": "smoke"}` or `{"suite_name": "New suite"}` |
| `GET /runs/{id}/exploration` | What a Discover session mapped: pages, flows, forms, issues noticed |

### Projects

| Endpoint | Description |
|----------|-------------|
| `POST /projects` | Create a project (`slug` is derived from `name` if omitted) |
| `GET /projects`, `GET /projects/{slug}` | Read projects (secrets masked) |
| `PUT /projects/{slug}` | Replace a project. Send `********` for a secret to keep the stored value |
| `DELETE /projects/{slug}` | Delete a project |
| `POST /projects/{slug}/discover` | Start a Discover session (optional `focus`) |
| `POST /projects/{slug}/explore` | Start an Explore session (`charter`) |

### Suites and scripts

| Endpoint | Description |
|----------|-------------|
| `GET/POST /projects/{slug}/suites` | List or create suites (`{"name": ..., "plan": "### TC-001: ..."}`) |
| `GET/PUT/DELETE /projects/{slug}/suites/{suite}` | Read, replace, or delete a suite |
| `POST /projects/{slug}/suites/{suite}/run` | Run a suite. Optional `mode`, `only`, `parallel`, `url`, `max_cost_usd` |
| `GET/DELETE /projects/{slug}/suites/{suite}/scripts/{test}` | Read or forget a test's recorded [script](scripts.md) |

### Schedules

| Endpoint | Description |
|----------|-------------|
| `GET/POST /projects/{slug}/schedules` | List or create [schedules](projects.md#schedules) |
| `GET/PUT/DELETE /projects/{slug}/schedules/{schedule}` | Read, replace (`"enabled": false` pauses it), or delete a schedule |
| `POST /projects/{slug}/schedules/{schedule}/run` | Run its suites now |

### Editing helpers

| Endpoint | Description |
|----------|-------------|
| `POST /plans/parse` | Markdown → test case fields |
| `POST /plans/render` | Test case fields → Markdown |
| `POST /drafts/test-case` | Draft steps and criteria from a sentence |
| `GET /health` | Liveness, whether an API key is required, and the default cost limits (public) |

## Docker

```bash
docker build -t argus-qa .
docker run -p 8080:8080 \
  -e ANTHROPIC_API_KEY=sk-ant-... \
  -e ARGUS_API_KEY=choose-a-secret \
  -v argus-data:/data \
  argus-qa
```

The image includes Chromium and everything Playwright MCP needs. Pass extra browser flags with `ARGUS_PLAYWRIGHT_ARGS`.
