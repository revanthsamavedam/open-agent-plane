# Connectors

Adding a connector = adding a YAML file in `connectors/` and reusing (or
adding) an adapter that implements the common `ConnectorAdapter` protocol
(`health` / `read` / `write_draft`). No core code changes.

## Credential rules (non-negotiable)

- Config fields that hold secret material are declared `secret_ref` and must
  contain a **reference** — `env:VAR_NAME`, `vault:path/to/secret`, or
  `secret:name`. Raw secrets are rejected at validation time.
- Plaintext secrets are never stored in the database, the audit trail, or
  run records. Audit stores hashes + redacted summaries of inputs.
- Adapters resolve references **service-side** and return only results.
  Agents never hold connector credentials.

## Honesty labels

- **LIVE** — the adapter performs a real read/write against a local stand-in
  (SQLite stand-in for databases, a filesystem stand-in bucket for S3), with
  the production shape: read-only enforcement for data reads, drafts table /
  draft object for approved writes. Swap the stand-in target for the real
  service in a deployment.
- **SCAFFOLD** — catalog-complete (auth type, config fields, least-privilege
  scopes, contract tests) with a generic REST adapter that validates config
  and can probe a base URL, but does **not** implement the vendor's API.
  Reads/writes through a scaffold connector are recorded as *intent* and
  labelled as such in runs and audit. Never present a scaffold as live.

## Catalog

| Connector | Id | Status | Auth | Adapter |
|---|---|---|---|---|
| Confluence | `confluence` | SCAFFOLD | api_key | generic_rest |
| GitHub | `github` | SCAFFOLD | api_key | generic_rest |
| GitLab | `gitlab` | SCAFFOLD | api_key | generic_rest |
| Google Workspace (Gmail, Drive, Calendar) | `google_workspace` | SCAFFOLD | oauth2 | generic_rest |
| HubSpot | `hubspot` | SCAFFOLD | api_key | generic_rest |
| Jira | `jira` | SCAFFOLD | api_key | generic_rest |
| Linear | `linear` | SCAFFOLD | api_key | generic_rest |
| Microsoft 365 / Outlook | `microsoft365` | SCAFFOLD | oauth2 | generic_rest |
| MySQL | `mysql` | LIVE | connection_string | sqlite |
| Notion | `notion` | SCAFFOLD | api_key | generic_rest |
| Postgres (read-only) | `postgres` | LIVE | connection_string | sqlite |
| S3 + Athena | `s3_athena` | LIVE | service_account | local_fs |
| Salesforce | `salesforce` | SCAFFOLD | oauth2 | generic_rest |
| ServiceNow | `servicenow` | SCAFFOLD | basic | generic_rest |
| SharePoint | `sharepoint` | SCAFFOLD | oauth2 | generic_rest |
| Slack | `slack` | SCAFFOLD | oauth2 | generic_rest |
| Snowflake | `snowflake` | SCAFFOLD | service_account | scaffold |
| Microsoft Teams | `teams` | SCAFFOLD | oauth2 | generic_rest |
| Zendesk | `zendesk` | SCAFFOLD | api_key | generic_rest |

Every entry also declares least-privilege scopes/permissions in its YAML
(see `connectors/<id>.yaml`) and has a contract test: config validation,
secret-ref rules, health-response shape, and LIVE/SCAFFOLD consistency.

## Adding a connector

1. Copy the closest existing YAML in `connectors/` and edit: `id`, `name`,
   `status`, `auth_type`, `scopes` (least privilege), `config_fields`
   (secret material as `secret_ref`), `actions`.
2. Pick an adapter kind (`sqlite`, `local_fs`, `generic_rest`, `scaffold`)
   or add a new adapter class in `app/connectors.py` implementing the
   protocol, registered in `ADAPTERS`.
3. Run the test suite — the contract tests parametrize over every YAML file
   automatically.
