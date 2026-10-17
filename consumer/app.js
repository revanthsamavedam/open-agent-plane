/* PocketPlane — the personal edition of Open Agent Plane.
 * "Your agents. Your model. In your pocket."
 *
 * Persistence: a REAL SQLite database running in this browser.
 *
 * Engine choice: sql.js — SQLite compiled to WebAssembly, vendored in
 * /assets/ (sql-wasm.js + sql-wasm.wasm), so the app is fully
 * self-contained and never hot-links a CDN at runtime. sql.js runs the
 * genuine SQLite engine on the main thread; the database bytes are
 * persisted durably in IndexedDB (db.export() after writes, reloaded on
 * boot) and can be downloaded as an actual .sqlite file from Settings.
 *
 * ALL state lives in SQLite tables — agents, messages, settings,
 * activity, meta, memories, summaries, workflows, workflow_steps.
 * There is NO parallel
 * localStorage store: localStorage is read exactly once, to import
 * pre-0.2.0 data (the old LS_KEY) into SQLite on first boot, after
 * which the key is removed. If SQLite/WASM fails to load, the app stops
 * with a clear error rather than silently falling back to localStorage.
 *
 * The API token is a row in the settings table: it lives only on this
 * device, is sent per-request to the stateless /api proxy, and the
 * server never persists, logs, or echoes it.
 *
 * Summaries are DERIVED VIEWS, never replacements: a saved summary
 * records the exact source message/memory ids it was generated from,
 * the model (or DEMO extractive digest) that produced it, and when.
 * Originals are never deleted or altered by summarization, and nothing
 * is stored until the user reviews the draft and clicks Save.
 */
"use strict";

const LS_KEY = "oapPersonal.v1"; // legacy pre-0.2.0 storage — import source only
const API = ""; // same origin
const APP_VERSION = "0.4.0";

const PROVIDER_LABELS = {
  openai: "OpenAI",
  anthropic: "Anthropic (Claude)",
  openrouter: "OpenRouter",
  google: "Google (Gemini)",
  custom: "Custom endpoint",
  local: "On-device",
};

const MODEL_PRESETS = {
  openai: ["gpt-4o-mini", "gpt-4o", "gpt-4.1-mini", "gpt-4.1"],
  anthropic: ["claude-sonnet-4-5", "claude-haiku-4-5", "claude-opus-4-1"],
  openrouter: ["openai/gpt-4o-mini", "anthropic/claude-sonnet-4-5", "meta-llama/llama-3.3-70b-instruct"],
  google: ["gemini-2.5-flash", "gemini-2.5-pro", "gemini-2.0-flash"],
  custom: [],
};

/* ============================================================ SQLite ====
 *
 * Schema (version tracked with PRAGMA user_version; currently v3):
 *   agents    (id, name, emoji, persona, model_override, created_at)
 *   messages  (id, agent_id FK, role, content, ts, usage_json,
 *              demo, model, provider, mid)   -- mid is a stable
 *              client-side message id (added in v2) so summaries can
 *              cite exact source messages even though save() rewrites
 *              rows (autoincrement ids are NOT stable across saves)
 *   settings  (key, value)               -- provider/model/base_url/token
 *   activity  (id, ts, agent_id, provider, model, tokens, note)
 *   meta      (key, value)               -- UI state + bookkeeping
 *   memories  (id, agent_id NULL, scope 'agent'|'personal', content,
 *              created_at)                 -- v2: user memory facts
 *   summaries (id, scope, agent_id NULL, title, content,
 *              source_message_ids JSON, source_memory_ids JSON,
 *              model, provider, demo, created_at)
 *              -- v2: derived digests with full provenance
 *   workflows (id, task, lead_agent_id, status, plan_source, demo,
 *              model, provider, synthesis, error, created_at,
 *              finished_at)          -- v3: Team-mode runs
 *   workflow_steps (id, workflow_id FK, step_key, agent_id, title,
 *              instruction, position, status, output, error, ts)
 *              -- v3: one row per planned step of a workflow; steps are
 *              stored ONLY here — never in the agents' chat threads
 * ====================================================================== */

const SCHEMA_VERSION = 3;
const IDB_NAME = "oapPersonal";
const IDB_STORE = "kv";
const IDB_DB_KEY = "sqlite-bytes";

let SQL = null;       // sql.js module object
let db = null;        // sql.js Database instance
let dbReady = false;  // true once boot has hydrated state from SQLite
let persistTimer = null;

/* ----------------------------------------------------- IndexedDB shell */

let _idbHandle = null;
function idbOpen() {
  if (_idbHandle) return Promise.resolve(_idbHandle);
  return new Promise((resolve, reject) => {
    const req = indexedDB.open(IDB_NAME, 1);
    req.onupgradeneeded = () => { req.result.createObjectStore(IDB_STORE); };
    req.onsuccess = () => { _idbHandle = req.result; resolve(_idbHandle); };
    req.onerror = () => reject(req.error);
  });
}

async function idbGet(key) {
  const handle = await idbOpen();
  return new Promise((resolve, reject) => {
    const r = handle.transaction(IDB_STORE).objectStore(IDB_STORE).get(key);
    r.onsuccess = () => resolve(r.result || null);
    r.onerror = () => reject(r.error);
  });
}

async function idbPut(key, value) {
  const handle = await idbOpen();
  return new Promise((resolve, reject) => {
    const tx = handle.transaction(IDB_STORE, "readwrite");
    tx.objectStore(IDB_STORE).put(value, key);
    tx.oncomplete = () => resolve();
    tx.onerror = () => reject(tx.error);
  });
}

/* Durable save: serialize the SQLite file into IndexedDB. Debounced for
 * bursts of mutations; also flushed on pagehide / tab-hidden so a chat
 * exchange is never lost to a closed tab. */
function persistNow() {
  if (!db) return Promise.resolve();
  let bytes;
  try { bytes = db.export(); } catch (e) { return Promise.resolve(); }
  return idbPut(IDB_DB_KEY, bytes).catch((e) =>
    console.warn("PocketPlane: IndexedDB persist failed", e));
}

function schedulePersist() {
  clearTimeout(persistTimer);
  persistTimer = setTimeout(persistNow, 200);
}

window.addEventListener("pagehide", () => { persistNow(); });
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "hidden") persistNow();
});

/* ------------------------------------------------------------ SQL bits */

function q(sql, params = []) {
  const stmt = db.prepare(sql);
  try {
    if (params && params.length) stmt.bind(params);
    const rows = [];
    while (stmt.step()) rows.push(stmt.getAsObject());
    return rows;
  } finally {
    stmt.free();
  }
}

function run(sql, params = []) { db.run(sql, params); }

function getMeta(key, dflt = null) {
  const rows = q("SELECT value FROM meta WHERE key = ?", [key]);
  return rows.length ? rows[0].value : dflt;
}

function setMeta(key, value) {
  run("INSERT INTO meta(key, value) VALUES(?, ?) " +
      "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
      [key, String(value)]);
}

function userVersion() {
  const r = db.exec("PRAGMA user_version");
  return r.length && r[0].values.length ? r[0].values[0][0] : 0;
}

function tableColumns(table) {
  return q(`PRAGMA table_info(${table})`).map((r) => r.name);
}

function migrate() {
  let v = userVersion();
  if (v < 1) {
    db.exec(`
      CREATE TABLE IF NOT EXISTS agents (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        emoji TEXT NOT NULL DEFAULT '',
        persona TEXT NOT NULL DEFAULT '',
        model_override TEXT NOT NULL DEFAULT '',
        created_at INTEGER NOT NULL DEFAULT 0
      );
      CREATE TABLE IF NOT EXISTS messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        agent_id TEXT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
        role TEXT NOT NULL,
        content TEXT NOT NULL,
        ts INTEGER NOT NULL DEFAULT 0,
        usage_json TEXT,
        demo INTEGER NOT NULL DEFAULT 0,
        model TEXT,
        provider TEXT
      );
      CREATE INDEX IF NOT EXISTS idx_messages_agent ON messages(agent_id, id);
      CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL DEFAULT ''
      );
      CREATE TABLE IF NOT EXISTS activity (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts INTEGER NOT NULL DEFAULT 0,
        agent_id TEXT,
        provider TEXT,
        model TEXT,
        tokens INTEGER,
        note TEXT
      );
      CREATE TABLE IF NOT EXISTS meta (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL DEFAULT ''
      );
      PRAGMA user_version = 1;
    `);
    v = 1;
  }
  if (v < 2) {
    // v2: memory facts + governed summaries, and a stable per-message
    // id (mid) so summaries can cite sources. Existing tables and the
    // token row are untouched; v1 databases upgrade in place.
    if (!tableColumns("messages").includes("mid")) {
      db.exec("ALTER TABLE messages ADD COLUMN mid TEXT");
    }
    db.exec(`
      CREATE TABLE IF NOT EXISTS memories (
        id TEXT PRIMARY KEY,
        agent_id TEXT,
        scope TEXT NOT NULL DEFAULT 'personal',
        content TEXT NOT NULL,
        created_at INTEGER NOT NULL DEFAULT 0
      );
      CREATE TABLE IF NOT EXISTS summaries (
        id TEXT PRIMARY KEY,
        scope TEXT NOT NULL DEFAULT 'personal',
        agent_id TEXT,
        title TEXT NOT NULL DEFAULT '',
        content TEXT NOT NULL,
        source_message_ids TEXT NOT NULL DEFAULT '[]',
        source_memory_ids TEXT NOT NULL DEFAULT '[]',
        model TEXT,
        provider TEXT,
        demo INTEGER NOT NULL DEFAULT 0,
        created_at INTEGER NOT NULL DEFAULT 0
      );
      PRAGMA user_version = 2;
    `);
    v = 2;
  }
  if (v < 3) {
    // v3: Team mode — workflows and their steps. Everything else
    // (agents, messages, settings incl. the token, memories,
    // summaries, activity) is untouched; v2 databases upgrade in place.
    db.exec(`
      CREATE TABLE IF NOT EXISTS workflows (
        id TEXT PRIMARY KEY,
        task TEXT NOT NULL DEFAULT '',
        lead_agent_id TEXT,
        status TEXT NOT NULL DEFAULT 'draft',
        plan_source TEXT NOT NULL DEFAULT 'template',
        demo INTEGER NOT NULL DEFAULT 0,
        model TEXT,
        provider TEXT,
        synthesis TEXT,
        error TEXT,
        created_at INTEGER NOT NULL DEFAULT 0,
        finished_at INTEGER NOT NULL DEFAULT 0
      );
      CREATE TABLE IF NOT EXISTS workflow_steps (
        id TEXT PRIMARY KEY,
        workflow_id TEXT NOT NULL REFERENCES workflows(id) ON DELETE CASCADE,
        step_key TEXT NOT NULL DEFAULT '',
        agent_id TEXT,
        title TEXT NOT NULL DEFAULT '',
        instruction TEXT NOT NULL DEFAULT '',
        position INTEGER NOT NULL DEFAULT 0,
        status TEXT NOT NULL DEFAULT 'pending',
        output TEXT,
        error TEXT,
        ts INTEGER NOT NULL DEFAULT 0
      );
      CREATE INDEX IF NOT EXISTS idx_wf_steps ON workflow_steps(workflow_id, position);
      PRAGMA user_version = 3;
    `);
    v = 3;
  }
  return v;
}

