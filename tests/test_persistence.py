"""State — including the audit chain — survives a store reopen."""
from app.models import Agent, Run
from app.store import Store, audit


def test_persistence_reopen(tmp_path):
    db = str(tmp_path / "reopen.db")
    store = Store(db)
    agent = Agent(name="Ops", owner="ops-lead@example.com", created_by="admin@example.com")
    store.put_agent(agent)
    run = Run(_store=store, agent_id=agent.id, skill="data-lookup",
              input="find alpha", created_by="admin@example.com")
    assert run._store is store, "store handle must survive the explicit __init__"
    run.status = "completed"
    run.save()
    audit(store, "run_finished", actor="admin@example.com", summary="done")
    store.close()

    reopened = Store(db)
    assert reopened.get_agent(agent.id).name == "Ops"
    assert reopened.get_run(run.id).status == "completed"
    assert len(reopened.all_audit()) == 1
    assert reopened.verify_audit_chain()["intact"] is True
    reopened.close()
