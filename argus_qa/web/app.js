// argus-qa web UI. Plain ES module, no build step. Talks to the same server's JSON API.

const view = document.getElementById("view");
const KEY_STORAGE = "argus.apiKey";
const ACTIVE = new Set(["queued", "running"]);
const FEED_POLL_MS = 1500;
const LIST_POLL_MS = 4000;

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
    else if (parts[0] === "projects" && parts[1]) await projectView(parts[1] === "new" ? null : parts[1], alive);
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
  const [runs, projects] = await Promise.all([
    api(`/runs?limit=200${project ? `&project=${encodeURIComponent(project)}` : ""}`),
    api("/projects"),
  ]);
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
    </div>
    <div id="runs-table"></div>`;

  view.querySelector("#project-filter").addEventListener("change", (e) => {
    location.hash = e.target.value ? `#/runs?project=${encodeURIComponent(e.target.value)}` : "#/runs";
  });

  const names = Object.fromEntries(projects.map((p) => [p.slug, p.name]));
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
      <thead><tr><th>Status</th><th>Run</th><th class="hide-sm">Project</th><th>Result</th>
      <th class="hide-sm">Started</th><th class="hide-sm">Duration</th><th class="hide-sm">Cost</th></tr></thead>
      <tbody>${list.map((r) => `
        <tr data-id="${esc(r.id)}">
          <td>${statusBadge(r.status)}</td>
          <td><a class="row-link" href="#/runs/${esc(r.id)}"><div class="title">${esc(r.title || r.test_ids.join(", "))}</div>
            <div class="id">${esc(r.id)} · ${esc(new URL(r.url).host)}</div></a></td>
          <td class="hide-sm">${r.project ? esc(names[r.project] || r.project) : '<span class="muted">—</span>'}</td>
          <td>${r.summary ? `<div style="display:grid;gap:6px">${tallyBar(r.summary)}${tally(r.summary)}</div>`
            : `<span class="muted small">${r.test_ids.length} test${r.test_ids.length === 1 ? "" : "s"}</span>`}</td>
          <td class="num hide-sm" title="${esc(r.created_at)}">${esc(relTime(r.created_at))}</td>
          <td class="num hide-sm">${esc(duration(r))}</td>
          <td class="num hide-sm">${esc(cost(r.cost_usd))}</td>
        </tr>`).join("")}</tbody></table>`;
    target.querySelectorAll("tbody tr").forEach((tr) => tr.addEventListener("click", (e) => {
      if (!e.target.closest("a")) location.hash = `#/runs/${tr.dataset.id}`;
    }));
  };

  draw(runs);
  every(async () => {
    const fresh = await api(`/runs?limit=200${project ? `&project=${encodeURIComponent(project)}` : ""}`).catch(() => null);
    if (fresh && alive()) draw(fresh);
  }, LIST_POLL_MS);
}

// ── New run ──────────────────────────────────────────────────────────

const SCENARIO_EXAMPLE = `**Steps:**
1. Log in as admin
2. Open Settings → Team
3. Invite a new member with a unique email

**Expected result:**
- The invite appears in the pending list
- A success message is shown`;

