// argus-qa web UI. Plain ES module, no build step. Talks to the same server's JSON API.

const view = document.getElementById("view");
const KEY_STORAGE = "argus.apiKey";
const ACTIVE = new Set(["queued", "running"]);
const FEED_POLL_MS = 1500;
const LIST_POLL_MS = 4000;
// Default cost limits per kind of run; replaced by the server's values at boot
const DEFAULTS = { test: 5, discover: 3, explore: 2 };

// ── Utilities ────────────────────────────────────────────────────────

const esc = (value) =>
  String(value ?? "").replace(/[&<>"']/g, (ch) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[ch]);

function getKey() {
  try { return localStorage.getItem(KEY_STORAGE) || ""; } catch { return ""; }
}
function setKey(value) {
  try { value ? localStorage.setItem(KEY_STORAGE, value) : localStorage.removeItem(KEY_STORAGE); } catch { /* private mode */ }
}

class ApiError extends Error {
  constructor(status, message) { super(message); this.status = status; }
}

async function api(path, { method = "GET", body, raw = false } = {}) {
  const headers = {};
  const key = getKey();
  if (key) headers.Authorization = `Bearer ${key}`;
  if (body !== undefined) headers["Content-Type"] = "application/json";
  const resp = await fetch(path, { method, headers, body: body === undefined ? undefined : JSON.stringify(body) });
  if (resp.status === 401) {
    askForKey();
    throw new ApiError(401, "This server needs an API key.");
  }
  if (!resp.ok) {
    let message = `${resp.status} ${resp.statusText}`;
    try {
      const data = await resp.json();
      message = formatDetail(data.detail) || message;
    } catch { /* not JSON */ }
    throw new ApiError(resp.status, message);
  }
  if (raw) return resp;
  if (resp.status === 204) return null;
  const type = resp.headers.get("content-type") || "";
  return type.includes("json") ? resp.json() : resp.text();
}

function formatDetail(detail) {
  if (!detail) return "";
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    return detail.map((d) => {
      const where = (d.loc || []).filter((p) => p !== "body").join(".");
      const msg = String(d.msg || "").replace(/^Value error, /, "");
      return where ? `${where}: ${msg}` : msg;
    }).join("\n");
  }
  return JSON.stringify(detail);
}

// Screenshots are behind the API key, so <img src> can't load them directly.
const blobCache = new Map();
async function screenshotUrl(runId, name) {
  const key = `${runId}/${name}`;
  if (!blobCache.has(key)) {
    blobCache.set(key, api(`/runs/${runId}/screenshots/${encodeURIComponent(name)}`, { raw: true })
      .then((r) => r.blob())
      .then((b) => URL.createObjectURL(b))
      .catch((e) => { blobCache.delete(key); throw e; }));
  }
  return blobCache.get(key);
}

async function hydrateImages(root, runId) {
  for (const img of root.querySelectorAll("img[data-shot]")) {
    screenshotUrl(runId, img.dataset.shot).then((url) => { img.src = url; }).catch(() => img.closest(".thumb, .ev-thumb")?.remove());
  }
}

async function download(path, filename) {
  try {
    const resp = await api(path, { raw: true });
    const url = URL.createObjectURL(await resp.blob());
    const a = Object.assign(document.createElement("a"), { href: url, download: filename });
    document.body.append(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 2000);
  } catch (e) {
    toast(e.message, true);
  }
}

function relTime(iso) {
  if (!iso) return "";
  const seconds = (Date.now() - new Date(iso).getTime()) / 1000;
  if (seconds < 45) return "just now";
  if (seconds < 3600) return `${Math.round(seconds / 60)} min ago`;
  if (seconds < 86400) return `${Math.round(seconds / 3600)} h ago`;
  return new Date(iso).toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

function duration(run) {
  if (!run.started_at) return "";
  const end = run.finished_at ? new Date(run.finished_at) : new Date();
  const s = Math.max(0, Math.round((end - new Date(run.started_at)) / 1000));
  return s < 60 ? `${s}s` : `${Math.floor(s / 60)}m ${String(s % 60).padStart(2, "0")}s`;
}

const cost = (usd) => (usd == null ? "" : `$${usd.toFixed(2)}`);

// "claude-sonnet-5" -> "Sonnet 5", "claude-opus-5-5" -> "Opus 5.5", "claude-haiku-4-5-20251001" -> "Haiku 4.5"
function modelName(id) {
  const m = /^claude-([a-z]+)-(\d+)(?:-(\d{1,2}))?(?:-\d{8})?$/.exec(id || "");
  if (!m) return id;
  return `${m[1][0].toUpperCase()}${m[1].slice(1)} ${m[2]}${m[3] ? `.${m[3]}` : ""}`;
}
const clock = (iso) => new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23" });

let toastTimer;
function toast(message, isError = false) {
  const el = document.getElementById("toast");
  el.textContent = message;
  el.classList.toggle("error", isError);
  el.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove("show"), 3200);
}

const AGENT_HUES = ["#5fc4cf", "#e0b458", "#c792ea", "#8fd18a", "#7fa7ff", "#f08c84", "#f5a97f", "#9fe0d4"];
function agentColor(name) {
  let h = 0;
  for (const ch of name || "") h = (h * 31 + ch.charCodeAt(0)) >>> 0;
  return AGENT_HUES[h % AGENT_HUES.length];
}

function statusBadge(status) {
  return `<span class="status ${esc(status)}">${esc(status)}</span>`;
}

function tallyBar(summary) {
  if (!summary || !summary.total) return "";
  const pct = (n) => `${(100 * n) / summary.total}%`;
  return `<span class="bar" aria-hidden="true">
    <i class="p" style="width:${pct(summary.passed)}"></i><i class="f" style="width:${pct(summary.failed)}"></i>
    <i class="b" style="width:${pct(summary.blocked)}"></i><i class="s" style="width:${pct(summary.skipped)}"></i>
  </span>`;
}

function tally(summary) {
  if (!summary) return "";
  const parts = [];
  if (summary.passed) parts.push(`<span class="p">${summary.passed} passed</span>`);
  if (summary.failed) parts.push(`<span class="f">${summary.failed} failed</span>`);
  if (summary.blocked) parts.push(`<span class="b">${summary.blocked} blocked</span>`);
  if (summary.skipped) parts.push(`<span class="s">${summary.skipped} skipped</span>`);
  return `<span class="tally">${parts.join("")}</span>`;
}

function renderMarkdown(md) {
  const html = window.marked.parse(md || "", { gfm: true, breaks: false });
  return window.DOMPurify.sanitize(html, { FORBID_TAGS: ["style", "form", "input"], FORBID_ATTR: ["style"] });
}

// ── Lightbox & API key dialog ────────────────────────────────────────

const lightbox = document.getElementById("lightbox");
function openLightbox(src, caption) {
  document.getElementById("lightbox-img").src = src;
  document.getElementById("lightbox-caption").textContent = caption || "";
  lightbox.showModal();
}
lightbox.addEventListener("click", (e) => { if (e.target === lightbox) lightbox.close(); });
document.addEventListener("click", (e) => {
  const img = e.target.closest(".thumb img, .ev-thumb, .prose img");
  if (img && img.src) {
    e.preventDefault();
    openLightbox(img.src, img.dataset.shot || img.alt);
  }
});

const keyDialog = document.getElementById("key-dialog");
function askForKey() {
  if (keyDialog.open) return;
  document.getElementById("key-input").value = getKey();
  keyDialog.showModal();
}
keyDialog.addEventListener("close", () => {
  if (keyDialog.returnValue === "save") {
    setKey(document.getElementById("key-input").value.trim());
    route();
  }
});
document.getElementById("key-btn").addEventListener("click", askForKey);

// ── Router ───────────────────────────────────────────────────────────

let cleanup = [];
let routeToken = 0;

function onLeave(fn) { cleanup.push(fn); }
function every(fn, ms) {
  const id = setInterval(fn, ms);
  onLeave(() => clearInterval(id));
}

function parseHash() {
  const [path, query = ""] = (location.hash.slice(1) || "/runs").split("?");
  return { parts: path.split("/").filter(Boolean), params: new URLSearchParams(query) };
}

async function route() {
  cleanup.forEach((fn) => fn());
  cleanup = [];
  const token = ++routeToken;
  const alive = () => token === routeToken;
  const { parts, params } = parseHash();

  document.querySelectorAll("[data-nav]").forEach((a) => a.classList.toggle("active", a.dataset.nav === parts[0]));
  document.body.classList.remove("live");
  document.title = "argus-qa";
  view.classList.remove("view-enter");
  void view.offsetWidth;
  view.classList.add("view-enter");

  try {
    if (parts[0] === "runs" && parts[1] === "new") await newRunView(params, alive);
    else if (parts[0] === "runs" && parts[1]) await runView(parts[1], alive);
    else if (parts[0] === "projects" && parts[2] === "suites") await suiteView(parts[1], parts[3] === "new" ? null : parts[3], alive);
    else if (parts[0] === "projects" && (parts[2] === "discover" || parts[2] === "explore")) await sessionView(parts[1], parts[2], alive);
    else if (parts[0] === "projects" && parts[1]) await projectView(parts[1] === "new" ? null : parts[1], params, alive);
    else if (parts[0] === "projects") await projectsView(alive);
    else await runsView(params, alive);
  } catch (e) {
    if (!alive()) return;
    view.innerHTML = `<div class="empty"><h2>${e.status === 404 ? "Not found" : "Something went wrong"}</h2>
      <p>${esc(e.message)}</p><a class="btn" href="#/runs">Back to runs</a></div>`;
  }
  view.focus({ preventScroll: true });
}

window.addEventListener("hashchange", route);

// ── Runs list ────────────────────────────────────────────────────────

async function runsView(params, alive) {
  const project = params.get("project") || "";
  const schedule = params.get("schedule") || "";
  const query = `/runs?limit=200${project ? `&project=${encodeURIComponent(project)}` : ""}${schedule ? `&schedule=${encodeURIComponent(schedule)}` : ""}`;
  const [runs, projects] = await Promise.all([api(query), api("/projects")]);
  if (!alive()) return;

  const options = projects.map((p) => `<option value="${esc(p.slug)}" ${p.slug === project ? "selected" : ""}>${esc(p.name)}</option>`).join("");
  view.innerHTML = `
    <div class="page-head">
      <div>
        <h1>Runs</h1>
        <p class="sub">Every test run, newest first. Click one to watch it or review the evidence.</p>
      </div>
    </div>
    <section class="overview" id="overview" aria-label="Last 7 days"></section>
    <div class="toolbar">
      <label class="sr-only" for="project-filter">Project</label>
      <select id="project-filter"><option value="">All projects</option>${options}</select>
      <span class="muted small" id="run-count"></span>
      ${schedule ? `<span class="small">Started by the schedule <b>${esc(schedule)}</b> · <a href="#/runs${project ? `?project=${encodeURIComponent(project)}` : ""}">show all</a></span>` : ""}
      <span class="spacer"></span>
      <button class="btn btn-small btn-danger" id="delete-selected" hidden></button>
    </div>
    <div id="runs-table"></div>`;

  view.querySelector("#project-filter").addEventListener("change", (e) => {
    location.hash = e.target.value ? `#/runs?project=${encodeURIComponent(e.target.value)}` : "#/runs";
  });

  const names = Object.fromEntries(projects.map((p) => [p.slug, p.name]));
  const selected = new Set();
  let current = runs;
  const syncSelection = () => {
    const ids = new Set(current.map((r) => r.id));
    [...selected].forEach((id) => { if (!ids.has(id)) selected.delete(id); });
    const btn = view.querySelector("#delete-selected");
    btn.hidden = selected.size === 0;
    btn.textContent = `Delete ${selected.size} run${selected.size === 1 ? "" : "s"}`;
    const all = view.querySelector("#select-all-runs");
    if (all) {
      const deletable = current.filter((r) => !ACTIVE.has(r.status)).length;
      all.checked = deletable > 0 && selected.size === deletable;
      all.indeterminate = selected.size > 0 && selected.size < deletable;
    }
  };
  view.querySelector("#delete-selected").addEventListener("click", async () => {
    const ids = [...selected];
    if (!confirm(`Delete ${ids.length} run${ids.length === 1 ? "" : "s"} and their screenshots and reports? Tests already saved to suites are kept.`)) return;
    try {
      const result = await api("/runs/delete", { method: "POST", body: { ids } });
      selected.clear();
      toast(`Deleted ${result.deleted.length} run${result.deleted.length === 1 ? "" : "s"}${result.skipped.length ? `, skipped ${result.skipped.length} still running` : ""}`);
      route();
    } catch (ex) {
      toast(ex.message, true);
    }
  });

  const drawOverview = (list) => {
    const target = view.querySelector("#overview");
    if (!list.length) { target.hidden = true; return; }
    target.hidden = false;
    const weekAgo = Date.now() - 7 * 86400e3;
    const week = list.filter((r) => new Date(r.created_at).getTime() >= weekAgo);
    const decided = week.filter((r) => r.status === "passed" || r.status === "failed");
    const passRate = decided.length ? Math.round((100 * decided.filter((r) => r.status === "passed").length) / decided.length) : null;
    const spend = week.reduce((sum, r) => sum + (r.cost_usd || 0), 0);
    const running = list.filter((r) => ACTIVE.has(r.status)).length;
    const recent = list.slice(0, 40).reverse();
    const stat = (value, label, cls = "") => `<div class="stat ${cls}"><b>${value}</b><span>${label}</span></div>`;
    target.innerHTML = `
      <div class="stats">
        ${stat(week.length, "Runs, last 7 days")}
        ${stat(passRate == null ? "—" : `${passRate}<small>%</small>`, "Pass rate", passRate == null ? "" : passRate >= 90 ? "good" : passRate < 70 ? "bad" : "")}
        ${stat(running, "Running now", running ? "live" : "")}
        ${stat(`$${spend.toFixed(2)}`, "Spend, last 7 days")}
      </div>
      <div class="recent">
        <span class="recent-label">Last ${recent.length} run${recent.length === 1 ? "" : "s"}</span>
        <div class="ticks">${recent.map((r) => `<a class="tick ${esc(r.status)}" href="#/runs/${esc(r.id)}"
          title="${esc(`${r.title || r.test_ids.join(", ")} · ${r.status} · ${relTime(r.created_at)}`)}"
          aria-label="${esc(`${r.title || r.id}: ${r.status}`)}"></a>`).join("")}</div>
      </div>`;
  };

  const draw = (list) => {
    current = list;
    document.body.classList.toggle("live", list.some((r) => ACTIVE.has(r.status)));
    drawOverview(list);
    view.querySelector("#run-count").textContent = list.length ? `${list.length} run${list.length === 1 ? "" : "s"}` : "";
    const target = view.querySelector("#runs-table");
    if (!list.length) {
      target.innerHTML = `<div class="empty">
        <h2>No runs yet</h2>
        <p>Describe what to check in plain English and argus-qa will run it in a real browser, with screenshots and a report.</p>
        <a class="btn btn-primary" href="#/runs/new${project ? `?project=${encodeURIComponent(project)}` : ""}">Start a run</a>
      </div>`;
      return;
    }
    target.innerHTML = `<table class="table">
      <thead><tr><th class="check"><input type="checkbox" id="select-all-runs" aria-label="Select all finished runs"></th><th>Status</th><th>Run</th><th class="hide-sm">Project</th><th>Result</th>
      <th class="hide-sm">Started</th><th class="hide-sm">Duration</th><th class="hide-sm">Cost</th></tr></thead>
      <tbody>${list.map((r) => `
        <tr data-id="${esc(r.id)}" class="${selected.has(r.id) ? "is-selected" : ""}">
          <td class="check"><input type="checkbox" value="${esc(r.id)}" ${selected.has(r.id) ? "checked" : ""}
            ${ACTIVE.has(r.status) ? 'disabled title="Cancel it first"' : ""} aria-label="Select run ${esc(r.id)}"></td>
          <td>${statusBadge(r.status)}</td>
          <td><a class="row-link" href="#/runs/${esc(r.id)}"><div class="title">${r.kind !== "test" ? `<span class="kind ${esc(r.kind)}">${r.kind === "discover" ? "Discover" : "Explore"}</span>` : ""}${r.schedule ? `<span class="kind scheduled" title="Started by the schedule ${esc(r.schedule)}">Scheduled</span>` : ""}${esc(r.kind !== "test" ? (r.brief || "Whole app") : (r.title || r.test_ids.join(", ")))}</div>
            <div class="id">${esc(r.id)} · ${esc(new URL(r.url).host)}</div></a></td>
          <td class="hide-sm">${r.project ? esc(names[r.project] || r.project) : '<span class="muted">—</span>'}</td>
          <td>${r.kind !== "test" ? `<span class="small">${sessionOutcome(r)}</span>` : r.summary ? `<div style="display:grid;gap:6px">${tallyBar(r.summary)}${tally(r.summary)}</div>`
            : `<span class="muted small">${r.test_ids.length} test${r.test_ids.length === 1 ? "" : "s"}</span>`}</td>
          <td class="num hide-sm" title="${esc(r.created_at)}">${esc(relTime(r.created_at))}</td>
          <td class="num hide-sm">${esc(duration(r))}</td>
          <td class="num hide-sm">${esc(cost(r.cost_usd))}</td>
        </tr>`).join("")}</tbody></table>`;
    target.querySelectorAll("tbody tr").forEach((tr) => tr.addEventListener("click", (e) => {
      if (!e.target.closest("a, .check")) location.hash = `#/runs/${tr.dataset.id}`;
    }));
    target.querySelectorAll("tbody .check input").forEach((box) => box.addEventListener("change", () => {
      if (box.checked) selected.add(box.value); else selected.delete(box.value);
      box.closest("tr").classList.toggle("is-selected", box.checked);
      syncSelection();
    }));
    target.querySelector("#select-all-runs").addEventListener("change", (e) => {
      list.filter((r) => !ACTIVE.has(r.status)).forEach((r) => (e.target.checked ? selected.add(r.id) : selected.delete(r.id)));
      draw(list);
    });
    syncSelection();
  };

  draw(runs);
  every(async () => {
    const fresh = await api(query).catch(() => null);
    if (fresh && alive()) draw(fresh);
  }, LIST_POLL_MS);
}

// ── Failure causes ────────────────────────────────────────────────────

// Why a test failed: [badge, explanation, one, many]
const CAUSES = {
  bug: ["App bug", "The app doesn't do what the test expects: the app needs fixing", "app bug", "app bugs"],
  outdated: ["Test needs updating", "The app works, but differently from what the test describes",
    "test needs updating", "tests need updating"],
  environment: ["Environment", "Something outside the feature got in the way: the site was down, an account or test data was missing",
    "environment problem", "environment problems"],
  unknown: ["Cause unclear", "It isn't clear whether the app or the test is at fault. A script can't tell a bug from a changed page: let the AI look",
    "unclear", "unclear"],
  not_reached: ["Not reached", "The run stopped before this test finished: it was cancelled, or reached its cost or turn limit",
    "not reached", "not reached"],
};
const causeLabel = (k, n) => CAUSES[k]?.[n === 1 ? 2 : 3] || k;

// The difference between two short lists of lines: [kind, text], kind being same | del | add
function diffLines(before, after) {
  const n = before.length;
  const m = after.length;
  const common = Array.from({ length: n + 1 }, () => new Array(m + 1).fill(0));
  for (let i = n - 1; i >= 0; i--) {
    for (let j = m - 1; j >= 0; j--) {
      common[i][j] = before[i] === after[j] ? common[i + 1][j + 1] + 1 : Math.max(common[i + 1][j], common[i][j + 1]);
    }
  }
  const out = [];
  let i = 0;
  let j = 0;
  while (i < n || j < m) {
    if (i < n && j < m && before[i] === after[j]) { out.push(["same", before[i]]); i++; j++; }
    else if (i < n && (j === m || common[i + 1][j] >= common[i][j + 1])) out.push(["del", before[i++]]);
    else out.push(["add", after[j++]]);
  }
  return out;
}

async function copyText(text, done) {
  try {
    await navigator.clipboard.writeText(text);
    toast(done);
  } catch {
    toast("Couldn't copy: the browser blocked access to the clipboard", true);
  }
}

// ── Run modes & scripts ──────────────────────────────────────────────

const RUN_MODES = [
  ["auto", "Auto", "Replay recorded scripts without AI; the AI steps in only for tests without a script or whose script fails, and re-records them."],
  ["script", "Script only", "Replay recorded scripts only: no AI, no cost. Tests without a script are blocked."],
  ["ai", "AI", "Run every test with AI, and re-record their scripts."],
];
const MODE_LABEL = Object.fromEntries(RUN_MODES.map(([k, label]) => [k, label]));

function modeSelect(name, value = "auto") {
  return `<select name="${name}" aria-label="How to run">${RUN_MODES.map(([k, label]) =>
    `<option value="${k}" ${k === value ? "selected" : ""}>${label}</option>`).join("")}</select>`;
}
const modeDescription = (k) => (RUN_MODES.find(([m]) => m === k) || [])[2] || "";

function scriptBadge(script) {
  const st = script?.status || "none";
  if (st === "ready") return `<span class="script-badge ready" title="Recorded ${esc(relTime(script.recorded_at))}; replays without AI">script</span>`;
  if (st === "outdated") return `<span class="script-badge outdated" title="The test changed after its script was recorded; the next Auto run re-records it">script outdated</span>`;
  return `<span class="script-badge none" title="No script yet; the next Auto or AI run records one">no script</span>`;
}

async function showScript(project, suite, testId) {
  let code;
  try {
    code = await api(`/projects/${encodeURIComponent(project)}/suites/${encodeURIComponent(suite)}/scripts/${encodeURIComponent(testId)}`);
  } catch (ex) {
    toast(ex.message, true);
    return;
  }
  const dialog = document.createElement("dialog");
  dialog.className = "dialog code-dialog";
  dialog.innerHTML = `<form method="dialog">
    <h2>Script for ${esc(testId)}</h2>
    <p class="muted small">Recorded from an AI run and replayed with plain Playwright. <code>v('…')</code> reads project values; <code>url('…')</code> is relative to the run's URL.</p>
    <pre class="plan-src">${esc(code)}</pre>
    <div class="dialog-actions">
      <button class="btn btn-danger" value="forget" type="submit">Forget script</button>
      <span class="spacer"></span>
      <button class="btn btn-primary" value="close">Close</button>
    </div></form>`;
  document.body.append(dialog);
  dialog.addEventListener("close", async () => {
    if (dialog.returnValue === "forget" && confirm(`Forget the script for ${testId}? The next Auto or AI run records a new one.`)) {
      await api(`/projects/${encodeURIComponent(project)}/suites/${encodeURIComponent(suite)}/scripts/${encodeURIComponent(testId)}`, { method: "DELETE" })
        .then(() => { toast("Script forgotten"); route(); }).catch((ex) => toast(ex.message, true));
    }
    dialog.remove();
  });
  dialog.showModal();
}

// ── Test case editor ─────────────────────────────────────────────────
// Edits test cases as fields (title, priority, steps, acceptance criteria…). The server
// converts to and from Markdown (/plans/parse, /plans/render), which stays the storage format.

const PRIORITIES = ["critical", "high", "medium", "low"];
const CATEGORIES = ["functional", "usability", "accessibility", "security", "performance"];
const CASE_LISTS = [
  { key: "preconditions", label: "Before you start", add: "Add precondition", placeholder: "e.g. Logged in as retailer" },
  { key: "steps", label: "Steps", add: "Add step", placeholder: "e.g. Click “New promotion”" },
  { key: "expected", label: "Acceptance criteria", add: "Add criterion", placeholder: "e.g. The promotion appears in the list as Active" },
];

const emptyCase = () => ({
  id: null, title: "", priority: "", category: "", preconditions: [], steps: [""], expected: [""], notes: "",
});

// Lines pasted from a ticket or document: "1. Open", "- Save", "[ ] Check" -> ["Open", "Save", "Check"]
function splitPasted(text) {
  return text.split(/\r?\n/)
    .map((l) => l.replace(/^\s*(?:\d+[.)]|[-*•+]|\[[ xX]\])\s*/, "").trim())
    .filter(Boolean);
}

