# PocketPlane — the personal edition of Open Agent Plane

**Your agents. Your model. In your pocket.**

PocketPlane is a personal, bring-your-own-key AI agent app that runs in
the browser. Create your own agents (a writing coach, a coding buddy,
anything with a persona), chat with them using **your own** model
provider account, keep a memory they can lean on — and keep everything
on your own device. The enterprise edition of Open Agent Plane is the
rest of this repo; PocketPlane is its personal edition, by Open Agent
Plane.

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
- **All your data is yours.** Agents, chat threads, settings, memory
  and the activity log live in a real SQLite database that runs inside
  your browser (see below). Download the actual `.sqlite` file, or
  export/import a JSON backup (the API token is excluded from JSON
  exports unless you explicitly opt in, with a warning).
- **Demo mode is honest.** With no token set, the app still works with
  clearly-labelled simulated replies — never presented as real model
  output.

## What's new in 0.3.0 — memory, summaries, and a new face

- **Memory view.** Keep memory facts in two scopes: **personal**
  (your whole workspace) or **per-agent**. Add, edit and delete facts;
  they live in the `memories` table in the same on-device SQLite DB.
- **Summaries are derived views, never replacements.** Pick a scope —
  one agent (its messages + its memories) or your personal workspace
  (all messages + all memories) — and PocketPlane drafts a concise
  digest. With a model key set, the draft comes from your model via
  the `/api/chat` proxy; in demo mode it is a deterministic on-device
  extractive digest, clearly labelled **DEMO**. The draft is editable,
  and **nothing is stored until you click Save** — that click is the
  human gate, the personal-scale echo of the enterprise edition's
  draft → review → approve flow. A saved summary keeps full
  provenance: the exact source message and memory IDs it was derived
  from, the model/provider (or DEMO label) that generated it, and
  when. Originals are never deleted or altered by a summary.
- **Summaries as context.** A toggle in the Memory view (on by
  default): when chatting, the latest personal summary and the
  chatting agent's own latest summary are prepended — labelled as
  summaries — to that agent's system context.
- **Schema v2.** `PRAGMA user_version` is now 2: v0.2.0 databases are
  migrated in place (agents, messages, settings incl. token, and
  activity untouched), messages gain a stable `mid` so summary
  provenance survives reloads, and the new `memories` and
  `summaries` tables are added.
- **Full visual redesign.** Premium dark theme, one violet→cyan
  accent gradient, glassy cards, an agent rail on desktop and a
  bottom nav on mobile, a first-run hero — same app, grown up.

There are no team/org scopes in PocketPlane — it is a single-user
app, and it does not fake multi-user concepts. Scopes here are
exactly two: **agent** and **personal**.

## Real SQLite in the browser

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
  localStorage state store.
- **Schema** (versioned with `PRAGMA user_version`, currently v2):
  `agents(id, name, emoji, persona, model_override, created_at)`,
  `messages(id, mid, agent_id FK, role, content, ts, usage_json,
  demo, model, provider)`, `settings(key, value)` — the API token is
  a settings row — `activity(id, ts, agent_id, provider, model,
  tokens, note)`, `meta(key, value)`,
  `memories(id, agent_id nullable, scope, content, created_at)`, and
  `summaries(id, scope, agent_id nullable, title, content,
  source_message_ids, source_memory_ids, model, provider, demo,
  created_at)`.
- **Migration:** v1 databases migrate in place to v2; pre-0.2.0
  `localStorage` state is imported once on first boot (agents,
  threads, settings incl. token, activity), the old key is removed,
  and the migration is noted in `meta`. If SQLite/WASM fails to
  load, the app shows a clear error and refuses to start — it never
  silently falls back to localStorage.
- **Backups:** Settings offers **Download database (.sqlite)** — the
  actual database file — and Import accepts both JSON backups
  (memories and summaries included) and `.sqlite` files (the
  imported database replaces the current one).
- A debug/test hook, `window.AgentPlaneDB.query(sql, params)`, runs
  SQL against the live database from the console.

## Features

- Providers: **OpenAI, Anthropic (Claude), OpenRouter, Google (Gemini),
  and any custom OpenAI-compatible endpoint** (your own base URL).
- Model is a free-text setting with per-provider preset suggestions;
  each agent can override the model.
- Agents: create / edit / delete personal agents with a name, emoji
  and persona (system prompt). Ships with three seeds: General
  Assistant, Writing Coach, Coding Buddy.
- Chat: per-agent threads, history on-device, animated typing
  indicator, token usage shown when the provider reports it.
- Memory: personal and per-agent facts, plus governed summaries
  with provenance (see above).
- Activity log: every model call (time, agent, provider/model,
  tokens) and every saved summary — the personal echo of the
  enterprise audit trail — exportable as JSON.
- Settings: provider, model, token (password field with show/hide
  and clear), custom base URL, and a **Test connection** button that
  makes a tiny real call to validate your key.

## Architecture

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
multi-user platform. PocketPlane deliberately is **not**:

| | PocketPlane (this) | Enterprise |
|---|---|---|
| Users | one person, one browser | whole organization |
| Model key | your token, in your device's SQLite DB | org's sanctioned route, server-side |
| RBAC roles | **none** — single user | admin / owner / approver / auditor / user |
| Audit | local activity log on your device | append-only, hash-chained, tamper-evident server trail |
| Connectors | **none** — chat only | governed catalog of 19 enterprise connectors |
| Memory | personal + per-agent facts and summaries, on-device | 3-tier (private/team/org), governed promotion |
| Summarization | draft → you edit → you save; provenance kept | draft run → reviewer veto → named human approval → applied with provenance |
| Approvals | your Save click is the gate | reviewer veto + named human approval on writes |
| Data | SQLite (WASM) in your browser, persisted via IndexedDB | org database, 3-tier governed memory |

## License

MIT — see the repo root.
