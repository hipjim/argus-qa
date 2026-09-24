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
          <td><a class="row-link" href="#/runs/${esc(r.id)}"><div class="title">${r.kind !== "test" ? `<span class="kind ${esc(r.kind)}">${r.kind === "discover" ? "Discover" : "Explore"}</span>` : ""}${esc(r.kind !== "test" ? (r.brief || "Whole app") : (r.title || r.test_ids.join(", ")))}</div>
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
    const fresh = await api(`/runs?limit=200${project ? `&project=${encodeURIComponent(project)}` : ""}`).catch(() => null);
    if (fresh && alive()) draw(fresh);
  }, LIST_POLL_MS);
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
function caseEditor(container, initial, { single = false, project = null, placeholders = [], onFocus = () => {}, onChange = () => {} } = {}) {
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

  const cardHtml = (c, ci) => `
    <article class="tc-card" data-ci="${ci}">
      <header class="tc-head">
        ${single ? "" : `<span class="tc-grip tc-card-grip" draggable="true" title="Drag to reorder test cases">⋮⋮</span>`}
        <span class="case-id">${esc(c.id || "new")}</span>
        <input class="tc-title" value="${esc(c.title)}" placeholder="What does this test check?" aria-label="Test title">
        <select class="tc-priority" aria-label="Priority"><option value="">Priority</option>
          ${PRIORITIES.map((p) => `<option ${c.priority === p ? "selected" : ""}>${p}</option>`).join("")}</select>
        <select class="tc-category" aria-label="Category"><option value="">Category</option>
          ${CATEGORIES.map((p) => `<option ${c.category === p ? "selected" : ""}>${p}</option>`).join("")}</select>
        ${single ? "" : `<button type="button" class="tc-x" data-remove-case title="Remove test case">✕</button>`}
      </header>
      <div class="tc-draft">
        <input class="tc-draft-input" placeholder="Describe the test in a sentence and let AI draft the steps, e.g. “Retailer creates a 10% promotion and sees it listed as active”">
        <button type="button" class="btn btn-small" data-draft>Draft steps</button>
      </div>
      ${CASE_LISTS.map((list) => `
        <section class="tc-list" data-list="${list.key}">
          <h4>${list.label}</h4>
          <div class="tc-items">${c[list.key].map((v, j) => itemHtml(list, ci, j, v)).join("")}</div>
          <button type="button" class="tc-add" data-add="${list.key}">+ ${list.add}</button>
        </section>`).join("")}
      <details class="tc-notes" ${c.notes ? "open" : ""}>
        <summary>Notes</summary>
        <textarea class="tc-notes-input" rows="2" placeholder="Anything else the tester should know">${esc(c.notes)}</textarea>
      </details>
    </article>`;

  const draw = (focus) => {
    container.innerHTML = cases.map(cardHtml).join("")
      + (single ? "" : `<button type="button" class="btn btn-small tc-add-case" data-add-case>+ Add test case</button>`);
    container.querySelectorAll(".tc-input, .tc-title, .tc-notes-input").forEach(checkPlaceholders);
    if (focus) {
      const el = container.querySelector(focus);
      if (el) { el.focus(); el.setSelectionRange?.(el.value.length, el.value.length); }
    }
    onChange();
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
    if (e.target.classList.contains("tc-priority")) cases[ci].priority = e.target.value;
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
    if (t.closest("[data-add]")) {
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
          <button type="button" data-mode="suite" aria-pressed="false" id="suite-mode-btn" hidden>Saved suite</button>
        </div>
      </div>

      <div id="mode-scenario" class="tc-editor"></div>

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

      <div id="mode-suite" class="form" style="gap:16px" hidden>
        <label class="field"><span>Suite</span><select name="suite"></select>
          <span class="hint" id="suite-hint"></span></label>
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
  let mode = "scenario";
  let lastFocused = null;
  let scenario = null;

  form.elements.plan.addEventListener("focus", (e) => { lastFocused = e.target; });
  const buildScenarioEditor = () => {
    const slug = form.elements.project.value || null;
    scenario = caseEditor(view.querySelector("#mode-scenario"), scenario ? scenario.cases() : [], {
      single: true, project: slug, placeholders: projectPlaceholders(byName[slug]),
      onFocus: (el) => { lastFocused = el; },
    });
  };

  let suites = [];
  const setMode = (next) => {
    mode = next;
    view.querySelectorAll("[data-mode]").forEach((x) => x.setAttribute("aria-pressed", String(x.dataset.mode === mode)));
    view.querySelector("#mode-scenario").hidden = mode !== "scenario";
    view.querySelector("#mode-plan").hidden = mode !== "plan";
    view.querySelector("#mode-suite").hidden = mode !== "suite";
    view.querySelector("#placeholders").classList.toggle("off", mode === "suite");
    lastFocused = mode === "plan" ? form.elements.plan : null;
  };
  const syncSuites = async () => {
    const slug = form.elements.project.value;
    suites = slug ? await api(`/projects/${encodeURIComponent(slug)}/suites`).catch(() => []) : [];
    if (!alive() || slug !== form.elements.project.value) return;
    view.querySelector("#suite-mode-btn").hidden = !suites.length;
    form.elements.suite.innerHTML = suites.map((st) => `<option value="${esc(st.slug)}">${esc(st.name)} (${st.test_count} test${st.test_count === 1 ? "" : "s"})</option>`).join("");
    const preferred = params.get("suite");
    if (preferred && suites.some((st) => st.slug === preferred)) {
      form.elements.suite.value = preferred;
      setMode("suite");
    } else if (mode === "suite" && !suites.length) {
      setMode("scenario");
    }
  };
  const syncProject = () => {
    syncSuites();
    const p = byName[form.elements.project.value];
    form.elements.url.placeholder = p?.url || "https://staging.example.com";
    view.querySelector("#url-hint").textContent = p?.url ? "Leave empty to use the project's URL." : "";
    const values = projectPlaceholders(p);
    view.querySelector("#placeholders").hidden = !values.length;
    placeholderChips(view.querySelector("#placeholder-chips"), values, () => lastFocused);
    buildScenarioEditor();
  };
  form.elements.project.addEventListener("change", syncProject);
  syncProject();

  view.querySelectorAll("[data-mode]").forEach((b) => b.addEventListener("click", () => setMode(b.dataset.mode)));

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
    if (Number(f.max_cost.value)) body.max_cost_usd = Number(f.max_cost.value);
    if (f.ai_report.checked) body.ai_report = true;
    if (f.project.value) body.project = f.project.value;
    if (f.url.value.trim()) body.url = f.url.value.trim();
    let endpoint = "/runs";
    if (mode === "suite") {
      endpoint = `/projects/${encodeURIComponent(f.project.value)}/suites/${encodeURIComponent(f.suite.value)}/run`;
      delete body.project;
    } else if (mode === "scenario") {
      const [test] = scenario.cases();
      if (!test.steps.length) {
        err.textContent = "Add at least one step, or describe the test in a sentence and click “Draft steps”.";
        err.hidden = false;
        return;
      }
      const title = test.title.trim() || "Scenario";
      try {
        body.plan = (await api("/plans/render", {
          method: "POST", body: { title, cases: [{ ...test, title }] },
        })).plan;
      } catch (ex) {
        err.textContent = ex.message;
        err.hidden = false;
        return;
      }
    } else {
      body.plan = f.plan.value;
      const only = f.only.value.split(",").map((s) => s.trim()).filter(Boolean);
      if (only.length) body.only = only;
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
  view.querySelector(form.elements.project.value ? "#mode-scenario .tc-title" : 'input[name="url"]')?.focus();
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
    const mark = { running: "●", queued: "○", passed: "✓", completed: "✓", failed: "✗", error: "!" }[run.status] || "";
    document.title = `${mark} ${run.status === "running" ? "Running" : run.status[0].toUpperCase() + run.status.slice(1)} · ${title} — argus-qa`.trim();
    $("#run-meta").innerHTML = [
      session ? `<span class="kind ${esc(run.kind)}">${run.kind === "discover" ? "Discover" : "Explore"}</span>` : "",
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
      if (run.failed_ids.length) actions.push(`<button class="btn btn-primary" data-act="rerun-failed">Re-run ${run.failed_ids.length} failed</button>`);
      actions.push(`<button class="btn" data-act="rerun-all">Re-run all</button>`);
    }
    if (!ACTIVE.has(run.status)) {
      actions.push(`<button class="btn btn-ghost btn-small btn-danger" data-act="delete">Delete</button>`);
    }
    if (results && !session) {
      actions.push(`<button class="btn btn-ghost btn-small" data-act="dl-junit">junit.xml</button>`);
      actions.push(`<button class="btn btn-ghost btn-small" data-act="dl-results">results.json</button>`);
    }
    $("#run-actions").innerHTML = actions.join("");

    $("#run-banner").innerHTML = run.error
      ? `<div class="banner">${esc(run.error)}</div>`
      : run.status === "queued" ? `<div class="banner info">Waiting for a free browser slot…</div>` : "";

    const s = run.summary || { passed: 0, failed: 0, blocked: 0, skipped: 0 };
    const score = (cls, n, label) => `<div class="score ${cls} ${n ? "" : "zero"}"><b>${n}</b><span>${label}</span></div>`;
    const saved = run.accepted?.length || 0;
    $("#scoreboard").innerHTML = session && run.summary
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
      } else if (act === "rerun-failed" || act === "rerun-all") {
        const fresh = await api(`/runs/${runId}/rerun`, { method: "POST", body: { failed_only: act === "rerun-failed" } });
        location.hash = `#/runs/${fresh.id}`;
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
        : [["tests", "Tests", run.test_ids.length], ["report", "Report"], ["screenshots", "Screenshots", images], ["plan", "Plan"]];
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
    if (!ACTIVE.has(run.status) && (!results || wasActive) && run.kind !== "discover") {
      results = await api(`/runs/${runId}/results`).catch(() => results);
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
    const signature = `${tab}|${JSON.stringify(results)?.length}|${tab === "proposed" ? "" : shots.length}|${run.status}|${live}`;
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

  const tab = params.get("tab") === "settings" ? "settings" : "suites";
  const [project, suites, sessions] = await Promise.all([
    api(`/projects/${encodeURIComponent(slug)}`),
    api(`/projects/${encodeURIComponent(slug)}/suites`),
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
      <a role="tab" href="${base}?tab=settings" aria-selected="${tab === "settings"}">Settings</a>
    </div>
    <div id="project-body"></div>`;

  const body = view.querySelector("#project-body");
  if (tab === "settings") {
    projectSettingsForm(body, project, false);
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
    <div class="page-head">
      <div><h1>${isNew ? "New test suite" : esc(suite.name)}</h1>
        <p class="sub">${isNew ? "A saved set of test cases you can run with one click." : `${suite.test_count} test${suite.test_count === 1 ? "" : "s"} · updated ${esc(relTime(suite.updated_at))}`}</p></div>
      ${isNew ? "" : `<div class="page-head-actions">
        <label class="inline-field">Parallel <input id="suite-parallel" type="number" min="1" max="8" value="1"></label>
        <button class="btn btn-primary" id="run-suite">Run suite</button>
      </div>`}
    </div>
    <div class="run-grid ${runs.length ? "" : "no-feed"}">
      <form class="form suite-form" id="suite-form" novalidate>
        <label class="field"><span>Suite name</span><input name="name" required value="${esc(suite?.name || "")}" placeholder="Smoke tests"></label>
        <div class="suite-toolbar">
          <div class="segmented" role="group" aria-label="Editing mode">
            <button type="button" data-edit-mode="fields" aria-pressed="true">Editor</button>
            <button type="button" data-edit-mode="markdown" aria-pressed="false">Markdown</button>
          </div>
          <span class="detected" id="detected"></span>
        </div>
        ${placeholders.length ? `<div class="field values-bar"><span>Project values <span class="muted">— click to insert into the field you're editing</span></span>
          <div class="chips" id="suite-chips"></div></div>` : ""}
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
          <span class="spacer"></span>
          ${isNew ? "" : `<button class="btn btn-danger" type="button" id="delete-suite">Delete suite</button>`}
        </div>
      </form>
      <aside class="side-list" aria-label="Recent runs">
        <h2 class="section-label">Recent runs</h2>
        ${runs.map((r) => `<a class="side-row" href="#/runs/${esc(r.id)}">${statusBadge(r.status)}
          <span>${r.summary ? tally(r.summary) : ""}</span><span class="muted small">${esc(relTime(r.created_at))}</span></a>`).join("")}
      </aside>
    </div>`;

  const form = view.querySelector("#suite-form");
  const $ = (sel) => view.querySelector(sel);
  let mode = "fields";
  let editor = null;
  let lastField = null;

  const countCases = () => {
    const n = mode === "fields"
      ? editor.cases().filter((c) => c.title.trim() || c.steps.length).length
      : [...form.elements.plan.value.matchAll(/^###\s+TC-\d+\s*:/gm)].length;
    $("#detected").textContent = `${n} test case${n === 1 ? "" : "s"}`;
  };
  const showEditor = () => {
    editor = caseEditor($("#cases"), plan.cases, {
      project: slug, placeholders, onFocus: (el) => { lastField = el; }, onChange: () => editor && countCases(),
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
    if (mode === "fields") showEditor(); else countCases();
  };
  view.querySelectorAll("[data-edit-mode]").forEach((b) => b.addEventListener("click", () => setMode(b.dataset.editMode)));
  form.elements.plan.addEventListener("input", countCases);
  form.elements.plan.addEventListener("focus", (e) => { lastField = e.target; });
  if (placeholders.length) placeholderChips($("#suite-chips"), placeholders, () => lastField);
  showEditor();

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const err = $("#form-error");
    err.hidden = true;
    try {
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
        method: "POST", body: { parallel: Number($("#suite-parallel").value) || 1 },
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
