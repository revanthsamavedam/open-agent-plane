/* Open Agent Plane — Personal.
 *
 * Everything lives in this browser's localStorage: agents, chat threads,
 * settings and the activity log. The API token is stored ONLY here, is
 * sent per-request to the stateless /api proxy, and the server never
 * persists, logs, or echoes it.
 */
"use strict";

const LS_KEY = "oapPersonal.v1";
const API = ""; // same origin

const PROVIDER_LABELS = {
  openai: "OpenAI",
  anthropic: "Anthropic (Claude)",
  openrouter: "OpenRouter",
  google: "Google (Gemini)",
  custom: "Custom endpoint",
};

const MODEL_PRESETS = {
  openai: ["gpt-4o-mini", "gpt-4o", "gpt-4.1-mini", "gpt-4.1"],
  anthropic: ["claude-sonnet-4-5", "claude-haiku-4-5", "claude-opus-4-1"],
  openrouter: ["openai/gpt-4o-mini", "anthropic/claude-sonnet-4-5", "meta-llama/llama-3.3-70b-instruct"],
  google: ["gemini-2.5-flash", "gemini-2.5-pro", "gemini-2.0-flash"],
  custom: [],
};

function seedAgents() {
  return [
    {
      id: "a-general", name: "General Assistant", emoji: "🤖",
      persona: "You are a helpful, friendly personal assistant. Answer clearly and concisely, and ask a short follow-up when it would genuinely help.",
      model: "",
    },
    {
      id: "a-writing", name: "Writing Coach", emoji: "✍️",
      persona: "You are a supportive writing coach. Help the user draft, tighten and polish their writing. Offer concrete rewrites, explain why a change helps, and keep their voice intact.",
      model: "",
    },
    {
      id: "a-coding", name: "Coding Buddy", emoji: "💻",
      persona: "You are a pragmatic coding partner. Give working code with brief explanations, call out edge cases and pitfalls, and prefer simple solutions over clever ones.",
      model: "",
    },
  ];
}

function defaultState() {
  const agents = seedAgents();
  return {
    settings: { provider: "openai", model: "gpt-4o-mini", base_url: "", token: "" },
    agents,
    threads: {},        // agentId -> [{role, content, ts, demo?, usage?, provider?, model?}]
    activity: [],       // [{ts, agent, provider, model, prompt_tokens, completion_tokens, total_tokens}]
    activeAgentId: agents[0].id,
    bannerDismissed: false,
    view: "chat",
  };
}

function loadState() {
  try {
    const raw = localStorage.getItem(LS_KEY);
    if (!raw) return defaultState();
    const parsed = JSON.parse(raw);
    const base = defaultState();
    return {
      ...base, ...parsed,
      settings: { ...base.settings, ...(parsed.settings || {}) },
      agents: Array.isArray(parsed.agents) && parsed.agents.length ? parsed.agents : base.agents,
      threads: parsed.threads || {},
      activity: Array.isArray(parsed.activity) ? parsed.activity : [],
    };
  } catch (e) {
    return defaultState();
  }
}

let state = loadState();
let editingAgentId = null;
let sending = false;

function save() {
  localStorage.setItem(LS_KEY, JSON.stringify(state));
}

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
}[c]));

function activeAgent() {
  return state.agents.find((a) => a.id === state.activeAgentId) || state.agents[0];
}
function hasToken() { return !!(state.settings.token || "").trim(); }
function effectiveModel(agent) {
  return (agent.model || "").trim() || state.settings.model;
}

/* ------------------------------------------------------------------ nav */

function goto(view) {
  state.view = view;
  save();
  for (const v of ["chat", "agents", "activity", "settings"]) {
    $("view-" + v).hidden = v !== view;
  }
  document.querySelectorAll(".nav-btn").forEach((b) =>
    b.classList.toggle("active", b.dataset.goto === view));
  if (view === "agents") renderAgents();
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
    badge.textContent = "Live · " + (PROVIDER_LABELS[state.settings.provider] || state.settings.provider);
    badge.className = "badge badge-live";
  } else {
    badge.textContent = "Demo mode";
    badge.className = "badge badge-demo";
  }
  $("setupBanner").hidden = hasToken() || state.bannerDismissed;
}

