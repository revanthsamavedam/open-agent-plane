"""Audit is append-only and hash-chained: tampering with a stored entry
must be detected by chain verification."""
import sqlite3

from app.models import AuditEntry
from app.store import Store, audit


def test_chain_intact_and_ordered(store):
    audit(store, "model_set", actor="admin@example.com", summary="model set")
    audit(store, "agent_created", actor="admin@example.com", summary="agent", detail="payload")
    entries = store.all_audit()
    assert [e.seq for e in entries] == [1, 2]
    assert entries[1].prev_hash == entries[0].entry_hash
    assert store.verify_audit_chain()["intact"] is True


def test_tampered_entry_is_detected(store, tmp_path):
    audit(store, "run_started", actor="a@example.com", summary="original summary")
    audit(store, "run_finished", actor="a@example.com", summary="done")
    assert store.verify_audit_chain()["intact"] is True
    # Tamper directly in the DB (bypassing the append-only API).
    conn = sqlite3.connect(str(tmp_path / "test.db"))
    row = conn.execute("SELECT payload FROM audit WHERE id = 'audit_1'").fetchone()
    entry = AuditEntry.model_validate_json(row[0])
    entry.summary = "forged summary"
    conn.execute("UPDATE audit SET payload = ? WHERE id = 'audit_1'",
                 (entry.model_dump_json(),))
    conn.commit()
    conn.close()
    result = store.verify_audit_chain()
    assert result["intact"] is False
    assert result["broken_at_seq"] == 1


def test_audit_has_no_update_or_delete_api(store):
    assert not hasattr(store, "update_audit")
    assert not hasattr(store, "delete_audit")
    assert not hasattr(store, "remove_audit")


def test_detail_is_hashed_never_stored_raw(store):
    audit(store, "tool_call", actor="a@example.com", summary="lookup",
          detail="super-sensitive-query-text")
    entry = store.all_audit()[-1]
    assert "super-sensitive" not in entry.model_dump_json()
    assert entry.detail_hashes.get("detail_sha256")