function unknownPlaceholders(text, known) {
  return [...String(text || "").matchAll(/\{\{\s*([\w.-]+)\s*\}\}/g)].map((m) => m[1]).filter((n) => !known.has(n));
}

/**
 * Render an editor for `cases` into `container`.
 * options: single (one case, no add/remove/reorder), project (slug, for drafting),
 *          placeholders (names usable as {{name}}), onFocus(el) (last focused field, for inserting values),
 *          onChange() (after any edit)
 * Returns { cases() } giving the current state.
 */
function caseEditor(container, initial, { single = false, project = null, placeholders = [], onFocus = () => {}, onChange = () => {}, badge = () => "" } = {}) {
  let cases = initial.length ? initial.map((c) => ({ ...emptyCase(), ...c })) : [emptyCase()];
  const known = new Set(placeholders);
  let drag = null;

  const itemHtml = (list, ci, j, value) => `
    <div class="tc-item" data-list="${list.key}" data-j="${j}">
      <span class="tc-marker">${list.key === "steps" ? j + 1 : list.key === "expected" ? "✓" : "•"}</span>
      <input class="tc-input" value="${esc(value)}" placeholder="${esc(list.placeholder)}" aria-label="${esc(list.label)} ${j + 1}">
      <span class="tc-grip" draggable="true" title="Drag to reorder" aria-hidden="true">⋮⋮</span>
      <button type="button" class="tc-x" data-remove-item title="Remove">✕</button>
    </div>`;

  // View-only state per case (collapsed, revealed optional sections), never sent to the server
  const ui = new WeakMap();
  const uiOf = (c) => { if (!ui.has(c)) ui.set(c, {}); return ui.get(c); };
  const isBlank = (c) => !c.title.trim() && ![...c.preconditions, ...c.steps, ...c.expected].some((v) => v.trim());
  const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;
  const summary = (c) => `${plural(c.steps.filter((v) => v.trim()).length, "step")} · ${plural(c.expected.filter((v) => v.trim()).length, "check")}`;
  const [PRE, STEPS, EXPECTED] = CASE_LISTS;

  const listHtml = (list, c, ci) => `
    <section class="tc-list" data-list="${list.key}">
      <h4>${list.label}</h4>
      <div class="tc-items">${c[list.key].map((v, j) => itemHtml(list, ci, j, v)).join("")}</div>
      <button type="button" class="tc-add" data-add="${list.key}">+ ${list.add}</button>
    </section>`;

  const cardHtml = (c, ci) => {
    const u = uiOf(c);
    const showPre = c.preconditions.length > 0 || u.pre;
    const showNotes = Boolean(c.notes) || u.notes;
    const showDraft = u.draft || isBlank(c);
    const collapsed = u.collapsed && !single;
    return `
    <article class="tc-card ${single ? "is-single" : ""} ${collapsed ? "is-collapsed" : ""}" data-ci="${ci}">
      <header class="tc-head">
        ${single ? "" : `<span class="tc-grip tc-card-grip" draggable="true" title="Drag to reorder test cases">⋮⋮</span>
          <button type="button" class="tc-fold" data-fold aria-expanded="${!collapsed}" title="Collapse or expand">
            <svg viewBox="0 0 16 16" aria-hidden="true"><path d="M4 6l4 4 4-4"/></svg></button>`}
        <span class="case-id">${esc(c.id || "new")}</span>
        <input class="tc-title" value="${esc(c.title)}" placeholder="What does this test check?" aria-label="Test title">
        <span class="tc-summary" data-fold>${summary(c)}</span>
        ${c.id ? badge(c.id) : ""}
        <select class="tc-priority prio-${esc(c.priority || "none")}" aria-label="Priority"><option value="">priority</option>
          ${PRIORITIES.map((p) => `<option ${c.priority === p ? "selected" : ""}>${p}</option>`).join("")}</select>
        <select class="tc-category" aria-label="Category"><option value="">category</option>
          ${CATEGORIES.map((p) => `<option ${c.category === p ? "selected" : ""}>${p}</option>`).join("")}</select>
        ${single ? "" : `<button type="button" class="tc-x" data-remove-case title="Remove test case">✕</button>`}
      </header>
      <div class="tc-body">
        ${showDraft ? `<div class="tc-draft">
          <input class="tc-draft-input" placeholder="Describe the test in a sentence and let AI draft it, e.g. “Retailer creates a 10% promotion and sees it listed as active”">
          <button type="button" class="btn btn-small" data-draft>Draft steps</button>
        </div>` : ""}
        ${showPre ? listHtml(PRE, c, ci) : ""}
        <div class="tc-columns">${listHtml(STEPS, c, ci)}${listHtml(EXPECTED, c, ci)}</div>
        ${showNotes ? `<section class="tc-notes"><h4>Notes</h4>
          <textarea class="tc-notes-input" rows="2" placeholder="Anything else the tester should know">${esc(c.notes)}</textarea></section>` : ""}
        ${showPre && showNotes && showDraft ? "" : `<div class="tc-more">
          ${showPre ? "" : `<button type="button" class="tc-add" data-reveal="pre">+ Precondition</button>`}
          ${showNotes ? "" : `<button type="button" class="tc-add" data-reveal="notes">+ Notes</button>`}
          ${showDraft ? "" : `<button type="button" class="tc-add" data-reveal="draft">Redraft with AI</button>`}
        </div>`}
      </div>
    </article>`;
  };

  // quiet: a view-only redraw (folding, revealing a section) that doesn't count as an edit
  const draw = (focus, quiet = false) => {
    container.innerHTML = cases.map(cardHtml).join("")
      + (single ? "" : `<button type="button" class="btn btn-small tc-add-case" data-add-case>+ Add test case</button>`);
    container.querySelectorAll(".tc-input, .tc-title, .tc-notes-input").forEach(checkPlaceholders);
    if (focus) {
      const el = container.querySelector(focus);
      if (el) { el.focus(); el.setSelectionRange?.(el.value.length, el.value.length); }
    }
    if (!quiet) onChange();
  };

  const checkPlaceholders = (el) => {
    const missing = known.size || project ? unknownPlaceholders(el.value, known) : [];
    el.classList.toggle("warn", missing.length > 0);
    el.title = missing.length ? `Not a value in this project: ${missing.map((m) => `{{${m}}}`).join(", ")}` : "";
  };

  const where = (el) => {
    const card = el.closest(".tc-card");
    const item = el.closest(".tc-item");
    return {
      ci: card ? Number(card.dataset.ci) : -1,
      list: item?.dataset.list || el.closest(".tc-list")?.dataset.list,
      j: item ? Number(item.dataset.j) : -1,
    };
  };
  const itemSelector = (ci, list, j) => `.tc-card[data-ci="${ci}"] .tc-item[data-list="${list}"][data-j="${j}"] input`;

  // Typing updates state in place, so focus and cursor stay put
  container.addEventListener("input", (e) => {
    const { ci, list, j } = where(e.target);
    if (ci < 0) return;
    const c = cases[ci];
    if (e.target.classList.contains("tc-input")) c[list][j] = e.target.value;
    else if (e.target.classList.contains("tc-title")) c.title = e.target.value;
    else if (e.target.classList.contains("tc-notes-input")) c.notes = e.target.value;
    checkPlaceholders(e.target);
    onChange();
  });
  container.addEventListener("change", (e) => {
    const { ci } = where(e.target);
    if (e.target.classList.contains("tc-priority")) {
      cases[ci].priority = e.target.value;
      e.target.className = `tc-priority prio-${e.target.value || "none"}`;
    }
    if (e.target.classList.contains("tc-category")) cases[ci].category = e.target.value;
  });
  container.addEventListener("focusin", (e) => {
    if (e.target.matches(".tc-input, .tc-title, .tc-notes-input")) onFocus(e.target);
  });

  container.addEventListener("keydown", (e) => {
    if (!e.target.classList.contains("tc-input")) return;
    const { ci, list, j } = where(e.target);
    const items = cases[ci][list];
    if (e.key === "Enter") {
      e.preventDefault();
      const at = e.target.selectionStart ?? e.target.value.length;
      const rest = items[j].slice(at);
      items[j] = items[j].slice(0, at);
      items.splice(j + 1, 0, rest);
      draw(itemSelector(ci, list, j + 1));
      container.querySelector(itemSelector(ci, list, j + 1))?.setSelectionRange(0, 0);
    } else if (e.key === "Backspace" && e.target.value === "" && items.length > 0) {
      e.preventDefault();
      items.splice(j, 1);
      draw(j > 0 ? itemSelector(ci, list, j - 1) : `.tc-card[data-ci="${ci}"] .tc-title`);
    } else if ((e.key === "ArrowDown" || e.key === "ArrowUp") && !e.shiftKey) {
      const next = container.querySelector(itemSelector(ci, list, j + (e.key === "ArrowDown" ? 1 : -1)));
      if (next) { e.preventDefault(); next.focus(); }
    }
  });

  container.addEventListener("paste", (e) => {
    if (!e.target.classList.contains("tc-input")) return;
    const lines = splitPasted(e.clipboardData.getData("text"));
    if (lines.length < 2) return;
    e.preventDefault();
    const { ci, list, j } = where(e.target);
    const items = cases[ci][list];
    const head = items[j].trim() ? [items[j]] : [];
    items.splice(j, 1, ...head, ...lines);
    draw(itemSelector(ci, list, j + head.length + lines.length - 1));
  });

  container.addEventListener("click", async (e) => {
    const t = e.target;
    const { ci, list, j } = where(t);
    if (t.closest("[data-fold]")) {
      const u = uiOf(cases[ci]);
      u.collapsed = !u.collapsed;
      const card = t.closest(".tc-card");
      card.classList.toggle("is-collapsed", u.collapsed);
      card.querySelector(".tc-fold").setAttribute("aria-expanded", String(!u.collapsed));
      card.querySelector(".tc-summary").textContent = summary(cases[ci]);
    } else if (t.closest("[data-reveal]")) {
      const key = t.closest("[data-reveal]").dataset.reveal;
      uiOf(cases[ci])[key] = true;
      if (key === "pre" && !cases[ci].preconditions.length) cases[ci].preconditions.push("");
      const card = `.tc-card[data-ci="${ci}"]`;
      draw(key === "pre" ? itemSelector(ci, "preconditions", cases[ci].preconditions.length - 1)
        : key === "notes" ? `${card} .tc-notes-input` : `${card} .tc-draft-input`, true);
    } else if (t.closest("[data-add]")) {
      const key = t.closest("[data-add]").dataset.add;
      cases[ci][key].push("");
      draw(itemSelector(ci, key, cases[ci][key].length - 1));
    } else if (t.closest("[data-remove-item]")) {
      cases[ci][list].splice(j, 1);
      draw();
    } else if (t.closest("[data-remove-case]")) {
      const c = cases[ci];
      const filled = c.title || [...c.steps, ...c.expected, ...c.preconditions].some((v) => v.trim());
      if (filled && !confirm(`Remove “${c.title || "this test case"}”?`)) return;
      cases.splice(ci, 1);
      if (!cases.length) cases.push(emptyCase());
      draw();
    } else if (t.closest("[data-add-case]")) {
      cases.push(emptyCase());
      draw(`.tc-card[data-ci="${cases.length - 1}"] .tc-title`);
    } else if (t.closest("[data-draft]")) {
      const input = t.closest(".tc-draft").querySelector(".tc-draft-input");
      const description = input.value.trim() || cases[ci].title.trim();
      if (description.length < 3) { input.focus(); toast("Describe the test in a sentence first", true); return; }
      const c = cases[ci];
      const hasWork = [...c.steps, ...c.expected, ...c.preconditions].some((v) => v.trim());
      if (hasWork && !confirm("Replace this test's steps and acceptance criteria with a draft?")) return;
      t.disabled = true;
      t.textContent = "Drafting…";
      try {
        const res = await api("/drafts/test-case", { method: "POST", body: { description, ...(project ? { project } : {}) } });
        cases[ci] = { ...res.case, id: c.id, notes: c.notes };
        if (!cases[ci].steps.length) cases[ci].steps = [""];
        if (!cases[ci].expected.length) cases[ci].expected = [""];
        draw();
        toast(`Drafted — review and edit it (cost ${cost(res.cost_usd)})`);
      } catch (ex) {
        toast(ex.message, true);
        t.disabled = false;
        t.textContent = "Draft steps";
      }
    }
  });

  // Drag and drop: steps/criteria within their list, and whole cards
  container.addEventListener("dragstart", (e) => {
    const grip = e.target.closest(".tc-grip");
    if (!grip) return;
    const { ci, list, j } = where(grip);
    drag = grip.classList.contains("tc-card-grip") ? { card: ci } : { ci, list, j };
    e.dataTransfer.effectAllowed = "move";
    e.dataTransfer.setData("text/plain", "");
    (grip.closest(".tc-item") || grip.closest(".tc-card")).classList.add("dragging");
  });
  container.addEventListener("dragend", () => {
    drag = null;
    container.querySelectorAll(".dragging, .drop-before, .drop-after").forEach((el) => el.classList.remove("dragging", "drop-before", "drop-after"));
  });
  const dropTarget = (e) => {
    if (!drag) return null;
    const el = drag.card !== undefined ? e.target.closest(".tc-card") : e.target.closest(`.tc-card[data-ci="${drag.ci}"] .tc-item[data-list="${drag.list}"]`);
    if (!el) return null;
    const box = el.getBoundingClientRect();
    return { el, after: e.clientY > box.top + box.height / 2 };
  };
  container.addEventListener("dragover", (e) => {
    const target = dropTarget(e);
    if (!target) return;
    e.preventDefault();
    container.querySelectorAll(".drop-before, .drop-after").forEach((el) => el.classList.remove("drop-before", "drop-after"));
    target.el.classList.add(target.after ? "drop-after" : "drop-before");
  });
  container.addEventListener("drop", (e) => {
    const target = dropTarget(e);
    if (!target) return;
    e.preventDefault();
    const move = (arr, from, to) => { const [x] = arr.splice(from, 1); arr.splice(from < to ? to - 1 : to, 0, x); };
    if (drag.card !== undefined) {
      const to = Number(target.el.dataset.ci) + (target.after ? 1 : 0);
      move(cases, drag.card, to);
    } else {
      const to = Number(target.el.dataset.j) + (target.after ? 1 : 0);
      move(cases[drag.ci][drag.list], drag.j, to);
    }
    drag = null;
    draw();
  });

  draw();
  return {
    setCollapsed: (collapsed) => { cases.forEach((c) => { uiOf(c).collapsed = collapsed; }); draw(null, true); },
    reveal: (ci) => {
      const card = container.querySelector(`.tc-card[data-ci="${ci}"]`);
      if (!card) return;
      if (uiOf(cases[ci]).collapsed) card.querySelector(".tc-fold")?.click();
      card.scrollIntoView({ behavior: "smooth", block: "start" });
      card.classList.remove("flash");
      void card.offsetWidth;
      card.classList.add("flash");
    },
    replace: (next) => { cases = next.length ? next.map((c) => ({ ...emptyCase(), ...c })) : [emptyCase()]; draw(); },
    cases: () => cases.map((c) => ({ ...c, preconditions: c.preconditions.filter((v) => v.trim()),
      steps: c.steps.filter((v) => v.trim()), expected: c.expected.filter((v) => v.trim()) })),
  };
}