/* Debug/test hook: run any SQL against the live database, e.g.
 * AgentPlaneDB.query("SELECT count(*) AS n FROM agents") */
window.AgentPlaneDB = {
  query: (sql, params = []) => q(sql, params),
  version: SCHEMA_VERSION,
};

/* ------------------------------------------------------- state <-> SQL */

let _midCounter = 0;
function newMid() {
  _midCounter += 1;
  return "msg-" + Date.now().toString(36) + "-" + _midCounter.toString(36) +
         Math.floor(Math.random() * 1e6).toString(36);
}

/* The in-memory `state` object mirrors the tables for the UI layer.
 * save() is a full write-through sync (state -> tables, in one
 * transaction) — data volumes here are a handful of agents, a few
 * hundred messages and max 500 activity rows, so this is cheap and
 * keeps exactly one write path. */
function save() {
  if (!dbReady || !db) return; // boot persists once hydration completes
  db.exec("BEGIN");
  try {
    run("DELETE FROM workflow_steps");
    run("DELETE FROM workflows");
    run("DELETE FROM messages");
    run("DELETE FROM activity");
    run("DELETE FROM agents");
    run("DELETE FROM settings");
    run("DELETE FROM memories");
    run("DELETE FROM summaries");
    for (const a of state.agents) {
      run("INSERT INTO agents(id, name, emoji, persona, model_override, created_at) " +
          "VALUES(?, ?, ?, ?, ?, ?)",
          [a.id, a.name, a.emoji || "", a.persona || "", a.model || "",
           a.created || 0]);
    }
    for (const a of state.agents) {
      for (const m of (state.threads[a.id] || [])) {
        if (!m.mid) m.mid = newMid();
        run("INSERT INTO messages(agent_id, role, content, ts, usage_json, demo, model, provider, mid) " +
            "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [a.id, m.role, m.content, m.ts || 0,
             m.usage ? JSON.stringify(m.usage) : null,
             m.demo ? 1 : 0, m.model || null, m.provider || null, m.mid]);
      }
    }
    const s = state.settings;
    for (const k of ["provider", "model", "base_url", "token"]) {
      run("INSERT INTO settings(key, value) VALUES(?, ?)", [k, s[k] || ""]);
    }
    for (const e of state.activity) {
      const note = JSON.stringify({
        agent_name: e.agent || "",
        prompt_tokens: e.prompt_tokens != null ? e.prompt_tokens : null,
        completion_tokens: e.completion_tokens != null ? e.completion_tokens : null,
      });
      run("INSERT INTO activity(ts, agent_id, provider, model, tokens, note) " +
          "VALUES(?, ?, ?, ?, ?, ?)",
          [e.ts || 0,
           e.agentId || findAgentIdByName(e.agent) || e.agent || null,
           e.provider || null, e.model || null,
           e.total_tokens != null ? e.total_tokens : null, note]);
    }
    for (const mem of state.memories) {
      run("INSERT INTO memories(id, agent_id, scope, content, created_at) " +
          "VALUES(?, ?, ?, ?, ?)",
          [mem.id, mem.agentId || null, mem.scope, mem.content,
           mem.created || 0]);
    }
    for (const sm of state.summaries) {
      run("INSERT INTO summaries(id, scope, agent_id, title, content, " +
          "source_message_ids, source_memory_ids, model, provider, demo, created_at) " +
          "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
          [sm.id, sm.scope, sm.agentId || null, sm.title || "", sm.content,
           JSON.stringify(sm.sourceMessageIds || []),
           JSON.stringify(sm.sourceMemoryIds || []),
           sm.model || null, sm.provider || null, sm.demo ? 1 : 0,
           sm.created || 0]);
    }
    for (const wf of state.workflows) {
      run("INSERT INTO workflows(id, task, lead_agent_id, status, plan_source, " +
          "demo, model, provider, synthesis, error, created_at, finished_at) " +
          "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
          [wf.id, wf.task || "", wf.leadAgentId || null, wf.status || "draft",
           wf.planSource || "template", wf.demo ? 1 : 0, wf.model || null,
           wf.provider || null, wf.synthesis || null, wf.error || null,
           wf.created || 0, wf.finished || 0]);
      (wf.steps || []).forEach((st, i) => {
        run("INSERT INTO workflow_steps(id, workflow_id, step_key, agent_id, " +
            "title, instruction, position, status, output, error, ts) " +
            "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [st.id, wf.id, st.key || "", st.agentId || null, st.title || "",
             st.instruction || "", i, st.status || "pending",
             st.output || null, st.error || null, st.ts || 0]);
      });
    }
    setMeta("active_agent_id", state.activeAgentId || "");
    setMeta("banner_dismissed", state.bannerDismissed ? "1" : "0");
    setMeta("welcome_dismissed", state.welcomeDismissed ? "1" : "0");
    setMeta("team_open_id", state.teamOpenId || "");
    setMeta("view", state.view || "chat");
    setMeta("use_summaries_ctx", state.useSummaries === false ? "0" : "1");
    db.exec("COMMIT");
  } catch (err) {
    try { db.exec("ROLLBACK"); } catch (e) { /* already rolled back */ }
    throw err;
  }
  schedulePersist();
}

function findAgentIdByName(name) {
  const a = state.agents.find((x) => x.name === name);
  return a ? a.id : null;
}

function parseJsonList(text) {
  try {
    const v = JSON.parse(text || "[]");
    return Array.isArray(v) ? v.map(String) : [];
  } catch (e) { return []; }
}

function hydrate() {
  const settings = { provider: "openai", model: "gpt-4o-mini", base_url: "", token: "" };
  for (const r of q("SELECT key, value FROM settings")) {
    if (r.key in settings) settings[r.key] = r.value;
  }
  const agents = q("SELECT * FROM agents ORDER BY created_at, id").map((r) => ({
    id: r.id, name: r.name, emoji: r.emoji, persona: r.persona,
    model: r.model_override, created: r.created_at,
  }));
  const threads = {};
  for (const r of q("SELECT * FROM messages ORDER BY agent_id, id")) {
    const m = { role: r.role, content: r.content, ts: r.ts,
                mid: r.mid || ("msg-row-" + r.id) };
    if (r.usage_json) { try { m.usage = JSON.parse(r.usage_json); } catch (e) { /* skip */ } }
    if (r.demo) m.demo = true;
    if (r.model) m.model = r.model;
    if (r.provider) m.provider = r.provider;
    (threads[r.agent_id] = threads[r.agent_id] || []).push(m);
  }
  const activity = q("SELECT * FROM activity ORDER BY id DESC").map((r) => {
    let note = {};
    try { note = JSON.parse(r.note || "{}"); } catch (e) { /* empty note */ }
    return {
      ts: r.ts, agentId: r.agent_id,
      agent: note.agent_name || r.agent_id || "",
      provider: r.provider, model: r.model,
      prompt_tokens: note.prompt_tokens != null ? note.prompt_tokens : null,
      completion_tokens: note.completion_tokens != null ? note.completion_tokens : null,
      total_tokens: r.tokens,
    };
  });
  const memories = q("SELECT * FROM memories ORDER BY created_at, id").map((r) => ({
    id: r.id, agentId: r.agent_id || null, scope: r.scope || "personal",
    content: r.content, created: r.created_at,
  }));
  const summaries = q("SELECT * FROM summaries ORDER BY created_at DESC, id DESC").map((r) => ({
    id: r.id, scope: r.scope || "personal", agentId: r.agent_id || null,
    title: r.title || "", content: r.content,
    sourceMessageIds: parseJsonList(r.source_message_ids),
    sourceMemoryIds: parseJsonList(r.source_memory_ids),
    model: r.model || "", provider: r.provider || "",
    demo: !!r.demo, created: r.created_at,
  }));
  const stepsByWf = {};
  for (const r of q("SELECT * FROM workflow_steps ORDER BY workflow_id, position")) {
    (stepsByWf[r.workflow_id] = stepsByWf[r.workflow_id] || []).push({
      id: r.id, key: r.step_key, agentId: r.agent_id || null,
      title: r.title, instruction: r.instruction,
      status: r.status || "pending",
      output: r.output || "", error: r.error || "", ts: r.ts || 0,
    });
  }
  const workflows = q("SELECT * FROM workflows ORDER BY created_at DESC, id DESC").map((r) => ({
    id: r.id, task: r.task || "", leadAgentId: r.lead_agent_id || null,
    status: r.status || "draft", planSource: r.plan_source || "template",
    demo: !!r.demo, model: r.model || "", provider: r.provider || "",
    synthesis: r.synthesis || "", error: r.error || "",
    created: r.created_at || 0, finished: r.finished_at || 0,
    steps: stepsByWf[r.id] || [],
  }));
  return {
    settings,
    agents,
    threads,
    activity,
    memories,
    summaries,
    workflows,
    activeAgentId: getMeta("active_agent_id") || (agents[0] && agents[0].id) || "",
    bannerDismissed: getMeta("banner_dismissed") === "1",
    welcomeDismissed: getMeta("welcome_dismissed") === "1" ||
                       getMeta("banner_dismissed") === "1",
    teamOpenId: getMeta("team_open_id") || "",
    view: getMeta("view") || "chat",
    useSummaries: getMeta("use_summaries_ctx") !== "0",
  };
}

/* ============================================================== seeds == */

function seedAgents() {
  const now = Date.now();
  return [
    {
      id: "a-general", name: "General Assistant", emoji: "🤖",
      persona: "You are a helpful, friendly personal assistant. Answer clearly and concisely, and ask a short follow-up when it would genuinely help.",
      model: "", created: now,
    },
    {
      id: "a-writing", name: "Writing Coach", emoji: "✍️",
      persona: "You are a supportive writing coach. Help the user draft, tighten and polish their writing. Offer concrete rewrites, explain why a change helps, and keep their voice intact.",
      model: "", created: now + 1,
    },
    {
      id: "a-coding", name: "Coding Buddy", emoji: "💻",
      persona: "You are a pragmatic coding partner. Give working code with brief explanations, call out edge cases and pitfalls, and prefer simple solutions over clever ones.",
      model: "", created: now + 2,
    },
  ];
}

function defaultState() {
  const agents = seedAgents();
  return {
    settings: { provider: "openai", model: "gpt-4o-mini", base_url: "", token: "" },
    agents,
    threads: {},        // agentId -> [{mid, role, content, ts, demo?, usage?, provider?, model?}]
    activity: [],       // [{ts, agentId, agent, provider, model, prompt_tokens, completion_tokens, total_tokens}]
    memories: [],       // [{id, agentId|null, scope, content, created}]
    summaries: [],      // [{id, scope, agentId|null, title, content, sourceMessageIds, sourceMemoryIds, model, provider, demo, created}]
    workflows: [],      // [{id, task, leadAgentId, status, planSource, demo, model, provider, synthesis, error, created, finished, steps:[{id, key, agentId, title, instruction, status, output, error, ts}]}]
    activeAgentId: agents[0].id,
    bannerDismissed: false,
    welcomeDismissed: false,
    teamOpenId: "",
    view: "chat",
    useSummaries: true,
  };
}

/* One-time import of pre-0.2.0 localStorage state into the shape the
 * SQL writer expects. Threads/messages/settings (incl. the token, if
 * present) and activity all come across. */
