# Security

## Threat notes

- **Agents are untrusted callers.** The model can be prompt-injected by
  content it reads. The controls therefore live outside the model: agents
  hold no credentials, data reads are read-only, writes are drafts gated
  by a reviewer and a named human, and every action is audited.
- **A compromised agent is a contained agent.** Allow-lists limit which
  connectors and skills an agent may use; runs fail closed when an agent
  exceeds them (covered by tests).
- **The audit chain is tamper-evident, not tamper-proof.** Hash chaining
  detects edits to stored entries; it does not prevent a database admin
  from rewriting the whole chain. For stronger guarantees, anchor chain
  heads externally (export + sign on a schedule) — the export endpoints
  exist for this.

## Secrets handling

- Connector configuration accepts **references only** (`env:VAR`,
  `vault:path`, `secret:name`) for secret material; values that look like
  raw secrets are rejected during validation and in drafts.
- Secrets are resolved service-side by adapters at call time and are never
  written to the database, the audit trail, run records, or this repo.
- `.env` is for local configuration and is git-ignored. `.env.example`
  contains placeholders only.

## Auth modes

- `AUTH_MODE=demo` (default): actor from the `X-Actor` header. **Local
  demos only** — anyone who can reach the API can name any actor.
- `AUTH_MODE=jwt`: OIDC/JWT bearer tokens. This reference build verifies
  HS256 tokens with `JWT_SECRET`; production deployments should verify
  against their IdP's JWKS (Entra ID / Okta / Google). If JWT
  configuration is missing, startup fails — there is no silent fallback
  to demo mode. Authentication failures are audited.

## Deployment notes

- Put the API behind your TLS-terminating ingress / reverse proxy; the
  built-in server is for evaluation and small self-hosted installs.
- Restrict database file access: the SQLite database contains run records
  and the audit chain.
- Set stand-in directories (`AGENTPLANE_STANDIN_DIR`) on a volume you
  control; LIVE connector stand-ins contain sample data only.
- Review each connector's declared scopes before granting them; the
  catalog declares least-privilege scopes, but the grant is yours.

## Reporting

This is a reference open-source project; report issues via GitHub issues.
Do not include secrets or real organizational data in reports.