$("dismissBanner").addEventListener("click", () => {
  state.bannerDismissed = true; save(); renderMode();
});

function renderChat() {
  renderMode();
  const tabs = $("agentTabs");
  tabs.innerHTML = state.agents.map((a) =>
    `<button class="agent-tab ${a.id === state.activeAgentId ? "active" : ""}" data-agent="${esc(a.id)}">${esc(a.emoji || "🤖")} ${esc(a.name)}</button>`
  ).join("");
  tabs.querySelectorAll("[data-agent]").forEach((b) =>
    b.addEventListener("click", () => {
      state.activeAgentId = b.dataset.agent; save(); renderChat();
    }));

  const agent = activeAgent();
  $("threadTitle").textContent =
    `${agent.emoji || "🤖"} ${agent.name} · ${effectiveModel(agent)} via ${PROVIDER_LABELS[state.settings.provider] || state.settings.provider}`;

  const thread = state.threads[agent.id] || [];
  const box = $("messages");
  if (!thread.length) {
    box.innerHTML = `<div class="empty-thread">Say hello to <strong>${esc(agent.name)}</strong>.<br>${esc(agent.persona.slice(0, 140))}${agent.persona.length > 140 ? "…" : ""}</div>`;
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
    `This is a canned demo response from ${name}. The full app works with your own OpenAI, Claude, Gemini or OpenRouter key — your key stays in this browser.`,
  ];
  const thread = state.threads[agent.id] || [];
  return replies[thread.length % replies.length];
}

