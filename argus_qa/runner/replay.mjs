// argus-qa script runner: replays recorded test scripts with plain Playwright, no AI.
//
// Reads a job from stdin:
//   { base_url, values, screenshot_dir, headless, no_sandbox, tests: [{ id, file }] }
// Writes { results: [{ id, status, steps, checks, error, failed_step, duration_ms }] } to stdout.
// Each test gets a fresh browser context, so tests can't depend on each other.

import { createRequire } from "node:module";
import { join } from "node:path";
import { pathToFileURL } from "node:url";

const require = createRequire(join(process.env.ARGUS_RUNNER_DIR || ".", "noop.js"));
const { chromium } = require("playwright-core");

const ACTION_TIMEOUT = 15_000;
const NAVIGATION_TIMEOUT = 30_000;
const CHECK_TIMEOUT = 8_000;

const readStdin = async () => {
  let data = "";
  for await (const chunk of process.stdin) data += chunk;
  return JSON.parse(data);
};

class CheckFailed extends Error {}

// Playwright errors carry terminal colour codes and a long call log; keep a readable summary
const summarize = (e) => {
  const text = String(e?.message || e).replace(/\u001b\[[0-9;]*m/g, "");
  const [head, ...rest] = text.split("\n").map((l) => l.trim()).filter(Boolean);
  const waiting = rest.find((l) => l.startsWith("- waiting for"));
  return waiting ? `${head} (${waiting.slice(2)})` : head;
};

async function poll(fn, timeout, describe) {
  const end = Date.now() + timeout;
  let last;
  while (Date.now() < end) {
    try {
      if (await fn()) return;
    } catch (e) {
      last = e;
    }
    await new Promise((r) => setTimeout(r, 200));
  }
  throw new CheckFailed(describe() + (last ? ` (${last.message.split("\n")[0]})` : ""));
}

async function runTest(browser, job, test) {
  const context = await browser.newContext({ viewport: { width: 1280, height: 800 } });
  const page = await context.newPage();
  page.setDefaultTimeout(ACTION_TIMEOUT);
  page.setDefaultNavigationTimeout(NAVIGATION_TIMEOUT);
  const result = { id: test.id, status: "passed", steps: [], checks: [], error: null, failed_step: null };
  const started = Date.now();
  const shot = async (name) => {
    const path = join(job.screenshot_dir, `${test.id}_${name}.png`);
    await page.screenshot({ path }).catch(() => {});
    return `${test.id}_${name}.png`;
  };

  const helpers = {
    v: (name) => {
      if (!(name in job.values)) throw new Error(`The project has no value named ${name}`);
      return job.values[name];
    },
    url: (path) => new URL(path, job.base_url).href,
    step: async (n, text, fn) => {
      const t0 = Date.now();
      try {
        await fn();
        result.steps.push({ step: n, text, status: "passed", ms: Date.now() - t0, screenshot: await shot(`step${n}`) });
      } catch (e) {
        result.steps.push({
          step: n, text, status: "failed", ms: Date.now() - t0,
          error: summarize(e), screenshot: await shot(`step${n}_fail`),
        });
        result.failed_step = n;
        throw e;
      }
    },
    check: {},
  };
  const check = async (criterion, fn) => {
    try {
      await fn();
      result.checks.push({ criterion, status: "passed" });
    } catch (e) {
      result.checks.push({ criterion, status: "failed", error: summarize(e), screenshot: await shot("check_fail") });
      throw e;
    }
  };
  helpers.check.textVisible = (text, criterion) => check(criterion, () =>
    page.getByText(text).first().waitFor({ state: "visible", timeout: CHECK_TIMEOUT })
      .catch(() => { throw new CheckFailed(`Text not visible: "${text}"`); }));
  // Case-sensitive, so "Secure Area" isn't confused with "...the secure area!"
  const exactly = (text) => new RegExp(text.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"));
  helpers.check.textHidden = (text, criterion) => check(criterion, () =>
    page.getByText(exactly(text)).first().waitFor({ state: "hidden", timeout: CHECK_TIMEOUT })
      .catch(() => { throw new CheckFailed(`Text still visible: "${text}"`); }));
  helpers.check.elementVisible = (role, name, criterion) => check(criterion, () =>
    page.getByRole(role, name ? { name } : {}).first().waitFor({ state: "visible", timeout: CHECK_TIMEOUT })
      .catch(() => { throw new CheckFailed(`No visible ${role}${name ? ` named "${name}"` : ""}`); }));
  helpers.check.urlContains = (part, criterion) => check(criterion, () =>
    poll(() => page.url().includes(part), CHECK_TIMEOUT, () => `URL ${page.url()} doesn't contain "${part}"`));
  helpers.check.fieldValue = (label, value, criterion) => check(criterion, () =>
    poll(async () => (await page.getByLabel(label).first().inputValue()) === value, CHECK_TIMEOUT,
      () => `Field "${label}" doesn't have the value "${value}"`));

  try {
    const module = await import(pathToFileURL(test.file).href + `?t=${Date.now()}`);
    await module.default(page, helpers);
    await shot("done");
  } catch (e) {
    result.status = "failed";
    result.error = summarize(e);
  } finally {
    result.duration_ms = Date.now() - started;
    await context.close().catch(() => {});
  }
  return result;
}

const job = await readStdin();
const browser = await chromium.launch({ headless: job.headless !== false, chromiumSandbox: !job.no_sandbox });
const results = [];
try {
  for (const test of job.tests) results.push(await runTest(browser, job, test));
} finally {
  await browser.close();
}
process.stdout.write(JSON.stringify({ results }));