function stateFromLegacy(parsed) {
  const base = defaultState();
  const merged = {
    ...base, ...parsed,
    settings: { ...base.settings, ...(parsed.settings || {}) },
    agents: Array.isArray(parsed.agents) && parsed.agents.length ? parsed.agents : base.agents,
    threads: parsed.threads || {},
    activity: Array.isArray(parsed.activity) ? parsed.activity : [],
    memories: Array.isArray(parsed.memories) ? parsed.memories : [],
    summaries: Array.isArray(parsed.summaries) ? parsed.summaries : [],
    workflows: Array.isArray(parsed.workflows) ? parsed.workflows : [],
  };
  const now = Date.now();
  merged.agents = merged.agents.map((a, i) => ({ ...a, created: a.created || now + i }));
  for (const tid of Object.keys(merged.threads)) {
    merged.threads[tid] = (merged.threads[tid] || []).map((m) =>
      m.mid ? m : { ...m, mid: newMid() });
  }
  return merged;
}

let state = defaultState();
let editingAgentId = null;
let editingMemoryId = null;
let summaryDraft = null; // unsaved summary draft (the human gate)
let sending = false;
let summarizing = false;
let teamRunning = false;  // a Team workflow is executing
let teamCancel = false;   // cancel flag, checked between steps
let connProvider = "openai"; // provider selected in the connect sheet

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
}[c]));
const VIEWS = ["chat", "team", "agents", "memory", "activity", "settings"];

function activeAgent() {
  return state.agents.find((a) => a.id === state.activeAgentId) || state.agents[0];
}
function agentById(id) { return state.agents.find((a) => a.id === id) || null; }
function hasToken() { return !!(state.settings.token || "").trim(); }
function effectiveModel(agent) {
  return (agent.model || "").trim() || state.settings.model;
}
function scopeLabel(scope, agentId) {
  if (scope === "agent") {
    const a = agentById(agentId);
    return a ? `${a.emoji || "🤖"} ${a.name}` : "Agent";
  }
  return "Personal workspace";
}

/* ---------------------------------------------------------------- toasts */

function toast(msg, kind = "ok") {
  const box = $("toasts");
  if (!box) return;
  const el = document.createElement("div");
  el.className = "toast toast-" + kind;
  el.textContent = msg;
  box.appendChild(el);
  requestAnimationFrame(() => el.classList.add("show"));
  setTimeout(() => {
    el.classList.remove("show");
    setTimeout(() => el.remove(), 350);
  }, 3400);
}

/* ------------------------------------------------------------------ nav */

function goto(view) {
  if (!VIEWS.includes(view)) view = "chat";
  state.view = view;
  save();
  for (const v of VIEWS) {
    $("view-" + v).hidden = v !== view;
  }
  document.querySelectorAll(".nav-btn").forEach((b) =>
    b.classList.toggle("active", b.dataset.goto === view));
  if (view === "agents") renderAgents();
  if (view === "team") renderTeam();
  if (view === "memory") renderMemory();
  if (view === "activity") renderActivity();
  if (view === "settings") renderSettings();
  if (view === "chat") renderChat();
  window.scrollTo(0, 0);
}

document.querySelectorAll("[data-goto]").forEach((el) =>
  el.addEventListener("click", () => goto(el.dataset.goto)));

/* ------------------------------------------------------------------ chat */

function renderMode() {
  const badge = $("modeBadge");
  if (hasToken()) {
    badge.textContent = "● Connected · " + (PROVIDER_LABELS[state.settings.provider] || state.settings.provider);
    badge.className = "badge badge-live";
  } else {
    badge.textContent = "● Demo mode";
    badge.className = "badge badge-demo";
  }
  // Welcome card: only for a not-yet-connected user who hasn't
  // dismissed it. Connecting a model retires it for good.
  $("welcomeCard").hidden = hasToken() || !!state.welcomeDismissed;
}

/* ------------------------------------------------------- onboarding
 *
 * Demo-first: a brand-new user lands in Chat, agents seeded, replies
 * simulated — zero setup required. Connecting a model is ONE optional
 * step: the connect sheet (provider cards + one key field + Connect).
 * It writes the same settings the Settings page edits, auto-picking
 * the provider's default model; Settings stays the advanced surface.
 * ------------------------------------------------------------------ */

function dismissWelcome() {
  state.welcomeDismissed = true;
  state.bannerDismissed = true; // keep the retired hero flag in sync
  save(); renderMode();
}
$("welcomeDismiss").addEventListener("click", dismissWelcome);
$("welcomeClose").addEventListener("click", dismissWelcome);
$("welcomeConnect").addEventListener("click", () => openConnectSheet());
$("modeBadge").addEventListener("click", () => {
  if (hasToken()) goto("settings"); else openConnectSheet();
});
$("quickConnect").addEventListener("click", () => openConnectSheet());

function showConnectSheet(open) { $("connectSheet").hidden = !open; }

function selectConnProvider(p) {
  connProvider = p;
  document.querySelectorAll("[data-conn-provider]").forEach((b) =>
    b.classList.toggle("selected", b.dataset.connProvider === p));
  $("connBaseRow").hidden = p !== "custom";
}

document.querySelectorAll("[data-conn-provider]").forEach((b) =>
  b.addEventListener("click", () => selectConnProvider(b.dataset.connProvider)));
$("connectSheet").addEventListener("click", (ev) => {
  if (ev.target === $("connectSheet")) showConnectSheet(false);
});
$("connSkip").addEventListener("click", () => showConnectSheet(false));

function openConnectSheet() {
  // Start from what's already in Settings (default: OpenAI).
  const p = state.settings.provider === "openrouter"
    ? "custom" : (state.settings.provider || "openai");
  selectConnProvider(p);
  $("connKey").value = state.settings.token || "";
  $("connBaseUrl").value = state.settings.base_url || "";
  $("connError").hidden = true;
  showConnectSheet(true);
  setTimeout(() => $("connKey").focus(), 60);
}

$("connectForm").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const key = $("connKey").value.trim();
  const errBox = $("connError");
  errBox.hidden = true;
  if (!key) {
    errBox.hidden = false;
    errBox.textContent = "Paste your API key first — or tap “Skip for now” and stay in demo mode.";
    return;
  }
  // Model is never asked here: the provider's sensible default.
  const presets = MODEL_PRESETS[connProvider] || [];
  const model = presets[0] ||
    (connProvider === "custom" ? (state.settings.model || "gpt-4o-mini") : "gpt-4o-mini");
  const baseUrl = connProvider === "custom" ? $("connBaseUrl").value.trim() : "";
  const btn = $("connConnect");
  btn.disabled = true;
  btn.textContent = "Connecting…";
  try {
    const resp = await fetch(API + "/api/test-key", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        provider: connProvider, model, api_key: key,
        base_url: connProvider === "custom" ? baseUrl : undefined,
      }),
    });
    let data = {};
    try { data = await resp.json(); } catch (e) { /* ignore */ }
    if (!resp.ok || !data.ok) {
      throw new Error(data.detail || `That didn't work (${resp.status}). Check the key and try again.`);
    }
    state.settings = { provider: connProvider, model, base_url: baseUrl, token: key };
    state.welcomeDismissed = true;
    save();
    showConnectSheet(false);
    renderMode();
    if (state.view === "settings") renderSettings();
    if (state.view === "chat") renderChat();
    toast("Connected — you're live");
  } catch (err) {
    errBox.hidden = false;
    errBox.textContent = "❌ " + err.message;
  } finally {
    btn.disabled = false;
    btn.textContent = "Connect";
  }
});

function renderChat() {
  renderMode();
  const tabs = $("agentTabs");
  tabs.innerHTML = state.agents.map((a) =>
    `<button class="agent-tab ${a.id === state.activeAgentId ? "active" : ""}" data-agent="${esc(a.id)}"><span class="tab-emoji">${esc(a.emoji || "🤖")}</span>${esc(a.name)}</button>`
  ).join("");
  tabs.querySelectorAll("[data-agent]").forEach((b) =>
    b.addEventListener("click", () => {
      state.activeAgentId = b.dataset.agent; save(); renderChat();
    }));

  const agent = activeAgent();
  $("chatAvatar").innerHTML = `<span class="avatar-inner">${esc(agent.emoji || "🤖")}</span>`;
  $("threadTitle").textContent = agent.name;
  $("threadSub").textContent =
    `${effectiveModel(agent)} · ${PROVIDER_LABELS[state.settings.provider] || state.settings.provider}`;

  const thread = state.threads[agent.id] || [];
  const box = $("messages");
  if (!thread.length) {
    const chips = starterChips(agent).map((c) =>
      `<button type="button" class="starter-chip" data-chip="${esc(c)}">${esc(c)}</button>`).join("");
    box.innerHTML = `<div class="empty-thread"><div class="empty-emoji">${esc(agent.emoji || "🤖")}</div><p>Say hello to <strong>${esc(agent.name)}</strong>.</p><p class="hint">${esc(agent.persona.slice(0, 160))}${agent.persona.length > 160 ? "…" : ""}</p><div class="starter-chips">${chips}</div></div>`;
    box.querySelectorAll("[data-chip]").forEach((b) =>
      b.addEventListener("click", () => {
        $("composerInput").value = b.dataset.chip;
        $("composer").requestSubmit();
      }));
  } else {
    box.innerHTML = thread.map((m) => {
      let meta = "";
      if (m.role === "assistant") {
        const bits = [];
        if (m.model) bits.push(esc(m.model));
        if (m.usage && m.usage.total_tokens != null) bits.push(esc(m.usage.total_tokens) + " tokens");
        if (m.demo) bits.push('<span class="demo-tag">demo reply</span>');
        if (bits.length) meta = `<span class="meta">${bits.join(" · ")}</span>`;
      }
      return `<div class="msg ${esc(m.role)}">${esc(m.content)}${meta}</div>`;
    }).join("");
    // Keep the newest message in view — but only when there IS a
    // thread. On an empty thread (e.g. first run, welcome card above)
    // scrolling here would push the top of the page out of sight.
    box.scrollIntoView({ block: "end" });
  }
}

$("clearThread").addEventListener("click", () => {
  const agent = activeAgent();
  if (!confirm(`Clear the whole conversation with ${agent.name}?`)) return;
  state.threads[agent.id] = [];
  save(); renderChat();
});

/* Tappable first-chat starters, shown only while a thread is empty.
 * Agent-flavoured where a seed agent matches; generic otherwise. */
function starterChips(agent) {
  const byId = {
    "a-writing": ["Help me write a thank-you note", "Make this sentence better: 'I am writing to inform you…'", "Give me a story idea"],
    "a-coding": ["Explain recursion like I'm five", "Write a haiku about debugging", "What's a clean way to name variables?"],
  };
  return byId[agent.id] ||
    ["What can you do?", "Plan my day", "Write a haiku about mornings"];
}

function demoReply(agent, userText) {
  const name = agent.name;
  const replies = [
    `I'm ${name}, running in demo mode — this is a simulated reply, not a real model. Connect your model and I'll answer "${userText.slice(0, 60)}" for real.`,
    `Demo mode here. I can see you asked about "${userText.slice(0, 60)}" — once you connect your model, ${name} will give you a genuine, personalised answer in this persona.`,
    `This is a canned demo response from ${name}. PocketPlane works with your own OpenAI, Claude, Gemini or OpenRouter key — and your key stays on this device.`,
  ];
  const thread = state.threads[agent.id] || [];
  return replies[thread.length % replies.length];
}