async function callChat(agent, thread) {
  const s = state.settings;
  const resp = await fetch(API + "/api/chat", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      provider: s.provider,
      model: effectiveModel(agent),
      api_key: s.token,
      base_url: s.provider === "custom" ? s.base_url : undefined,
      system: agent.persona,
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
  thread.push({ role: "user", content: text, ts: Date.now() });
  input.value = "";
  input.style.height = "auto";
  save(); renderChat();
  $("chatError").hidden = true;

  if (!hasToken()) {
    sending = true;
    $("typing").hidden = false;
    setTimeout(() => {
      thread.push({
        role: "assistant", content: demoReply(agent, text),
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
    const data = await callChat(agent, thread);
    thread.push({
      role: "assistant", content: data.reply, ts: Date.now(),
      usage: data.usage || null, provider: data.provider, model: data.model,
    });
    state.activity.unshift({
      ts: Date.now(), agent: agent.name,
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
  $("agentList").innerHTML = state.agents.map((a) => `
    <button class="card agent-card" data-edit="${esc(a.id)}">
      <span class="emoji">${esc(a.emoji || "🤖")}</span>
      <span>
        <span class="a-name">${esc(a.name)}</span>
        <span class="a-meta">${esc(a.model ? "model: " + a.model : "uses your Settings model")}</span>
        <span class="a-persona">${esc(a.persona)}</span>
      </span>
    </button>`).join("");
  $("agentList").querySelectorAll("[data-edit]").forEach((b) =>
    b.addEventListener("click", () => openAgentForm(b.dataset.edit)));
}

function openAgentForm(id) {
  editingAgentId = id || null;
  const a = id ? state.agents.find((x) => x.id === id) : null;
  $("agentFormTitle").textContent = a ? "Edit agent" : "New agent";
  $("agentName").value = a ? a.name : "";
  $("agentEmoji").value = a ? a.emoji : "";
  $("agentPersona").value = a ? a.persona : "";
  $("agentModel").value = a ? a.model : "";
  $("agentDelete").hidden = !a || state.agents.length <= 1;
  $("agentForm").hidden = false;
  $("agentForm").scrollIntoView({ behavior: "smooth", block: "start" });
}

$("newAgentBtn").addEventListener("click", () => openAgentForm(null));
$("agentFormCancel").addEventListener("click", () => { $("agentForm").hidden = true; });

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
    state.agents.push({ id, ...data });
    state.activeAgentId = id;
  }
  save();
  $("agentForm").hidden = true;
  renderAgents(); renderMode();
});

$("agentDelete").addEventListener("click", () => {
  const a = state.agents.find((x) => x.id === editingAgentId);
  if (!a || !confirm(`Delete ${a.name} and its conversation?`)) return;
  state.agents = state.agents.filter((x) => x.id !== a.id);
  delete state.threads[a.id];
  if (state.activeAgentId === a.id) state.activeAgentId = state.agents[0].id;
  save();
  $("agentForm").hidden = true;
  renderAgents();
});

/* -------------------------------------------------------------- activity */

function renderActivity() {
  const total = state.activity.reduce((n, a) => n + (a.total_tokens || 0), 0);
  $("activityTotals").textContent = state.activity.length
    ? `${state.activity.length} model call${state.activity.length === 1 ? "" : "s"} · ${total.toLocaleString()} total tokens (as reported by providers)`
    : "No model calls yet. Demo replies are not logged.";
  $("activityList").innerHTML = state.activity.length
    ? state.activity.map((a) => `
      <div class="card activity-card">
        <div class="when">${esc(new Date(a.ts).toLocaleString())}</div>
        <div class="what">${esc(a.agent)} — ${esc(a.model)}</div>
        <div class="detail">${esc(PROVIDER_LABELS[a.provider] || a.provider)} · ${a.total_tokens != null ? esc(a.total_tokens) + " tokens" : "usage not reported"}${a.prompt_tokens != null ? ` (${esc(a.prompt_tokens)} in / ${esc(a.completion_tokens)} out)` : ""}</div>
      </div>`).join("")
    : "";
}

function downloadJSON(obj, filename) {
  const blob = new Blob([JSON.stringify(obj, null, 2)], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url; a.download = filename; a.click();
  setTimeout(() => URL.revokeObjectURL(url), 2000);
}

$("exportActivity").addEventListener("click", () =>
  downloadJSON(state.activity, "agentplane-activity.json"));

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
  r.textContent = "✅ Settings saved — they live only in this browser.";
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
    } else {
      r.className = "test-result err";
      r.textContent = "❌ " + (data.detail || `Test failed (${resp.status}).`);
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
    app: "open-agent-plane-personal",
    exported_at: new Date().toISOString(),
    agents: state.agents,
    threads: state.threads,
    activity: state.activity,
  };
  if (includeSettings) {
    // User explicitly opted in (warned in the UI): includes the token.
    data.settings = state.settings;
  }
  downloadJSON(data, "agentplane-backup.json");
  const m = $("dataMsg");
  m.hidden = false; m.className = "test-result ok";
  m.textContent = includeSettings
    ? "Exported WITH settings — remember, that file contains your API token in plain text."
    : "Exported — settings and your API token were NOT included.";
});

$("importFile").addEventListener("change", (ev) => {
  const file = ev.target.files[0];
  if (!file) return;
  const reader = new FileReader();
  reader.onload = () => {
    const m = $("dataMsg");
    try {
      const data = JSON.parse(reader.result);
      if (!Array.isArray(data.agents) || !data.agents.length) {
        throw new Error("That file doesn't look like an Agent Plane backup (no agents).");
      }
      state.agents = data.agents;
      state.threads = data.threads || {};
      state.activity = Array.isArray(data.activity) ? data.activity : [];
      if (data.settings) {
        // A settings block without a token keeps the token already on
        // this device; one with a token replaces it.
        state.settings = { ...state.settings, ...data.settings };
      }
      if (!state.agents.find((a) => a.id === state.activeAgentId)) {
        state.activeAgentId = state.agents[0].id;
      }
      save();
      m.hidden = false; m.className = "test-result ok";
      m.textContent = "✅ Import complete.";
      renderSettings(); renderMode();
    } catch (err) {
      m.hidden = false; m.className = "test-result err";
      m.textContent = "❌ Import failed: " + err.message;
    }
    ev.target.value = "";
  };
  reader.readAsText(file);
});

/* ------------------------------------------------------------------ init */

goto(state.view || "chat");
renderChat();
