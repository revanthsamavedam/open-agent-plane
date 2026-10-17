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
 * activity, meta, memories, summaries. There is NO parallel
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
const APP_VERSION = "0.3.0";

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
 * Schema (version tracked with PRAGMA user_version; currently v2):
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
 * ====================================================================== */

const SCHEMA_VERSION = 2;
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
    setMeta("active_agent_id", state.activeAgentId || "");
    setMeta("banner_dismissed", state.bannerDismissed ? "1" : "0");
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
  return {
    settings,
    agents,
    threads,
    activity,
    memories,
    summaries,
    activeAgentId: getMeta("active_agent_id") || (agents[0] && agents[0].id) || "",
    bannerDismissed: getMeta("banner_dismissed") === "1",
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
    activeAgentId: agents[0].id,
    bannerDismissed: false,
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

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
}[c]));
const VIEWS = ["chat", "agents", "memory", "activity", "settings"];

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
  $("setupBanner").hidden = hasToken() || state.bannerDismissed;
}

$("dismissBanner").addEventListener("click", () => {
  state.bannerDismissed = true; save(); renderMode();
});
$("heroDemo").addEventListener("click", () => {
  goto("chat");
  setTimeout(() => $("composerInput").focus(), 60);
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
    box.innerHTML = `<div class="empty-thread"><div class="empty-emoji">${esc(agent.emoji || "🤖")}</div><p>Say hello to <strong>${esc(agent.name)}</strong>.</p><p class="hint">${esc(agent.persona.slice(0, 160))}${agent.persona.length > 160 ? "…" : ""}</p></div>`;
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
  }
  box.scrollIntoView({ block: "end" });
}

$("clearThread").addEventListener("click", () => {
  const agent = activeAgent();
  if (!confirm(`Clear the whole conversation with ${agent.name}?`)) return;
  state.threads[agent.id] = [];
  save(); renderChat();
});

function demoReply(agent, userText) {
  const name = agent.name;
  const replies = [
    `I'm ${name}, running in demo mode — this is a simulated reply, not a real model. Add your API token in Settings and I'll answer "${userText.slice(0, 60)}" for real.`,
    `Demo mode here. I can see you asked about "${userText.slice(0, 60)}" — once you add a model token in Settings, ${name} will give you a genuine, personalised answer in this persona.`,
    `This is a canned demo response from ${name}. PocketPlane works with your own OpenAI, Claude, Gemini or OpenRouter key — your key stays in this device's SQLite database.`,
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

async function callChat(agent, thread, systemPrompt) {
  const s = state.settings;
  const resp = await fetch(API + "/api/chat", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      provider: s.provider,
      model: effectiveModel(agent),
      api_key: s.token,
      base_url: s.provider === "custom" ? s.base_url : undefined,
      system: systemPrompt,
      messages: thread
        .filter((m) => !m.demo)
        .slice(-40)
        .map((m) => ({ role: m.role, content: m.content })),
    }),
  });
  let data = {};
  try { data = await resp.json(); } catch (e) { /* non-JSON error page */ }
  if (!resp.ok) {
    throw new Error(data.detail || `Request failed (${resp.status}).`);
  }
  return data;
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
  r.textContent = "✅ Settings saved — stored in this device's SQLite database.";
  toast("Settings saved to this device's SQLite.");
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

/* Download the actual SQLite database file (a byte-for-byte export of
 * the live in-browser database). */
$("exportDb").addEventListener("click", async () => {
  const m = $("dataMsg");
  try {
    save();
    await persistNow();
    const blob = new Blob([db.export()], { type: "application/vnd.sqlite3" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url; a.download = "pocketplane.sqlite"; a.click();
    setTimeout(() => URL.revokeObjectURL(url), 2000);
    m.hidden = false; m.className = "test-result ok";
    m.textContent = "✅ Database downloaded — a real SQLite file. Import it here any time via “Import data”.";
    toast("Database downloaded.");
  } catch (err) {
    m.hidden = false; m.className = "test-result err";
    m.textContent = "❌ Database export failed: " + err.message;
  }
});

$("importFile").addEventListener("change", async (ev) => {
  const file = ev.target.files[0];
  if (!file) return;
  const m = $("dataMsg");
  try {
    const buf = new Uint8Array(await file.arrayBuffer());

    /* .sqlite import: the file's first bytes are the SQLite magic
     * header. Open it as a database, sanity-check it has our tables,
     * then swap it in as the live database and re-hydrate. */
    const magic = new TextDecoder().decode(buf.slice(0, 16));
    if (magic.startsWith("SQLite format 3")) {
      const tmp = new SQL.Database(buf);
      tmp.exec("PRAGMA foreign_keys = ON");
      const t = tmp.exec("SELECT name FROM sqlite_master WHERE type='table' AND name='agents'");
      if (!t.length || !t[0].values.length) {
        tmp.close();
        throw new Error("that .sqlite file has no agents table — not a PocketPlane database.");
      }
      db.close();
      db = tmp;
      migrate(); // upgrade in place if it came from an older version
      state = hydrate();
      dbReady = true;
      setMeta("imported_sqlite_at", String(Date.now()));
      await persistNow();
      m.hidden = false; m.className = "test-result ok";
      m.textContent = "✅ SQLite database imported — this device now uses that database.";
      toast("Database imported.");
      renderSettings(); renderMode(); renderChat();
      ev.target.value = "";
      return;
    }

    /* JSON import: parsed, validated, then written into the SQLite
     * tables by the normal save() path. */
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
    m.textContent = "✅ Import complete — saved into this device's SQLite database.";
    toast("Import complete.");
    renderSettings(); renderMode();
  } catch (err) {
    m.hidden = false; m.className = "test-result err";
    m.textContent = "❌ Import failed: " + err.message;
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
      throw new Error("The SQLite engine script (/assets/sql-wasm.js) failed to load.");
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
    console.error("PocketPlane boot failed:", err);
    window.__oapBootError = String((err && err.message) || err);
    const overlay = $("bootOverlay");
    if (overlay) overlay.hidden = true;
    const box = $("bootError");
    if (box) {
      $("bootErrorMsg").textContent = window.__oapBootError;
      box.hidden = false;
    }
  }
}

boot();