/* Saved summaries as context (Memory view toggle, default ON): the
 * latest personal summary plus the chatting agent's own latest summary
 * are prepended — clearly labelled as summaries — to the system prompt.
 * Summaries are derived views; the originals remain in their threads. */
function summaryContext(agent) {
  if (state.useSummaries === false) return "";
  const parts = [];
  const personal = state.summaries.find((s) => s.scope === "personal");
  if (personal) {
    parts.push("[Saved summary — personal workspace (derived digest; background context)]\n" +
               personal.content);
  }
  const own = state.summaries.find(
    (s) => s.scope === "agent" && s.agentId === agent.id);
  if (own) {
    parts.push(`[Saved summary — this agent, ${agent.name} (derived digest; background context)]\n` +
               own.content);
  }
  return parts.join("\n\n");
}

function systemFor(agent) {
  const ctx = summaryContext(agent);
  return ctx ? agent.persona + "\n\n---\n" + ctx : agent.persona;
}

/* One low-level "ask the model" call: used by chat, by the summarizer,
 * and by every Team-mode step, plan and synthesis. Returns the parsed
 * response ({reply, provider, model, usage}) or throws with the
 * provider's (already sanitized) error message. */
async function proxyChatRaw(model, systemPrompt, messages) {
  const s = state.settings;
  const resp = await fetch(API + "/api/chat", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      provider: s.provider,
      model,
      api_key: s.token,
      base_url: s.provider === "custom" ? s.base_url : undefined,
      system: systemPrompt,
      messages,
    }),
  });
  let data = {};
  try { data = await resp.json(); } catch (e) { /* non-JSON error page */ }
  if (!resp.ok) {
    throw new Error(data.detail || `Request failed (${resp.status}).`);
  }
  return data;
}

async function callChat(agent, thread, systemPrompt) {
  return proxyChatRaw(
    effectiveModel(agent),
    systemPrompt,
    thread
      .filter((m) => !m.demo)
      .slice(-40)
      .map((m) => ({ role: m.role, content: m.content })),
  );
}

$("composer").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  if (sending) return;
  const input = $("composerInput");
  const text = input.value.trim();
  if (!text) return;
  const agent = activeAgent();
  const thread = (state.threads[agent.id] = state.threads[agent.id] || []);
  thread.push({ mid: newMid(), role: "user", content: text, ts: Date.now() });
  input.value = "";
  input.style.height = "auto";
  save(); renderChat();
  $("chatError").hidden = true;

  // One prompt-assembly path (persona + saved summaries) for both the
  // real proxy call and demo mode. Demo replies ignore the summary
  // content-wise, but the assembly must never break them.
  const systemPrompt = systemFor(agent);

  if (!hasToken()) {
    sending = true;
    $("typing").hidden = false;
    setTimeout(() => {
      void systemPrompt;
      thread.push({
        mid: newMid(), role: "assistant", content: demoReply(agent, text),
        ts: Date.now(), demo: true, model: effectiveModel(agent),
      });
      sending = false;
      $("typing").hidden = true;
      save(); renderChat();
    }, 650);
    return;
  }

  sending = true;
  $("typing").hidden = false;
  $("sendBtn").disabled = true;
  try {
    const data = await callChat(agent, thread, systemPrompt);
    thread.push({
      mid: newMid(), role: "assistant", content: data.reply, ts: Date.now(),
      usage: data.usage || null, provider: data.provider, model: data.model,
    });
    state.activity.unshift({
      ts: Date.now(), agentId: agent.id, agent: agent.name,
      provider: data.provider, model: data.model,
      prompt_tokens: data.usage ? data.usage.prompt_tokens : null,
      completion_tokens: data.usage ? data.usage.completion_tokens : null,
      total_tokens: data.usage ? data.usage.total_tokens : null,
    });
    state.activity = state.activity.slice(0, 500);
  } catch (err) {
    const box = $("chatError");
    box.textContent = "⚠️ " + err.message;
    box.hidden = false;
  } finally {
    sending = false;
    $("typing").hidden = true;
    $("sendBtn").disabled = false;
    save(); renderChat();
  }
});

$("composerInput").addEventListener("keydown", (ev) => {
  if (ev.key === "Enter" && !ev.shiftKey) {
    ev.preventDefault();
    $("composer").requestSubmit();
  }
});
$("composerInput").addEventListener("input", (ev) => {
  const t = ev.target;
  t.style.height = "auto";
  t.style.height = Math.min(t.scrollHeight, 130) + "px";
});

/* ---------------------------------------------------------------- agents */

function renderAgents() {
  $("agentList").innerHTML = state.agents.map((a) => {
    const n = (state.threads[a.id] || []).length;
    return `
    <button class="card agent-card" data-edit="${esc(a.id)}">
      <span class="avatar"><span class="avatar-inner">${esc(a.emoji || "🤖")}</span></span>
      <span class="agent-card-body">
        <span class="a-name">${esc(a.name)}</span>
        <span class="a-meta">${esc(a.model ? "model: " + a.model : "uses your Settings model")} · ${n} message${n === 1 ? "" : "s"}</span>
        <span class="a-persona">${esc(a.persona)}</span>
      </span>
      <span class="a-chevron">›</span>
    </button>`;
  }).join("");
  $("agentList").querySelectorAll("[data-edit]").forEach((b) =>
    b.addEventListener("click", () => openAgentForm(b.dataset.edit)));
}

function showAgentSheet(open) { $("agentSheet").hidden = !open; }

function openAgentForm(id) {
  editingAgentId = id || null;
  const a = id ? state.agents.find((x) => x.id === id) : null;
  $("agentFormTitle").textContent = a ? "Edit agent" : "New agent";
  $("agentName").value = a ? a.name : "";
  $("agentEmoji").value = a ? a.emoji : "";
  $("agentPersona").value = a ? a.persona : "";
  $("agentModel").value = a ? a.model : "";
  $("agentDelete").hidden = !a || state.agents.length <= 1;
  showAgentSheet(true);
}

$("newAgentBtn").addEventListener("click", () => openAgentForm(null));
$("agentFormCancel").addEventListener("click", () => showAgentSheet(false));
$("agentSheet").addEventListener("click", (ev) => {
  if (ev.target === $("agentSheet")) showAgentSheet(false);
});

$("agentForm").addEventListener("submit", (ev) => {
  ev.preventDefault();
  const data = {
    name: $("agentName").value.trim() || "Untitled agent",
    emoji: $("agentEmoji").value.trim() || "🤖",
    persona: $("agentPersona").value.trim(),
    model: $("agentModel").value.trim(),
  };
  if (editingAgentId) {
    Object.assign(state.agents.find((x) => x.id === editingAgentId), data);
  } else {
    const id = "a-" + Date.now().toString(36);
    state.agents.push({ id, created: Date.now(), ...data });
    state.activeAgentId = id;
  }
  save();
  showAgentSheet(false);
  renderAgents(); renderMode();
  toast(editingAgentId ? "Agent updated." : "Agent created.");
});

$("agentDelete").addEventListener("click", () => {
  const a = state.agents.find((x) => x.id === editingAgentId);
  if (!a || !confirm(`Delete ${a.name} and its conversation?`)) return;
  state.agents = state.agents.filter((x) => x.id !== a.id);
  delete state.threads[a.id];
  state.memories = state.memories.filter(
    (m) => !(m.scope === "agent" && m.agentId === a.id));
  if (state.activeAgentId === a.id) state.activeAgentId = state.agents[0].id;
  save();
  showAgentSheet(false);
  renderAgents();
  toast("Agent deleted.");
});

/* ---------------------------------------------------------------- memory
 *
 * Scopes: "agent" (one agent's messages + agent-scoped memories) and
 * "personal" (the whole local workspace: every agent's messages + every
 * memory fact). There are no team/org scopes here — single user,
 * single device.
 *
 * Summaries are DERIVED VIEWS: generated into an editable DRAFT first;
 * nothing is stored until the user clicks Save. The stored summary
 * carries the source message/memory ids, the model (or DEMO), and the
 * timestamp. Originals are never touched.
 * -------------------------------------------------------------------- */

function collectSources(scope, agentId) {
  let messages = [];
  let memories = [];
  if (scope === "agent") {
    const a = agentById(agentId);
    messages = (state.threads[agentId] || []).map((m) => ({
      mid: m.mid, role: m.role, content: m.content, ts: m.ts,
      from: a ? a.name : "agent",
    }));
    memories = state.memories.filter(
      (m) => m.scope === "agent" && m.agentId === agentId);
  } else {
    for (const a of state.agents) {
      for (const m of (state.threads[a.id] || [])) {
        messages.push({ mid: m.mid, role: m.role, content: m.content,
                        ts: m.ts, from: a.name });
      }
    }
    messages.sort((x, y) => (x.ts || 0) - (y.ts || 0));
    memories = state.memories.slice();
  }
  return { messages, memories };
}

function sourcesToText(src) {
  const lines = [];
  for (const mem of src.memories) {
    lines.push(`[memory ${mem.id}] ${mem.content}`);
  }
  for (const m of src.messages) {
    lines.push(`[message ${m.mid} · ${m.from} · ${m.role}] ${m.content}`);
  }
  let text = lines.join("\n");
  if (text.length > 26000) text = text.slice(0, 26000) + "\n…(truncated)";
  return text;
}

/* Deterministic local extractive digest (demo mode): memory facts as
 * bullets, then the first sentence of each message, in order, capped.
 * No model involved — always labelled DEMO. */
function demoDigest(scope, agentId, src) {
  const label = scopeLabel(scope, agentId);
  const lines = [
    `Extractive digest of ${src.messages.length} message(s) and ` +
    `${src.memories.length} memory fact(s) — ${label}.`,
  ];
  for (const mem of src.memories) {
    lines.push(`• Memory: ${mem.content}`);
  }
  let used = 0;
  for (const m of src.messages) {
    if (used >= 12) break;
    const text = String(m.content).split(/\s+/).join(" ").trim();
    if (!text) continue;
    let first = text.split(/(?<=[.!?])\s+/)[0];
    if (first.length > 170) first = first.slice(0, 170) + "…";
    lines.push(`• ${m.role === "user" ? "You" : m.from}: ${first}`);
    used += 1;
  }
  if (!lines.length) lines.push("• (nothing to summarize yet)");
  return lines.join("\n");
}

async function callSummarizeModel(scope, agentId, src) {
  const s = state.settings;
  const resp = await fetch(API + "/api/chat", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      provider: s.provider,
      model: s.model,
      api_key: s.token,
      base_url: s.provider === "custom" ? s.base_url : undefined,
      system: "You are PocketPlane's summarizer. Produce a concise digest " +
              "of the user's memory facts and conversations: short bullet " +
              "lines, concrete facts, decisions and open threads only. " +
              "Do not invent anything that is not in the sources.",
      messages: [{
        role: "user",
        content: "Summarize these sources into a bullet digest.\n\n" +
                 sourcesToText(src),
      }],
    }),
  });
  let data = {};
  try { data = await resp.json(); } catch (e) { /* non-JSON error page */ }
  if (!resp.ok) {
    throw new Error(data.detail || `Request failed (${resp.status}).`);
  }
  return data; // {reply, provider, model, usage}
}

