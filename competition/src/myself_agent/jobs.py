"""One durable web job at a time, with a bounded subprocess execution lifetime."""

import fcntl
import io
import json
import os
import re
import signal
import sqlite3
import subprocess
import sys
import threading
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime

from myself_agent.config import DEFAULT_CONFIG, Settings
from myself_agent.scenarios import get_scenario
from myself_agent.tracking import changes, file_versions
from myself_agent.workflow import write_json

ACTIVE = ("queued", "running")
RUN_ID = re.compile(r"[a-f0-9]{32}")


class BusyError(Exception):
    pass


def read_json(path, default=None):
    if not path.is_file() or path.is_symlink():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


class JobManager:
    def __init__(self, settings: Settings, config_path=DEFAULT_CONFIG, runner=None):
        self.settings = settings
        self.config_path = config_path
        self.root = settings.run_root
        self.data = self.root.parent / "web"
        self.data.mkdir(parents=True, mode=0o750, exist_ok=True)
        self.root.mkdir(parents=True, mode=0o750, exist_ok=True)
        self.lock_file = (self.data / "manager.lock").open("a")
        try:
            fcntl.flock(self.lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.lock_file.close()
            raise RuntimeError("Another web worker already owns the project job queue") from None
        self.database = self.data / "jobs.sqlite"
        with self.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS jobs (
                run_id TEXT PRIMARY KEY, request_id TEXT UNIQUE NOT NULL,
                scenario TEXT NOT NULL, brief TEXT NOT NULL, concurrency INTEGER NOT NULL,
                max_repairs INTEGER NOT NULL, status TEXT NOT NULL, created TEXT NOT NULL,
                updated TEXT NOT NULL, error_type TEXT)""")
            columns = {row[1] for row in db.execute("PRAGMA table_info(jobs)")}
            for column in ("conversation_id", "parent_run_id"):
                if column not in columns:
                    db.execute(f"ALTER TABLE jobs ADD COLUMN {column} TEXT")
            db.execute("UPDATE jobs SET conversation_id=run_id WHERE conversation_id IS NULL")
            db.execute(
                "UPDATE jobs SET status='interrupted', error_type='ServerRestart' "
                "WHERE status IN ('queued','running')"
            )
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="myself-job")
        self.runner = runner or self.execute
        self.closed = threading.Event()
        self.process_lock = threading.Lock()
        self.process = None
        self.import_history()

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.database, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def import_history(self):
        for directory in self.root.iterdir():
            if not RUN_ID.fullmatch(directory.name) or directory.is_symlink():
                continue
            summary = read_json(directory / "summary.json")
            if not summary or summary.get("outcome") not in ("passed", "failed"):
                continue
            now = datetime.fromtimestamp(
                (directory / "summary.json").stat().st_mtime, UTC
            ).isoformat()
            with self.connect() as db:
                db.execute(
                    "INSERT OR IGNORE INTO jobs (run_id,request_id,scenario,brief,concurrency,"
                    "max_repairs,status,created,updated,error_type,conversation_id,parent_run_id) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        directory.name,
                        "history-" + directory.name,
                        summary.get("scenario", "csv-orders"),
                        "",
                        summary.get("concurrency", 2),
                        self.settings.max_repairs,
                        summary["outcome"],
                        now,
                        now,
                        None,
                        directory.name,
                        None,
                    ),
                )

    def list(self):
        with self.connect() as db:
            return [
                dict(row) for row in db.execute("SELECT * FROM jobs ORDER BY created DESC LIMIT 50")
            ]

    def directory(self, run_id):
        if not RUN_ID.fullmatch(run_id):
            raise KeyError("Invalid run identifier")
        directory = self.root / run_id
        if directory.is_symlink() or not directory.is_dir():
            raise KeyError("Unknown run")
        return directory

    def get(self, run_id):
        self.directory(run_id)
        with self.connect() as db:
            row = db.execute("SELECT * FROM jobs WHERE run_id=?", (run_id,)).fetchone()
        if row is None:
            raise KeyError("Unknown run")
        return dict(row)

    def submit(self, request):
        if self.closed.is_set():
            raise BusyError("服务正在停止，请稍后重试")
        now = datetime.now(UTC).isoformat()
        run_id = uuid.uuid4().hex
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            previous = db.execute(
                "SELECT * FROM jobs WHERE request_id=?", (request["request_id"],)
            ).fetchone()
            if previous:
                if any(
                    previous[key] != request[key]
                    for key in ("scenario", "brief", "concurrency", "max_repairs")
                ):
                    raise BusyError("同一请求编号不能提交不同内容，请重新创建任务")
                for key in ("conversation_id", "parent_run_id"):
                    if request.get(key) is not None and request[key] != previous[key]:
                        raise BusyError("同一请求不能切换会话或基础版本")
                return dict(previous)
            if db.execute("SELECT 1 FROM jobs WHERE status IN ('queued','running')").fetchone():
                raise BusyError("已有任务正在运行，请等待完成后再提交")
            conversation_id = request.get("conversation_id") or run_id
            if not RUN_ID.fullmatch(conversation_id):
                raise BusyError("会话编号无效")
            parent_id = request.get("parent_run_id")
            baseline = {}
            latest = db.execute(
                "SELECT * FROM jobs WHERE conversation_id=? "
                "ORDER BY created DESC, rowid DESC LIMIT 1",
                (conversation_id,),
            ).fetchone()
            if latest is not None and parent_id != latest["run_id"]:
                raise BusyError("请基于会话的最新任务继续修改")
            if parent_id:
                parent = self.get(parent_id)
                if (
                    parent["conversation_id"] != conversation_id
                    or parent["scenario"] != request["scenario"]
                    or parent["status"] in ACTIVE
                ):
                    raise BusyError("只能继续修改当前会话中已结束的同场景任务")
                parent_directory = self.directory(parent_id)
                snapshots = file_versions(parent_directory, get_scenario(parent["scenario"]))
                baseline = dict(read_json(parent_directory / "baseline.json", {}))
                baseline.update(
                    {name: versions[-1]["content"] for name, versions in snapshots.items()}
                )
            request = {**request, "conversation_id": conversation_id, "parent_run_id": parent_id}
            db.execute(
                "INSERT INTO jobs (run_id,request_id,scenario,brief,concurrency,max_repairs,"
                "status,created,updated,error_type,conversation_id,parent_run_id) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    run_id,
                    request["request_id"],
                    request["scenario"],
                    request["brief"],
                    request["concurrency"],
                    request["max_repairs"],
                    "queued",
                    now,
                    now,
                    None,
                    conversation_id,
                    parent_id,
                ),
            )
            directory = self.root / run_id
            directory.mkdir(mode=0o750, exist_ok=False)
            write_json(directory / "request.json", request)
            (directory / "brief.txt").write_text(request["brief"], encoding="utf-8")
            if baseline:
                write_json(directory / "baseline.json", baseline)
        self.pool.submit(self.work, run_id, request)
        return self.get(run_id)

    def update(self, run_id, status, error_type=None):
        with self.connect() as db:
            db.execute(
                "UPDATE jobs SET status=?, updated=?, error_type=? WHERE run_id=?",
                (status, datetime.now(UTC).isoformat(), error_type, run_id),
            )

    def execute(self, directory, request):
        command = [
            sys.executable,
            "-m",
            "myself_agent",
            "--config",
            str(self.config_path),
            "--run-root",
            str(self.root),
            "--run-id",
            directory.name,
            "--scenario",
            request["scenario"],
            "--brief-file",
            str(directory / "brief.txt"),
            "--concurrency",
            str(request["concurrency"]),
            "--max-repairs",
            str(request["max_repairs"]),
        ]
        if (directory / "baseline.json").is_file():
            command += ["--baseline-file", str(directory / "baseline.json")]
        environment = dict(os.environ, LANGSMITH_TRACING="false", LANGCHAIN_TRACING_V2="false")
        with (directory / "process.log").open("wb") as log:
            with self.process_lock:
                if self.closed.is_set():
                    raise RuntimeError("ServerShutdown")
                self.process = subprocess.Popen(
                    command, stdout=log, stderr=log, env=environment, start_new_session=True
                )
                process = self.process
            try:
                code = process.wait(timeout=600)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=5)
                raise TimeoutError("JobRuntimeLimit") from None
            finally:
                with self.process_lock:
                    self.process = None
        summary = read_json(directory / "summary.json")
        if not summary or code not in (0, 1):
            error = read_json(directory / "error.json", {})
            raise RuntimeError(error.get("error_type", "WorkerProcessError"))
        return summary

    def work(self, run_id, request):
        self.update(run_id, "running")
        try:
            summary = self.runner(self.directory(run_id), request)
            if summary["outcome"] not in ("passed", "failed"):
                raise ValueError("Invalid worker outcome")
            self.update(run_id, summary["outcome"])
        except Exception as error:
            # Exception class only: SDK errors may embed bodies or authorization headers.
            self.update(
                run_id, "interrupted" if self.closed.is_set() else "error", type(error).__name__
            )
        finally:
            # Clean up only containers belonging to this exact project run, including timeout.
            if self.runner == self.execute:
                for round_number in range(4):
                    subprocess.run(
                        [
                            "sudo",
                            "-n",
                            "docker",
                            "rm",
                            "-f",
                            f"myself-agent-{run_id}-{round_number}",
                        ],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        timeout=10,
                        check=False,
                    )

    def events(self, run_id):
        directory = self.directory(run_id)
        events = []
        path = directory / "events.jsonl"
        if path.is_file() and not path.is_symlink():
            for line in path.read_text(encoding="utf-8").splitlines():
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    continue  # A concurrent append can expose an incomplete final line.
        return events

    def conversations(self):
        with self.connect() as db:
            groups = db.execute(
                "SELECT conversation_id, MAX(updated) AS updated, COUNT(*) AS turns "
                "FROM jobs GROUP BY conversation_id ORDER BY updated DESC LIMIT 50"
            ).fetchall()
            result = []
            for group in groups:
                first = db.execute(
                    "SELECT * FROM jobs WHERE conversation_id=? ORDER BY created,rowid LIMIT 1",
                    (group["conversation_id"],),
                ).fetchone()
                last = db.execute(
                    "SELECT * FROM jobs WHERE conversation_id=? "
                    "ORDER BY created DESC,rowid DESC LIMIT 1",
                    (group["conversation_id"],),
                ).fetchone()
                result.append(
                    {
                        **dict(group),
                        "title": first["brief"][:48] or get_scenario(first["scenario"]).title,
                        "scenario": last["scenario"],
                        "status": last["status"],
                        "latest_run_id": last["run_id"],
                    }
                )
            return result

    def conversation(self, conversation_id):
        if not RUN_ID.fullmatch(conversation_id):
            raise KeyError("Invalid conversation identifier")
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM jobs WHERE conversation_id=? "
                "ORDER BY created DESC,rowid DESC LIMIT 100",
                (conversation_id,),
            ).fetchall()
        if not rows:
            raise KeyError("Unknown conversation")
        turns = []
        for row in reversed(rows):
            directory = self.directory(row["run_id"])
            turns.append(
                {
                    "job": dict(row),
                    "events": self.events(row["run_id"]),
                    "summary": read_json(directory / "summary.json"),
                }
            )
        return {"conversation_id": conversation_id, "turns": turns}

    def details(self, run_id):
        job = self.get(run_id)
        directory = self.directory(run_id)
        scenario = get_scenario(job["scenario"])
        events = self.events(run_id)
        rounds = []
        for number in range(4):
            report = read_json(directory / f"round-{number}" / "verification.json")
            if report:
                rounds.append({"round": number, **report})
        versions = file_versions(directory, scenario)
        baseline = read_json(directory / "baseline.json", {})
        artifacts = {
            **baseline,
            **{name: snapshots[-1]["content"] for name, snapshots in versions.items()},
        }
        return {
            "job": job,
            "scenario": scenario.public(),
            "events": events,
            "plan": read_json(directory / "plan.json"),
            "rounds": rounds,
            "summary": read_json(directory / "summary.json"),
            "artifacts": artifacts,
            "changes": changes(versions, baseline),
        }

    def archive(self, run_id):
        detail = self.details(run_id)
        if detail["job"]["status"] in ACTIVE:
            raise BusyError("任务结束后可下载结果")
        directory = self.directory(run_id)
        names = [
            "summary.json",
            "events.jsonl",
            "plan.json",
            "workflow.mmd",
            "request.json",
            "baseline.json",
        ]
        for number in range(4):
            names += [f"round-{number}/verification.json", f"round-{number}/artifact-hashes.json"]
            names += [
                f"round-{number}/workspace/{name}"
                for name in get_scenario(detail["job"]["scenario"]).dependencies
            ]
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
            for name in names:
                path = directory / name
                if (
                    path.is_file()
                    and not path.is_symlink()
                    and path.resolve().is_relative_to(directory)
                ):
                    archive.writestr(name, path.read_bytes())
        return output.getvalue()

    def close(self):
        self.closed.set()
        with self.process_lock:
            process = self.process
            if process and process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
        self.pool.shutdown(wait=True)
        fcntl.flock(self.lock_file, fcntl.LOCK_UN)
        self.lock_file.close()
