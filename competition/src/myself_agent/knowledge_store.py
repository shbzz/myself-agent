"""Workspace-scoped, append-only sources and explicitly reviewed experience memory."""

import json
import uuid

from myself_agent.general_settings import ConfigurationError
from myself_agent.general_store import now
from myself_agent.jobs import BusyError
from myself_agent.knowledge_search import ALGORITHM, chunks, digest, text_digest

MAX_RECORDS = 64
MAX_VERSIONS = 64
MAX_WORKSPACE_BYTES = 8 * 1024 * 1024


class KnowledgeStore:
    def __init__(self, store):
        self.store = store
        with store.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS knowledge_records (
                workspace_id TEXT NOT NULL, kind TEXT NOT NULL, id TEXT NOT NULL,
                version INTEGER NOT NULL, value TEXT NOT NULL,
                PRIMARY KEY(workspace_id,kind,id,version))""")

    @staticmethod
    def _get(db, kind, workspace_id, identifier, version=None):
        sql = "SELECT value FROM knowledge_records WHERE workspace_id=? AND kind=? AND id=?"
        args = [workspace_id, kind, identifier]
        if version is not None:
            sql += " AND version=?"
            args.append(version)
        row = db.execute(sql + " ORDER BY version DESC LIMIT 1", args).fetchone()
        if not row:
            raise KeyError(identifier)
        return json.loads(row[0])

    def get(self, kind, workspace_id, identifier, version=None):
        self.store.workspace(workspace_id)
        with self.store.connect() as db:
            return self._get(db, kind, workspace_id, identifier, version)

    @staticmethod
    def summary(value):
        return {key: item for key, item in value.items() if key not in {"text", "chunks"}}

    def listing(self, kind, workspace_id):
        self.store.workspace(workspace_id)
        with self.store.connect() as db:
            rows = db.execute(
                "SELECT value FROM knowledge_records k WHERE workspace_id=? AND kind=? "
                "AND version=(SELECT MAX(version) FROM knowledge_records n "
                "WHERE n.workspace_id=k.workspace_id AND n.kind=k.kind AND n.id=k.id) "
                "ORDER BY id",
                (workspace_id, kind),
            ).fetchall()
        return [self.summary(json.loads(row[0])) for row in rows]

    def versions(self, kind, workspace_id, identifier):
        self.get(kind, workspace_id, identifier)
        with self.store.connect() as db:
            rows = db.execute(
                "SELECT value FROM knowledge_records WHERE workspace_id=? AND kind=? AND id=? "
                "ORDER BY version DESC",
                (workspace_id, kind, identifier),
            ).fetchall()
        return [self.summary(json.loads(row[0])) for row in rows]

    @staticmethod
    def _write(db, kind, workspace_id, identifier, expected, value, *, revocation=False):
        version = db.execute(
            "SELECT COALESCE(MAX(version),0) FROM knowledge_records "
            "WHERE workspace_id=? AND kind=? AND id=?",
            (workspace_id, kind, identifier),
        ).fetchone()[0]
        if version != expected:
            raise BusyError("Knowledge revision changed; reload before editing")
        count = db.execute(
            "SELECT COUNT(DISTINCT id) FROM knowledge_records WHERE workspace_id=? AND kind=?",
            (workspace_id, kind),
        ).fetchone()[0]
        if not revocation and (version >= MAX_VERSIONS or (version == 0 and count >= MAX_RECORDS)):
            raise ConfigurationError("Knowledge record/version limit reached")
        value = value | {
            "id": identifier,
            "workspace_id": workspace_id,
            "version": version + 1,
            "updated": now(),
        }
        encoded = json.dumps(value, ensure_ascii=False)
        used = db.execute(
            "SELECT COALESCE(SUM(LENGTH(CAST(value AS BLOB))),0) FROM knowledge_records "
            "WHERE workspace_id=?",
            (workspace_id,),
        ).fetchone()[0]
        if not revocation and used + len(encoded.encode()) > MAX_WORKSPACE_BYTES:
            raise ConfigurationError("Workspace knowledge history byte limit reached")
        db.execute(
            "INSERT INTO knowledge_records VALUES (?,?,?,?,?)",
            (workspace_id, kind, identifier, version + 1, encoded),
        )
        return value

    def save_source(self, body):
        self.store.workspace(body.workspace_id)
        text = body.text.replace("\r\n", "\n").replace("\r", "\n")
        value = {
            "title": body.title.strip(),
            "reference": body.reference,
            "active": body.active,
            "sha256": text_digest(text),
            "text": text,
            "chunks": chunks(text),
        }
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if not body.active:
                previous = self._get(db, "source", body.workspace_id, body.id)
                if not previous["active"]:
                    raise BusyError("Source is already withdrawn")
                # Compact revocations remain possible at the normal byte/version cap.
                value = {k: v for k, v in previous.items() if k not in {"text", "chunks"}}
                value.update(active=False, withdrawn_from_version=previous["version"])
            return self._write(
                db,
                "source",
                body.workspace_id,
                body.id,
                body.expected_version,
                value,
                revocation=not body.active,
            )

    @staticmethod
    def _evidence(db, body):
        row = db.execute(
            "SELECT r.status,r.action,o.snapshot,o.result FROM runs r "
            "JOIN sessions s ON s.id=r.session_id JOIN orchestrations o ON o.run_id=r.id "
            "WHERE r.id=? AND s.workspace_id=?",
            (body.run_id, body.workspace_id),
        ).fetchone()
        if not row:
            raise KeyError(body.run_id)
        overall = json.loads(row["result"]) if row["result"] else {}
        task = db.execute(
            "SELECT * FROM orchestration_tasks WHERE run_id=? AND path=?",
            (body.run_id, body.verification_path),
        ).fetchone()
        result = json.loads(task["result"]) if task and task["result"] else {}
        checks = result.get("checks", [])
        artifacts = result.get("artifacts") or {}
        if not (
            row["status"] == "succeeded"
            and row["action"] == "orchestrate"
            and overall.get("status") == "succeeded"
            and task
            and task["kind"] == "verify"
            and task["status"] == "succeeded"
            and result.get("passed") is True
            and result.get("review_passed") is True
            and checks
            and all(type(c.get("exit_code")) is int and c["exit_code"] == 0 for c in checks)
            and artifacts.get("before")
            and artifacts["before"] == artifacts.get("after")
            and result.get("producer_paths")
            and set(result["producer_paths"]) <= set(result.get("verified_paths", []))
            and task["agent_id"] not in result.get("producer_agents", [])
        ):
            raise ConfigurationError(
                "Experience requires completed independent acceptance evidence"
            )
        return {
            "run_id": body.run_id,
            "verification_path": body.verification_path,
            "verifier_agent_id": task["agent_id"],
            "artifact_sha256": artifacts["after"]["sha256"],
            "checks_sha256": digest(checks),
            "check_count": len(checks),
            "verified_paths_sha256": digest(result["verified_paths"]),
            "snapshot_sha256": digest(json.loads(row["snapshot"])),
        }

    def propose(self, body):
        self.store.workspace(body.workspace_id)
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            evidence = self._evidence(db, body)
            return self._write(
                db,
                "memory",
                body.workspace_id,
                uuid.uuid4().hex,
                0,
                {
                    "statement": body.statement,
                    "status": "candidate",
                    "evidence": evidence,
                    "action": "propose",
                    "reason": "Awaiting explicit human review",
                },
            )

    def decide(self, identifier, body):
        self.store.workspace(body.workspace_id)
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            previous = self._get(db, "memory", body.workspace_id, identifier)
            if body.action == "withdraw" and previous["status"] == "withdrawn":
                raise BusyError("Experience is already withdrawn")
            if body.action == "approve" and previous["status"] != "candidate":
                raise BusyError("Only a candidate experience can be approved")
            status = {"approve": "approved", "correct": "candidate", "withdraw": "withdrawn"}[
                body.action
            ]
            value = previous | {
                "status": status,
                "action": body.action,
                "reason": body.reason,
                "statement": body.statement if body.action == "correct" else previous["statement"],
            }
            return self._write(
                db,
                "memory",
                body.workspace_id,
                identifier,
                body.expected_version,
                value,
                revocation=body.action == "withdraw",
            )

    def freeze(self, session_id, agents):
        """Snapshot a team's explicit grants in one read transaction. No global fallback."""
        workspace_id = self.store.session(session_id)["workspace_id"]
        sources, memories, grants = {}, {}, {}
        with self.store.connect() as db:
            db.execute("BEGIN")
            for agent in agents:
                if not agent.get("enabled", True):
                    continue
                source_ids, memory_ids = agent.get("knowledge_ids", []), agent.get("memory_ids", [])
                if not source_ids and not memory_ids:
                    continue
                for identifier in source_ids:
                    source = self._get(db, "source", workspace_id, identifier)
                    if not source["active"]:
                        raise ConfigurationError("Referenced knowledge source is withdrawn")
                    sources[identifier] = {
                        key: value for key, value in source.items() if key != "text"
                    }
                for identifier in memory_ids:
                    memory = self._get(db, "memory", workspace_id, identifier)
                    if memory["status"] != "approved":
                        raise ConfigurationError("Referenced experience is not approved")
                    memories[identifier] = memory
                grants[agent["id"]] = {
                    "knowledge_ids": sorted(source_ids),
                    "memory_ids": sorted(memory_ids),
                }
        if not grants:
            return None
        value = {
            "workspace_id": workspace_id,
            "algorithm": ALGORITHM,
            "sources": [sources[k] for k in sorted(sources)],
            "memories": [memories[k] for k in sorted(memories)],
            "grants": grants,
        }
        return value | {"digest": digest(value)}
