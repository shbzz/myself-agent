"""Durable human checkpoints: explicit continuation, never crash replay."""

import hashlib
import json
import uuid

from myself_agent.general_settings import ConfigurationError
from myself_agent.general_store import now
from myself_agent.jobs import BusyError
from myself_agent.recovery_models import HumanDecision


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


class RecoveryStore:
    def __init__(self, store):
        self.store = store
        with store.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS human_checkpoints (
                  id TEXT PRIMARY KEY, run_id TEXT NOT NULL, path TEXT NOT NULL,
                  state TEXT NOT NULL, digest TEXT NOT NULL, payload TEXT NOT NULL,
                  created TEXT NOT NULL, UNIQUE(run_id,path));
                CREATE TABLE IF NOT EXISTS human_decisions (
                  request_id TEXT PRIMARY KEY, checkpoint_id TEXT UNIQUE NOT NULL,
                  run_id TEXT NOT NULL, body TEXT NOT NULL, actor TEXT NOT NULL,
                  created TEXT NOT NULL);
            """)

    @staticmethod
    def fence_workspace(db, workspace_id):
        if db.execute(
            "SELECT 1 FROM runs r JOIN sessions s ON s.id=r.session_id "
            "WHERE s.workspace_id=? AND r.status='waiting_human'",
            (workspace_id,),
        ).fetchone():
            raise BusyError("Workspace is reserved by a human checkpoint; approve or cancel first")

    def pause(self, run_id, checkpoint, runtime, manifest):
        snapshot = self.store.orchestration.snapshot(run_id)
        payload = checkpoint | {
            "format_version": 1,
            "runtime": runtime,
            "manifest": manifest,
            "snapshot_digest": digest(snapshot),
        }
        encoded = canonical(payload)
        if len(encoded.encode()) > 2 * 1024 * 1024:
            raise ConfigurationError("Human checkpoint exceeds storage bound")
        identifier = uuid.uuid4().hex
        value = digest({"id": identifier, "run_id": run_id, "payload": payload})
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            active = db.execute("SELECT status FROM runs WHERE id=?", (run_id,)).fetchone()
            running = db.execute(
                "SELECT 1 FROM orchestration_tasks WHERE run_id=? AND status='running'",
                (run_id,),
            ).fetchone()
            if not active or active[0] != "running" or running:
                raise BusyError("Checkpoint requires an active run at a quiescent boundary")
            db.execute(
                "UPDATE human_checkpoints SET state='consumed' WHERE run_id=? AND state='resuming'",
                (run_id,),
            )
            db.execute(
                "INSERT INTO human_checkpoints VALUES (?,?,?,?,?,?,?)",
                (identifier, run_id, checkpoint["path"], "waiting", value, encoded, now()),
            )
            db.execute(
                "UPDATE runs SET status='waiting_human',updated=? WHERE id=?", (now(), run_id)
            )
        self.store.event(
            run_id,
            "human.waiting",
            {"checkpoint_id": identifier, "digest": value, "path": checkpoint["path"]},
        )
        return self.view(run_id)

    def view(self, run_id):
        self.store.orchestration.snapshot(run_id)
        with self.store.connect() as db:
            checkpoints = db.execute(
                "SELECT * FROM human_checkpoints WHERE run_id=? ORDER BY created,id", (run_id,)
            ).fetchall()
            decisions = db.execute(
                "SELECT * FROM human_decisions WHERE run_id=? ORDER BY created,request_id",
                (run_id,),
            ).fetchall()
        return {
            "checkpoints": [
                dict(row) | {"payload": json.loads(row["payload"])} for row in checkpoints
            ],
            "decisions": [dict(row) | {"body": json.loads(row["body"])} for row in decisions],
        }

    def decide(self, run_id, body, *, actor):
        from myself_agent.orchestration_runtime import artifact_manifest

        body = HumanDecision.model_validate(body).model_dump()
        self.store.orchestration.snapshot(run_id)
        session = self.store.session(self.store.run(run_id)["session_id"])
        directory = self.store.directory(session["workspace_id"])
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            previous = db.execute(
                "SELECT * FROM human_decisions WHERE request_id=?", (body["request_id"],)
            ).fetchone()
            if previous:
                if (
                    previous["run_id"] != run_id
                    or previous["body"] != canonical(body)
                    or previous["actor"] != actor
                ):
                    raise BusyError("Decision request ID already belongs to different content")
                return dict(previous) | {"body": json.loads(previous["body"])}, False
            checkpoint = db.execute(
                "SELECT * FROM human_checkpoints WHERE id=? AND run_id=?",
                (body["checkpoint_id"], run_id),
            ).fetchone()
            parent = db.execute("SELECT status FROM runs WHERE id=?", (run_id,)).fetchone()
            if (
                not checkpoint
                or checkpoint["state"] != "waiting"
                or parent[0] != "waiting_human"
                or checkpoint["digest"] != body["digest"]
            ):
                raise BusyError("Human checkpoint or approval digest is stale")
            payload = json.loads(checkpoint["payload"])
            if body["decision"] == "approve":
                if db.execute("SELECT 1 FROM runs WHERE status IN ('queued','running')").fetchone():
                    raise BusyError("Another run is active")
                if payload["verification"]["passed"] is not True:
                    raise ConfigurationError("Human approval cannot override failed verification")
                if artifact_manifest(directory, include_git=True) != payload["manifest"]:
                    raise ConfigurationError("Workspace changed since human checkpoint")
                status, checkpoint_state = "queued", "approved"
            else:
                status, checkpoint_state = "failed", "rejected"
                db.execute(
                    "UPDATE orchestrations SET result=? WHERE run_id=?",
                    (
                        canonical(
                            {
                                "passed": False,
                                "status": "rejected",
                                "reason": "HumanRejected",
                                "checkpoint_id": checkpoint["id"],
                            }
                        ),
                        run_id,
                    ),
                )
                db.execute(
                    "UPDATE orchestration_tasks SET status='rejected',updated=? WHERE run_id=? "
                    "AND path=? AND status='waiting'",
                    (now(), run_id, checkpoint["path"]),
                )
            created = now()
            db.execute(
                "INSERT INTO human_decisions VALUES (?,?,?,?,?,?)",
                (
                    body["request_id"],
                    checkpoint["id"],
                    run_id,
                    canonical(body),
                    actor,
                    created,
                ),
            )
            db.execute(
                "UPDATE human_checkpoints SET state=? WHERE id=?",
                (checkpoint_state, checkpoint["id"]),
            )
            db.execute("UPDATE runs SET status=?,updated=? WHERE id=?", (status, created, run_id))
        record = {
            "request_id": body["request_id"],
            "checkpoint_id": checkpoint["id"],
            "run_id": run_id,
            "body": body,
            "actor": actor,
            "created": created,
        }
        self.store.event(run_id, "human.decided", record)
        return record, body["decision"] == "approve"

    def claim(self, run_id):
        """Consume only an explicitly approved checkpoint, once, in the worker."""
        from myself_agent.orchestration_runtime import artifact_manifest

        snapshot = self.store.orchestration.snapshot(run_id)
        session = self.store.session(self.store.run(run_id)["session_id"])
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM human_checkpoints WHERE run_id=? "
                "ORDER BY created DESC,id DESC LIMIT 1",
                (run_id,),
            ).fetchone()
            if not row:
                return None
            parent = db.execute("SELECT status FROM runs WHERE id=?", (run_id,)).fetchone()
            if row["state"] != "approved" or parent[0] != "running":
                raise BusyError("No approved checkpoint is available for continuation")
            payload = json.loads(row["payload"])
            if payload.get("format_version") != 1:
                raise ConfigurationError("Unsupported checkpoint format version")
            if digest(snapshot) != payload["snapshot_digest"]:
                raise ConfigurationError("Frozen run definition changed")
            if (
                artifact_manifest(self.store.directory(session["workspace_id"]), include_git=True)
                != payload["manifest"]
            ):
                raise ConfigurationError("Workspace changed after human approval")
            decision = db.execute(
                "SELECT request_id FROM human_decisions WHERE checkpoint_id=?", (row["id"],)
            ).fetchone()
            db.execute("UPDATE human_checkpoints SET state='resuming' WHERE id=?", (row["id"],))
        return payload | {"checkpoint_id": row["id"], "decision_id": decision[0]}