function renderMemory() {
  // Scope pickers (facts form + summarize form) list Personal + agents.
  const scopeOptions = (selected) => {
    const opts = [`<option value="personal" ${selected === "personal" ? "selected" : ""}>Personal workspace</option>`];
    for (const a of state.agents) {
      const val = "agent:" + a.id;
      opts.push(`<option value="${esc(val)}" ${selected === val ? "selected" : ""}>${esc(a.emoji || "🤖")} ${esc(a.name)}</option>`);
    }
    return opts.join("");
  };
  $("memScope").innerHTML = scopeOptions($("memScope").value || "personal");
  $("sumScope").innerHTML = scopeOptions($("sumScope").value || "personal");
  $("sumCtxToggle").checked = state.useSummaries !== false;

  // Facts
  const facts = state.memories.slice().sort((a, b) => (b.created || 0) - (a.created || 0));
  $("factList").innerHTML = facts.length ? facts.map((m) => `
    <div class="card fact-card">
      <div class="card-badges">
        <span class="badge badge-scope">${esc(scopeLabel(m.scope, m.agentId))}</span>
        <span class="fact-date">${esc(new Date(m.created).toLocaleDateString())}</span>
      </div>
      <p class="fact-content">${esc(m.content)}</p>
      <div class="row row-tight">
        <button class="btn btn-ghost btn-sm" data-mem-edit="${esc(m.id)}">Edit</button>
        <button class="btn btn-ghost btn-sm" data-mem-del="${esc(m.id)}">Delete</button>
      </div>
    </div>`).join("")
    : `<div class="empty-state"><div class="empty-emoji">📌</div><p>No memory facts yet.</p><p class="hint">Add facts your agents should remember — a preference, a project detail, anything that should survive the chat.</p></div>`;
  $("factList").querySelectorAll("[data-mem-edit]").forEach((b) =>
    b.addEventListener("click", () => openMemoryForm(b.dataset.memEdit)));
  $("factList").querySelectorAll("[data-mem-del]").forEach((b) =>
    b.addEventListener("click", () => deleteMemory(b.dataset.memDel)));

  // Draft (the human gate: editable, unsaved until "Save summary")
  const d = summaryDraft;
  $("summaryDraft").hidden = !d;
  if (d) {
    $("sumDraftMeta").innerHTML =
      `<span class="badge badge-scope">${esc(scopeLabel(d.scope, d.agentId))}</span> ` +
      (d.demo
        ? `<span class="badge badge-demo">DEMO summary</span>`
        : `<span class="badge badge-live">${esc(PROVIDER_LABELS[d.provider] || d.provider)} · ${esc(d.model)}</span>`) +
      ` <span class="badge">${(d.sourceMessageIds.length + d.sourceMemoryIds.length)} sources</span>`;
    if (document.activeElement !== $("sumDraftTitle")) $("sumDraftTitle").value = d.title;
    if (document.activeElement !== $("sumDraftContent")) $("sumDraftContent").value = d.content;
  }

  // Saved summaries
  $("summaryList").innerHTML = state.summaries.length ? state.summaries.map((sm) => {
    const nSources = sm.sourceMessageIds.length + sm.sourceMemoryIds.length;
    const prov = [
      sm.sourceMemoryIds.length ? `Memory facts: ${sm.sourceMemoryIds.join(", ")}` : "",
      sm.sourceMessageIds.length ? `Messages: ${sm.sourceMessageIds.join(", ")}` : "",
    ].filter(Boolean).join("\n") || "No sources recorded.";
    return `
    <div class="card summary-card">
      <div class="card-badges">
        <span class="badge badge-scope">${esc(scopeLabel(sm.scope, sm.agentId))}</span>
        ${sm.demo
          ? `<span class="badge badge-demo">DEMO</span>`
          : `<span class="badge badge-live">${esc(PROVIDER_LABELS[sm.provider] || sm.provider)} · ${esc(sm.model)}</span>`}
        <span class="badge">${nSources} source${nSources === 1 ? "" : "s"}</span>
        <span class="fact-date">${esc(new Date(sm.created).toLocaleString())}</span>
      </div>
      <h3 class="summary-title">${esc(sm.title)}</h3>
      <p class="summary-content">${esc(sm.content)}</p>
      <details class="provenance"><summary>Provenance — source ids</summary><pre>${esc(prov)}</pre></details>
      <div class="row row-tight">
        <button class="btn btn-ghost btn-sm" data-sum-copy="${esc(sm.id)}">Copy</button>
        <button class="btn btn-ghost btn-sm" data-sum-del="${esc(sm.id)}">Delete</button>
      </div>
    </div>`;
  }).join("")
    : `<div class="empty-state"><div class="empty-emoji">🧾</div><p>No saved summaries yet.</p><p class="hint">Generate a digest of a conversation or your whole workspace, review it, and save it — originals always stay put.</p></div>`;
  $("summaryList").querySelectorAll("[data-sum-del]").forEach((b) =>
    b.addEventListener("click", () => {
      state.summaries = state.summaries.filter((x) => x.id !== b.dataset.sumDel);
      save(); renderMemory();
      toast("Summary deleted. Its sources were not touched.");
    }));
  $("summaryList").querySelectorAll("[data-sum-copy]").forEach((b) =>
    b.addEventListener("click", async () => {
      const sm = state.summaries.find((x) => x.id === b.dataset.sumCopy);
      if (!sm) return;
      try {
        await navigator.clipboard.writeText(sm.content);
        toast("Summary copied.");
      } catch (e) {
        toast("Copy failed in this browser.", "err");
      }
    }));
}

function parseScopeValue(v) {
  if (v && v.startsWith("agent:")) return { scope: "agent", agentId: v.slice(6) };
  return { scope: "personal", agentId: null };
}

function openMemoryForm(id) {
  editingMemoryId = id || null;
  const m = id ? state.memories.find((x) => x.id === id) : null;
  $("memoryFormTitle").textContent = m ? "Edit memory fact" : "Add a memory fact";
  $("memContent").value = m ? m.content : "";
  $("memScope").value = m
    ? (m.scope === "agent" ? "agent:" + m.agentId : "personal")
    : "personal";
  $("memContent").focus();
}

$("memoryForm").addEventListener("submit", (ev) => {
  ev.preventDefault();
  const content = $("memContent").value.trim();
  if (!content) return;
  const { scope, agentId } = parseScopeValue($("memScope").value);
  if (editingMemoryId) {
    const m = state.memories.find((x) => x.id === editingMemoryId);
    if (m) Object.assign(m, { content, scope, agentId });
    toast("Memory fact updated.");
  } else {
    state.memories.push({
      id: "mem-" + Date.now().toString(36) + Math.floor(Math.random() * 1e4).toString(36),
      agentId, scope, content, created: Date.now(),
    });
    toast("Memory fact saved.");
  }
  editingMemoryId = null;
  $("memoryFormTitle").textContent = "Add a memory fact";
  $("memContent").value = "";
  save(); renderMemory();
});

function deleteMemory(id) {
  const m = state.memories.find((x) => x.id === id);
  if (!m || !confirm("Delete this memory fact? Saved summaries keep their provenance record of it.")) return;
  state.memories = state.memories.filter((x) => x.id !== id);
  if (editingMemoryId === id) editingMemoryId = null;
  save(); renderMemory();
  toast("Memory fact deleted.");
}

$("genSummary").addEventListener("click", async () => {
  if (summarizing) return;
  const { scope, agentId } = parseScopeValue($("sumScope").value);
  const src = collectSources(scope, agentId);
  const box = $("sumError");
  box.hidden = true;
  if (!src.messages.length && !src.memories.length) {
    box.hidden = false;
    box.textContent = "Nothing to summarize in that scope yet — chat first or add a memory fact.";
    return;
  }
  const sourceMessageIds = src.messages.map((m) => m.mid).filter(Boolean);
  const sourceMemoryIds = src.memories.map((m) => m.id);
  const title = `${scopeLabel(scope, agentId)} digest — ` +
                new Date().toLocaleDateString();
  summarizing = true;
  $("genSummary").disabled = true;
  $("genSummary").textContent = "Summarizing…";
  try {
    if (hasToken()) {
      const data = await callSummarizeModel(scope, agentId, src);
      summaryDraft = {
        scope, agentId, title, content: data.reply,
        sourceMessageIds, sourceMemoryIds,
        model: data.model, provider: data.provider, demo: false,
        usage: data.usage || null,
      };
    } else {
      summaryDraft = {
        scope, agentId, title,
        content: demoDigest(scope, agentId, src),
        sourceMessageIds, sourceMemoryIds,
        model: "extractive-demo", provider: "local", demo: true,
        usage: null,
      };
    }
    renderMemory();
    $("summaryDraft").scrollIntoView({ behavior: "smooth", block: "start" });
  } catch (err) {
    box.hidden = false;
    box.textContent = "⚠️ " + err.message;
  } finally {
    summarizing = false;
    $("genSummary").disabled = false;
    $("genSummary").textContent = "Generate summary draft";
  }
});

$("saveSummary").addEventListener("click", () => {
  if (!summaryDraft) return;
  summaryDraft.title = $("sumDraftTitle").value.trim() || summaryDraft.title;
  summaryDraft.content = $("sumDraftContent").value;
  const sm = {
    id: "sum-" + Date.now().toString(36) + Math.floor(Math.random() * 1e4).toString(36),
    created: Date.now(),
    ...summaryDraft,
  };
  state.summaries.unshift(sm);
  state.activity.unshift({
    ts: Date.now(), agentId: sm.agentId,
    agent: "🧠 Memory summary",
    provider: sm.provider, model: sm.model,
    prompt_tokens: sm.usage ? sm.usage.prompt_tokens : null,
    completion_tokens: sm.usage ? sm.usage.completion_tokens : null,
    total_tokens: sm.usage ? sm.usage.total_tokens : null,
  });
  state.activity = state.activity.slice(0, 500);
  summaryDraft = null;
  save(); renderMemory();
  toast("Summary saved — a derived view; originals untouched.");
});

$("discardSummary").addEventListener("click", () => {
  summaryDraft = null;
  renderMemory();
  toast("Draft discarded — nothing was stored.");
});

$("sumCtxToggle").addEventListener("change", () => {
  state.useSummaries = $("sumCtxToggle").checked;
  save();
  toast(state.useSummaries
    ? "Saved summaries will be used as agent context."
    : "Summaries will no longer be sent as agent context.");
});

/* ---------------------------------------------------------------- team
 *
 * Team mode: the browser is the orchestrator. One task → a lead agent
 * plans steps → each step is ONE model call by one of the user's own
 * agents → a final synthesis by the lead. Steps NEVER write into the
 * agents' chat threads — they live only in the workflow tables.
 *
 * Planning honesty (plan_source): with a model connected, the plan
 * comes from the model as strict JSON ("model"); any parse/shape
 * problem falls back to the fixed template ("template-fallback");
 * with no key, it's the template directly ("template"). In demo mode
 * every step and the synthesis are simulated and labelled DEMO.
 * ------------------------------------------------------------------ */

let _wfCounter = 0;
function newWorkflowId() {
  _wfCounter += 1;
  return "wf-" + Date.now().toString(36) + "-" + _wfCounter.toString(36) +
         Math.floor(Math.random() * 1e6).toString(36);
}
function newStepId() {
  _wfCounter += 1;
  return "wfs-" + Date.now().toString(36) + "-" + _wfCounter.toString(36) +
         Math.floor(Math.random() * 1e6).toString(36);
}

function workflowById(id) {
  return state.workflows.find((w) => w.id === id) || null;
}

