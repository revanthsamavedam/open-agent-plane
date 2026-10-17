# PocketPlane — the personal edition of Open Agent Plane

**Your agents. Your model. In your pocket.**

1. **Your agents** — agents you create, with memory you can see and edit.
2. **Your model** — works with OpenAI, Anthropic, Google or OpenRouter, on your own key. Switch anytime; you're never locked to one company.
3. **Your data** — everything stays on your device. No account. No server copy.

PocketPlane is a personal AI agent app that runs in your browser. Create
your own agents (a writing coach, a coding buddy, a whole team of them),
chat with them using your own model account, keep a memory they can lean
on — and keep everything on your own device. PocketPlane is the personal
edition of Open Agent Plane (the enterprise edition is the rest of this
repo); it is free and open source, and you can deploy your own copy to
Vercel in a few minutes — see "Deploy your own copy" below.

Open it and you're chatting in seconds: demo mode needs no setup at
all, and connecting your model is one optional step (pick a provider,
paste your key, tap Connect — your key stays on this device).

## What's new in 0.4.0 — Team mode, and simpler onboarding

- **Team mode.** Give your agents one job and they split the work. A
  lead agent plans the steps (with your model connected, the plan
  comes from the model; otherwise PocketPlane uses a starter
  template — the plan always says which), you can edit who's doing
  what before anything runs, then each step runs as its own call,
  with earlier results passed along, and the lead combines everything
  into one final answer. Runs are kept on this device: reopen a past
  run to see every step and its output. In demo mode the whole run is
  simulated and labelled DEMO. Honest cost note: **each step is a
  separate call on your model key**, so a 3-step run plus its final
  answer is 4 calls.
- **Simpler onboarding.** No setup wizard, no required steps: you land
  in Chat with demo agents ready. One slim welcome card explains the
  three claims above; "Connect your model" is a single sheet with
  provider cards and one key field. Tappable starter suggestions help
  you send your first message.
- **Plainer words.** The app now talks about your data being "on this
  device" instead of reaching for engineering terms.

## Features

- **Your agents:** create / edit / delete personal agents with a name,
  emoji and persona. Ships with three seeds: General Assistant,
  Writing Coach, Coding Buddy. Each agent can use its own model.
- **Chat:** per-agent threads, history kept on your device, animated
  typing indicator, token usage shown when your provider reports it,
  starter suggestions on empty threads.
- **Team mode:** multi-agent runs — plan, edit, run, resume after a
  failed step, and a combined final answer (see above).
- **Memory:** facts you keep, in two scopes — **personal** (your whole
  space) or **per-agent** — plus summaries your agents can lean on.
  A summary is a *derived view, never a replacement*: you review and
  edit the draft, nothing is stored until you click Save, and a saved
  summary keeps exactly which messages and facts it came from.
  Originals never change. Saved summaries can be used as agent
  context (a toggle in the Memory view, on by default).
- **Activity:** every call made from this device (time, agent,
  provider/model, tokens) and every saved summary — exportable as a
  copy.
- **Your data:** download a backup (one file with everything — only
  you hold it), export a copy as JSON, or restore a backup. Your key
  is left out of JSON copies unless you explicitly opt in, with a
  warning.
- **Settings:** the advanced surface — provider, model (with preset
  suggestions and per-agent overrides), your own endpoint address,
  and a **Test connection** button.

There are no team-of-people or organisation concepts in PocketPlane —
it is a single-user app, and it does not fake multi-user ideas.

## Deploy your own copy to Vercel