// Chips that insert {{name}} into whichever field was focused last
function placeholderChips(target, names, getField) {
  target.innerHTML = names.map((v) => `<button type="button" class="chip" data-insert="${esc(v)}">{{${esc(v)}}}</button>`).join("");
  target.onclick = (e) => {
    const chip = e.target.closest("[data-insert]");
    const field = getField();
    if (!chip || !field) return;
    const text = `{{${chip.dataset.insert}}}`;
    const at = field.selectionStart ?? field.value.length;
    field.value = field.value.slice(0, at) + text + field.value.slice(field.selectionEnd ?? at);
    field.dispatchEvent(new Event("input", { bubbles: true }));
    field.focus();
    field.selectionStart = field.selectionEnd = at + text.length;
  };
}

function projectPlaceholders(p) {
  return p ? [
    ...Object.keys(p.credentials).flatMap((role) => [`${role}.username`, `${role}.password`]),
    ...Object.keys(p.variables), ...Object.keys(p.secrets),
  ] : [];
}

// ── New run ──────────────────────────────────────────────────────────

async function newRunView(params, alive) {
  const projects = await api("/projects");
  if (!alive()) return;
  const preselect = params.get("project") || (projects.length === 1 ? projects[0].slug : "");

  view.innerHTML = `
    <a class="crumb" href="#/runs">← Runs</a>
    <div class="page-head"><div>
      <h1>New run</h1>
      <p class="sub">Run a quick one-off test, or one of a project's saved suites.</p>
    </div></div>
    <form class="form" id="run-form" novalidate>
      <div class="form-row">
        <label class="field"><span>Project</span>
          <select name="project">
            <option value="">No project</option>
            ${projects.map((p) => `<option value="${esc(p.slug)}" ${p.slug === preselect ? "selected" : ""}>${esc(p.name)}</option>`).join("")}
          </select>
          <span class="hint">Supplies the URL, test accounts, and test data. <a href="#/projects/new">New project</a></span>
        </label>
        <label class="field"><span>URL</span>
          <input name="url" type="url" placeholder="https://staging.example.com" autocomplete="off">
          <span class="hint" id="url-hint"></span>
        </label>
      </div>

      <div class="field">
        <span>What to test</span>
        <div class="segmented" role="group" aria-label="Input type">
          <button type="button" data-mode="quick" aria-pressed="true">Quick test</button>
          <button type="button" data-mode="suite" aria-pressed="false" id="suite-mode-btn">Saved suite</button>
        </div>
        <span class="hint" id="mode-hint">One test, run now. It isn't saved, apart from the run's results.</span>
      </div>

      <div id="mode-quick" class="tc-editor"></div>

      <div id="mode-suite-empty" class="notice" hidden></div>
      <div id="mode-suite" class="form" style="gap:14px" hidden>
        <label class="field"><span>Suite</span><select name="suite"></select>
          <span class="hint" id="suite-hint"></span></label>
        <label class="field"><span>How to run</span>${modeSelect("run_mode")}
          <span class="hint" id="run-mode-hint">${esc(modeDescription("auto"))}</span></label>
        <fieldset class="suite-picks">
          <legend class="small">Tests to run</legend>
          <label class="select-all"><input type="checkbox" id="suite-all" checked> All</label>
          <div id="suite-tests"></div>
        </fieldset>
      </div>

      <div class="field" id="placeholders" hidden>
        <span>Project values</span>
        <div class="chips" id="placeholder-chips"></div>
        <span class="hint">Click to insert. Tests can also just say “Log in as <i>role</i>”.</span>
      </div>

      <p class="form-error" id="form-error" hidden></p>
      <div class="form-actions">
        <button class="btn btn-primary" type="submit" id="submit-btn">Start run</button>
        <a class="btn btn-ghost" href="#/runs">Cancel</a>
        <span class="spacer"></span>
        <label class="inline-field">Parallel browsers
          <input name="parallel" type="number" min="1" max="8" value="1">
        </label>
        <label class="inline-field" title="An agent writes the report instead of building it from the results. Costs extra.">
          <input name="ai_report" type="checkbox" style="width:auto"> AI-written report
        </label>
        <label class="inline-field" title="Estimated at API prices. Agents stop when the run reaches this.">Cost limit $
          <input name="max_cost" type="number" min="0.1" max="100" step="0.5" value="${DEFAULTS.test}">
        </label>
      </div>
    </form>`;

  const form = view.querySelector("#run-form");
  const byName = Object.fromEntries(projects.map((p) => [p.slug, p]));
  let mode = "quick";
  let lastFocused = null;
  let quick = null;

  const buildQuickEditor = () => {
    const slug = form.elements.project.value || null;
    quick = caseEditor(view.querySelector("#mode-quick"), quick ? quick.cases() : [], {
      single: true, project: slug, placeholders: projectPlaceholders(byName[slug]),
      onFocus: (el) => { lastFocused = el; },
    });
  };

  let suites = [];
  const setMode = (next) => {
    mode = next;
    view.querySelectorAll("[data-mode]").forEach((x) => x.setAttribute("aria-pressed", String(x.dataset.mode === mode)));
    const noSuites = !suites.length;
    view.querySelector("#mode-quick").hidden = mode !== "quick";
    view.querySelector("#mode-suite").hidden = mode !== "suite" || noSuites;
    view.querySelector("#placeholders").classList.toggle("off", mode === "suite");
    view.querySelector("#mode-hint").textContent = mode === "quick"
      ? "Write one test below and run it now. It isn't saved, apart from the run's results."
      : "Run some or all of a project's saved tests.";
    // Explain why there's nothing to pick, and what to do about it
    const empty = view.querySelector("#mode-suite-empty");
    const slug = form.elements.project.value;
    empty.hidden = mode !== "suite" || !noSuites;
    empty.innerHTML = !slug
      ? "Saved suites belong to a project. Pick a project above to run one of its suites."
      : `<b>${esc(byName[slug]?.name || slug)}</b> has no saved suites yet. `
        + `<a href="#/projects/${esc(slug)}/discover">Discover tests</a> to have argus-qa propose some, or `
        + `<a href="#/projects/${esc(slug)}/suites/new">write a suite</a>.`;
    view.querySelector("#submit-btn").disabled = mode === "suite" && noSuites;
    lastFocused = null;
  };
  const drawSuite = () => {
    const suite = suites.find((st) => st.slug === form.elements.suite.value);
    if (!suite) return;
    const last = suite.last_run ? ` · last run ${suite.last_run.status} ${relTime(suite.last_run.created_at)}` : "";
    view.querySelector("#suite-hint").innerHTML = `${suite.test_count} test${suite.test_count === 1 ? "" : "s"}${esc(last)} · `
      + `<a href="#/projects/${esc(form.elements.project.value)}/suites/${esc(suite.slug)}">Edit suite</a>`;
    view.querySelector("#suite-tests").innerHTML = suite.tests.map((t) => `
      <label class="suite-test"><input type="checkbox" value="${esc(t.id)}" checked>
        <span class="case-id">${esc(t.id)}</span> ${esc(t.name)} ${scriptBadge(t.script)}</label>`).join("");
    view.querySelector("#suite-all").checked = true;
    view.querySelector("#suite-all").indeterminate = false;
  };
  const syncSuites = async () => {
    const slug = form.elements.project.value;
    suites = slug ? await api(`/projects/${encodeURIComponent(slug)}/suites`).catch(() => []) : [];
    if (!alive() || slug !== form.elements.project.value) return;
    form.elements.suite.innerHTML = suites.map((st) => `<option value="${esc(st.slug)}">${esc(st.name)} (${st.test_count} test${st.test_count === 1 ? "" : "s"})</option>`).join("");
    const preferred = params.get("suite");
    if (preferred && suites.some((st) => st.slug === preferred)) {
      form.elements.suite.value = preferred;
      setMode("suite");
    } else {
      setMode(mode);
    }
    drawSuite();
  };
  form.elements.suite.addEventListener("change", drawSuite);
  form.elements.run_mode.addEventListener("change", (e) => {
    view.querySelector("#run-mode-hint").textContent = modeDescription(e.target.value);
  });
  view.querySelector("#suite-all").addEventListener("change", (e) => {
    view.querySelectorAll("#suite-tests input").forEach((b) => { b.checked = e.target.checked; });
  });
  view.querySelector("#suite-tests").addEventListener("change", () => {
    const boxes = [...view.querySelectorAll("#suite-tests input")];
    const n = boxes.filter((b) => b.checked).length;
    view.querySelector("#suite-all").checked = n === boxes.length;
    view.querySelector("#suite-all").indeterminate = n > 0 && n < boxes.length;
  });
  const syncProject = () => {
    syncSuites();
    const p = byName[form.elements.project.value];
    form.elements.url.placeholder = p?.url || "https://staging.example.com";
    view.querySelector("#url-hint").textContent = p?.url ? "Leave empty to use the project's URL." : "";
    const values = projectPlaceholders(p);
    view.querySelector("#placeholders").hidden = !values.length;
    placeholderChips(view.querySelector("#placeholder-chips"), values, () => lastFocused);
    buildQuickEditor();
  };
  form.elements.project.addEventListener("change", syncProject);
  syncProject();

  view.querySelectorAll("[data-mode]").forEach((b) => b.addEventListener("click", () => setMode(b.dataset.mode)));

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const err = view.querySelector("#form-error");
    err.hidden = true;
    const f = form.elements;
    const body = { parallel: Number(f.parallel.value) || 1 };
    if (Number(f.max_cost.value)) body.max_cost_usd = Number(f.max_cost.value);
    if (f.ai_report.checked) body.ai_report = true;
    if (f.project.value) body.project = f.project.value;
    if (f.url.value.trim()) body.url = f.url.value.trim();
    let endpoint = "/runs";
    if (mode === "suite") {
      endpoint = `/projects/${encodeURIComponent(f.project.value)}/suites/${encodeURIComponent(f.suite.value)}/run`;
      delete body.project;
      const boxes = [...view.querySelectorAll("#suite-tests input")];
      const picked = boxes.filter((b) => b.checked).map((b) => b.value);
      if (!picked.length) {
        err.textContent = "Pick at least one test to run.";
        err.hidden = false;
        return;
      }
      if (picked.length < boxes.length) body.only = picked;
      body.mode = f.run_mode.value;
    } else {
      const [test] = quick.cases();
      if (!test.steps.length) {
        err.textContent = "Add at least one step, or describe the test in a sentence and click “Draft steps”.";
        err.hidden = false;
        return;
      }
      const title = test.title.trim() || "Quick test";
      try {
        body.plan = (await api("/plans/render", {
          method: "POST", body: { title, cases: [{ ...test, title }] },
        })).plan;
      } catch (ex) {
        err.textContent = ex.message;
        err.hidden = false;
        return;
      }
    }
    const btn = view.querySelector("#submit-btn");
    btn.disabled = true;
    btn.textContent = "Starting…";
    try {
      const run = await api(endpoint, { method: "POST", body });
      location.hash = `#/runs/${run.id}`;
    } catch (ex) {
      err.textContent = ex.message;
      err.hidden = false;
      btn.disabled = false;
      btn.textContent = "Start run";
    }
  });
  view.querySelector(form.elements.project.value ? "#mode-quick .tc-title" : 'input[name="url"]')?.focus();
}

