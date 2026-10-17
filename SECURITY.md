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

- `AUTH_MODE=demo` (default): actor from the `X-Actor` header, role from
  the `X-Role` header. **Local demos only** — anyone who can reach the
  API can name any actor and role.
- `AUTH_MODE=jwt`: OIDC/JWT bearer tokens. This reference build verifies
  HS256 tokens with `JWT_SECRET`; production deployments should verify
  against their IdP's JWKS (Entra ID / Okta / Google). If JWT
  configuration is missing, startup fails — there is no silent fallback
  to demo mode. Authentication failures are audited.

## RBAC (v1.1)

Every caller acts in exactly one role: **admin**, **owner**,
**approver**, **auditor**, or **user**. Authorization is enforced
server-side per endpoint; the UI merely reflects it.

Role source:

- JWT mode: the token's `role` claim (string) or `roles` claim (list) —
  the first recognized role wins; no recognized role resolves to `user`.
- Demo mode: the `X-Role` header. With no header, the default actor
  (`admin@example.com`) defaults to `admin`; any other actor defaults
  to `user`. Unrecognized role strings resolve to `user`.

Permission matrix:

| Action | admin | owner | approver | auditor | user |
|---|:---:|:---:|:---:|:---:|:---:|
| Read catalog / connectors / skills / agents / runs / settings / audit (incl. verify, exports, evidence packs) | ✓ | ✓ | ✓ | ✓ | ✓ |
| Set the model; run the model certification eval | ✓ | – | – | – | – |
| Configure a connector; test a connector | ✓ | – | – | – | – |
| Create an agent | ✓ | ✓ (own only) | – | – | – |
| Create a run | ✓ | ✓ (own agents) | – | – | ✓ (own agents) |
| Approve / reject a run | ✓ | – | ✓ | – | – |
| Add a memory item | ✓ | ✓ | ✓ | – | ✓ |
| Promote a memory item (private → team) | ✓ | – | ✓ | – | – |

Non-admin agent creation and runs are scoped by ownership: a non-admin
owner creates agents only for themselves, and non-admins run only
agents whose owner is the caller.

**Separation of duties.** The person who approves or rejects a run must
not be the person who created it — regardless of role (an admin who ran
it is still denied). Likewise, a memory item's owner cannot promote
their own item. Separation is evaluated on the effective approver (the
request's named approver, defaulting to the caller).

**Denials are evidence.** Every authorization denial (403) is written
to the audit trail as an `authz_denied` event naming the actor, the
role, and the attempted action, and appears in the audit viewer and
exports like any other event. `GET /api/me` reports the caller's
resolved actor, role, and auth mode.

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