function pickAgentFor(keywords, fallback) {
  const hit = state.agents.find((a) => {
    const hay = (a.name + " " + a.persona).toLowerCase();
    return keywords.some((k) => hay.includes(k));
  });
  return hit || fallback;
}

/* The deterministic template plan: outline → draft → review, each
 * assigned by best name/persona match, else the lead does it. */
function templatePlan(task, lead) {
  void task;
  const research = pickAgentFor(["research", "general", "assistant", "planner"], lead);
  const draft = pickAgentFor(["writing", "draft", "coding", "writer", "coach", "build"], research);
  const review = pickAgentFor(["review", "editor", "coach", "buddy", "critic"], draft);
  return [
    { key: "outline", agentId: research.id, title: "Research & outline",
      instruction: "Break the task down. List what needs to be done, the key points to cover, and a clear outline for the final result." },
    { key: "draft", agentId: draft.id, title: "Draft the result",
      instruction: "Using the outline from the previous step, produce the main draft / result for the task, as completely as you can." },
    { key: "review", agentId: review.id, title: "Review & improve",
      instruction: "Review the draft from the previous step against the original task. Fix gaps and mistakes, tighten the wording, and return the improved version." },
  ];
}

const TEAM_PLANNER_SYSTEM =
  "You are the planning step of PocketPlane's Team mode. Break the " +
  "user's task into 3 to 6 sequential steps that the listed agents " +
  "can each complete in one reply. Respond with ONLY a JSON array — " +
  "no prose, no code fences — of objects with exactly these fields: " +
  "\"key\" (short lowercase id), \"agent_name\" (must be one of the " +
  "listed agent names), \"title\" (short), \"instruction\" (what that " +
  "agent should do in its step, written to the agent). Later steps " +
  "may build on earlier steps' results; results are passed along " +
  "automatically, so do not ask the agent for anything else.";

/* Defensive parse of a model plan: anything unexpected → null, and
 * the caller falls back to the template (plan_source records it). */
function parsePlanJson(text, lead) {
  const start = String(text).indexOf("[");
  const end = String(text).lastIndexOf("]");
  if (start < 0 || end <= start) return null;
  let arr;
  try { arr = JSON.parse(String(text).slice(start, end + 1)); } catch (e) { return null; }
  if (!Array.isArray(arr)) return null;
  const steps = [];
  const usedKeys = new Set();
  for (const raw of arr) {
    if (!raw || typeof raw !== "object") continue;
    const instruction = String(raw.instruction || "").trim();
    if (!instruction) continue;
    let agent = state.agents.find((a) =>
      a.name.toLowerCase() === String(raw.agent_name || "").trim().toLowerCase());
    if (!agent) agent = lead;
    let key = String(raw.key || "").trim().toLowerCase()
      .replace(/[^a-z0-9_-]+/g, "-") || ("step-" + (steps.length + 1));
    if (usedKeys.has(key)) key = key + "-" + (steps.length + 1);
    usedKeys.add(key);
    steps.push({
      key, agentId: agent.id,
      title: String(raw.title || "").trim() || ("Step " + (steps.length + 1)),
      instruction,
    });
    if (steps.length >= 6) break;
  }
  return steps.length ? steps : null;
}

async function planTeamTask() {
  const task = $("teamTask").value.trim();
  const errBox = $("teamError");
  errBox.hidden = true;
  if (!task) {
    errBox.hidden = false;
    errBox.textContent = "Describe the task first — one or two sentences is plenty.";
    return;
  }
  const lead = agentById($("teamLead").value) || activeAgent();
  // One editable draft at a time: planning again replaces it.
  state.workflows = state.workflows.filter((w) => w.status !== "draft");
  const btn = $("planBtn");
  btn.disabled = true;
  btn.textContent = "Planning…";
  let steps = null;
  let planSource = "template";
  try {
    if (hasToken()) {
      try {
        const agentList = state.agents.map((a) =>
          `- ${a.name}: ${a.persona.slice(0, 120)}`).join("\n");
        const data = await proxyChatRaw(effectiveModel(lead), TEAM_PLANNER_SYSTEM, [{
          role: "user",
          content: `Agents available:\n${agentList}\n\nTask: ${task}\n\nReturn the JSON plan now.`,
        }]);
        steps = parsePlanJson(data.reply, lead);
        planSource = steps ? "model" : "template-fallback";
      } catch (e) {
        steps = null;
        planSource = "template-fallback";
      }
    }
    if (!steps) steps = templatePlan(task, lead);
  } finally {
    btn.disabled = false;
    btn.textContent = "Plan it";
  }
  const wf = {
    id: newWorkflowId(), task, leadAgentId: lead.id, status: "draft",
    planSource, demo: !hasToken(),
    model: hasToken() ? effectiveModel(lead) : "on-device demo",
    provider: hasToken() ? state.settings.provider : "local",
    synthesis: "", error: "", created: Date.now(), finished: 0,
    steps: steps.map((s) => ({
      id: newStepId(), status: "pending", output: "", error: "", ts: 0, ...s,
    })),
  };
  state.workflows.unshift(wf);
  state.teamOpenId = wf.id;
  save(); renderTeam();
  $("teamDetail").scrollIntoView({ behavior: "smooth", block: "start" });
}

$("planBtn").addEventListener("click", planTeamTask);

/* Step prompt: the overall task + this step's instruction + previous
 * steps' outputs under clear headings. The API caps one message at
 * 8000 chars, so outputs are trimmed (2000 chars each, newest kept
 * first when the budget runs out) to stay safely under it. */
function buildStepPrompt(wf, step) {
  const MAX = 7600;
  let prompt = `Overall task:\n${wf.task}\n\nYour step — ${step.title}:\n${step.instruction}`;
  const done = wf.steps.filter((s) => s.status === "done" && s.output);
  if (done.length) {
    const blocks = [];
    let used = prompt.length;
    for (let i = done.length - 1; i >= 0; i--) {
      const s = done[i];
      const a = agentById(s.agentId);
      let out = s.output;
      if (out.length > 2000) out = out.slice(0, 2000) + "\n…(trimmed)";
      const block = `\n\n## Result of step "${s.title}" (by ${a ? a.name : "an agent"})\n${out}`;
      if (used + block.length > MAX) break;
      blocks.unshift(block);
      used += block.length;
    }
    if (blocks.length) prompt += "\n\nResults from previous steps:" + blocks.join("");
  }
  return prompt.slice(0, MAX);
}

function buildSynthesisPrompt(wf) {
  const parts = [`Overall task:\n${wf.task}`];
  for (const s of wf.steps) {
    if (s.status !== "done") continue;
    const a = agentById(s.agentId);
    let out = s.output || "";
    if (out.length > 2500) out = out.slice(0, 2500) + "\n…(trimmed)";
    parts.push(`\n## Step "${s.title}" (by ${a ? a.name : "an agent"})\n${out}`);
  }
  parts.push("\nWrite the final combined answer for the user now: complete and in one piece, without describing the steps themselves.");
  return parts.join("\n").slice(0, 7600);
}

function demoStepOutput(agent, step, wf) {
  return [
    "— Simulated demo output —",
    `${agent.name} worked on "${step.title}" for the task: "${wf.task}"`,
    "",
    `Following its instruction ("${step.instruction}"), a connected model would produce the real work for this step right here.`,
    "Connect your model and the team does the whole job for real.",
  ].join("\n");
}

function demoSynthesis(wf) {
  const lines = [
    "— Simulated demo synthesis —",
    `Task: ${wf.task}`,
    "",
    ...wf.steps.filter((s) => s.status === "done")
      .map((s) => `• ${s.title}: done (simulated).`),
    "",
    "With your model connected, this is where the lead agent combines every step into one final answer.",
  ];
  return lines.join("\n");
}

function logTeamActivity(label, wf, usage) {
  state.activity.unshift({
    ts: Date.now(), agentId: wf.leadAgentId, agent: label,
    provider: wf.provider, model: wf.model,
    prompt_tokens: usage ? usage.prompt_tokens : null,
    completion_tokens: usage ? usage.completion_tokens : null,
    total_tokens: usage ? usage.total_tokens : null,
  });
  state.activity = state.activity.slice(0, 500);
}

async function runWorkflow(wf, fromIndex) {
  if (teamRunning) return;
  teamRunning = true;
  teamCancel = false;
  const lead = agentById(wf.leadAgentId) || state.agents[0];
  wf.demo = !hasToken();
  wf.model = wf.demo ? "on-device demo" : effectiveModel(lead);
  wf.provider = wf.demo ? "local" : state.settings.provider;
  wf.status = "running";
  wf.error = "";
  wf.finished = 0;
  logTeamActivity("👥 Team run started", wf, null);
  save(); renderTeam();

  let promptTotal = 0, completionTotal = 0, anyUsage = false;
  const trackUsage = (usage) => {
    if (!usage) return;
    anyUsage = true;
    promptTotal += usage.prompt_tokens || 0;
    completionTotal += usage.completion_tokens || 0;
  };

  try {
    for (let i = fromIndex; i < wf.steps.length; i++) {
      if (teamCancel) {
        wf.status = "cancelled";
        wf.finished = Date.now();
        save(); renderTeam();
        toast("Team run cancelled.");
        return;
      }
      const step = wf.steps[i];
      if (step.status === "done") continue;
      const agent = agentById(step.agentId) || lead;
      step.status = "running";
      step.error = "";
      step.ts = Date.now();
      save(); renderTeam();
      try {
        if (wf.demo) {
          await new Promise((r) => setTimeout(r, 600));
          step.output = demoStepOutput(agent, step, wf);
        } else {
          const data = await proxyChatRaw(
            effectiveModel(agent), systemFor(agent),
            [{ role: "user", content: buildStepPrompt(wf, step) }]);
          step.output = data.reply;
          trackUsage(data.usage);
          state.activity.unshift({
            ts: Date.now(), agentId: agent.id, agent: agent.name,
            provider: data.provider, model: data.model,
            prompt_tokens: data.usage ? data.usage.prompt_tokens : null,
            completion_tokens: data.usage ? data.usage.completion_tokens : null,
            total_tokens: data.usage ? data.usage.total_tokens : null,
          });
          state.activity = state.activity.slice(0, 500);
        }
        step.status = "done";
        step.ts = Date.now();
      } catch (err) {
        step.status = "failed";
        step.error = err.message;
        step.ts = Date.now();
        wf.status = "failed";
        wf.error = `${step.title}: ${err.message}`;
        wf.finished = Date.now();
        logTeamActivity("👥 Team run failed", wf, null);
        save(); renderTeam();
        toast("A step failed — you can resume the run where it stopped.", "err");
        return;
      }
      save(); renderTeam();
    }

    if (teamCancel) {
      wf.status = "cancelled";
      wf.finished = Date.now();
      save(); renderTeam();
      toast("Team run cancelled.");
      return;
    }
    // Final synthesis by the lead agent.
    if (wf.demo) {
      await new Promise((r) => setTimeout(r, 500));
      wf.synthesis = demoSynthesis(wf);
    } else {
      const data = await proxyChatRaw(
        effectiveModel(lead), systemFor(lead),
        [{ role: "user", content: buildSynthesisPrompt(wf) }]);
      wf.synthesis = data.reply;
      trackUsage(data.usage);
      state.activity.unshift({
        ts: Date.now(), agentId: lead.id, agent: lead.name + " (synthesis)",
        provider: data.provider, model: data.model,
        prompt_tokens: data.usage ? data.usage.prompt_tokens : null,
        completion_tokens: data.usage ? data.usage.completion_tokens : null,
        total_tokens: data.usage ? data.usage.total_tokens : null,
      });
      state.activity = state.activity.slice(0, 500);
    }
    wf.status = "done";
    wf.finished = Date.now();
    logTeamActivity("👥 Team run finished", wf, anyUsage ? {
      prompt_tokens: promptTotal, completion_tokens: completionTotal,
      total_tokens: promptTotal + completionTotal,
    } : null);
    save(); renderTeam();
    toast("Team run finished — final answer is ready.");
  } finally {
    teamRunning = false;
    save(); renderTeam();
  }
}