// ── Run detail ───────────────────────────────────────────────────────

async function runView(runId, alive) {
  let run = await api(`/runs/${runId}`);
  if (!alive()) return;

  const session = run.kind !== "test";
  let results = null;
  let exploration = null;
  let proposed = null;
  let suiteChoices = null;
  let shots = [];
  let tab = run.kind === "explore" ? "bugs" : run.kind === "discover" ? "proposed" : "tests";
  let reportHtml = null;
  let planText = null;
  let planCases = null;   // test ID -> the test's fields as the run had them, for showing proposed updates
  let feedCursor = 0;
  let actionCount = 0;
  let lastAgent = "";
  let lastSignature = "";
  let lastTime = 0;       // when the feed last showed a timestamp
  let quietLine = null;   // the latest line of look-only actions, which the next ones join
  const caseNames = {};
  const agentCase = {};     // agent -> test case it's working on now
  const agentAction = {};   // agent -> its latest browser action
  const seenCases = new Set();

  view.innerHTML = `
    <a class="crumb" href="#/runs${run.project ? `?project=${encodeURIComponent(run.project)}` : ""}">← Runs</a>
    <div class="page-head">
      <div>
        <h1 id="run-title"></h1>
        <div class="run-meta" id="run-meta"></div>
      </div>
      <div class="page-head-actions" id="run-actions"></div>
    </div>
    <div id="run-banner"></div>
    <div class="scoreboard" id="scoreboard"></div>
    <div class="run-grid">
      <section aria-label="Run details">
        <div class="tabs" role="tablist" id="tabs"></div>
        <div id="tab-body"></div>
      </section>
      <aside class="feed" aria-label="Agent activity">
        <div class="feed-head"><span>Agent activity</span><span id="feed-state"></span></div>
        <div class="feed-body" id="feed" aria-live="polite"></div>
      </aside>
    </div>`;

  const $ = (sel) => view.querySelector(sel);

  // The run's failures, split into the tests it stopped before and the ones that really failed
  const failures = () => {
    const unreached = (results?.results || []).filter((r) => r.cause === "not_reached").map((r) => r.id);
    return { unreached, failed: run.failed_ids.filter((id) => !unreached.includes(id)) };
  };

  const rerun = async (body) => {
    const fresh = await api(`/runs/${runId}/rerun`, { method: "POST", body });
    location.hash = `#/runs/${fresh.id}`;
  };

  const drawHeader = () => {
    document.body.classList.toggle("live", ACTIVE.has(run.status));
    const onlyCase = results?.results?.length === 1 ? results.results[0].name : "";
    const title = run.title || onlyCase || run.test_ids.join(", ");
    $("#run-title").textContent = title;
    const mark = { running: "●", queued: "○", passed: "✓", completed: "✓", failed: "✗", error: "!" }[run.status] || "";
    document.title = `${mark} ${run.status === "running" ? "Running" : run.status[0].toUpperCase() + run.status.slice(1)} · ${title} — argus-qa`.trim();
    $("#run-meta").innerHTML = [
      session ? `<span class="kind ${esc(run.kind)}">${run.kind === "discover" ? "Discover" : "Explore"}</span>` : "",
      run.suite ? `<span class="kind mode-${esc(run.mode)}" title="${esc(modeDescription(run.mode))}">${esc(MODE_LABEL[run.mode] || run.mode)}</span>` : "",
      statusBadge(run.status),
      run.project ? `<a href="#/projects/${esc(run.project)}">${esc(run.project)}</a>` : "",
      `<a class="mono" href="${esc(run.url)}" target="_blank" rel="noopener">${esc(run.url)}</a>`,
      `<span title="${esc(run.created_at)}">${esc(relTime(run.created_at))}</span>`,
      run.started_at ? `<span class="mono">${esc(duration(run))}</span>` : "",
      run.cost_usd != null ? `<span class="mono">${esc(cost(run.cost_usd))}</span>` : "",
      run.models?.length ? `<span class="models" title="${esc(run.models.join(", "))}">${esc(run.models.map(modelName).join(" + "))}</span>` : "",
      `<span class="mono muted">${esc(run.id)}</span>`,
    ].filter(Boolean).join("");

    const actions = [];
    if (ACTIVE.has(run.status)) actions.push(`<button class="btn btn-danger" data-act="cancel">Cancel run</button>`);
    if (!ACTIVE.has(run.status) && !session) {
      // The most useful next step comes first and is the highlighted one
      const { unreached, failed } = failures();
      const next = [];
      if (unreached.length) next.push(["continue", `Continue with ${unreached.length} not reached`, "Run the tests this run stopped before"]);
      if (failed.length && run.suite && run.mode === "script") {
        next.push(["ai-look", `Let the AI look at ${failed.length} failed`, "Run the failed tests again in Auto: the AI works out whether the app or the test is wrong, and re-records the script"]);
      }
      if (failed.length) next.push(["rerun-failed", `Re-run ${failed.length} failed`, ""]);
      next.forEach(([act, label, title], n) => actions.push(
        `<button class="btn ${n ? "" : "btn-primary"}" data-act="${act}" title="${esc(title)}">${label}</button>`));
      actions.push(`<button class="btn" data-act="rerun-all">Re-run all</button>`);
    }
    if (!ACTIVE.has(run.status)) {
      actions.push(`<button class="btn btn-ghost btn-small btn-danger" data-act="delete">Delete</button>`);
    }
    if (results && !results.partial && !session) {
      actions.push(`<button class="btn btn-ghost btn-small" data-act="dl-junit">junit.xml</button>`);
      actions.push(`<button class="btn btn-ghost btn-small" data-act="dl-results">results.json</button>`);
    }
    $("#run-actions").innerHTML = actions.join("");

    const how = (run.summary?.by_script !== undefined
      ? `<div class="how-run"><b>${run.summary.by_script}</b> by script · <b>${run.summary.by_ai}</b> by AI${run.summary.healed ? ` · <b class="healed">${run.summary.healed} healed</b> (the page changed; scripts re-recorded)` : ""}${run.summary.flaky ? ` · <b class="flaky">${run.summary.flaky} flaky</b> (passed on a retry)` : ""}</div>` : "")
      + (run.summary?.causes ? `<div class="how-run why">Why tests failed: ${Object.entries(run.summary.causes)
        .map(([k, n]) => `<span class="cause cause-${esc(k)}">${n} ${esc(causeLabel(k, n))}</span>`).join(" · ")}</div>` : "");
    $("#run-banner").innerHTML = (run.error
      ? `<div class="banner">${esc(run.error)}</div>`
      : run.status === "queued" ? `<div class="banner info">Waiting for a free browser slot…</div>` : "") + how;

    const s = run.summary || { passed: 0, failed: 0, blocked: 0, skipped: 0 };
    const score = (cls, n, label) => `<div class="score ${cls} ${n ? "" : "zero"}"><b>${n}</b><span>${label}</span></div>`;
    const saved = run.accepted?.length || 0;
    const sofar = !run.summary && results?.partial && results.results.length ? results.summary : null;
    $("#scoreboard").innerHTML = sofar
      // Still running: what has finished so far
      ? score("p", sofar.passed, "Passed") + score("f", sofar.failed + sofar.blocked, "Failed")
        + score("", run.test_ids.length - results.results.length, "To go") + score("", duration(run) || "—", "Elapsed")
      : session && run.summary
      ? (run.kind === "discover"
        ? score("", run.summary.proposed || 0, "Tests proposed") + score("", run.summary.pages || 0, "Pages mapped")
          + score("", run.summary.flows || 0, "User flows") + score("p", saved, "Saved to suites")
        : score("f", run.summary.bugs || 0, "Bugs found") + score("", run.summary.proposed || 0, "Regression tests")
          + score("", shots.filter(isImage).length, "Screenshots") + score("p", saved, "Saved to suites"))
      : run.summary
      ? score("p", s.passed, "Passed") + score("f", s.failed, "Failed") + score("b", s.blocked, "Blocked") + score("s", s.skipped, "Skipped")
      : (session ? score("", run.max_cost_usd ? cost(run.max_cost_usd) : "—", "Cost limit")
        : score("", run.test_ids.length, run.test_ids.length === 1 ? "Test" : "Tests"))
        + score("", shots.filter(isImage).length, "Screenshots")
        + score("", actionCount, "Browser actions")
        + score("", duration(run) || "—", "Elapsed");
  };

  $("#run-actions").addEventListener("click", async (e) => {
    const act = e.target.closest("[data-act]")?.dataset.act;
    if (!act) return;
    try {
      if (act === "cancel") {
        run = await api(`/runs/${runId}/cancel`, { method: "POST" });
        toast("Cancelling…");
        drawHeader();
      } else if (act === "rerun-all") {
        await rerun({ failed_only: false });
      } else if (act === "rerun-failed" || act === "ai-look" || act === "continue") {
        const { unreached, failed } = failures();
        const which = act === "continue" ? { only: unreached } : unreached.length ? { only: failed } : { failed_only: true };
        await rerun({ ...which, ...(act === "ai-look" ? { mode: "auto" } : {}) });
      } else if (act === "delete") {
        const extra = session && run.accepted?.length ? " Tests already saved to suites are kept." : "";
        if (!confirm(`Delete this run and its screenshots, report, and activity log?${extra}`)) return;
        await api(`/runs/${runId}`, { method: "DELETE" });
        toast("Run deleted");
        location.hash = run.project ? `#/runs?project=${encodeURIComponent(run.project)}` : "#/runs";
      } else if (act === "dl-junit") {
        download(`/runs/${runId}/junit`, `argus-${runId}-junit.xml`);
      } else if (act === "dl-results") {
        download(`/runs/${runId}/results`, `argus-${runId}-results.json`);
      }
    } catch (ex) {
      toast(ex.message, true);
    }
  });

  const isImage = (name) => /\.(png|jpe?g|webp)$/i.test(name);

  const drawTabs = () => {
    const images = shots.filter(isImage).length;
    const nProposed = run.status === "completed" ? run.test_ids.length : 0;
    const defs = run.kind === "discover"
      ? [["proposed", "Proposed tests", nProposed], ["map", "App map"], ["report", "Report"], ["screenshots", "Screenshots", images]]
      : run.kind === "explore"
        ? [["bugs", "Bugs", run.summary?.bugs], ["proposed", "Regression tests", nProposed], ["report", "Report"], ["screenshots", "Screenshots", images]]
        : [["tests", "Tests", run.test_ids.length], ["report", "Report"], ["screenshots", "Screenshots", images], ["plan", "Source"]];
    $("#tabs").innerHTML = defs.map(([id, label, count]) =>
      `<button role="tab" data-tab="${id}" aria-selected="${tab === id}">${label}${count ? `<span class="count">${count}</span>` : ""}</button>`).join("");
  };
  $("#tab-body").addEventListener("click", (e) => {
    e.target.closest(".case-notes")?.classList.toggle("open");
  });
  $("#tabs").addEventListener("click", (e) => {
    const b = e.target.closest("[data-tab]");
    if (!b) return;
    tab = b.dataset.tab;
    lastSignature = "";
    drawTabs();
    drawBody();
  });

  const thumb = (name, caption) =>
    `<button type="button" class="thumb" aria-label="Open ${esc(caption || name)}"><img data-shot="${esc(name)}" alt="${esc(caption || name)}"></button>`;

  // A test that has no result yet: waiting, or being worked on right now
  const waitingCase = (id) => {
    const live = ACTIVE.has(run.status);
    const agent = Object.keys(agentCase).find((a) => agentCase[a] === id);
    const state = !live ? run.status : agent ? "running" : seenCases.has(id) ? "done" : "queued";
    const now = agent && agentAction[agent]
      ? `<p class="case-now"><span>Now</span>${esc(agentAction[agent])}</p>` : "";
    return `<article class="case ${agent ? "is-current" : ""}"><div class="case-head"><span class="case-id">${esc(id)}</span>
      <span class="case-name ${caseNames[id] ? "" : "muted"}">${esc(caseNames[id] || (live ? "Waiting for results…" : "No results recorded"))}</span>
      ${statusBadge(state === "done" ? "checked" : state)}</div>${now}</article>`;
  };

  const drawTests = () => {
    const live = ACTIVE.has(run.status);
    const liveNote = live ? `<p class="muted small live-note">Each test's result, step by step, appears here as soon as the agent finishes that test.</p>` : "";
    if (!results) return `<div>${run.test_ids.map(waitingCase).join("")}${liveNote}</div>`;
    const images = new Set(shots.filter(isImage));
    const bugs = (results.bugs || []).map((b) => `
      <div class="bug">
        <div class="bug-head"><span class="sev ${esc(String(b.severity || "").toLowerCase())}">${esc(b.severity || "bug")}</span>
          <span class="bug-title">${esc(b.title)}</span><span class="case-id">${esc(b.test_case || "")}</span></div>
        ${b.expected ? `<p><b>Expected:</b> ${esc(b.expected)}</p>` : ""}
        ${b.actual ? `<p><b>Actual:</b> ${esc(b.actual)}</p>` : ""}
      </div>`).join("");
    const finishedCase = (r) => `
      <article class="case">
        <div class="case-head"><span class="case-id">${esc(r.id)}</span><span class="case-name">${esc(r.name || "")}</span>${ranBy(r)}${r.cause ? `<span class="cause cause-${esc(r.cause)}" title="${esc(CAUSES[r.cause]?.[1] || "")}">${esc(CAUSES[r.cause]?.[0] || r.cause)}</span>` : ""}${statusBadge(r.status)}</div>
        ${r.script ? `<p class="script-note ${r.script === "recorded" ? "ok" : ""}">${r.script === "recorded" ? "✓ Script recorded: next time this test replays without AI" : esc(r.script.replace(/^not recorded: /, "No script recorded: "))}</p>` : ""}
        ${r.notes ? `<p class="case-notes" title="Click to expand">${esc(r.notes)}</p>` : ""}
        ${evidence(r.evidence)}
        ${proposedUpdate(r)}
        ${nextSteps(r)}
        ${(r.steps || []).length ? `<ol class="steps">${r.steps.map((st) => `
          <li class="step ${esc(st.status || "")}">
            <div class="step-text">${esc(st.step)}</div>
            ${st.screenshot && images.has(st.screenshot) ? `<div class="step-shot">${thumb(st.screenshot, `${r.id}: ${st.step}`)}</div>` : ""}
            <dl class="step-ea">
              ${st.expected ? `<dt>Expected</dt><dd>${esc(st.expected)}</dd>` : ""}
              ${st.actual ? `<dt>Actual</dt><dd class="actual">${esc(st.actual)}</dd>` : ""}
            </dl>
          </li>`).join("")}</ol>` : ""}
      </article>`;
    // While the run is going, the tests without a result yet keep their place in the list
    const byId = new Map(results.results.map((r) => [r.id, r]));
    const cases = results.partial
      ? run.test_ids.map((id) => (byId.has(id) ? finishedCase(byId.get(id)) : waitingCase(id))).join("") + liveNote
      : results.results.map(finishedCase).join("");
    return (bugs ? `<section class="bugs"><h2 class="section-label">Bugs found</h2>${bugs}</section>` : "") + cases;
  };

  // What to do about a failed test, by its cause
  const nextSteps = (r) => {
    if (!r.cause || ACTIVE.has(run.status)) return "";
    const button = (act, label, title) =>
      `<button type="button" class="btn btn-small" data-case-act="${act}" data-id="${esc(r.id)}" title="${esc(title)}">${label}</button>`;
    const suiteHref = `#/projects/${esc(run.project)}/suites/${esc(run.suite)}`;
    const acts = [];
    if (r.cause === "bug") {
      acts.push(button("copy-bug", "Copy bug report", "Steps, expected and actual, and the evidence, ready to paste into a ticket"));
    } else if (r.cause === "not_reached") {
      acts.push(button("run-one", "Run this test", "Run just this test now"));
    } else if (r.mode === "script" && run.suite) {
      // A script can only say that it failed (or that there is none)
      acts.push(button("ai-look", r.status === "blocked" ? "Run it with AI" : "Let the AI look",
        "Run this test again in Auto: the AI works out whether the app or the test is wrong, and records a script"));
    } else if (r.cause === "outdated" && !r.update && run.suite) {
      acts.push(`<a class="btn btn-small" href="${suiteHref}">Edit the test in its suite</a>`);
    } else if (r.cause === "environment" && run.project) {
      acts.push(`<a class="btn btn-small" href="#/projects/${esc(run.project)}">Check the project's URL and accounts</a>`);
    } else if (r.cause === "unknown" && r.status === "blocked") {
      acts.push(button("run-one", "Run this test", "The agent gave no result for this test: run just this one again"));
    }
    return acts.length ? `<div class="case-actions">${acts.join("")}</div>` : "";
  };

  // The tester's corrected wording for a test, next to what the test says now
  const proposedUpdate = (r) => {
    if (!r.update) return "";
    const before = planCases?.[r.id] || {};
    const part = (key, title) => (r.update[key] ? `<h4>${title}</h4><ul class="diff">${
      diffLines(before[key] || [], r.update[key]).map(([kind, text]) =>
        `<li class="${kind}">${kind === "del" ? `<del>${esc(text)}</del>` : kind === "add" ? `<ins>${esc(text)}</ins>` : esc(text)}</li>`).join("")}</ul>` : "");
    const button = (act, label, cls = "") =>
      `<button type="button" class="btn btn-small ${cls}" data-case-act="${act}" data-id="${esc(r.id)}">${label}</button>`;
    const suiteLink = `<a href="#/projects/${esc(run.project)}/suites/${esc(run.suite)}">Edit it by hand</a>`;
    let foot;
    if ((run.updated || []).includes(r.id)) {
      foot = `<span class="applied">✓ Applied to the suite</span>${button("run-updated", "Run the updated test")}`;
    } else if (JSON.stringify(r.update).includes("[redacted]")) {
      foot = `<span class="muted small">The tester wrote out a secret value here, which was removed. ${run.suite ? suiteLink : "Edit the test by hand"} and use a placeholder for it.</span>`;
    } else if (run.suite && !ACTIVE.has(run.status)) {
      foot = `${button("apply-update", "Apply to suite", "btn-primary")}<span class="muted small">Keeps the test's ID. ${suiteLink}</span>`;
    } else {
      foot = `${button("copy-update", "Copy the new wording")}${run.suite ? "" : `<span class="muted small">This run didn't come from a saved suite, so there is no suite to update.</span>`}`;
    }
    return `<div class="update">
      <p class="update-head">${r.status === "passed" ? "This test passed, but its wording no longer matches the app." : "The app changed."} The tester proposes this update to the test:</p>
      ${part("steps", "Steps")}${part("expected", "Acceptance criteria")}
      <div class="case-actions">${foot}</div></div>`;
  };

  const bugReport = (r) => {
    const bug = (results.bugs || []).find((b) => b.test_case === r.id) || {};
    const failed = (r.steps || []).find((st) => st.status === "failed") || {};
    const steps = bug.steps_to_reproduce?.length ? bug.steps_to_reproduce : (r.steps || []).map((st) => st.step);
    const expected = bug.expected || failed.expected;
    const actual = bug.actual || failed.actual;
    const lines = [`# ${bug.title || r.name || r.id}`, "", `Found by the test ${r.id}${r.name ? `: ${r.name}` : ""}, on ${run.url}`, ""];
    if (bug.severity) lines.push(`**Severity:** ${bug.severity}`, "");
    if (steps.length) lines.push("**Steps to reproduce:**", ...steps.map((st, n) => `${n + 1}. ${st}`), "");
    if (expected) lines.push(`**Expected:** ${expected}`, "");
    if (actual) lines.push(`**Actual:** ${actual}`, "");
    if (r.notes) lines.push(r.notes, "");
    for (const [kind, title] of [["console", "Console errors"], ["network", "Failed requests"]]) {
      if (r.evidence?.[kind]?.length) lines.push(`**${title}:**`, ...r.evidence[kind].map((line) => `- \`${line}\``), "");
    }
    lines.push(`Run: ${location.origin}${location.pathname}#/runs/${runId}`);
    return lines.join("\n");
  };

  const updateText = (r) => [
    ...(r.update.steps ? ["Steps:", ...r.update.steps.map((st, n) => `${n + 1}. ${st}`), ""] : []),
    ...(r.update.expected ? ["Acceptance criteria:", ...r.update.expected.map((line) => `- ${line}`)] : []),
  ].join("\n").trim();

  $("#tab-body").addEventListener("click", async (e) => {
    const btn = e.target.closest("[data-case-act]");
    const r = btn && results?.results?.find((x) => x.id === btn.dataset.id);
    if (!r) return;
    const act = btn.dataset.caseAct;
    try {
      if (act === "copy-bug") {
        await copyText(bugReport(r), "Bug report copied");
      } else if (act === "copy-update") {
        await copyText(updateText(r), "New wording copied");
      } else if (act === "apply-update") {
        btn.disabled = true;
        const suite = await api(`/runs/${runId}/tests/${encodeURIComponent(r.id)}/update`, { method: "POST" });
        toast(`${r.id} updated in “${suite.name}”`);
        run = await api(`/runs/${runId}`);
        lastSignature = "";
        await refresh();
      } else {
        btn.disabled = true;
        // An updated test's script is outdated, and only the AI can say why a script failed
        const auto = act === "ai-look" || (act === "run-updated" && run.mode === "script");
        await rerun({ only: [r.id], ...(auto ? { mode: "auto" } : {}) });
      }
    } catch (ex) {
      toast(ex.message, true);
      btn.disabled = false;
    }
  });

  const evidence = (ev) => {
    const rows = [["console", "Console errors"], ["network", "Failed requests"]].filter(([k]) => ev?.[k]?.length);
    return rows.length ? `<dl class="evidence">${rows.map(([k, title]) =>
      `<dt>${title}</dt>${ev[k].map((line) => `<dd><code>${esc(line)}</code></dd>`).join("")}`).join("")}</dl>` : "";
  };

  const ranBy = (r) => {
    if (r.cause === "not_reached") return "";
    if (r.flaky) return `<span class="ran-by flaky" title="Its script failed once, then passed on a retry">flaky</span>`;
    if (r.healed) return `<span class="ran-by healed" title="Its script failed, the AI completed it: the page changed">healed by AI</span>`;
    if (r.mode === "script") return `<span class="ran-by script">script${r.duration_ms ? ` · ${(r.duration_ms / 1000).toFixed(1)}s` : ""}</span>`;
    if (r.mode === "ai") return `<span class="ran-by ai">AI</span>`;
    return "";
  };

  const waiting = (what) => `<p class="muted">${ACTIVE.has(run.status)
    ? `${what} appear here when the agent finishes. Watch it work in the activity panel.`
    : run.status === "completed" ? `No ${what.toLowerCase()} in this session.` : `This session ended without ${what.toLowerCase()}.`}</p>`;

  const drawProposed = async (body) => {
    if (run.status !== "completed") { body.innerHTML = waiting("Proposed tests"); return; }
    if (proposed === null || suiteChoices === null) {
      [proposed, suiteChoices] = await Promise.all([
        api(`/runs/${runId}/proposed`).then((d) => d.cases),
        api(`/projects/${encodeURIComponent(run.project)}/suites`),
      ]);
      if (!alive() || tab !== "proposed") return;
    }
    if (!proposed.length) { body.innerHTML = waiting("Proposed tests"); return; }
    const open = proposed.filter((c) => !c.accepted);
    const defaultName = run.kind === "explore" ? "Regression tests"
      : run.brief ? run.brief.replace(/^./, (ch) => ch.toUpperCase()).slice(0, 48) : "Discovered tests";
    body.innerHTML = `
      <p class="muted small proposal-intro">${run.kind === "explore"
        ? "Each bug found became a regression test that checks it stays fixed."
        : "Review the proposed tests, untick any you don't want, and save the rest to a suite."}</p>
      ${open.length ? `<label class="select-all"><input type="checkbox" id="select-all" checked> Select all</label>` : ""}
      <ul class="proposals">${proposed.map((c) => `
        <li class="proposal ${c.accepted ? "is-saved" : ""}">
          <label class="proposal-head">
            <input type="checkbox" value="${esc(c.id)}" ${c.accepted ? "checked disabled" : "checked"}>
            <span class="case-id">${esc(c.id)}</span>
            <span class="case-name">${esc(c.name)}</span>
            ${c.priority ? `<span class="chip static">${esc(c.priority)}</span>` : ""}
            ${c.accepted ? `<span class="saved">Saved</span>` : ""}
          </label>
          <details><summary>Steps and expected result</summary>
            <div class="prose proposal-body">${renderMarkdown(c.markdown.replace(/^###.*\n/, ""))}</div></details>
        </li>`).join("")}</ul>
      ${open.length ? `
      <form class="accept-bar sticky-actions" id="accept-form">
        <span>Save <b id="n-selected">${open.length}</b> to</span>
        <select name="target" aria-label="Suite">
          <option value="">a new suite named…</option>
          ${suiteChoices.map((st) => `<option value="${esc(st.slug)}">${esc(st.name)} (${st.test_count})</option>`).join("")}
        </select>
        <input name="suite_name" value="${esc(defaultName)}" aria-label="New suite name">
        <button class="btn btn-primary" type="submit">Save to suite</button>
      </form>` : `<div class="banner info">All proposed tests are saved. <a href="#/projects/${esc(run.project)}">Go to the project's suites →</a></div>`}`;

    const form = body.querySelector("#accept-form");
    if (!form) return;
    const boxes = () => [...body.querySelectorAll(".proposal input[type=checkbox]:not(:disabled)")];
    const sync = () => {
      const n = boxes().filter((b) => b.checked).length;
      body.querySelector("#n-selected").textContent = n;
      form.querySelector("button").disabled = n === 0;
      const all = body.querySelector("#select-all");
      all.checked = n === boxes().length;
      all.indeterminate = n > 0 && n < boxes().length;
      form.elements.suite_name.hidden = Boolean(form.elements.target.value);
    };
    body.querySelector("#select-all").addEventListener("change", (e) => { boxes().forEach((b) => { b.checked = e.target.checked; }); sync(); });
    boxes().forEach((b) => b.addEventListener("change", sync));
    form.elements.target.addEventListener("change", sync);
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const ids = boxes().filter((b) => b.checked).map((b) => b.value);
      const target = form.elements.target.value;
      const payload = target ? { case_ids: ids, suite: target } : { case_ids: ids, suite_name: form.elements.suite_name.value.trim() };
      form.querySelector("button").disabled = true;
      try {
        const suite = await api(`/runs/${runId}/accept`, { method: "POST", body: payload });
        toast(`Saved ${ids.length} test${ids.length === 1 ? "" : "s"} to “${suite.name}”`);
        proposed = null;
        suiteChoices = null;
        run = await api(`/runs/${runId}`);
        drawHeader();
        await drawBody();
      } catch (ex) {
        toast(ex.message, true);
        form.querySelector("button").disabled = false;
      }
    });
    sync();
  };

  const drawBugs = (body) => {
    if (!results) { body.innerHTML = waiting("Bugs"); return; }
    const bugs = results.bugs || [];
    const images = new Set(shots.filter(isImage));
    body.innerHTML = `
      ${results.summary ? `<p class="session-summary">${esc(results.summary)}</p>` : ""}
      ${(results.areas_covered || []).length ? `<p class="muted small">Areas covered: ${esc(results.areas_covered.join(", "))}</p>` : ""}
      ${bugs.length ? bugs.map((b) => `
        <article class="bug-card">
          <div class="bug-card-main">
            <div class="bug-head"><span class="sev ${esc(String(b.severity || "").toLowerCase())}">${esc(b.severity || "bug")}</span>
              <h3>${esc(b.title)}</h3></div>
            ${b.url ? `<p class="mono small muted">${esc(b.url)}</p>` : ""}
            ${(b.steps_to_reproduce || []).length ? `<ol class="repro">${b.steps_to_reproduce.map((st) => `<li>${esc(st)}</li>`).join("")}</ol>` : ""}
            <dl class="step-ea">
              ${b.expected ? `<dt>Expected</dt><dd>${esc(b.expected)}</dd>` : ""}
              ${b.actual ? `<dt>Actual</dt><dd class="actual">${esc(b.actual)}</dd>` : ""}
            </dl>
          </div>
          ${b.screenshot && images.has(b.screenshot) ? `<div>${thumb(b.screenshot, b.title)}</div>` : ""}
        </article>`).join("") : `<div class="banner info">No bugs found in this session.</div>`}
      ${(results.observations || []).length ? `<h2 class="section-label" style="margin-top:28px">Observations</h2>
        <ul class="observations">${results.observations.map((o) => `<li>${esc(o)}</li>`).join("")}</ul>` : ""}`;
  };

  const drawMap = (body) => {
    if (!exploration) { body.innerHTML = waiting("The app map will"); return; }
    const e = exploration;
    body.innerHTML = `
      ${e.summary ? `<p class="session-summary">${esc(e.summary)}</p>` : ""}
      ${(e.pages || []).length ? `<h2 class="section-label">Pages</h2>
        <table class="table map-table"><tbody>${e.pages.map((pg) => `
          <tr><td class="mono small">${esc(pg.url)}</td><td><b>${esc(pg.title || "")}</b><div class="muted small">${esc(pg.description || "")}</div></td></tr>`).join("")}</tbody></table>` : ""}
      ${(e.user_flows || []).length ? `<h2 class="section-label" style="margin-top:28px">User flows</h2>
        <ul class="flows">${e.user_flows.map((f) => `<li><b>${esc(f.name)}</b><span class="muted"> — ${esc((f.steps || []).join(" → "))}</span></li>`).join("")}</ul>` : ""}
      ${(e.issues_noticed || []).length ? `<h2 class="section-label" style="margin-top:28px">Issues noticed</h2>
        <ul class="observations">${e.issues_noticed.map((i) => `<li>${esc(i.description)} <span class="mono small muted">${esc(i.page || "")}</span></li>`).join("")}</ul>` : ""}`;
  };

  const drawBody = async () => {
    const body = $("#tab-body");
    if (tab === "proposed") {
      await drawProposed(body);
      return;
    } else if (tab === "bugs") {
      drawBugs(body);
    } else if (tab === "map") {
      drawMap(body);
    } else if (tab === "tests") {
      body.innerHTML = drawTests();
    } else if (tab === "screenshots") {
      const images = shots.filter(isImage);
      body.innerHTML = images.length
        ? `<div class="shots">${images.map((n) => `<figure>${thumb(n)}<figcaption>${esc(n)}</figcaption></figure>`).join("")}</div>`
        : `<p class="muted">${ACTIVE.has(run.status) ? "Screenshots appear here as the agent takes them." : "No screenshots were taken."}</p>`;
    } else if (tab === "report") {
      if (reportHtml === null) {
        body.innerHTML = `<p class="skeleton">Loading report…</p>`;
        try {
          reportHtml = renderMarkdown(await api(`/runs/${runId}/report`));
        } catch {
          body.innerHTML = `<p class="muted">${ACTIVE.has(run.status) ? "The report is written when the agent has finished." : "No report for this run."}</p>`;
          return;
        }
        if (tab !== "report" || !alive()) return;
      }
      body.innerHTML = `<article class="prose">${reportHtml}</article>`;
      body.querySelectorAll(".prose img").forEach((img) => {
        const src = img.getAttribute("src") || "";
        if (!/^(https?:|data:|blob:)/.test(src)) {
          img.dataset.shot = src.split("/").pop();
          img.removeAttribute("src");
        }
      });
    } else if (tab === "plan") {
      if (planText === null) planText = await api(`/runs/${runId}/plan`).catch(() => "");
      if (tab !== "plan" || !alive()) return;
      body.innerHTML = `<pre class="plan-src">${esc(planText)}</pre>`;
    }
    hydrateImages(body, runId);
  };

  // Activity feed
  const feed = $("#feed");
  const drawEvent = (ev) => {
    const el = document.createElement("div");
    el.className = `ev ${ev.kind}`;
    // Name the agent only when the speaker changes
    const who = ev.agent && ev.agent !== lastAgent
      ? `<span class="ev-agent" style="color:${agentColor(ev.agent)}">${esc(ev.agent)}</span>` : "";
    if (ev.kind === "phase") lastAgent = "";
    else if (ev.agent) lastAgent = ev.agent;
    // A timestamp when the speaker changes, the agent speaks, or 15s have passed
    const at = Date.parse(ev.ts) || 0;
    const stamp = who || ev.kind !== "action" || at - lastTime >= 15000;
    if (stamp) lastTime = at;
    const time = stamp ? `<time datetime="${esc(ev.ts)}">${esc(clock(ev.ts))}</time>` : "<span></span>";
    const tcMatch = ev.kind !== "result" && (ev.file || ev.text || "").match(/TC-\d+/i);
    if (ev.agent && tcMatch) {
      const id = tcMatch[0].toUpperCase();
      if (agentCase[ev.agent] && agentCase[ev.agent] !== id) seenCases.add(agentCase[ev.agent]);
      agentCase[ev.agent] = id;
      seenCases.add(id);
    }
    if (ev.kind === "result") {
      // The agent handed in a test's result: it is no longer working on that test
      seenCases.add(ev.test);
      if (agentCase[ev.agent] === ev.test) delete agentCase[ev.agent];
      el.innerHTML = `${time}<div class="ev-body">${who}<span class="case-id">${esc(ev.test)}</span> ${statusBadge(ev.status)}</div>`;
    } else if (ev.kind === "phase") {
      el.innerHTML = esc(ev.text);
      lastAgent = "";
    } else if (ev.kind === "action") {
      actionCount += 1;
      if (ev.agent) agentAction[ev.agent] = ev.text;
      const [verb, ...rest] = ev.text.split(": ");
      const detail = rest.join(": ");
      if (ev.quiet) {
        // Reading the page, logs or waiting: one dim line per burst, not one line each
        const bit = `<span class="bit" title="${esc(ev.text)}">${esc(verb)}${ev.tool === "wait_for" && detail ? ` ${esc(detail)}` : ""}</span>`;
        if (quietLine && !who && quietLine === feed.lastElementChild && quietLine.dataset.agent === ev.agent) {
          quietLine.querySelector(".ev-body").insertAdjacentHTML("beforeend", bit);
          return;
        }
        el.classList.add("quiet");
        el.dataset.agent = ev.agent || "";
        el.innerHTML = `${time}<div class="ev-body">${who}${bit}</div>`;
        quietLine = el;
      } else {
        // Values typed or filled ("…" after ←) stand out from the element they went into
        const shown = detail.split(/ ← ("(?:[^"]|"(?!, |$))*")/).map((part, n) => (n % 2
          ? ` <span class="arrow">←</span> <span class="val">${esc(part)}</span>` : esc(part))).join("");
        el.innerHTML = `${time}<div class="ev-body">${who}<span class="verb">${esc(verb)}</span>${detail ? ` ${shown}` : ""}</div>`;
      }
      if (ev.file) {
        el.querySelector(".ev-body").insertAdjacentHTML("beforeend", `<img class="ev-thumb" data-shot="${esc(ev.file)}" alt="" aria-label="${esc(ev.file)}">`);
        // During a live run, give the browser a moment to write the file
        setTimeout(() => hydrateImages(el, runId), ACTIVE.has(run.status) ? 1500 : 0);
      }
    } else if (ev.kind === "say") {
      el.innerHTML = `${time}<div class="ev-body">${who}<div class="ev-text">${esc(ev.text)}</div></div>`;
      el.addEventListener("click", () => el.classList.toggle("open"));
      el.title = "Click to expand";
    } else {
      el.innerHTML = `${time}<div class="ev-body">${who}${esc(ev.text)}</div>`;
    }
    const atBottom = feed.scrollHeight - feed.scrollTop - feed.clientHeight < 60;
    feed.append(el);
    if (atBottom) feed.scrollTop = feed.scrollHeight;
  };

  const pollFeed = async () => {
    const data = await api(`/runs/${runId}/events?after=${feedCursor}`).catch(() => null);
    if (!data || !alive()) return;
    feedCursor = data.next;
    if (feedCursor && feed.querySelector(".feed-empty")) feed.innerHTML = "";
    data.events.forEach(drawEvent);
    // Older runs have no activity log; give their results the full width
    $(".run-grid").classList.toggle("no-feed", !feedCursor && !ACTIVE.has(run.status));
    if (!feedCursor && !feed.children.length && ACTIVE.has(run.status)) {
      feed.innerHTML = `<div class="feed-empty">Starting the browser…</div>`;
    }
  };

  const refresh = async () => {
    const wasActive = ACTIVE.has(run.status);
    run = await api(`/runs/${runId}`);
    if (!alive()) return;
    shots = await api(`/runs/${runId}/screenshots`).catch(() => shots);
    // A test run saves each test's result as it finishes (results.partial until the run ends);
    // an explore session's results only exist once it has finished
    const stale = !results || results.partial || wasActive;
    if (run.kind === "test" ? run.status === "running" || (!ACTIVE.has(run.status) && stale)
      : run.kind === "explore" && !ACTIVE.has(run.status) && (!results || wasActive)) {
      results = await api(`/runs/${runId}/results`).catch(() => results);
    }
    if (planCases === null && planText && results?.results?.some((r) => r.update)) {
      planCases = {};
      const parsed = await api("/plans/parse", { method: "POST", body: { plan: planText } }).catch(() => null);
      (parsed?.cases || []).forEach((tc) => { planCases[tc.id] = tc; });
    }
    if (!ACTIVE.has(run.status) && (!exploration || wasActive) && run.kind === "discover") {
      exploration = await api(`/runs/${runId}/exploration`).catch(() => exploration);
    }
    if (!alive()) return;
    $("#feed-state").innerHTML = ACTIVE.has(run.status) ? `<span class="live-dot">● live</span>` : esc(run.status);
    drawHeader();
    drawTabs();
    // Redraw the tab only when what it shows has changed, so thumbnails don't flicker
    // Live progress only affects the Tests tab; other tabs mustn't redraw on every action
    const live = ACTIVE.has(run.status) && tab === "tests" ? JSON.stringify([agentCase, agentAction]) : "";
    const signature = `${tab}|${JSON.stringify(results)?.length}|${tab === "proposed" ? "" : shots.length}|${run.status}|${live}|${run.updated?.length}|${Object.keys(planCases || {}).length}`;
    if (signature !== lastSignature) {
      lastSignature = signature;
      await drawBody();
    }
    if (wasActive && !ACTIVE.has(run.status)) {
      reportHtml = null;
      proposed = null;
      toast(`Run ${run.status}`);
    }
    return ACTIVE.has(run.status);
  };

  planText = session ? "" : await api(`/runs/${runId}/plan`).catch(() => "");
  for (const m of planText.matchAll(/^###\s+(TC-\d+)\s*:\s*(.+)$/gm)) caseNames[m[1].toUpperCase()] = m[2].trim();
  await pollFeed();
  await refresh();
  if (ACTIVE.has(run.status)) {
    let busy = false;
    const tick = async () => {
      if (busy) return;
      busy = true;
      try {
        await pollFeed();
        const stillActive = await refresh();
        if (!stillActive) {
          await pollFeed();
          cleanup.forEach((fn) => fn());
          cleanup = [];
        }
      } finally { busy = false; }
    };
    every(tick, FEED_POLL_MS);
  }
}

// ── Projects ─────────────────────────────────────────────────────────

async function projectsView(alive) {
  const projects = await api("/projects");
  if (!alive()) return;
  view.innerHTML = `
    <div class="page-head">
      <div>
        <h1>Projects</h1>
        <p class="sub">An app under test: where it lives, which accounts to log in with, its saved test suites, and rules the tester follows.</p>
      </div>
      <div class="page-head-actions"><a class="btn btn-primary" href="#/projects/new">New project</a></div>
    </div>
    ${projects.length ? `<div class="projects">${projects.map((p) => `
      <a class="project-row" href="#/projects/${esc(p.slug)}">
        <div><div class="name">${esc(p.name)}</div><div class="slug">${esc(p.slug)}</div></div>
        <div class="url">${esc(p.url || "No default URL")}</div>
        <div class="chips">${Object.keys(p.credentials).map((r) => `<span class="chip static">${esc(r)}</span>`).join("") || '<span class="muted small">No test accounts</span>'}</div>
        <span class="btn btn-small" data-start="${esc(p.slug)}">Start run</span>
      </a>`).join("")}</div>`
    : `<div class="empty">
        <h2>No projects yet</h2>
        <p>A project keeps the URL, test accounts, and test data for an app. Then argus-qa can discover the app and propose a test suite for it.</p>
        <a class="btn btn-primary" href="#/projects/new">Create a project</a>
      </div>`}`;
  view.querySelectorAll("[data-start]").forEach((b) => b.addEventListener("click", (e) => {
    e.preventDefault();
    location.hash = `#/runs/new?project=${encodeURIComponent(b.dataset.start)}`;
  }));
}

const MASK = "********";

async function projectView(slug, params, alive) {
  if (!slug) {
    view.innerHTML = `<a class="crumb" href="#/projects">← Projects</a>
      <div class="page-head"><div><h1>New project</h1>
      <p class="sub">After creating it, argus-qa can explore the app and propose tests.</p></div></div>
      <div id="settings"></div>`;
    projectSettingsForm(view.querySelector("#settings"), {
      name: "", url: "", description: "", instructions: "",
      credentials: { admin: { username: "", password: "", notes: "" } }, variables: {}, secrets: {},
    }, true);
    return;
  }

  const tab = ["settings", "schedules"].includes(params.get("tab")) ? params.get("tab") : "suites";
  const [project, suites, schedules, sessions] = await Promise.all([
    api(`/projects/${encodeURIComponent(slug)}`),
    api(`/projects/${encodeURIComponent(slug)}/suites`),
    api(`/projects/${encodeURIComponent(slug)}/schedules`),
    api(`/runs?project=${encodeURIComponent(slug)}&limit=200`),
  ]);
  if (!alive()) return;
  const base = `#/projects/${encodeURIComponent(slug)}`;

  view.innerHTML = `
    <a class="crumb" href="#/projects">← Projects</a>
    <div class="page-head">
      <div><h1>${esc(project.name)}</h1>
        <p class="sub"><span class="mono">${esc(project.slug)}</span>${project.url ? ` · <a class="mono" href="${esc(project.url)}" target="_blank" rel="noopener">${esc(project.url)}</a>` : ""}</p></div>
      <div class="page-head-actions">
        <a class="btn" href="${base}/explore">Explore for bugs</a>
        <a class="btn" href="${base}/discover">Discover tests</a>
        <a class="btn btn-primary" href="#/runs/new?project=${esc(project.slug)}">New run</a>
      </div>
    </div>
    <div class="tabs" role="tablist">
      <a role="tab" href="${base}" aria-selected="${tab === "suites"}">Test suites<span class="count">${suites.length || ""}</span></a>
      <a role="tab" href="${base}?tab=schedules" aria-selected="${tab === "schedules"}">Schedules<span class="count">${schedules.length || ""}</span></a>
      <a role="tab" href="${base}?tab=settings" aria-selected="${tab === "settings"}">Settings</a>
    </div>
    <div id="project-body"></div>`;

  const body = view.querySelector("#project-body");
  if (tab === "settings") {
    projectSettingsForm(body, project, false);
    return;
  }
  if (tab === "schedules") {
    schedulesTab(body, project, suites, schedules, alive);
    return;
  }

  const sessionRuns = sessions.filter((r) => r.kind !== "test").slice(0, 8);
  body.innerHTML = `
    ${suites.length ? `<div class="suites">${suites.map((s) => `
      <div class="suite-row">
        <a class="suite-main" href="${base}/suites/${esc(s.slug)}">
          <span class="name">${esc(s.name)}</span>
          <span class="muted small">${s.test_count} test${s.test_count === 1 ? "" : "s"} · updated ${esc(relTime(s.updated_at))}</span>
        </a>
        <div class="suite-last">${s.last_run
          ? `<a href="#/runs/${esc(s.last_run.id)}">${statusBadge(s.last_run.status)}</a> <span class="muted small">${esc(relTime(s.last_run.created_at))}</span>`
          : '<span class="muted small">Never run</span>'}</div>
        <button class="btn btn-small btn-primary" data-run-suite="${esc(s.slug)}">Run</button>
      </div>`).join("")}</div>
      <div class="suite-actions"><a class="btn btn-small" href="${base}/suites/new">New suite</a></div>`
    : `<div class="empty">
        <h2>No test suites yet</h2>
        <p>Let argus-qa explore ${esc(project.url ? new URL(project.url).host : "the app")} and propose test cases. You review them and keep the ones you want.</p>
        <a class="btn btn-primary" href="${base}/discover">Discover tests</a>
        <a class="btn" href="${base}/suites/new">Write a suite</a>
      </div>`}
    ${sessionRuns.length ? `
      <h2 class="section-label" style="margin-top:36px">Discovery and exploration</h2>
      <div class="sessions">${sessionRuns.map((r) => `
        <a class="session-row" href="#/runs/${esc(r.id)}">
          <span class="kind ${esc(r.kind)}">${r.kind === "discover" ? "Discover" : "Explore"}</span>
          <span class="session-brief">${esc(r.brief || "Whole app")}</span>
          <span class="small">${sessionOutcome(r)}</span>
          <span class="muted small">${esc(relTime(r.created_at))}</span>
        </a>`).join("")}</div>` : ""}`;

  body.addEventListener("click", async (e) => {
    const suiteSlug = e.target.closest("[data-run-suite]")?.dataset.runSuite;
    if (!suiteSlug) return;
    e.target.disabled = true;
    try {
      const run = await api(`/projects/${encodeURIComponent(slug)}/suites/${encodeURIComponent(suiteSlug)}/run`, { method: "POST", body: {} });
      location.hash = `#/runs/${run.id}`;
    } catch (ex) {
      toast(ex.message, true);
      e.target.disabled = false;
    }
  });
}

// ── Schedules ────────────────────────────────────────────────────────

const WEEKDAYS = [["mon", "Mon"], ["tue", "Tue"], ["wed", "Wed"], ["thu", "Thu"], ["fri", "Fri"], ["sat", "Sat"], ["sun", "Sun"]];

function browserZone() {
  try { return Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC"; } catch { return "UTC"; }
}

function cadence(s) {
  const days = s.days.length === 0 || s.days.length === 7 ? "Every day"
    : s.days.join() === "mon,tue,wed,thu,fri" ? "Weekdays"
    : s.days.join() === "sat,sun" ? "Weekends"
    : WEEKDAYS.filter(([d]) => s.days.includes(d)).map(([, label]) => label).join(", ");
  return `${days} at ${s.time}`;
}

const dayTime = (iso) => new Date(iso).toLocaleString(undefined, { weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit", hourCycle: "h23" });

// The fields the API accepts back, from a schedule as the API returns it
const scheduleBody = (s) => ({
  name: s.name, suites: s.suites, days: s.days, time: s.time, timezone: s.timezone, mode: s.mode, parallel: s.parallel,
  max_cost_usd: s.max_cost_usd, callback_url: s.callback_url, notify_on: s.notify_on, enabled: s.enabled,
});

function schedulesTab(body, project, suites, schedules, alive) {
  const slug = project.slug;
  const base = `#/projects/${encodeURIComponent(slug)}`;
  const path = (s) => `/projects/${encodeURIComponent(slug)}/schedules/${encodeURIComponent(s.slug)}`;

  if (!suites.length) {
    body.innerHTML = `<div class="empty"><h2>No test suites to schedule</h2>
      <p>A schedule runs saved suites, so this project needs one first.</p>
      <a class="btn btn-primary" href="${base}">Go to test suites</a></div>`;
    return;
  }

  const row = (s) => {
    const failing = s.last_runs.some((r) => ["failed", "error"].includes(r.status));
    return `<div class="schedule-row ${s.enabled ? "" : "is-paused"}" data-schedule="${esc(s.slug)}">
      <div class="schedule-main">
        <span class="name">${esc(s.name)}</span>
        <span class="muted small">${esc(cadence(s))} · ${esc(s.timezone)} · ${esc(MODE_LABEL[s.mode])}</span>
        <div class="chips">${s.suites.map((id) => s.suite_names[id]
          ? `<a class="chip" href="${base}/suites/${esc(id)}">${esc(s.suite_names[id])}</a>`
          : `<span class="chip static missing" title="This suite was deleted">${esc(id)} (deleted)</span>`).join("")}</div>
        ${s.last_error ? `<p class="schedule-error">${esc(s.last_error)}</p>` : ""}
      </div>
      <div class="schedule-when small">
        <div>${s.enabled ? `<span class="muted">Next</span> <span title="${esc(s.next_run_at)}">${esc(dayTime(s.next_run_at))}</span>` : '<span class="muted">Paused</span>'}</div>
        <div>${s.last_runs.length
          ? `<span class="muted">Last</span> ${s.last_runs.map((r) => `<a href="#/runs/${esc(r.id)}" title="${esc(r.title)}">${statusBadge(r.status)}</a>`).join(" ")}
             <span class="muted">${esc(relTime(s.last_fired_at))}</span>`
          : '<span class="muted">Never run</span>'}
          ${s.last_fired_at ? ` · <a href="#/runs?project=${encodeURIComponent(slug)}&schedule=${encodeURIComponent(s.slug)}" class="${failing ? "text-fail" : ""}">History</a>` : ""}</div>
      </div>
      <div class="schedule-buttons">
        <button class="btn btn-small" data-act="run">Run now</button>
        <button class="btn btn-small" data-act="toggle">${s.enabled ? "Pause" : "Resume"}</button>
        <button class="btn btn-small" data-act="edit">Edit</button>
      </div>
    </div>`;
  };

  body.innerHTML = schedules.length
    ? `<p class="muted small schedule-intro">Each suite in a schedule gets its own run. Schedules fire while the server is running; a run that comes due while it's down for over an hour is skipped.</p>
       <div class="schedules">${schedules.map(row).join("")}</div>
       <div class="suite-actions"><button class="btn btn-small" id="new-schedule">New schedule</button></div>`
    : `<div class="empty"><h2>No schedules yet</h2>
        <p>Run this project's suites on their own, every night or at the end of the week. In Auto mode, recorded scripts replay without AI and the AI only steps in for tests that fail.</p>
        <button class="btn btn-primary" id="new-schedule">New schedule</button></div>`;

  body.querySelector("#new-schedule").addEventListener("click", () => scheduleDialog(project, suites, null));
  body.addEventListener("click", async (e) => {
    const act = e.target.closest("[data-act]")?.dataset.act;
    if (!act) return;
    const s = schedules.find((x) => x.slug === e.target.closest("[data-schedule]").dataset.schedule);
    if (act === "edit") return scheduleDialog(project, suites, s);
    e.target.disabled = true;
    try {
      if (act === "run") {
        await api(`${path(s)}/run`, { method: "POST" });
        location.hash = `#/runs?project=${encodeURIComponent(slug)}&schedule=${encodeURIComponent(s.slug)}`;
      } else {
        await api(path(s), { method: "PUT", body: { ...scheduleBody(s), enabled: !s.enabled } });
        toast(s.enabled ? "Schedule paused" : "Schedule resumed");
        if (alive()) route();
      }
    } catch (ex) {
      toast(ex.message, true);
      e.target.disabled = false;
    }
  });
}

function scheduleDialog(project, suites, schedule) {
  const isNew = !schedule;
  const s = schedule || {
    name: "", suites: suites.map((x) => x.slug), days: [], time: "02:00", timezone: browserZone(), mode: "auto",
    parallel: 1, max_cost_usd: null, callback_url: null, notify_on: "always", enabled: true,
  };
  const everyDay = s.days.length === 0;
  let zones = [];
  try { zones = Intl.supportedValuesOf("timeZone"); } catch { /* older browsers: free text */ }
  const url = `/projects/${encodeURIComponent(project.slug)}/schedules`;

  const dialog = document.createElement("dialog");
  dialog.className = "dialog schedule-dialog";
  dialog.innerHTML = `<form novalidate>
    <h2>${isNew ? "New schedule" : `Edit ${esc(s.name)}`}</h2>
    <label class="field"><span>Name</span><input name="name" required value="${esc(s.name)}" placeholder="Nightly"></label>
    <fieldset class="suite-picks">
      <legend class="small">Suites to run</legend>
      ${suites.map((x) => `<label class="suite-test"><input type="checkbox" name="suite" value="${esc(x.slug)}" ${s.suites.includes(x.slug) ? "checked" : ""}>
        ${esc(x.name)} <span class="muted small">${x.test_count} test${x.test_count === 1 ? "" : "s"} · ${x.scripts_ready} with a script</span></label>`).join("")}
    </fieldset>
    <div class="field"><span>Days</span>
      <div class="day-picks">${WEEKDAYS.map(([d, label]) => `<label><input type="checkbox" name="day" value="${d}" ${everyDay || s.days.includes(d) ? "checked" : ""}><span>${label}</span></label>`).join("")}</div>
    </div>
    <div class="form-row schedule-time">
      <label class="field"><span>Time</span><input type="time" name="time" required value="${esc(s.time)}"></label>
      <label class="field"><span>Timezone</span><input name="timezone" required value="${esc(s.timezone)}" list="schedule-zones" autocomplete="off">
        <datalist id="schedule-zones"><option value="UTC">${zones.map((z) => `<option value="${esc(z)}">`).join("")}</datalist></label>
    </div>
    <label class="field"><span>How to run</span>${modeSelect("mode", s.mode)}
      <span class="hint" data-hint="mode"></span></label>
    <label class="field"><span>Cost limit per suite (USD)</span>
      <input type="number" name="max_cost_usd" min="0.1" max="100" step="0.1" value="${s.max_cost_usd ?? ""}" placeholder="${DEFAULTS.test}">
      <span class="hint" data-hint="cost"></span></label>
    <label class="field"><span>Webhook URL (optional)</span>
      <input name="callback_url" type="url" value="${esc(s.callback_url || "")}" placeholder="https://hooks.example.com/argus">
      <span class="hint">Each run's record is POSTed here when it finishes.</span></label>
    <label class="suite-test"><input type="checkbox" name="failures_only" ${s.notify_on === "failure" ? "checked" : ""}> Only call the webhook for runs that don't pass</label>
    <p class="form-error" hidden></p>
    <div class="dialog-actions">
      ${isNew ? "" : '<button class="btn btn-danger" type="button" data-delete>Delete</button>'}
      <span class="spacer"></span>
      <button class="btn" type="button" data-cancel>Cancel</button>
      <button class="btn btn-primary" type="submit">${isNew ? "Create schedule" : "Save"}</button>
    </div></form>`;
  document.body.append(dialog);

  const form = dialog.querySelector("form");
  const f = form.elements;
  const checked = (name) => [...form.querySelectorAll(`[name="${name}"]:checked`)].map((box) => box.value);
  const hints = () => {
    const count = checked("suite").length;
    const limit = Number(f.max_cost_usd.value) || DEFAULTS.test;
    form.querySelector('[data-hint="mode"]').textContent = modeDescription(f.mode.value);
    form.querySelector('[data-hint="cost"]').textContent = f.mode.value === "script"
      ? "Script-only runs don't use AI, so they cost nothing."
      : `Each suite's run stops at this limit, so one firing can cost up to ${cost(limit * count)} (${count} suite${count === 1 ? "" : "s"} × ${cost(limit)}).`;
  };
  form.addEventListener("input", hints);
  hints();

  const close = () => { dialog.close(); dialog.remove(); };
  dialog.addEventListener("cancel", () => dialog.remove());
  form.querySelector("[data-cancel]").addEventListener("click", close);
  form.querySelector("[data-delete]")?.addEventListener("click", async () => {
    if (!confirm(`Delete the schedule “${s.name}”? Its past runs are kept.`)) return;
    try {
      await api(`${url}/${encodeURIComponent(s.slug)}`, { method: "DELETE" });
      close();
      toast("Schedule deleted");
      route();
    } catch (ex) {
      toast(ex.message, true);
    }
  });

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const err = form.querySelector(".form-error");
    const fail = (message) => { err.textContent = message; err.hidden = false; };
    err.hidden = true;
    const days = checked("day");
    if (!f.name.value.trim()) return fail("Give the schedule a name.");
    if (!checked("suite").length) return fail("Pick at least one suite.");
    if (!days.length) return fail("Pick at least one day.");
    const body = {
      ...scheduleBody(s),
      name: f.name.value.trim(),
      suites: checked("suite"),
      days: days.length === 7 ? [] : days,
      time: f.time.value,
      timezone: f.timezone.value.trim(),
      mode: f.mode.value,
      max_cost_usd: f.max_cost_usd.value ? Number(f.max_cost_usd.value) : null,
      callback_url: f.callback_url.value.trim() || null,
      notify_on: f.failures_only.checked ? "failure" : "always",
    };
    try {
      await (isNew ? api(url, { method: "POST", body }) : api(`${url}/${encodeURIComponent(s.slug)}`, { method: "PUT", body }));
      close();
      toast(isNew ? "Schedule created" : "Saved");
      route();
    } catch (ex) {
      fail(ex.message);
    }
  });

  dialog.showModal();
  if (isNew) f.name.focus();
}

function sessionOutcome(run) {
  if (ACTIVE.has(run.status)) return statusBadge(run.status);
  if (run.status !== "completed") return statusBadge(run.status);
  const s = run.summary || {};
  const accepted = run.accepted?.length ? ` · ${run.accepted.length} saved` : "";
  if (run.kind === "explore") {
    return `<span class="${s.bugs ? "text-fail" : "text-pass"}">${s.bugs || 0} bug${s.bugs === 1 ? "" : "s"} found</span>${accepted}`;
  }
  return `${s.proposed || 0} tests proposed${accepted}`;
}

function projectSettingsForm(container, project, isNew) {
  const slug = project.slug;
  container.innerHTML = `
    <form class="form" id="project-form" novalidate>
      <div class="form-row">
        <label class="field"><span>Name</span><input name="name" required value="${esc(project.name)}" placeholder="Looma staging"></label>
        ${isNew ? `<label class="field"><span>Slug</span><input name="slug" placeholder="derived from the name" pattern="[a-z0-9][a-z0-9-]*">
          <span class="hint">Used in the API and URLs. Can't be changed later.</span></label>` : ""}
      </div>
      <label class="field"><span>Default URL</span><input name="url" value="${esc(project.url || "")}" placeholder="https://staging.example.com">
        <span class="hint">Runs use this unless they give their own. <code>\${ENV_VAR}</code> references are read from the server's environment.</span></label>
      <label class="field"><span>Description</span><input name="description" value="${esc(project.description)}" placeholder="What the app is, anything the tester should know"></label>
      <label class="field"><span>Standing instructions</span>
        <textarea name="instructions" rows="4" placeholder="- Accept the cookie banner if it appears.&#10;- Never click Delete account.">${esc(project.instructions)}</textarea>
        <span class="hint">Followed by the tester in every test, discovery, and exploration.</span></label>

      <fieldset>
        <legend>Test accounts</legend>
        <p class="legend-hint">Tests say “Log in as <i>role</i>” or use <code>{{role.username}}</code> and <code>{{role.password}}</code>. Passwords are never shown again after saving.</p>
        <div class="kv" id="creds">
          <div class="kv-row cred kv-head"><span>Role</span><span>Username</span><span>Password</span><span>Notes</span><span></span></div>
        </div>
        <div><button type="button" class="btn btn-small" data-add="cred">Add account</button></div>
      </fieldset>

      <fieldset>
        <legend>Test data</legend>
        <p class="legend-hint">Values tests can use as <code>{{name}}</code>.</p>
        <div class="kv" id="vars"></div>
        <div><button type="button" class="btn btn-small" data-add="var">Add value</button></div>
      </fieldset>

      <fieldset>
        <legend>Secrets</legend>
        <p class="legend-hint">Like test data, but hidden here and redacted from results, reports, and logs.</p>
        <div class="kv" id="secrets"></div>
        <div><button type="button" class="btn btn-small" data-add="secret">Add secret</button></div>
      </fieldset>

      <p class="form-error" id="form-error" hidden></p>
      <div class="form-actions sticky-actions">
        <button class="btn btn-primary" type="submit">${isNew ? "Create project" : "Save changes"}</button>
        <a class="btn btn-ghost" href="${isNew ? "#/projects" : `#/projects/${esc(slug)}`}">Cancel</a>
        <span class="spacer"></span>
        ${isNew ? "" : `<button class="btn btn-danger" type="button" id="delete-btn">Delete project</button>`}
      </div>
    </form>`;

  const $ = (sel) => container.querySelector(sel);
  const removeBtn = `<button type="button" class="icon-btn" data-remove aria-label="Remove"><svg viewBox="0 0 24 24"><path d="M6 6l12 12M18 6L6 18"/></svg></button>`;
  const secretInput = (value, name) =>
    `<input type="password" name="${name}" value="${esc(value)}" autocomplete="new-password" placeholder="${value === MASK ? "" : "password"}"
      ${value === MASK ? 'data-masked title="Unchanged. Type to replace."' : ""}>`;

  const addCred = (role = "", c = { username: "", password: "", notes: "" }) => $("#creds").insertAdjacentHTML("beforeend", `
    <div class="kv-row cred" data-row="cred">
      <input name="role" value="${esc(role)}" placeholder="admin" aria-label="Role">
      <input name="username" value="${esc(c.username)}" placeholder="qa@example.com" aria-label="Username" autocomplete="off">
      ${secretInput(c.password, "password")}
      <input name="notes" value="${esc(c.notes)}" placeholder="Full access" aria-label="Notes">
      ${removeBtn}
    </div>`);
  const addPair = (target, kind, key = "", value = "") => $(target).insertAdjacentHTML("beforeend", `
    <div class="kv-row pair" data-row="${kind}">
      <input name="key" value="${esc(key)}" placeholder="${kind === "secret" ? "api_token" : "customer_name"}" aria-label="Name">
      ${kind === "secret" ? secretInput(value, "value") : `<input name="value" value="${esc(value)}" placeholder="Acme Corp" aria-label="Value">`}
      ${removeBtn}
    </div>`);

  Object.entries(project.credentials).forEach(([role, c]) => addCred(role, c));
  Object.entries(project.variables).forEach(([k, v]) => addPair("#vars", "var", k, v));
  Object.entries(project.secrets).forEach(([k, v]) => addPair("#secrets", "secret", k, v));

  const form = $("#project-form");
  form.addEventListener("click", (e) => {
    const add = e.target.closest("[data-add]")?.dataset.add;
    if (add === "cred") addCred();
    if (add === "var") addPair("#vars", "var");
    if (add === "secret") addPair("#secrets", "secret");
    if (e.target.closest("[data-remove]")) e.target.closest(".kv-row").remove();
  });
  // A masked secret field shows dots for "unchanged"; focusing it offers a clean slate
  form.addEventListener("focusin", (e) => {
    if (e.target.dataset?.masked !== undefined && e.target.value === MASK) e.target.select();
  });

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const err = $("#form-error");
    err.hidden = true;
    const f = form.elements;
    const rows = (kind) => [...form.querySelectorAll(`[data-row="${kind}"]`)];
    const body = {
      name: f.name.value.trim(),
      url: f.url.value.trim() || null,
      description: f.description.value.trim(),
      instructions: f.instructions.value,
      credentials: Object.fromEntries(rows("cred")
        .filter((r) => r.querySelector('[name="role"]').value.trim())
        .map((r) => [r.querySelector('[name="role"]').value.trim(), {
          username: r.querySelector('[name="username"]').value,
          password: r.querySelector('[name="password"]').value,
          notes: r.querySelector('[name="notes"]').value,
        }])),
      variables: Object.fromEntries(rows("var").map((r) => [r.querySelector('[name="key"]').value.trim(), r.querySelector('[name="value"]').value]).filter(([k]) => k)),
      secrets: Object.fromEntries(rows("secret").map((r) => [r.querySelector('[name="key"]').value.trim(), r.querySelector('[name="value"]').value]).filter(([k]) => k)),
    };
    if (isNew && f.slug.value.trim()) body.slug = f.slug.value.trim();
    try {
      const saved = isNew
        ? await api("/projects", { method: "POST", body })
        : await api(`/projects/${encodeURIComponent(slug)}`, { method: "PUT", body });
      toast(isNew ? "Project created" : "Saved");
      if (isNew) location.hash = `#/projects/${saved.slug}`;
      else route();
    } catch (ex) {
      err.textContent = ex.message;
      err.hidden = false;
    }
  });

  $("#delete-btn")?.addEventListener("click", async () => {
    if (!confirm(`Delete project “${project.name}” and its test suites? Past runs are kept.`)) return;
    try {
      await api(`/projects/${encodeURIComponent(slug)}`, { method: "DELETE" });
      toast("Project deleted");
      location.hash = "#/projects";
    } catch (ex) {
      toast(ex.message, true);
    }
  });
  if (isNew) form.elements.name.focus();
}

