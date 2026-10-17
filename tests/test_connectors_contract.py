"""Contract tests for EVERY catalog connector: config validation, secret-ref
rules, health shape, and honest LIVE/SCAFFOLD labelling."""
import pytest

from app.connectors import get_adapter, load_catalog, validate_config

CATALOG = load_catalog()

EXPECTED_IDS = {
    "microsoft365", "google_workspace", "slack", "teams", "jira", "confluence",
    "github", "gitlab", "servicenow", "salesforce", "postgres", "mysql",
    "snowflake", "s3_athena", "sharepoint", "notion", "linear", "zendesk",
    "hubspot",
}


def test_catalog_covers_common_enterprise_connectors():
    assert EXPECTED_IDS <= set(CATALOG), f"missing: {EXPECTED_IDS - set(CATALOG)}"


def minimal_config(entry):
    cfg = {}
    for f in entry.config_fields:
        if f.required:
            cfg[f.name] = "env:DEMO_REF" if f.kind == "secret_ref" else "https://example.invalid"
    if entry.adapter in ("sqlite", "local_fs"):
        cfg.setdefault("standin_name", "contract-test")
    return cfg


@pytest.mark.parametrize("entry", list(CATALOG.values()), ids=list(CATALOG))
def test_connector_contract(entry):
    assert entry.status in ("LIVE", "SCAFFOLD")
    assert entry.auth_type in ("oauth2", "api_key", "service_account", "basic", "connection_string")
    assert entry.scopes, "every connector must declare least-privilege scopes"
    # secret material is only ever declared as secret_ref fields
    for f in entry.config_fields:
        if any(w in f.name for w in ("key", "token", "password", "secret", "dsn")):
            assert f.kind == "secret_ref", f"{entry.id}.{f.name} must be a secret_ref"
    cfg = minimal_config(entry)
    assert validate_config(entry, cfg) == []
    adapter = get_adapter(entry)
    assert adapter.live == (entry.status == "LIVE")
    health = adapter.health(cfg)
    assert set(health) >= {"ok", "live", "adapter", "detail"}
    assert health["live"] == adapter.live
    result = adapter.read(cfg, entry.actions[0] if entry.actions else "read", {})
    assert "live" in result and result["live"] == adapter.live
    if not adapter.live:
        assert result.get("scaffold") is True, "scaffold reads must label themselves"


def test_raw_secret_rejected_where_ref_expected():
    entry = CATALOG["github"]
    fake_raw_token = "ghp_" + "x" * 30  # obviously fake fixture, built dynamically
    errors = validate_config(entry, {"api_key_ref": fake_raw_token})
    assert errors, "a raw token must be rejected"
    errors = validate_config(entry, {"api_key_ref": "not-a-ref"})
    assert errors, "a plain string is not a reference"
    assert validate_config(entry, {"api_key_ref": "env:GITHUB_TOKEN"}) == []
    assert validate_config(entry, {"api_key_ref": "vault:github/token"}) == []


def test_missing_required_field_rejected():
    entry = CATALOG["jira"]
    assert validate_config(entry, {}) != []