function resumeWorkflow(wf) {
  if (teamRunning) return;
  let idx = wf.steps.findIndex((s) => s.status !== "done");
  for (const s of wf.steps) {
    if (s.status !== "done") { s.status = "pending"; s.error = ""; }
  }
  wf.synthesis = "";
  if (idx < 0) idx = wf.steps.length; // all steps done — synthesis only
  runWorkflow(wf, idx);
}

/* ------------------------------------------------------------ team UI */

const WF_STATUS_LABEL = {
  draft: "Draft plan", running: "Running…", done: "Done",
  failed: "Failed", cancelled: "Cancelled",
};
const PLAN_SOURCE_LABEL = {
  model: "Planned by your model",
  template: "Starter template",
  "template-fallback": "Starter template (fallback)",
};

function wfBadges(wf) {
  const bits = [
    `<span class="badge badge-wf-${esc(wf.status)}">${esc(WF_STATUS_LABEL[wf.status] || wf.status)}</span>`,
    `<span class="badge badge-scope">${esc(PLAN_SOURCE_LABEL[wf.planSource] || wf.planSource)}</span>`,
  ];
  if (wf.demo) bits.push(`<span class="badge badge-demo">DEMO</span>`);
  const lead = agentById(wf.leadAgentId);
  if (lead) bits.push(`<span class="badge">Lead: ${esc(lead.emoji || "🤖")} ${esc(lead.name)}</span>`);
  bits.push(`<span class="badge">${esc(new Date(wf.created).toLocaleString())}</span>`);
  return bits.join(" ");
}

function agentOptions(selectedId) {
  return state.agents.map((a) =>
    `<option value="${esc(a.id)}" ${a.id === selectedId ? "selected" : ""}>${esc(a.emoji || "🤖")} ${esc(a.name)}</option>`).join("");
}

function workflowDetailHtml(wf) {
  const head = `
    <div class="card wf-card">
      <div class="card-badges">${wfBadges(wf)}</div>
      <h3 class="wf-task">${esc(wf.task)}</h3>`;
  if (wf.status === "draft") {
    const steps = wf.steps.map((st, i) => `
      <div class="wf-edit-step" data-step-row="${esc(st.id)}">
        <div class="wf-edit-top">
          <span class="wf-step-n">${i + 1}</span>
          <input type="text" class="wf-step-title" data-step-title="${esc(st.id)}" value="${esc(st.title)}" maxlength="90" aria-label="Step title">
          <button type="button" class="btn btn-ghost btn-sm" data-step-del="${esc(st.id)}">Remove</button>
        </div>
        <label>Agent
          <select class="wf-step-agent" data-step-agent="${esc(st.id)}">${agentOptions(st.agentId)}</select>
        </label>
        <label>Instruction
          <textarea class="wf-step-instr" data-step-instr="${esc(st.id)}" rows="2" maxlength="1500">${esc(st.instruction)}</textarea>
        </label>
      </div>`).join("");
    return head + `
      <p class="hint">This is the plan — adjust who's doing what, then run it. Each step is a separate call on your model key.</p>
      <div class="wf-edit-list">${steps}</div>
      <div class="row">
        <button type="button" class="btn btn-glass btn-sm" id="wfAddStep">+ Add step</button>
      </div>
      <div class="row">
        <button type="button" class="btn btn-primary" id="wfRun">▶ Run team</button>
        <button type="button" class="btn btn-ghost" id="wfDiscard">Discard plan</button>
      </div>
    </div>`;
  }
  const nodes = wf.steps.map((st) => {
    const a = agentById(st.agentId);
    return `
    <div class="wf-node wf-node-${esc(st.status)}">
      <span class="wf-dot"></span>
      <div class="wf-node-body">
        <div class="wf-node-head">
          <strong>${esc(st.title)}</strong>
          <span class="badge">${esc(a ? (a.emoji || "🤖") + " " + a.name : "Former agent")}</span>
          <span class="badge badge-wf-${esc(st.status)}">${esc(st.status)}</span>
          ${wf.demo ? `<span class="badge badge-demo">DEMO</span>` : ""}
        </div>
        <p class="wf-instr">${esc(st.instruction)}</p>
        ${st.output ? `<details class="provenance"><summary>Step output</summary><pre>${esc(st.output)}</pre></details>` : ""}
        ${st.error ? `<p class="wf-err">⚠️ ${esc(st.error)}</p>` : ""}
      </div>
    </div>`;
  }).join("");
  let tail = "";
  if (wf.status === "running") {
    tail = `<div class="row"><button type="button" class="btn btn-danger" id="wfCancel">Cancel run</button></div>`;
  } else if (wf.status === "failed" || wf.status === "cancelled") {
    tail = `<div class="row"><button type="button" class="btn btn-primary" id="wfResume">Resume from where it stopped</button></div>`;
  }
  return head + `
    ${wf.error && wf.status === "failed" ? `<div class="error-box">⚠️ ${esc(wf.error)}</div>` : ""}
    <div class="wf-tree">${nodes}</div>
    ${wf.synthesis ? `
      <div class="wf-synthesis">
        <h4>✅ Final answer ${wf.demo ? `<span class="badge badge-demo">DEMO</span>` : ""}</h4>
        <p class="summary-content">${esc(wf.synthesis)}</p>
      </div>` : ""}
    ${tail}
    <div class="row row-tight">
      <button type="button" class="btn btn-ghost btn-sm" id="wfClose">Close</button>
    </div>
  </div>`;
}

function bindWorkflowDetail(wf) {
  const detail = $("teamDetail");
  if (wf.status === "draft") {
    detail.querySelectorAll("[data-step-title]").forEach((el) =>
      el.addEventListener("input", () => {
        const st = wf.steps.find((s) => s.id === el.dataset.stepTitle);
        if (st) st.title = el.value;
      }));
    detail.querySelectorAll("[data-step-instr]").forEach((el) =>
      el.addEventListener("input", () => {
        const st = wf.steps.find((s) => s.id === el.dataset.stepInstr);
        if (st) st.instruction = el.value;
      }));
    detail.querySelectorAll("[data-step-agent]").forEach((el) =>
      el.addEventListener("change", () => {
        const st = wf.steps.find((s) => s.id === el.dataset.stepAgent);
        if (st) { st.agentId = el.value; save(); }
      }));
    detail.querySelectorAll("[data-step-title], [data-step-instr]").forEach((el) =>
      el.addEventListener("change", () => save()));
    detail.querySelectorAll("[data-step-del]").forEach((b) =>
      b.addEventListener("click", () => {
        wf.steps = wf.steps.filter((s) => s.id !== b.dataset.stepDel);
        save(); renderTeam();
      }));
    const add = $("wfAddStep");
    if (add) add.addEventListener("click", () => {
      const lead = agentById(wf.leadAgentId) || state.agents[0];
      wf.steps.push({
        id: newStepId(), key: "step-" + (wf.steps.length + 1),
        agentId: lead.id, title: "New step", instruction: "",
        status: "pending", output: "", error: "", ts: 0,
      });
      save(); renderTeam();
    });
    const run = $("wfRun");
    if (run) run.addEventListener("click", () => {
      const bad = wf.steps.find((s) => !s.instruction.trim() || !agentById(s.agentId));
      if (!wf.steps.length) { toast("Add at least one step first.", "err"); return; }
      if (bad) { toast("Every step needs an agent and an instruction.", "err"); return; }
      runWorkflow(wf, 0);
    });
    const discard = $("wfDiscard");
    if (discard) discard.addEventListener("click", () => {
      state.workflows = state.workflows.filter((w) => w.id !== wf.id);
      state.teamOpenId = "";
      save(); renderTeam();
      toast("Plan discarded — nothing was run.");
    });
    return;
  }
  const cancel = $("wfCancel");
  if (cancel) cancel.addEventListener("click", () => { teamCancel = true; });
  const resume = $("wfResume");
  if (resume) resume.addEventListener("click", () => resumeWorkflow(wf));
  const close = $("wfClose");
  if (close) close.addEventListener("click", () => {
    state.teamOpenId = "";
    save(); renderTeam();
  });
}

function renderTeam() {
  const leadSel = $("teamLead");
  const prevLead = leadSel.value;
  leadSel.innerHTML = agentOptions(
    state.agents.some((a) => a.id === prevLead) ? prevLead : state.activeAgentId);
  const open = state.teamOpenId ? workflowById(state.teamOpenId) : null;
  $("teamDetail").innerHTML = open ? workflowDetailHtml(open) : "";
  if (open) bindWorkflowDetail(open);

  const history = state.workflows.filter(
    (w) => w.status !== "draft" && (!open || w.id !== open.id));
  $("teamHistory").innerHTML = history.length ? history.map((w) => `
    <div class="card wf-history-card">
      <div class="card-badges">${wfBadges(w)}</div>
      <p class="wf-history-task">${esc(w.task)}</p>
      <p class="hint">${w.steps.length} step${w.steps.length === 1 ? "" : "s"}${w.synthesis ? " · final answer ready" : ""}</p>
      <div class="row row-tight">
        <button type="button" class="btn btn-ghost btn-sm" data-wf-open="${esc(w.id)}">Open</button>
        <button type="button" class="btn btn-ghost btn-sm" data-wf-del="${esc(w.id)}">Delete</button>
      </div>
    </div>`).join("")
    : `<div class="empty-state"><div class="empty-emoji">👥</div><p>No team runs yet.</p><p class="hint">Plan a task above and your agents will split the work — planning, drafting and reviewing — then combine it into one answer.</p></div>`;
  $("teamHistory").querySelectorAll("[data-wf-open]").forEach((b) =>
    b.addEventListener("click", () => {
      state.teamOpenId = b.dataset.wfOpen;
      save(); renderTeam();
      $("teamDetail").scrollIntoView({ behavior: "smooth", block: "start" });
    }));
  $("teamHistory").querySelectorAll("[data-wf-del]").forEach((b) =>
    b.addEventListener("click", () => {
      if (!confirm("Delete this team run and its step outputs?")) return;
      state.workflows = state.workflows.filter((w) => w.id !== b.dataset.wfDel);
      if (state.teamOpenId === b.dataset.wfDel) state.teamOpenId = "";
      save(); renderTeam();
      toast("Team run deleted.");
    }));
}

/* -------------------------------------------------------------- activity */