// ── Suites ───────────────────────────────────────────────────────────

async function suiteView(slug, suiteSlug, alive) {
  const isNew = !suiteSlug;
  const [project, suite, runs] = await Promise.all([
    api(`/projects/${encodeURIComponent(slug)}`),
    isNew ? null : api(`/projects/${encodeURIComponent(slug)}/suites/${encodeURIComponent(suiteSlug)}`),
    isNew ? [] : api(`/runs?project=${encodeURIComponent(slug)}&suite=${encodeURIComponent(suiteSlug)}&limit=10`),
  ]);
  if (!alive()) return;
  let plan = isNew
    ? { title: "", url: project.url || null, notes: "", cases: [] }
    : await api("/plans/parse", { method: "POST", body: { plan: suite.plan } });
  if (!alive()) return;
  const base = `#/projects/${encodeURIComponent(slug)}`;
  const placeholders = projectPlaceholders(project);

  view.innerHTML = `
    <a class="crumb" href="${base}">← ${esc(project.name)}</a>
    <header class="suite-head">
      <div class="suite-title">
        <input class="title-input" name="name" form="suite-form" required value="${esc(suite?.name || "")}"
          placeholder="${isNew ? "Name this suite, e.g. Smoke tests" : "Suite name"}" aria-label="Suite name">
        <p class="sub">${isNew ? "A saved set of test cases you can run with one click." : `${suite.test_count} test${suite.test_count === 1 ? "" : "s"} · ${suite.scripts_ready} with a recorded script · updated ${esc(relTime(suite.updated_at))}`}</p>
      </div>
      ${isNew ? "" : `<div class="run-bar" role="group" aria-label="Run this suite">
        <label class="run-opt" title="${esc(modeDescription("auto"))}"><span>Mode</span>${modeSelect("suite_mode")}</label>
        <label class="run-opt"><span>Parallel</span><input id="suite-parallel" type="number" min="1" max="8" value="1"></label>
        <button class="btn btn-primary" id="run-suite">Run suite</button>
      </div>`}
    </header>
    <div class="suite-layout">
      <form class="suite-form" id="suite-form" novalidate>
        <div class="suite-toolbar">
          <div class="segmented" role="group" aria-label="Editing mode">
            <button type="button" data-edit-mode="fields" aria-pressed="true">Editor</button>
            <button type="button" data-edit-mode="markdown" aria-pressed="false">Markdown</button>
          </div>
          <span class="detected" id="detected"></span>
          <span class="spacer"></span>
          <button type="button" class="btn btn-small btn-ghost" id="fold-all">Collapse all</button>
          <label class="btn btn-small" title="Add the test cases from a Markdown test plan (.md) file">
            Import Markdown…<input type="file" id="import-file" accept=".md,.markdown,.txt" hidden></label>
        </div>
        <div id="cases" class="tc-editor"></div>
        <textarea name="plan" rows="26" hidden aria-label="Suite as Markdown"></textarea>
        <details class="plan-notes" id="plan-notes" ${plan.notes ? "open" : ""}>
          <summary>Setup notes</summary>
          <textarea name="notes" rows="4" placeholder="Anything that applies to every test: setup steps, feature flags, test data…">${esc(plan.notes || "")}</textarea>
        </details>
        <p class="form-error" id="form-error" hidden></p>
        <div class="form-actions sticky-actions">
          <button class="btn btn-primary" type="submit">${isNew ? "Create suite" : "Save changes"}</button>
          <a class="btn btn-ghost" href="${base}">Cancel</a>
          <span class="dirty-note" id="dirty-note" hidden>Unsaved changes</span>
          <span class="spacer"></span>
          ${isNew ? "" : `<button class="btn btn-danger" type="button" id="delete-suite">Delete suite</button>`}
        </div>
      </form>
      <aside class="suite-aside">
        <section class="aside-block" id="outline-block">
          <h2 class="section-label">Tests</h2>
          <nav class="outline" id="outline" aria-label="Jump to a test"></nav>
        </section>
        ${placeholders.length ? `<section class="aside-block">
          <h2 class="section-label">Project values</h2>
          <p class="muted small">Click to insert into the field you're editing.</p>
          <div class="chips" id="suite-chips"></div>
        </section>` : ""}
        ${runs.length ? `<section class="aside-block">
          <h2 class="section-label">Recent runs</h2>
          ${runs.slice(0, 6).map((r) => `<a class="aside-run" href="#/runs/${esc(r.id)}">${statusBadge(r.status)}
            <span class="aside-run-bar">${tallyBar(r.summary)}</span><span class="muted small">${esc(relTime(r.created_at))}</span></a>`).join("")}
        </section>` : ""}
      </aside>
    </div>`;

  const form = view.querySelector("#suite-form");
  const $ = (sel) => view.querySelector(sel);
  let mode = "fields";
  let editor = null;
  let lastField = null;
  let allCollapsed = false;

  const drawOutline = () => {
    $("#outline-block").hidden = mode !== "fields";
    if (mode !== "fields") return;
    $("#outline").innerHTML = editor.cases().map((c, ci) => `
      <button type="button" class="outline-item" data-jump="${ci}">
        <span class="case-id">${esc(c.id || "new")}</span><span class="outline-title ${c.title.trim() ? "" : "muted"}">${esc(c.title.trim() || "Untitled")}</span>
      </button>`).join("");
    spy();
  };
  // Highlights the test at the top of the viewport in the outline
  const spy = () => {
    const cards = [...view.querySelectorAll(".tc-card")];
    const current = cards.find((card) => card.getBoundingClientRect().bottom > 140) || cards[cards.length - 1];
    view.querySelectorAll(".outline-item").forEach((b) => b.classList.toggle("active", current && b.dataset.jump === current.dataset.ci));
  };
  let spyQueued = false;
  const onScroll = () => {
    if (spyQueued) return;
    spyQueued = true;
    requestAnimationFrame(() => { spyQueued = false; spy(); });
  };
  window.addEventListener("scroll", onScroll, { passive: true });
  onLeave(() => window.removeEventListener("scroll", onScroll));

  const markEdited = () => { $("#dirty-note").hidden = false; };
  view.addEventListener("input", (e) => {
    if (e.target.matches('.title-input, textarea[name="notes"], textarea[name="plan"]')) markEdited();
    if (e.target.matches(".title-input")) e.target.classList.remove("invalid");
  });

  const countCases = () => {
    const n = mode === "fields"
      ? editor.cases().filter((c) => c.title.trim() || c.steps.length).length
      : [...form.elements.plan.value.matchAll(/^###\s+TC-\d+\s*:/gm)].length;
    $("#detected").textContent = `${n} test case${n === 1 ? "" : "s"}`;
    drawOutline();
  };
  const showEditor = () => {
    const scripts = Object.fromEntries((suite?.tests || []).map((t) => [t.id, t.script]));
    editor = caseEditor($("#cases"), plan.cases, {
      project: slug, placeholders, onFocus: (el) => { lastField = el; }, onChange: () => { if (editor) { countCases(); markEdited(); } },
      badge: (id) => (suite ? `<button type="button" class="badge-btn" data-script="${esc(id)}" ${scripts[id]?.status === "none" || !scripts[id] ? "disabled" : ""}>${scriptBadge(scripts[id])}</button>` : ""),
    });
    countCases();
  };
  const currentPlan = () => ({
    title: form.elements.name.value.trim() || plan.title || "Untitled",
    url: plan.url,
    notes: form.elements.notes.value,
    cases: editor.cases().filter((c) => c.title.trim() || c.steps.length || c.expected.length),
  });
  const setMode = async (next) => {
    if (next === mode) return;
    try {
      if (next === "markdown") {
        form.elements.plan.value = (await api("/plans/render", { method: "POST", body: currentPlan() })).plan;
      } else {
        plan = await api("/plans/parse", { method: "POST", body: { plan: form.elements.plan.value } });
        form.elements.notes.value = plan.notes || "";
      }
    } catch (ex) {
      toast(ex.message, true);
      return;
    }
    mode = next;
    view.querySelectorAll("[data-edit-mode]").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.editMode === mode)));
    $("#cases").hidden = mode !== "fields";
    $("#plan-notes").hidden = mode !== "fields";
    form.elements.plan.hidden = mode !== "markdown";
    $("#fold-all").hidden = mode !== "fields";
    allCollapsed = false;
    $("#fold-all").textContent = "Collapse all";
    if (mode === "fields") showEditor(); else countCases();
  };
  $("#fold-all").addEventListener("click", (e) => {
    allCollapsed = !allCollapsed;
    editor.setCollapsed(allCollapsed);
    e.target.textContent = allCollapsed ? "Expand all" : "Collapse all";
  });
  $("#outline").addEventListener("click", (e) => {
    const b = e.target.closest("[data-jump]");
    if (b) editor.reveal(Number(b.dataset.jump));
  });
  view.querySelectorAll("[data-edit-mode]").forEach((b) => b.addEventListener("click", () => setMode(b.dataset.editMode)));
  $("#cases").addEventListener("click", (e) => {
    const id = e.target.closest("[data-script]")?.dataset.script;
    if (id) showScript(slug, suiteSlug, id);
  });
  form.elements.plan.addEventListener("input", countCases);
  form.elements.plan.addEventListener("focus", (e) => { lastField = e.target; });
  if (placeholders.length) placeholderChips($("#suite-chips"), placeholders, () => lastField);
  showEditor();

  $("#import-file").addEventListener("change", async (e) => {
    const file = e.target.files[0];
    e.target.value = "";
    if (!file) return;
    try {
      const imported = await api("/plans/parse", { method: "POST", body: { plan: await file.text() } });
      if (!imported.cases.length) throw new Error(`No test cases found in ${file.name}. Each needs a heading like “### TC-001: Title”.`);
      if (mode !== "fields") await setMode("fields");
      const kept = editor.cases().filter((c) => c.title.trim() || c.steps.length || c.expected.length);
      // Imported tests get new numbers when saved, so they can't clash with this suite's IDs
      editor.replace([...kept, ...imported.cases.map((c) => ({ ...c, id: null }))]);
      if (!form.elements.name.value.trim() && imported.title) form.elements.name.value = imported.title;
      if (!form.elements.notes.value.trim() && imported.notes) {
        form.elements.notes.value = imported.notes;
        $("#plan-notes").open = true;
      }
      if (!plan.url && imported.url) plan.url = imported.url;
      toast(`Imported ${imported.cases.length} test${imported.cases.length === 1 ? "" : "s"} from ${file.name} — review, then save`);
    } catch (ex) {
      toast(ex.message, true);
    }
  });

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const err = $("#form-error");
    err.hidden = true;
    const nameInput = form.elements.name;
    nameInput.classList.remove("invalid");
    try {
      // A suite written by drafting a test with AI often has no name yet: name it after its first test
      if (!nameInput.value.trim() && mode === "fields") {
        nameInput.value = (editor.cases().find((c) => c.title.trim())?.title || "").trim().slice(0, 60);
      }
      if (!nameInput.value.trim()) {
        nameInput.classList.add("invalid");
        nameInput.focus();
        throw new Error("Give the suite a name — it's the title field at the top of the page.");
      }
      let planText;
      if (mode === "markdown") {
        planText = form.elements.plan.value;
      } else {
        const data = currentPlan();
        if (!data.cases.length) throw new Error("Add at least one test case with a title and steps.");
        const untitled = data.cases.filter((c) => !c.title.trim()).length;
        if (untitled) throw new Error(`${untitled} test case${untitled === 1 ? " has" : "s have"} no title.`);
        planText = (await api("/plans/render", { method: "POST", body: data })).plan;
      }
      const body = { name: form.elements.name.value.trim(), plan: planText };
      const saved = isNew
        ? await api(`/projects/${encodeURIComponent(slug)}/suites`, { method: "POST", body })
        : await api(`/projects/${encodeURIComponent(slug)}/suites/${encodeURIComponent(suiteSlug)}`, { method: "PUT", body });
      toast(isNew ? "Suite created" : "Saved");
      if (isNew) location.hash = `${base}/suites/${saved.slug}`;
      else route();
    } catch (ex) {
      err.textContent = ex.message;
      err.hidden = false;
    }
  });

  $("#run-suite")?.addEventListener("click", async (e) => {
    e.target.disabled = true;
    try {
      const run = await api(`/projects/${encodeURIComponent(slug)}/suites/${encodeURIComponent(suiteSlug)}/run`, {
        method: "POST", body: { parallel: Number($("#suite-parallel").value) || 1, mode: view.querySelector('select[name="suite_mode"]').value },
      });
      location.hash = `#/runs/${run.id}`;
    } catch (ex) {
      toast(ex.message, true);
      e.target.disabled = false;
    }
  });

  $("#delete-suite")?.addEventListener("click", async () => {
    if (!confirm(`Delete the suite “${suite.name}”? Its past runs are kept.`)) return;
    try {
      await api(`/projects/${encodeURIComponent(slug)}/suites/${encodeURIComponent(suiteSlug)}`, { method: "DELETE" });
      toast("Suite deleted");
      location.hash = base;
    } catch (ex) {
      toast(ex.message, true);
    }
  });
  if (isNew) form.elements.name.focus();
}

