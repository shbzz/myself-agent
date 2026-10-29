"""Independent, additive storage for arbitrary workspace sessions and durable events."""

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime

from myself_agent.jobs import BusyError


def now():
    return datetime.now(UTC).isoformat()


class AgentStore:
    def __init__(self, root):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.database = self.root / "general.sqlite"
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS workspaces (
                  id TEXT PRIMARY KEY, name TEXT NOT NULL, created TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS sessions (
                  id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL, title TEXT NOT NULL,
                  created TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS runs (
                  id TEXT PRIMARY KEY, session_id TEXT NOT NULL, request_id TEXT UNIQUE NOT NULL,
                  text TEXT NOT NULL, status TEXT NOT NULL, created TEXT NOT NULL,
                  updated TEXT NOT NULL, error_type TEXT);
                CREATE TABLE IF NOT EXISTS messages (
                  sequence INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL,
                  run_id TEXT NOT NULL, message TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS events (
                  sequence INTEGER PRIMARY KEY AUTOINCREMENT, workspace_id TEXT NOT NULL,
                  session_id TEXT NOT NULL, run_id TEXT NOT NULL, kind TEXT NOT NULL,
                  timestamp TEXT NOT NULL, payload TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS events_session ON events(session_id, sequence);
            """)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.database, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def workspace(self, identifier):
        with self.connect() as db:
            row = db.execute("SELECT * FROM workspaces WHERE id=?", (identifier,)).fetchone()
        if row is None:
            raise KeyError(identifier)
        return dict(row)

    def directory(self, workspace_id):
        self.workspace(workspace_id)
        return self.root / "workspaces" / workspace_id

    def workspaces(self):
        with self.connect() as db:
            return [dict(r) for r in db.execute("SELECT * FROM workspaces ORDER BY created")]

    def create_workspace(self, name):
        identifier = uuid.uuid4().hex
        directory = self.root / "workspaces" / identifier
        directory.mkdir(parents=True, mode=0o700)
        with self.connect() as db:
            db.execute("INSERT INTO workspaces VALUES (?,?,?)", (identifier, name, now()))
        return self.workspace(identifier)

    def session(self, identifier):
        with self.connect() as db:
            row = db.execute("SELECT * FROM sessions WHERE id=?", (identifier,)).fetchone()
        if row is None:
            raise KeyError(identifier)
        return dict(row)

    def sessions(self, workspace_id):
        self.workspace(workspace_id)
        with self.connect() as db:
            return [
                dict(r)
                for r in db.execute(
                    "SELECT * FROM sessions WHERE workspace_id=? ORDER BY created DESC",
                    (workspace_id,),
                )
            ]

    def create_session(self, workspace_id, title):
        self.workspace(workspace_id)
        identifier = uuid.uuid4().hex
        with self.connect() as db:
            db.execute(
                "INSERT INTO sessions VALUES (?,?,?,?)", (identifier, workspace_id, title, now())
            )
        return self.session(identifier)

    def run(self, identifier):
        with self.connect() as db:
            row = db.execute("SELECT * FROM runs WHERE id=?", (identifier,)).fetchone()
        if row is None:
            raise KeyError(identifier)
        return dict(row)

    def submit(self, session_id, request_id, text):
        self.session(session_id)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            previous = db.execute("SELECT * FROM runs WHERE request_id=?", (request_id,)).fetchone()
            if previous:
                if previous["session_id"] != session_id or previous["text"] != text:
                    raise BusyError("同一请求编号不能用于不同内容")
                return dict(previous), False
            if db.execute("SELECT 1 FROM runs WHERE status IN ('queued','running')").fetchone():
                raise BusyError("已有任务正在运行，请等待或停止它")
            identifier = uuid.uuid4().hex
            db.execute(
                "INSERT INTO runs VALUES (?,?,?,?,?,?,?,?)",
                (identifier, session_id, request_id, text, "queued", now(), now(), None),
            )
            db.execute(
                "UPDATE sessions SET title=? WHERE id=? AND title=?",
                (text[:48], session_id, "新会话"),
            )
        return self.run(identifier), True

    def status(self, run_id, status, error_type=None):
        with self.connect() as db:
            db.execute(
                "UPDATE runs SET status=?,updated=?,error_type=? WHERE id=?",
                (status, now(), error_type, run_id),
            )

    def message(self, session_id, run_id, message):
        with self.connect() as db:
            db.execute(
                "INSERT INTO messages(session_id,run_id,message) VALUES (?,?,?)",
                (session_id, run_id, json.dumps(message, ensure_ascii=False)),
            )

    def messages(self, session_id):
        self.session(session_id)
        with self.connect() as db:
            return [
                dict(r) | {"message": json.loads(r["message"])}
                for r in db.execute(
                    "SELECT * FROM messages WHERE session_id=? ORDER BY sequence", (session_id,)
                )
            ]

    def event(self, run_id, kind, payload):
        run = self.run(run_id)
        session = self.session(run["session_id"])
        with self.connect() as db:
            db.execute(
                "INSERT INTO events(workspace_id,session_id,run_id,kind,timestamp,payload) "
                "VALUES (?,?,?,?,?,?)",
                (
                    session["workspace_id"],
                    session["id"],
                    run_id,
                    kind,
                    now(),
                    json.dumps(payload, ensure_ascii=False),
                ),
            )

    def events(self, session_id, after=0):
        self.session(session_id)
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM events WHERE session_id=? AND sequence>? "
                "ORDER BY sequence LIMIT 200",
                (session_id, after),
            )
            return [
                dict(r) | {"event_id": str(r["sequence"]), "payload": json.loads(r["payload"])}
                for r in rows
            ]

    def runs(self, session_id):
        self.session(session_id)
        with self.connect() as db:
            return [
                dict(r)
                for r in db.execute(
                    "SELECT * FROM runs WHERE session_id=? ORDER BY created", (session_id,)
                )
            ]