async function newRunView(params, alive) {
  const projects = await api("/projects");
  if (!alive()) return;
  const preselect = params.get("project") || (projects.length === 1 ? projects[0].slug : "");

  view.innerHTML = `
    <a class="crumb" href="#/runs">← Runs</a>
    <div class="page-head"><div>
      <h1>New run</h1>
      <p class="sub">Describe one scenario in plain English, or paste a full test plan.</p>
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
          <button type="button" data-mode="scenario" aria-pressed="true">Scenario</button>
          <button type="button" data-mode="plan" aria-pressed="false">Test plan</button>
        </div>
      </div>

      <div id="mode-scenario" class="form" style="gap:16px">
        <label class="field"><span>Name</span>
          <input name="name" placeholder="Admin can invite a team member" autocomplete="off">
        </label>
        <label class="field"><span>Steps and expected result</span>
          <textarea name="scenario" rows="7" placeholder="${esc(SCENARIO_EXAMPLE)}"></textarea>
        </label>
      </div>

      <div id="mode-plan" class="form" style="gap:16px" hidden>
        <label class="field"><span>Test plan (Markdown)</span>
          <textarea name="plan" rows="16" placeholder="### TC-001: Login with valid credentials&#10;&#10;**Steps:**&#10;1. …"></textarea>
          <span class="detected" id="detected"></span>
        </label>
        <div class="form-row">
          <label class="field"><span>Load a file</span><input type="file" accept=".md,.markdown,.txt" id="plan-file"></label>
          <label class="field"><span>Only these tests</span><input name="only" placeholder="TC-001, TC-004" autocomplete="off">
            <span class="hint">Optional, comma-separated.</span></label>
        </div>
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
      </div>
    </form>`;

  const form = view.querySelector("#run-form");
  const byName = Object.fromEntries(projects.map((p) => [p.slug, p]));
  let mode = "scenario";
  let lastFocused = form.elements.scenario;

  form.querySelectorAll("textarea").forEach((t) => t.addEventListener("focus", () => { lastFocused = t; }));

  const syncProject = () => {
    const p = byName[form.elements.project.value];
    form.elements.url.placeholder = p?.url || "https://staging.example.com";
    view.querySelector("#url-hint").textContent = p?.url ? "Leave empty to use the project's URL." : "";
    const values = p ? [
      ...Object.keys(p.credentials).flatMap((role) => [`${role}.username`, `${role}.password`]),
      ...Object.keys(p.variables), ...Object.keys(p.secrets),
    ] : [];
    view.querySelector("#placeholders").hidden = !values.length;
    view.querySelector("#placeholder-chips").innerHTML = values.map((v) => `<button type="button" class="chip" data-insert="${esc(v)}">{{${esc(v)}}}</button>`).join("");
  };
  form.elements.project.addEventListener("change", syncProject);
  syncProject();

  view.querySelector("#placeholder-chips").addEventListener("click", (e) => {
    const chip = e.target.closest("[data-insert]");
    if (!chip) return;
    const t = lastFocused;
    const text = `{{${chip.dataset.insert}}}`;
    const at = t.selectionStart ?? t.value.length;
    t.value = t.value.slice(0, at) + text + t.value.slice(t.selectionEnd ?? at);
    t.focus();
    t.selectionStart = t.selectionEnd = at + text.length;
  });

  view.querySelectorAll("[data-mode]").forEach((b) => b.addEventListener("click", () => {
    mode = b.dataset.mode;
    view.querySelectorAll("[data-mode]").forEach((x) => x.setAttribute("aria-pressed", String(x === b)));
    view.querySelector("#mode-scenario").hidden = mode !== "scenario";
    view.querySelector("#mode-plan").hidden = mode !== "plan";
    lastFocused = mode === "plan" ? form.elements.plan : form.elements.scenario;
  }));

  const detect = () => {
    const ids = [...form.elements.plan.value.matchAll(/^###\s+(TC-\d+)\s*:\s*(.+)$/gm)].map((m) => m[1]);
    view.querySelector("#detected").innerHTML = form.elements.plan.value.trim()
      ? (ids.length ? `<b>${ids.length}</b> test case${ids.length === 1 ? "" : "s"}: ${esc(ids.join(", "))}`
        : "No test cases found. Each needs a heading like <b>### TC-001: Title</b>.")
      : "";
  };
  form.elements.plan.addEventListener("input", detect);
  view.querySelector("#plan-file").addEventListener("change", async (e) => {
    const file = e.target.files[0];
    if (!file) return;
    form.elements.plan.value = await file.text();
    detect();
  });

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const err = view.querySelector("#form-error");
    err.hidden = true;
    const f = form.elements;
    const body = { parallel: Number(f.parallel.value) || 1 };
    if (f.project.value) body.project = f.project.value;
    if (f.url.value.trim()) body.url = f.url.value.trim();
    if (mode === "scenario") {
      body.scenario = f.scenario.value;
      if (f.name.value.trim()) body.name = f.name.value.trim();
    } else {
      body.plan = f.plan.value;
      const only = f.only.value.split(",").map((s) => s.trim()).filter(Boolean);
      if (only.length) body.only = only;
    }
    const btn = view.querySelector("#submit-btn");
    btn.disabled = true;
    btn.textContent = "Starting…";
    try {
      const run = await api("/runs", { method: "POST", body });
      location.hash = `#/runs/${run.id}`;
    } catch (ex) {
      err.textContent = ex.message;
      err.hidden = false;
      btn.disabled = false;
      btn.textContent = "Start run";
    }
  });
  (form.elements.project.value ? form.elements.name : form.elements.url).focus();
}