// ── Discover / explore ───────────────────────────────────────────────

const CHARTER_IDEAS = [
  "Try to break the sign-up and login forms with unusual input",
  "Create, edit, and delete items; look for data that doesn't save correctly",
  "Use the app in a narrow mobile-sized window and look for layout problems",
  "Look for dead links, missing images, and console errors across the main pages",
];

async function sessionView(slug, mode, alive) {
  const project = await api(`/projects/${encodeURIComponent(slug)}`);
  if (!alive()) return;
  const base = `#/projects/${encodeURIComponent(slug)}`;
  const discover = mode === "discover";
  const roles = Object.keys(project.credentials);

  view.innerHTML = `
    <a class="crumb" href="${base}">← ${esc(project.name)}</a>
    <div class="page-head"><div>
      <h1>${discover ? "Discover tests" : "Explore for bugs"}</h1>
      <p class="sub">${discover
        ? "An agent explores the app like a new user, maps its pages and flows, and proposes test cases. You choose which to keep."
        : "An agent pokes at the app like a skeptical human tester, without a script, and reports the bugs it finds. Each bug can become a regression test."}</p>
    </div></div>
    <form class="form" id="session-form" novalidate>
      ${discover ? `
        <label class="field"><span>Focus <span class="muted">(optional)</span></span>
          <textarea name="brief" rows="3" placeholder="e.g. The promotions area: creating, editing, and scheduling promotions"></textarea>
          <span class="hint">Leave empty to map the whole app.</span></label>`
      : `
        <label class="field"><span>Charter</span>
          <textarea name="brief" rows="3" required placeholder="What should the tester explore, and what should it look for?"></textarea>
          <div class="chips">${CHARTER_IDEAS.map((idea) => `<button type="button" class="chip" data-idea="${esc(idea)}">${esc(idea)}</button>`).join("")}</div>
        </label>`}
      <div class="form-row">
        <label class="field"><span>Start at</span>
          <input name="url" type="url" placeholder="${esc(project.url || "https://staging.example.com")}" autocomplete="off">
          <span class="hint">${project.url ? "Leave empty to use the project's URL." : "This project has no default URL."}</span></label>
        <label class="field"><span>Cost limit (USD)</span>
          <input name="max_cost" type="number" min="0.1" max="100" step="0.5" value="${DEFAULTS[mode]}">
          <span class="hint">Estimated at API prices. The agent stops when it reaches this.</span></label>
      </div>
      <div class="notice">
        <b>Safety rules the agent always follows:</b> it stays on ${esc(project.url ? new URL(project.url).host : "the app's domain")}, never deletes data,
        pays, or messages real people, and names anything it creates “argus-test …”. ${roles.length
          ? `It logs in as ${roles.map((r) => `<code>${esc(r)}</code>`).join(", ")} when needed.`
          : `This project has no test accounts, so it can only see public pages. <a href="${base}?tab=settings">Add one</a>.`}
        Use a staging environment.
      </div>
      <p class="form-error" id="form-error" hidden></p>
      <div class="form-actions">
        <button class="btn btn-primary" type="submit" id="submit-btn">${discover ? "Start discovery" : "Start exploring"}</button>
        <a class="btn btn-ghost" href="${base}">Cancel</a>
      </div>
    </form>`;

  const form = view.querySelector("#session-form");
  form.addEventListener("click", (e) => {
    const idea = e.target.closest("[data-idea]")?.dataset.idea;
    if (idea) { form.elements.brief.value = idea; form.elements.brief.focus(); }
  });
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const err = view.querySelector("#form-error");
    err.hidden = true;
    const f = form.elements;
    const body = { [discover ? "focus" : "charter"]: f.brief.value.trim() };
    if (f.url.value.trim()) body.url = f.url.value.trim();
    if (Number(f.max_cost.value)) body.max_cost_usd = Number(f.max_cost.value);
    const btn = view.querySelector("#submit-btn");
    btn.disabled = true;
    try {
      const run = await api(`/projects/${encodeURIComponent(slug)}/${mode}`, { method: "POST", body });
      location.hash = `#/runs/${run.id}`;
    } catch (ex) {
      err.textContent = ex.message;
      err.hidden = false;
      btn.disabled = false;
    }
  });
  form.elements.brief.focus();
}

// ── Boot ─────────────────────────────────────────────────────────────

(async () => {
  try {
    const health = await fetch("/health").then((r) => r.json());
    Object.assign(DEFAULTS, health.default_max_cost_usd || {});
    if (health.auth_required && !getKey()) askForKey();
  } catch { /* server unreachable; views will show the error */ }
  route();
})();
