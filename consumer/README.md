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
- **Your API token lives only in your browser's localStorage.** When you
  send a message, the token travels with that one request to the proxy,
  which uses it exactly once to call your provider and then discards it.
  It is **never persisted, logged, or echoed** by the server — provider
  error messages are sanitized to strip the key if a provider reflects
  it back, and the test suite asserts the key appears in no response.
- **All your data is yours.** Agents, chat threads, settings and the
  activity log live in localStorage. Export/import a full backup as JSON
  (the API token is excluded from exports unless you explicitly opt in,
  with a warning).
- **Demo mode is honest.** With no token set, the app still works with
  clearly-labelled simulated replies — never presented as real model
  output.

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
├── app.js          all client logic; state in localStorage
├── styles.css      mobile-first styling
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
| Model key | your token, in your browser | org's sanctioned route, server-side |
| RBAC roles | **none** (v0.1) | admin / owner / approver / auditor / user |
| Audit | local activity log on your device | append-only, hash-chained, tamper-evident server trail |
| Connectors | **none** (v0.1) — chat only | governed catalog of 19 enterprise connectors |
| Approvals | none — it's your agent | reviewer veto + named human approval on writes |
| Data | your browser's localStorage | org database, 3-tier governed memory |

## License

MIT — see the repo root.