// ── Run detail ───────────────────────────────────────────────────────

async function runView(runId, alive) {
  let run = await api(`/runs/${runId}`);
  if (!alive()) return;

  let results = null;
  let shots = [];
  let tab = "tests";
  let reportHtml = null;
  let planText = null;
  let feedCursor = 0;
  let actionCount = 0;
  let lastAgent = "";
  let lastSignature = "";
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

  const drawHeader = () => {
    document.body.classList.toggle("live", ACTIVE.has(run.status));
    const onlyCase = results?.results?.length === 1 ? results.results[0].name : "";
    const title = run.title || onlyCase || run.test_ids.join(", ");
    $("#run-title").textContent = title;
    const mark = { running: "●", queued: "○", passed: "✓", failed: "✗", error: "!" }[run.status] || "";
    document.title = `${mark} ${run.status === "running" ? "Running" : run.status[0].toUpperCase() + run.status.slice(1)} · ${title} — argus-qa`.trim();
    $("#run-meta").innerHTML = [
      statusBadge(run.status),
      run.project ? `<a href="#/projects/${esc(run.project)}">${esc(run.project)}</a>` : "",
      `<a class="mono" href="${esc(run.url)}" target="_blank" rel="noopener">${esc(run.url)}</a>`,
      `<span title="${esc(run.created_at)}">${esc(relTime(run.created_at))}</span>`,
      run.started_at ? `<span class="mono">${esc(duration(run))}</span>` : "",
      run.cost_usd != null ? `<span class="mono">${esc(cost(run.cost_usd))}</span>` : "",
      `<span class="mono muted">${esc(run.id)}</span>`,
    ].filter(Boolean).join("");

    const actions = [];
    if (ACTIVE.has(run.status)) actions.push(`<button class="btn btn-danger" data-act="cancel">Cancel run</button>`);
    if (!ACTIVE.has(run.status)) {
      if (run.failed_ids.length) actions.push(`<button class="btn btn-primary" data-act="rerun-failed">Re-run ${run.failed_ids.length} failed</button>`);
      actions.push(`<button class="btn" data-act="rerun-all">Re-run all</button>`);
    }
    if (results) {
      actions.push(`<button class="btn btn-ghost btn-small" data-act="dl-junit">junit.xml</button>`);
      actions.push(`<button class="btn btn-ghost btn-small" data-act="dl-results">results.json</button>`);
    }
    $("#run-actions").innerHTML = actions.join("");

    $("#run-banner").innerHTML = run.error
      ? `<div class="banner">${esc(run.error)}</div>`
      : run.status === "queued" ? `<div class="banner info">Waiting for a free browser slot…</div>` : "";

    const s = run.summary || { passed: 0, failed: 0, blocked: 0, skipped: 0 };
    const score = (cls, n, label) => `<div class="score ${cls} ${n ? "" : "zero"}"><b>${n}</b><span>${label}</span></div>`;
    $("#scoreboard").innerHTML = run.summary
      ? score("p", s.passed, "Passed") + score("f", s.failed, "Failed") + score("b", s.blocked, "Blocked") + score("s", s.skipped, "Skipped")
      : score("", run.test_ids.length, run.test_ids.length === 1 ? "Test" : "Tests")
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
      } else if (act === "rerun-failed" || act === "rerun-all") {
        const fresh = await api(`/runs/${runId}/rerun`, { method: "POST", body: { failed_only: act === "rerun-failed" } });
        location.hash = `#/runs/${fresh.id}`;
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
    const defs = [["tests", "Tests", run.test_ids.length], ["report", "Report"], ["screenshots", "Screenshots", images], ["plan", "Plan"]];
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

  const drawTests = () => {
    if (!results) {
      const live = ACTIVE.has(run.status);
      const current = Object.fromEntries(Object.entries(agentCase).map(([agent, id]) => [id, agent]));
      return `<div>${run.test_ids.map((id) => {
        const agent = current[id];
        const state = !live ? run.status : agent ? "running" : seenCases.has(id) ? "done" : "queued";
        const now = agent && agentAction[agent]
          ? `<p class="case-now"><span>Now</span>${esc(agentAction[agent])}</p>` : "";
        return `<article class="case ${agent ? "is-current" : ""}"><div class="case-head"><span class="case-id">${esc(id)}</span>
          <span class="case-name ${caseNames[id] ? "" : "muted"}">${esc(caseNames[id] || (live ? "Waiting for results…" : "No results recorded"))}</span>
          ${statusBadge(state === "done" ? "checked" : state)}</div>${now}</article>`;
      }).join("")}
      ${live ? `<p class="muted small live-note">Step-by-step results, expected vs actual, appear here when the run finishes.</p>` : ""}</div>`;
    }
    const images = new Set(shots.filter(isImage));
    const bugs = (results.bugs || []).map((b) => `
      <div class="bug">
        <div class="bug-head"><span class="sev ${esc(String(b.severity || "").toLowerCase())}">${esc(b.severity || "bug")}</span>
          <span class="bug-title">${esc(b.title)}</span><span class="case-id">${esc(b.test_case || "")}</span></div>
        ${b.expected ? `<p><b>Expected:</b> ${esc(b.expected)}</p>` : ""}
        ${b.actual ? `<p><b>Actual:</b> ${esc(b.actual)}</p>` : ""}
      </div>`).join("");
    const cases = results.results.map((r) => `
      <article class="case">
        <div class="case-head"><span class="case-id">${esc(r.id)}</span><span class="case-name">${esc(r.name || "")}</span>${statusBadge(r.status)}</div>
        ${r.notes ? `<p class="case-notes" title="Click to expand">${esc(r.notes)}</p>` : ""}
        ${(r.steps || []).length ? `<ol class="steps">${r.steps.map((st) => `
          <li class="step ${esc(st.status || "")}">
            <div class="step-text">${esc(st.step)}</div>
            ${st.screenshot && images.has(st.screenshot) ? `<div class="step-shot">${thumb(st.screenshot, `${r.id}: ${st.step}`)}</div>` : ""}
            <dl class="step-ea">
              ${st.expected ? `<dt>Expected</dt><dd>${esc(st.expected)}</dd>` : ""}
              ${st.actual ? `<dt>Actual</dt><dd class="actual">${esc(st.actual)}</dd>` : ""}
            </dl>
          </li>`).join("")}</ol>` : ""}
      </article>`).join("");
    return (bugs ? `<section class="bugs"><h2 class="section-label">Bugs found</h2>${bugs}</section>` : "") + cases;
  };

  const drawBody = async () => {
    const body = $("#tab-body");
    if (tab === "tests") {
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
          body.innerHTML = `<p class="muted">${ACTIVE.has(run.status) ? "The report is written when all tests have finished." : "No report for this run."}</p>`;
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
    const time = `<time datetime="${esc(ev.ts)}">${esc(clock(ev.ts))}</time>`;
    const tcMatch = (ev.file || ev.text || "").match(/TC-\d+/i);
    if (ev.agent && tcMatch) {
      const id = tcMatch[0].toUpperCase();
      if (agentCase[ev.agent] && agentCase[ev.agent] !== id) seenCases.add(agentCase[ev.agent]);
      agentCase[ev.agent] = id;
      seenCases.add(id);
    }
    if (ev.kind === "phase") {
      el.innerHTML = esc(ev.text);
      lastAgent = "";
    } else if (ev.kind === "action") {
      actionCount += 1;
      if (ev.agent) agentAction[ev.agent] = ev.text;
      const [verb, ...rest] = ev.text.split(": ");
      el.innerHTML = `${time}<div class="ev-body">${who}<span class="verb">${esc(verb)}</span>${rest.length ? ` ${esc(rest.join(": "))}` : ""}</div>`;
      if (ev.file) {
        el.querySelector(".ev-body").insertAdjacentHTML("beforeend", `<img class="ev-thumb" data-shot="${esc(ev.file)}" alt="${esc(ev.file)}">`);
        setTimeout(() => hydrateImages(el, runId), 1500); // give the browser a moment to write the file
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
    // Results only exist once the run has finished
    if (!ACTIVE.has(run.status) && (!results || wasActive)) {
      results = await api(`/runs/${runId}/results`).catch(() => results);
    }
    if (!alive()) return;
    $("#feed-state").innerHTML = ACTIVE.has(run.status) ? `<span class="live-dot">● live</span>` : esc(run.status);
    drawHeader();
    drawTabs();
    // Redraw the tab only when what it shows has changed, so thumbnails don't flicker
    // Live progress only affects the Tests tab; other tabs mustn't redraw on every action
    const live = ACTIVE.has(run.status) && tab === "tests" ? JSON.stringify([agentCase, agentAction]) : "";
    const signature = `${tab}|${JSON.stringify(results)?.length}|${shots.length}|${run.status}|${live}`;
    if (signature !== lastSignature) {
      lastSignature = signature;
      await drawBody();
    }
    if (wasActive && !ACTIVE.has(run.status)) {
      reportHtml = null;
      toast(`Run ${run.status}`);
    }
    return ACTIVE.has(run.status);
  };

  planText = await api(`/runs/${runId}/plan`).catch(() => "");
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
        <p class="sub">An app under test: where it lives, which accounts to log in with, and rules the tester follows.</p>
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
        <p>A project keeps the URL, test accounts, and test data for an app, so a run only needs the scenario.</p>
        <a class="btn btn-primary" href="#/projects/new">Create a project</a>
      </div>`}`;
  view.querySelectorAll("[data-start]").forEach((b) => b.addEventListener("click", (e) => {
    e.preventDefault();
    location.hash = `#/runs/new?project=${encodeURIComponent(b.dataset.start)}`;
  }));
}

const MASK = "********";

async function projectView(slug, alive) {
  const project = slug ? await api(`/projects/${encodeURIComponent(slug)}`) : {
    name: "", url: "", description: "", instructions: "",
    credentials: { admin: { username: "", password: "", notes: "" } }, variables: {}, secrets: {},
  };
  if (!alive()) return;
  const isNew = !slug;

  view.innerHTML = `
    <a class="crumb" href="#/projects">← Projects</a>
    <div class="page-head">
      <div><h1>${isNew ? "New project" : esc(project.name)}</h1>
        ${isNew ? "" : `<p class="sub mono">${esc(project.slug)}</p>`}</div>
      <div class="page-head-actions">
        ${isNew ? "" : `<a class="btn" href="#/runs?project=${esc(project.slug)}">Runs</a>
          <a class="btn btn-primary" href="#/runs/new?project=${esc(project.slug)}">Start run</a>`}
      </div>
    </div>
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
        <span class="hint">Followed by the tester in every test.</span></label>

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
        <a class="btn btn-ghost" href="#/projects">Cancel</a>
        <span class="spacer"></span>
        ${isNew ? "" : `<button class="btn btn-danger" type="button" id="delete-btn">Delete project</button>`}
      </div>
    </form>`;

  const $ = (sel) => view.querySelector(sel);
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
    if (!confirm(`Delete project “${project.name}”? Its past runs are kept.`)) return;
    try {
      await api(`/projects/${encodeURIComponent(slug)}`, { method: "DELETE" });
      toast("Project deleted");
      location.hash = "#/projects";
    } catch (ex) {
      toast(ex.message, true);
    }
  });
  const first = form.elements.name;
  if (!first.value) first.focus();
}

// ── Boot ─────────────────────────────────────────────────────────────

(async () => {
  try {
    const health = await fetch("/health").then((r) => r.json());
    if (health.auth_required && !getKey()) askForKey();
  } catch { /* server unreachable; views will show the error */ }
  route();
})();
