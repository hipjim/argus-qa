# Scripts: run without AI, heal with AI

Running every test with AI every time is slow and costs money. So when a saved suite test passes with AI, argus-qa records what the agent did as a plain Playwright script, and replays that instead.

## How recording works

While a test runs, Playwright MCP reports the code for each browser action. The agent marks where each step starts and turns each acceptance criterion into a check at the moment it confirms it. Put together, that's a script like this:

```js
// argus-qa script for TC-001: Member logs in
export default async function run(page, { step, check, v, url }) {
  await step(1, "Enter the member's credentials", async () => {
    await page.goto(url("/login"));
    await page.getByRole('textbox', { name: 'Username' }).fill(v("member.username"));
    await page.getByRole('textbox', { name: 'Password' }).fill(v("member.password"));
    await page.getByRole('button', { name: 'Login' }).click();
  });
  await check.urlContains("/secure", "Lands on the secure page");
  await check.textVisible("Secure Area", "The Secure Area is shown");
}
```

- Scripts contain **no secrets**: `v("member.password")` reads project values at replay time.
- Scripts contain **no hard-coded host**: `url("/login")` uses the run's URL, so the same script works on staging and on a preview deploy.
- A script is kept only after it has **replayed successfully once**.
- Each test replays in a fresh browser.

## Run modes

| Mode | What happens | Cost |
|------|--------------|------|
| **Auto** (default) | Replay each test's script. If it fails, or there's no script yet, the AI runs that test. If the AI passes it, the test is marked **healed** (the page changed but still works) and its script is re-recorded. If the AI fails too, it's a real failure | $0 when nothing changed |
| **Script only** | Replay scripts; any mismatch fails. Tests without a script are blocked. Fast and free, good for CI | $0 |
| **AI** | Every test runs with AI, and scripts are re-recorded | full AI cost |

A failed script is retried once before anything else. If it passes on the retry, the test is marked **flaky**.

Measured on a 6-test practice suite: recording with AI cost $0.81. **Script only took 12 seconds and cost $0.00** (2–3 seconds per test). Auto cost $0.26: scripts for the 4 passing tests, AI for the 2 with known bugs. A simulated redesign (a renamed button) was healed by the AI for $0.14 and the script re-recorded.

## Good to know

- **Editing a test makes its script outdated.** The next Auto run re-records it.
- On the suite page, each test shows `script`, `script outdated`, or `no script`. Click the badge to read the script or forget it.
- Criteria that need judgement ("the layout looks right") can't become checks, so those tests stay AI-only.
- Scripts are for saved suites. Quick tests always use AI.
- Replays use the same Playwright and Chromium as the agents (`argus-qa setup` installs both). If the replay runner can't be installed or started, Auto runs fall back to AI and only Script-only runs fail.

| Endpoint | Description |
|----------|-------------|
| `POST /projects/{slug}/suites/{suite}/run` | `"mode": "auto"` (default), `"script"`, or `"ai"` |
| `GET /projects/{slug}/suites/{suite}/scripts/{test}` | A test's recorded script (JavaScript) |
| `DELETE /projects/{slug}/suites/{suite}/scripts/{test}` | Forget it; the next Auto or AI run records a new one |