1. Fork or clone [the repo](https://github.com/revanthsamavedam/open-agent-plane).
2. In Vercel, **Add New → Project**, import the repo, and set the
   **Root Directory** to `consumer`.
3. Deploy. No environment variables are needed — there is nothing to
   configure server-side, by design.
4. Open your deployment URL and chat. When you're ready, tap
   **Connect your model**, pick your provider, paste your key, and
   tap Connect.

Local development:

```bash
cd consumer
pip install fastapi httpx pydantic uvicorn pytest
uvicorn api.index:app --port 8137   # then open a static server for the UI
python -m pytest tests -q
```

## Under the hood (for developers)

Everything below is implementation detail — none of it is needed to
use PocketPlane.

### The privacy model

- **The server is stateless.** The only backend is a small proxy
  (`api/index.py`) with three routes: `/api/health`, `/api/chat`,
  `/api/test-key`. It has no database, no accounts, and no logging of
  request bodies.
- **Your API token lives only in this device's SQLite database.**
  When you send a message, the token travels with that one request to
  the proxy, which uses it exactly once to call your provider and then
  discards it. It is **never persisted, logged, or echoed** by the
  server — provider error messages are sanitized to strip the key if
  a provider reflects it back, and the test suite asserts the key
  appears in no response.
- **All your data is yours.** Agents, chat threads, settings, memory,
  summaries, workflows and the activity log live in a real SQLite
  database that runs inside your browser (next section). Download
  the actual `.sqlite` backup file, or export/import a JSON backup
  (the API token is excluded from JSON exports unless you explicitly
  opt in, with a warning).
- **Demo mode is honest.** With no token set, the app still works
  with clearly-labelled simulated replies — never presented as real
  model output.

### Real SQLite in the browser

- **Engine:** [sql.js](https://sql.js.org) — SQLite compiled to
  WebAssembly — vendored into `assets/` (`sql-wasm.js` +
  `sql-wasm.wasm`). The app never hot-links a CDN at runtime. The
  official sqlite.org WASM build with an OPFS backend was the
  alternative; its OPFS VFS requires cross-origin isolation
  (COOP/COEP + `SharedArrayBuffer`) and an async-proxy worker, which
  is fragile for a no-build-step static app and buys nothing
  user-visible here, so sql.js on the main thread is the pragmatic
  choice.
- **Persistence:** the database file is serialized to **IndexedDB**
  after every mutation (debounced, and flushed on tab-hide/page-hide)
  and reloaded on boot. All reads/writes go through SQL — there is no
  localStorage state store.
- **Schema** (versioned with `PRAGMA user_version`, currently v3):
  `agents(id, name, emoji, persona, model_override, created_at)`,
  `messages(id, mid, agent_id FK, role, content, ts, usage_json,
  demo, model, provider)`, `settings(key, value)` — the API token is
  a settings row — `activity(id, ts, agent_id, provider, model,
  tokens, note)`, `meta(key, value)`,
  `memories(id, agent_id nullable, scope, content, created_at)`,
  `summaries(id, scope, agent_id nullable, title, content,
  source_message_ids, source_memory_ids, model, provider, demo,
  created_at)`, and — new in v3 — `workflows(id, task, lead_agent_id,
  status, plan_source, demo, model, provider, synthesis, error,
  created_at, finished_at)` plus `workflow_steps(id, workflow_id FK,
  step_key, agent_id, title, instruction, position, status, output,
  error, ts)`. Team-mode steps are stored **only** in
  `workflow_steps` — they never write into the agents' chat threads.
- **Migration:** v1 and v2 databases migrate in place to v3;
  pre-0.2.0 `localStorage` state is imported once on first boot
  (agents, threads, settings incl. token, activity), the old key is
  removed, and the migration is noted in `meta`. If SQLite/WASM fails
  to load, the app shows a clear error and refuses to start — it
  never silently falls back to localStorage.
- **Backups:** Settings offers **Download backup** — the actual
  database file — and Restore accepts both JSON backups (memories,
  summaries and workflows included) and `.sqlite` files (the imported
  database replaces the current one).
- A debug/test hook, `window.AgentPlaneDB.query(sql, params)`, runs
  SQL against the live database from the console.

### How Team mode works

The browser is the orchestrator; the server stays stateless.

- **Planning.** With a key set, one `/api/chat` call asks the model
  for a strict-JSON plan (`[{key, agent_name, title, instruction}]`,
  3–6 steps, agents chosen by name from the user's list). Parsing is
  defensive: invalid JSON, unknown agents or an empty plan fall back
  to the deterministic template (outline → draft → review, assigned
  by best persona/name match) and the run is stamped
  `plan_source="template-fallback"`; demo mode uses the template
  directly (`"template"`).
- **Execution.** Steps run sequentially, one `/api/chat` call each.
  The step's system prompt is that agent's usual context assembly
  (persona + saved summaries); the user message carries the overall
  task, the step's instruction, and previous steps' outputs under
  headings (each trimmed to ~2000 chars, newest kept first, total
  kept under the API's per-message cap). A Cancel button stops
  between steps; a failed step keeps its stored outputs and offers
  Resume from that step. A final synthesis call by the lead agent
  combines all step outputs into one answer.
- **Demo mode.** No key: every step and the synthesis are simulated
  on-device and labelled DEMO (workflow and step badges).

### Architecture

```
consumer/
├── index.html      single-page app shell (no build step)
├── app.js          all client logic; state in an in-browser SQLite DB
├── styles.css      mobile-first styling (dark theme, agent rail)
├── assets/         vendored sql.js: sql-wasm.js + sql-wasm.wasm
├── api/index.py    FastAPI proxy (Vercel Python runtime)
├── requirements.txt
├── vercel.json     /api/* → api/index.py
└── tests/          pytest suite (providers mocked via httpx.MockTransport)
```

Direct provider calls are made with `httpx` from the serverless
function (no agent framework — serverless weight matters). All
request payloads are validated Pydantic v2 models. Sensible caps:
50 messages per request, 60k characters total.

### Honest differences vs the enterprise edition

The enterprise Open Agent Plane (the rest of this repo) is a governed,
multi-user platform. PocketPlane deliberately is **not**:

| | PocketPlane (this) | Enterprise |
|---|---|---|
| Users | one person, one browser | whole organization |
| Model key | your token, in your device's SQLite DB | org's sanctioned route, server-side |
| RBAC roles | **none** — single user | admin / owner / approver / auditor / user |
| Audit | local activity log on your device | append-only, hash-chained, tamper-evident server trail |
| Connectors | **none** | governed catalog of 19 enterprise connectors |
| Orchestration | Team mode: browser-orchestrated steps on your key | governed child runs: RBAC-scoped, reviewer veto, human approval gates |
| Memory | personal + per-agent facts and summaries, on-device | 3-tier (private/team/org), governed promotion |
| Summarization | draft → you edit → you save; provenance kept | draft run → reviewer veto → named human approval → applied with provenance |
| Approvals | your Save click is the gate | reviewer veto + named human approval on writes |
| Data | SQLite (WASM) in your browser, persisted via IndexedDB | org database, 3-tier governed memory |

## License

MIT — see the repo root.
