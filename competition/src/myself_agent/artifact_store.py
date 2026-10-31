"""Bounded private snapshots and explicit Linux atomic-exchange rollback.

No database is restored. Pending/uncertain exchanges fence new workspace runs.
"""

import ctypes
import fcntl
import hashlib
import json
import os
import shutil
import stat
import time
import uuid
from contextlib import contextmanager

from myself_agent.general_settings import ConfigurationError
from myself_agent.general_store import now
from myself_agent.jobs import BusyError
from myself_agent.recovery_models import ArtifactRollbackInput, ArtifactSnapshotInput
from myself_agent.recovery_store import canonical, digest


def inventory(directory, destination=None):
    """Walk by directory FD; never follow links or copy special/hard-linked files."""
    entries, total = [], 0
    deadline = time.monotonic() + 15

    def walk(fd, prefix, target):
        nonlocal total
        for name in sorted(os.listdir(fd)):
            if len(entries) >= 1000 or time.monotonic() >= deadline:
                raise ConfigurationError("Artifact snapshot entry/time bound reached")
            info = os.stat(name, dir_fd=fd, follow_symlinks=False)
            relative = prefix + name
            mode = stat.S_IMODE(info.st_mode) & 0o777
            if stat.S_ISDIR(info.st_mode):
                child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                try:
                    child_target = target / name if target else None
                    if child_target:
                        child_target.mkdir(mode=0o700)
                    entries.append((relative + "/", 0, "directory", mode))
                    walk(child, relative + "/", child_target)
                    if child_target:
                        os.chmod(child_target, mode)
                finally:
                    os.close(child)
            elif stat.S_ISREG(info.st_mode):
                source = os.open(name, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW, dir_fd=fd)
                output = None
                try:
                    before = os.fstat(source)
                    if (
                        not stat.S_ISREG(before.st_mode)
                        or before.st_nlink != 1
                        or before.st_size > 16 * 1024 * 1024
                    ):
                        raise ConfigurationError("Artifact snapshot refuses file type/size/links")
                    if target:
                        output = (target / name).open("xb")
                    length, sha = 0, hashlib.sha256()
                    while chunk := os.read(source, 65536):
                        length += len(chunk)
                        total += len(chunk)
                        if (
                            length > 16 * 1024 * 1024
                            or total > 32 * 1024 * 1024
                            or time.monotonic() >= deadline
                        ):
                            raise ConfigurationError("Artifact snapshot byte/time bound reached")
                        sha.update(chunk)
                        if output:
                            output.write(chunk)
                    after = os.fstat(source)
                    if (before.st_size, before.st_mtime_ns, before.st_ctime_ns, before.st_mode) != (
                        after.st_size,
                        after.st_mtime_ns,
                        after.st_ctime_ns,
                        after.st_mode,
                    ):
                        raise ConfigurationError("Artifact changed during snapshot")
                    if output:
                        output.flush()
                        os.fchmod(output.fileno(), mode)
                        os.fsync(output.fileno())
                    entries.append((relative, length, sha.hexdigest(), mode))
                finally:
                    if output:
                        output.close()
                    os.close(source)
            else:
                raise ConfigurationError("Artifact snapshot refuses links and special files")
        if target:
            sync_directory(target)

    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        walk(fd, "", destination)
    finally:
        os.close(fd)
    return {"sha256": digest(entries), "entries": len(entries), "bytes": total}


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def exchange(left, right):
    """Fail closed if Linux RENAME_EXCHANGE is unavailable; no two-rename fallback."""
    libc = ctypes.CDLL(None, use_errno=True)
    function = getattr(libc, "renameat2", None)
    if function is None:
        raise ConfigurationError("Atomic directory exchange is unavailable on this host")
    function.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    function.restype = ctypes.c_int
    if function(-100, os.fsencode(left), -100, os.fsencode(right), 2) != 0:
        raise OSError(ctypes.get_errno(), "Atomic artifact exchange failed")
    sync_directory(left.parent)
    sync_directory(right.parent)