function renderActivity() {
  const total = state.activity.reduce((n, a) => n + (a.total_tokens || 0), 0);
  $("activityTotals").textContent = state.activity.length
    ? `${state.activity.length} model call${state.activity.length === 1 ? "" : "s"} · ${total.toLocaleString()} total tokens (as reported by providers)`
    : "No model calls yet. Demo replies are not logged.";
  $("activityList").innerHTML = state.activity.length
    ? state.activity.map((a) => `
      <div class="activity-item">
        <span class="activity-dot"></span>
        <div class="activity-body">
          <div class="when">${esc(new Date(a.ts).toLocaleString())}</div>
          <div class="what">${esc(a.agent)} — ${esc(a.model)}</div>
          <div class="detail">${esc(PROVIDER_LABELS[a.provider] || a.provider)} · ${a.total_tokens != null ? esc(a.total_tokens) + " tokens" : "usage not reported"}${a.prompt_tokens != null ? ` (${esc(a.prompt_tokens)} in / ${esc(a.completion_tokens)} out)` : ""}</div>
        </div>
      </div>`).join("")
    : `<div class="empty-state"><div class="empty-emoji">📜</div><p>Nothing here yet.</p><p class="hint">Real model calls (and saved summaries) will appear on this timeline.</p></div>`;
}

function downloadJSON(obj, filename) {
  const blob = new Blob([JSON.stringify(obj, null, 2)], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url; a.download = filename; a.click();
  setTimeout(() => URL.revokeObjectURL(url), 2000);
}

$("exportActivity").addEventListener("click", () =>
  downloadJSON(state.activity, "pocketplane-activity.json"));

/* -------------------------------------------------------------- settings */

function renderSettings() {
  const s = state.settings;
  $("setProvider").value = s.provider;
  $("setModel").value = s.model;
  $("setBaseUrl").value = s.base_url || "";
  $("setToken").value = s.token || "";
  $("setToken").type = "password";
  $("toggleToken").textContent = "Show";
  updateProviderUI();
}

function updateProviderUI() {
  const p = $("setProvider").value;
  $("baseUrlRow").hidden = p !== "custom";
  $("modelPresets").innerHTML = (MODEL_PRESETS[p] || [])
    .map((m) => `<option value="${esc(m)}">`).join("");
}

$("setProvider").addEventListener("change", updateProviderUI);

$("toggleToken").addEventListener("click", () => {
  const input = $("setToken");
  const show = input.type === "password";
  input.type = show ? "text" : "password";
  $("toggleToken").textContent = show ? "Hide" : "Show";
});

$("clearToken").addEventListener("click", () => {
  $("setToken").value = "";
  state.settings.token = "";
  save(); renderMode();
  toast("Token cleared from this device.");
});

$("settingsForm").addEventListener("submit", (ev) => {
  ev.preventDefault();
  state.settings = {
    provider: $("setProvider").value,
    model: $("setModel").value.trim() || "gpt-4o-mini",
    base_url: $("setBaseUrl").value.trim(),
    token: $("setToken").value.trim(),
  };
  save(); renderMode();
  const r = $("testResult");
  r.hidden = false; r.className = "test-result ok";
  r.textContent = "✅ Settings saved — everything stays on this device.";
  toast("Settings saved on this device.");
});

$("testKey").addEventListener("click", async () => {
  // Test what's currently in the form, even before saving.
  const payload = {
    provider: $("setProvider").value,
    model: $("setModel").value.trim(),
    api_key: $("setToken").value.trim(),
    base_url: $("setProvider").value === "custom" ? $("setBaseUrl").value.trim() : undefined,
  };
  const r = $("testResult");
  r.hidden = false; r.className = "test-result";
  if (!payload.api_key) { r.className = "test-result err"; r.textContent = "Paste an API token first, then test."; return; }
  if (!payload.model) { r.className = "test-result err"; r.textContent = "Enter a model name first."; return; }
  r.textContent = "Testing…";
  $("testKey").disabled = true;
  try {
    const resp = await fetch(API + "/api/test-key", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    let data = {};
    try { data = await resp.json(); } catch (e) { /* ignore */ }
    if (resp.ok && data.ok) {
      r.className = "test-result ok";
      r.textContent = `✅ Connection works — ${PROVIDER_LABELS[data.provider] || data.provider} accepted the key for model ${data.model}. Hit "Save settings" to keep it.`;
      toast("Connection works.");
    } else {
      r.className = "test-result err";
      r.textContent = "❌ " + (data.detail || `Test failed (${resp.status}).`);
      toast("Connection test failed.", "err");
    }
  } catch (err) {
    r.className = "test-result err";
    r.textContent = "❌ Could not reach the app server: " + err.message;
  } finally {
    $("testKey").disabled = false;
  }
});

/* --------------------------------------------------------- export/import */

$("exportAll").addEventListener("click", () => {
  const includeSettings = $("exportIncludeSettings").checked;
  const data = {
    app: "pocketplane",
    version: APP_VERSION,
    exported_at: new Date().toISOString(),
    agents: state.agents,
    threads: state.threads,
    activity: state.activity,
    memories: state.memories,
    summaries: state.summaries,
    workflows: state.workflows,
  };
  if (includeSettings) {
    // User explicitly opted in (warned in the UI): includes the token.
    data.settings = state.settings;
  }
  downloadJSON(data, "pocketplane-backup.json");
  const m = $("dataMsg");
  m.hidden = false; m.className = "test-result ok";
  m.textContent = includeSettings
    ? "Exported WITH settings — remember, that file contains your API token in plain text."
    : "Exported — settings and your API token were NOT included.";
});

/* Download the backup file: one file with everything in it — only
 * the person holding it can open the app's data with it. */
$("exportDb").addEventListener("click", async () => {
  const m = $("dataMsg");
  try {
    save();
    await persistNow();
    const blob = new Blob([db.export()], { type: "application/vnd.sqlite3" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url; a.download = "pocketplane-backup.sqlite"; a.click();
    setTimeout(() => URL.revokeObjectURL(url), 2000);
    m.hidden = false; m.className = "test-result ok";
    m.textContent = "✅ Backup downloaded — one file with everything. Restore it here any time via “Restore backup”.";
    toast("Backup downloaded.");
  } catch (err) {
    m.hidden = false; m.className = "test-result err";
    m.textContent = "❌ Backup failed: " + err.message;
  }
});

$("importFile").addEventListener("change", async (ev) => {
  const file = ev.target.files[0];
  if (!file) return;
  const m = $("dataMsg");
  try {
    const buf = new Uint8Array(await file.arrayBuffer());

    /* Backup-file restore: the file's first bytes are the backup
     * format header. Open it, sanity-check it holds PocketPlane data,
     * then swap it in as this device's data and re-hydrate. */
    const magic = new TextDecoder().decode(buf.slice(0, 16));
    if (magic.startsWith("SQLite format 3")) {
      const tmp = new SQL.Database(buf);
      tmp.exec("PRAGMA foreign_keys = ON");
      const t = tmp.exec("SELECT name FROM sqlite_master WHERE type='table' AND name='agents'");
      if (!t.length || !t[0].values.length) {
        tmp.close();
        throw new Error("that file doesn't look like a PocketPlane backup.");
      }
      db.close();
      db = tmp;
      migrate(); // upgrade in place if it came from an older version
      state = hydrate();
      dbReady = true;
      setMeta("imported_sqlite_at", String(Date.now()));
      await persistNow();
      m.hidden = false; m.className = "test-result ok";
      m.textContent = "✅ Backup restored — this device now uses that data.";
      toast("Backup restored.");
      renderSettings(); renderMode(); renderChat();
      ev.target.value = "";
      return;
    }

    /* JSON restore: parsed, validated, then written in by the normal
     * save() path. */
    const data = JSON.parse(new TextDecoder().decode(buf));
    if (!Array.isArray(data.agents) || !data.agents.length) {
      throw new Error("That file doesn't look like a PocketPlane backup (no agents).");
    }
    state.agents = data.agents.map((a, i) => ({ created: Date.now() + i, ...a }));
    state.threads = data.threads || {};
    for (const tid of Object.keys(state.threads)) {
      state.threads[tid] = (state.threads[tid] || []).map((mm) =>
        mm.mid ? mm : { ...mm, mid: newMid() });
    }
    state.activity = Array.isArray(data.activity) ? data.activity : [];
    state.memories = Array.isArray(data.memories) ? data.memories : [];
    state.summaries = Array.isArray(data.summaries) ? data.summaries : [];
    state.workflows = Array.isArray(data.workflows)
      ? data.workflows.map((w) => ({ steps: [], ...w })) : [];
    if (data.settings) {
      // A settings block without a token keeps the token already on
      // this device; one with a token replaces it.
      state.settings = { ...state.settings, ...data.settings };
    }
    if (!state.agents.find((a) => a.id === state.activeAgentId)) {
      state.activeAgentId = state.agents[0].id;
    }
    summaryDraft = null;
    save();
    await persistNow();
    m.hidden = false; m.className = "test-result ok";
    m.textContent = "✅ Restore complete — everything is saved on this device.";
    toast("Restore complete.");
    renderSettings(); renderMode();
  } catch (err) {
    m.hidden = false; m.className = "test-result err";
    m.textContent = "❌ Restore failed: " + err.message;
  }
  ev.target.value = "";
});

/* ------------------------------------------------------------------ boot
 *
 * Boot sequence: load sql.js -> open the persisted SQLite bytes from
 * IndexedDB (or create a fresh database) -> migrate schema (v1 -> v2
 * in place) -> on the very first boot, import any legacy localStorage
 * state into SQLite and remove the old key (otherwise seed the default
 * agents) -> hydrate state from the tables -> render.
 *
 * Any failure here is LOUD: an in-page error with a retry button. We
 * deliberately do NOT fall back to localStorage — that would silently
 * hide a broken database layer.
 * ---------------------------------------------------------------------- */

async function boot() {
  try {
    if (typeof initSqlJs !== "function") {
      throw new Error("engine script (/assets/sql-wasm.js) failed to load");
    }
    SQL = await initSqlJs({ locateFile: (f) => "/assets/" + f });

    const bytes = await idbGet(IDB_DB_KEY);
    db = bytes ? new SQL.Database(new Uint8Array(bytes)) : new SQL.Database();
    db.exec("PRAGMA foreign_keys = ON");
    migrate();

    if (!getMeta("bootstrapped")) {
      // First boot on this device: one-time legacy import, or seed.
      let imported = false;
      const legacyRaw = localStorage.getItem(LS_KEY);
      if (legacyRaw) {
        try {
          state = stateFromLegacy(JSON.parse(legacyRaw));
          imported = true;
        } catch (e) {
          console.warn("PocketPlane: legacy localStorage state was unreadable; seeding fresh.", e);
          state = defaultState();
        }
      }
      dbReady = true;
      save(); // imported (or seeded) state lands in the SQLite tables
      setMeta("bootstrapped", "1");
      if (imported) {
        setMeta("migrated_from_localstorage", String(Date.now()));
        localStorage.removeItem(LS_KEY);
      }
      await persistNow();
    } else {
      state = hydrate();
      dbReady = true;
    }

    window.__oapBooted = true;
    $("bootOverlay").hidden = true;
    const hashView = (location.hash || "").replace(/^#\/?/, "");
    goto(VIEWS.includes(hashView) ? hashView : (state.view || "chat"));
    renderChat();
  } catch (err) {
    // Technical detail goes to the console only; the user gets plain words.
    console.error("PocketPlane boot failed:", err);
    window.__oapBootError = String((err && err.message) || err);
    const overlay = $("bootOverlay");
    if (overlay) overlay.hidden = true;
    const box = $("bootError");
    if (box) {
      $("bootErrorMsg").textContent =
        "PocketPlane couldn't open your saved data on this device.";
      box.hidden = false;
    }
  }
}

boot();
