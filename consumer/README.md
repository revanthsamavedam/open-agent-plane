# Open Agent Plane — Personal (consumer edition)

**One agent per person — now for everyone, not just enterprises.**

A personal, bring-your-own-key AI agent app that runs in the browser.
Create your own agents (a writing coach, a coding buddy, anything with a
persona), chat with them using **your own** model provider account, and
keep everything on your own device.

> Live: deploy your own copy to Vercel in a few steps — see below.

## The privacy model (the whole point)

- **The server is stateless.** The only backend is a tiny proxy
  (`api/index.py`) with three routes: `/api/health`, `/api/chat`,
  `/api/test-key`. It has no database, no accounts, no logging of
  request bodies.
- **Your API token lives only in this device's SQLite database.**
  When you send a message, the token travels with that one request to
  the proxy, which uses it exactly once to call your provider and then
  discards it. It is **never persisted, logged, or echoed** by the
  server — provider error messages are sanitized to strip the key if a
  provider reflects it back, and the test suite asserts the key appears
  in no response.
- **All your data is yours.** Agents, chat threads, settings and the
  activity log live in a real SQLite database that runs inside your
  browser (see below). Download the actual `.sqlite` file, or
  export/import a JSON backup (the API token is excluded from JSON
  exports unless you explicitly opt in, with a warning).
- **Demo mode is honest.** With no token set, the app still works with
  clearly-labelled simulated replies — never presented as real model
  output.

## What's new in 0.2.0 — real SQLite in the browser

Until 0.1.0 the app kept its state in `localStorage`. It now runs a
**real SQLite database in the browser**:

- **Engine:** [sql.js](https://sql.js.org) — SQLite compiled to
  WebAssembly — vendored into `assets/` (`sql-wasm.js` +
  `sql-wasm.wasm`). The app never hot-links a CDN at runtime.
  The official sqlite.org WASM build with an OPFS backend was the
  alternative; its OPFS VFS requires cross-origin isolation
  (COOP/COEP + `SharedArrayBuffer`) and an async-proxy worker, which
  is fragile for a no-build-step static app and buys nothing
  user-visible here, so sql.js on the main thread is the pragmatic
  choice.
- **Persistence:** the database file is serialized to **IndexedDB**
  after every mutation (debounced, and flushed on tab-hide/page-hide)
  and reloaded on boot. All reads/writes go through SQL — there is no
  localStorage state store anymore.
- **Schema** (versioned with `PRAGMA user_version`, currently v1):
  `agents(id, name, emoji, persona, model_override, created_at)`,
  `messages(id, agent_id FK, role, content, ts, usage_json, demo,
  model, provider)`, `settings(key, value)` — the API token is a
  settings row — `activity(id, ts, agent_id, provider, model, tokens,
  note)`, and `meta(key, value)`.
- **Migration:** on first boot, any pre-0.2.0 `localStorage` state is
  imported into the tables (agents, threads, settings incl. token,
  activity), the old key is removed, and the migration is noted in
  `meta`. If SQLite/WASM fails to load, the app shows a clear error
  and refuses to start — it never silently falls back to localStorage.
- **Backups:** Settings offers **Download database (.sqlite)** — the
  actual database file — and Import accepts both JSON backups and
  `.sqlite` files (the imported database replaces the current one).
- A debug/test hook, `window.AgentPlaneDB.query(sql, params)`, runs
  SQL against the live database from the console.

## Features

- Providers: **OpenAI, Anthropic (Claude), OpenRouter, Google (Gemini),
  and any custom OpenAI-compatible endpoint** (your own base URL).
- Model is a free-text setting with per-provider preset suggestions;
  each agent can override the model.
- Agents: create / edit / delete personal agents with a name, emoji and
  persona (system prompt). Ships with three seeds: General Assistant,
  Writing Coach, Coding Buddy.
- Chat: per-agent threads, history on-device, typing indicator, token
  usage shown when the provider reports it.
- Activity log: every model call (time, agent, provider/model, tokens)
  — the personal echo of the enterprise audit trail — exportable as JSON.
- Settings: provider, model, token (password field with show/hide and
  clear), custom base URL, and a **Test connection** button that makes a
  tiny real call to validate your key.

## Architecture

```
consumer/
├── index.html      single-page app shell (no build step)
├── app.js          all client logic; state in an in-browser SQLite DB
├── styles.css      mobile-first styling
├── assets/         vendored sql.js: sql-wasm.js + sql-wasm.wasm
├── api/index.py    FastAPI proxy (Vercel Python runtime)
├── requirements.txt
├── vercel.json     /api/* → api/index.py
└── tests/          pytest suite (providers mocked via httpx.MockTransport)
```

Direct provider calls are made with `httpx` from the serverless
function (no agent framework — serverless weight matters). All request
payloads are validated Pydantic v2 models. Sensible caps: 50 messages
per request, 60k characters total.

## Deploy your own copy to Vercel

1. Fork or clone [the repo](https://github.com/revanthsamavedam/open-agent-plane).
2. In Vercel, **Add New → Project**, import the repo, and set the
   **Root Directory** to `consumer`.
3. Deploy. No environment variables are needed — there is nothing to
   configure server-side, by design.
4. Open the deployment URL, go to **Settings**, pick your provider,
   paste your API token, hit **Test connection**, then save.

Local development:

```bash
cd consumer
pip install fastapi httpx pydantic uvicorn pytest
uvicorn api.index:app --port 8137   # then open a static server for the UI
python -m pytest tests -q
```

## Honest differences vs the enterprise edition

The enterprise Open Agent Plane (the rest of this repo) is a governed,
multi-user platform. This personal edition deliberately is **not**:

| | Personal (this) | Enterprise |
|---|---|---|
| Users | one person, one browser | whole organization |
| Model key | your token, in your device's SQLite DB | org's sanctioned route, server-side |
| RBAC roles | **none** (v0.2) | admin / owner / approver / auditor / user |
| Audit | local activity log on your device | append-only, hash-chained, tamper-evident server trail |
| Connectors | **none** (v0.2) — chat only | governed catalog of 19 enterprise connectors |
| Approvals | none — it's your agent | reviewer veto + named human approval on writes |
| Data | SQLite (WASM) in your browser, persisted via IndexedDB | org database, 3-tier governed memory |

## License

MIT — see the repo root.
