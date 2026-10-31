"""Terminal transition tests for manual orchestration stops."""

import sqlite3

import pytest
from test_orchestration_engine import new_workflow, review, work


def add_running_task(store, run_id, path):
    store.orchestration.task(run_id, path, work(), "running", agent_id="worker-a")


def test_stop_failed_marks_parent_and_running_leaf(tmp_path):
    store, run, _, _ = new_workflow(tmp_path, [work(), review()])
    add_running_task(store, run["id"], "root/build")

    store.orchestration.stop(run["id"], "failed", "OSError")
    persisted = store.orchestration.run(run["id"])
    parent = persisted["run"]

    assert parent["status"] == "failed"
    assert persisted["result"] == {"status": "failed", "reason": "OSError"}
    task = next(t for t in persisted["tasks"] if t["path"] == "root/build")
    assert task["status"] == "failed"


def test_cancelled_stop_blocks_late_stop_and_finish(tmp_path):
    store, run, _, _ = new_workflow(tmp_path, [work(), review()])
    add_running_task(store, run["id"], "root/build")

    store.orchestration.stop(run["id"], "cancelled", "UserStopped")
    store.orchestration.stop(run["id"], "failed", "LateFailure")

    persisted = store.orchestration.run(run["id"])
    parent = persisted["run"]
    assert parent["status"] == "cancelled"
    assert persisted["result"] == {"status": "cancelled", "reason": "UserStopped"}
    task = next(t for t in persisted["tasks"] if t["path"] == "root/build")
    assert task["status"] == "cancelled"

    assert store.orchestration.finish(run["id"], {"status": "succeeded", "passed": True}) is False
    persisted = store.orchestration.run(run["id"])
    parent = persisted["run"]
    assert parent["status"] == "cancelled"
    assert persisted["result"] == {"status": "cancelled", "reason": "UserStopped"}


def test_stop_failure_rolls_back_parent_task_and_result(tmp_path):
    store, run, _, _ = new_workflow(tmp_path, [work(), review()])
    add_running_task(store, run["id"], "root/build")
    assert store.run(run["id"])["status"] == "running"

    with store.connect() as connection:
        connection.execute(
            """
            CREATE TRIGGER fixture_rollback
            BEFORE UPDATE ON orchestrations
            BEGIN
                SELECT RAISE(ABORT, 'fixture rollback');
            END
            """
        )

    with pytest.raises(sqlite3.IntegrityError):
        store.orchestration.stop(run["id"], "failed", "OSError")

    persisted = store.orchestration.run(run["id"])
    parent = persisted["run"]
    assert parent["status"] == "running"
    assert persisted["result"] is None
    task = next(t for t in persisted["tasks"] if t["path"] == "root/build")
    assert task["status"] == "running"
    assert task["result"] is None