class ArtifactStore:
    def __init__(self, store):
        self.store = store
        self.directory = store.root / "artifact-snapshots"
        self.directory.mkdir(mode=0o700, exist_ok=True)
        with store.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS artifact_snapshots (
                  id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL, request_id TEXT UNIQUE NOT NULL,
                  manifest TEXT NOT NULL, note TEXT NOT NULL, created TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS artifact_rollbacks (
                  request_id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL,
                  snapshot_id TEXT NOT NULL, displaced_id TEXT NOT NULL, body TEXT NOT NULL,
                  before_manifest TEXT NOT NULL, after_manifest TEXT NOT NULL,
                  state TEXT NOT NULL, created TEXT NOT NULL, updated TEXT NOT NULL);
            """)

    @staticmethod
    def fence_workspace(db, workspace_id):
        if db.execute(
            "SELECT 1 FROM artifact_rollbacks WHERE workspace_id=? "
            "AND state IN ('prepared','uncertain')",
            (workspace_id,),
        ).fetchone():
            raise BusyError("Workspace has an unresolved artifact rollback; inspect and reconcile")

    def idle(self, db, workspace_id):
        self.fence_workspace(db, workspace_id)
        if db.execute(
            "SELECT 1 FROM runs r JOIN sessions s ON r.session_id=s.id "
            "WHERE s.workspace_id=? AND r.status IN ('queued','running','waiting_human')",
            (workspace_id,),
        ).fetchone():
            raise BusyError(
                "Artifact operation requires an idle workspace; cancel waiting runs first"
            )

    def copy(self, source):
        if len(list(self.directory.iterdir())) >= 32:
            raise ConfigurationError("Private artifact snapshot quota reached (32 directories)")
        identifier = uuid.uuid4().hex
        destination = self.directory / identifier
        destination.mkdir(mode=0o700)
        try:
            before = inventory(source)
            copied = inventory(source, destination)
            if before != copied or inventory(source) != copied or inventory(destination) != copied:
                raise ConfigurationError("Artifact changed while copying snapshot")
            sync_directory(self.directory)
            return identifier, copied
        except BaseException:
            # Only this newly allocated, private destination can be removed here.
            assert destination.parent == self.directory and len(destination.name) == 32
            shutil.rmtree(destination)
            raise

    def create(self, workspace_id, body):
        body = ArtifactSnapshotInput.model_validate(body).model_dump()
        directory = self.store.directory(workspace_id)
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            previous = db.execute(
                "SELECT * FROM artifact_snapshots WHERE request_id=?", (body["request_id"],)
            ).fetchone()
            if previous:
                if previous["workspace_id"] != workspace_id or previous["note"] != body["note"]:
                    raise BusyError("Snapshot request ID has different content")
                return dict(previous) | {"manifest": json.loads(previous["manifest"])}
            self.idle(db, workspace_id)
            identifier, manifest = self.copy(directory)
            created = now()
            db.execute(
                "INSERT INTO artifact_snapshots VALUES (?,?,?,?,?,?)",
                (
                    identifier,
                    workspace_id,
                    body["request_id"],
                    canonical(manifest),
                    body["note"],
                    created,
                ),
            )
        return {
            "id": identifier,
            "workspace_id": workspace_id,
            "request_id": body["request_id"],
            "manifest": manifest,
            "note": body["note"],
            "created": created,
        }

    def view(self, workspace_id):
        directory = self.store.directory(workspace_id)
        with self.store.connect() as db:
            snapshots = [
                dict(row) | {"manifest": json.loads(row["manifest"])}
                for row in db.execute(
                    "SELECT * FROM artifact_snapshots WHERE workspace_id=? ORDER BY created,id",
                    (workspace_id,),
                )
            ]
            rollbacks = [
                self.decode(row)
                for row in db.execute(
                    "SELECT * FROM artifact_rollbacks WHERE workspace_id=? "
                    "ORDER BY created,request_id",
                    (workspace_id,),
                )
            ]
        return {"current": inventory(directory), "snapshots": snapshots, "rollbacks": rollbacks}

    @staticmethod
    def decode(row):
        return dict(row) | {
            key: json.loads(row[key]) for key in ("body", "before_manifest", "after_manifest")
        }

    @contextmanager
    def operation(self):
        # Reconciliation must not race a live exchange between its durable phases.
        with (self.store.root / "artifact-operations.lock").open("a") as lock:
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise BusyError("Another artifact exchange or reconciliation is active") from None
            yield

    def rollback(self, workspace_id, body):
        body = ArtifactRollbackInput.model_validate(body).model_dump()
        with self.operation():
            return self._rollback(workspace_id, body)

    def _rollback(self, workspace_id, body):
        directory = self.store.directory(workspace_id)
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            previous = db.execute(
                "SELECT * FROM artifact_rollbacks WHERE request_id=?", (body["request_id"],)
            ).fetchone()
            if previous:
                if previous["workspace_id"] != workspace_id or previous["body"] != canonical(body):
                    raise BusyError("Rollback request ID has different content")
                return self.decode(previous)
            self.idle(db, workspace_id)
            snapshot = db.execute(
                "SELECT * FROM artifact_snapshots WHERE id=? AND workspace_id=?",
                (body["snapshot_id"], workspace_id),
            ).fetchone()
            if not snapshot:
                raise KeyError(body["snapshot_id"])
            before = inventory(directory)
            if before["sha256"] != body["expected_current_sha256"]:
                raise ConfigurationError("Current artifact fingerprint differs from confirmation")
            displaced_id, after = self.copy(self.directory / snapshot["id"])
            if after != json.loads(snapshot["manifest"]):
                raise ConfigurationError("Stored artifact snapshot failed integrity validation")
            db.execute(
                "INSERT INTO artifact_rollbacks VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    body["request_id"],
                    workspace_id,
                    snapshot["id"],
                    displaced_id,
                    canonical(body),
                    canonical(before),
                    canonical(after),
                    "prepared",
                    now(),
                    now(),
                ),
            )
        # Durable prepared state blocks all application writers before the filesystem step.
        try:
            if inventory(directory) != before:
                raise ConfigurationError("Current artifacts changed before atomic exchange")
            exchange(directory, self.directory / displaced_id)
        except Exception:
            with self.store.connect() as db:
                db.execute(
                    "UPDATE artifact_rollbacks SET state='uncertain',updated=? WHERE request_id=?",
                    (now(), body["request_id"]),
                )
            raise
        return self._reconcile(workspace_id, body["request_id"])

    def reconcile(self, workspace_id, request_id):
        with self.operation():
            return self._reconcile(workspace_id, request_id)

    def _reconcile(self, workspace_id, request_id):
        """Observe a prepared exchange; never replay it or discard either directory."""
        directory = self.store.directory(workspace_id)
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM artifact_rollbacks WHERE request_id=? AND workspace_id=?",
                (request_id, workspace_id),
            ).fetchone()
            if not row:
                raise KeyError(request_id)
            if row["state"] not in ("prepared", "uncertain"):
                return self.decode(row)
            current = inventory(directory)
            retained = inventory(self.directory / row["displaced_id"])
            before, after = json.loads(row["before_manifest"]), json.loads(row["after_manifest"])
            if current == after and retained == before:
                state = "completed"
                db.execute(
                    "INSERT OR IGNORE INTO artifact_snapshots VALUES (?,?,?,?,?,?)",
                    (
                        row["displaced_id"],
                        workspace_id,
                        "displaced-" + request_id,
                        canonical(before),
                        "Preserved pre-rollback artifacts",
                        now(),
                    ),
                )
            elif current == before and retained == after:
                state = "not_applied"
            else:
                state = "uncertain"
            db.execute(
                "UPDATE artifact_rollbacks SET state=?,updated=? WHERE request_id=?",
                (state, now(), request_id),
            )
            updated = db.execute(
                "SELECT * FROM artifact_rollbacks WHERE request_id=?", (request_id,)
            ).fetchone()
        return self.decode(updated)
