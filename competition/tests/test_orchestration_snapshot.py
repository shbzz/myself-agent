from test_orchestration_engine import new_workflow, review, work


def test_public_run_uses_one_read_snapshot_during_terminal_commit(tmp_path, monkeypatch):
    store, run, _, _ = new_workflow(tmp_path, [work(), review()])
    store.orchestration.task(run["id"], "root/build", work(), "running", agent_id="worker-a")
    with store.connect() as db:
        db.execute("PRAGMA journal_mode=WAL")
    original = store.orchestration._snapshot_value
    committed = []

    def interleave(raw, public):
        value = original(raw, public)
        if not committed:
            committed.append(store.orchestration.stop(run["id"], "failed", "OSError"))
        return value

    monkeypatch.setattr(store.orchestration, "_snapshot_value", interleave)
    before = store.orchestration.run(run["id"])
    assert committed == [True]
    assert before["run"]["status"] == "running"
    assert before["tasks"][0]["status"] == "running"
    assert before["result"] is None
    after = store.orchestration.run(run["id"])
    assert after["run"]["status"] == "failed"
    assert after["tasks"][0]["status"] == "failed"
    assert after["result"] == {"status": "failed", "reason": "OSError"}


def test_supervisor_failure_keeps_completed_rejection_evidence(tmp_path):
    store, run, _, _ = new_workflow(tmp_path, [work(), review()])
    result = {"status": "rejected", "passed": False, "checks": [{"exit_code": 1}]}
    assert store.orchestration.finish(run["id"], result)
    assert store.orchestration.stop(run["id"], "failed", "AgentExecutionError")
    value = store.orchestration.run(run["id"])
    assert value["run"]["status"] == "failed"
    assert value["result"] == result
