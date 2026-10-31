"""Freeze once, execute each synthetic slot at most once, retain failures."""

import argparse
import fcntl
import json
import os
import re
import signal
import subprocess
import sys
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

from myself_agent.config import DEFAULT_CONFIG, DEFAULT_IMAGE
from myself_agent.evaluation import (
    EFFORT,
    POLICY,
    configuration_identity,
    run_worker,
    source_hashes,
)
from myself_agent.evaluation import (
    write_json as save,
)
from myself_agent.evaluation_cases import cases
from myself_agent.evaluation_metrics import summarize
from myself_agent.general_manager import AgentManager
from myself_agent.general_sandbox import WorkspaceSandbox
from myself_agent.general_settings import ConfigurationError, PolicyInput
from myself_agent.general_store import AgentStore
from myself_agent.orchestration_runtime import artifact_manifest
from myself_agent.recovery_store import digest


def revision():
    root = Path(__file__).resolve().parents[3]
    if subprocess.check_output(["git", "-C", str(root), "status", "--porcelain"], text=True):
        raise ConfigurationError("Commit reviewed benchmark sources before freezing")
    return subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip()


def freeze(root, config_path=DEFAULT_CONFIG):
    commit = revision()
    image = subprocess.check_output(
        ["sudo", "-n", "docker", "image", "inspect", DEFAULT_IMAGE, "--format", "{{.Id}}"],
        text=True,
    ).strip()
    if not re.fullmatch(r"sha256:[a-f0-9]{64}", image):
        raise ConfigurationError("A local immutable Docker image is required")
    root.mkdir(mode=0o700, parents=True, exist_ok=False)
    store = AgentStore(root / "control")
    store.config.save_policy("global", PolicyInput(**POLICY))
    matrix = [
        ("orders-v1", "single"),
        ("events-v1", "custom"),
        ("orders-v1", "fixed_team"),
        ("events-v1", "single"),
        ("orders-v1", "custom"),
        ("events-v1", "fixed_team"),
    ]
    order = [
        {"case_id": case, "variant": variant, "repeat": 1, "slot": f"{i:02d}-{case}-{variant}"}
        for i, (case, variant) in enumerate(matrix)
    ]
    protocol = {
        "version": 1,
        "created_at": datetime.now(UTC).isoformat(),
        "source_commit": commit,
        "source_hashes": source_hashes(),
        "cases": cases(),
        "order": order,
        "image": DEFAULT_IMAGE,
        "image_id": image,
        "config_path": str(config_path.absolute()),
        "configuration": configuration_identity(store.config.resolve()),
        "reasoning_effort": EFFORT,
        "max_model_calls_per_run": POLICY["max_model_calls"],
        "max_total_model_calls": 72,
        "active_seconds_per_run": POLICY["timeout_seconds"],
        "automatic_retries": 0,
        "primary_acceptance": "No worker error, strict true verdict and all host checks pass",
        "wrong_acceptance": "Pre-evaluator success claim rejected by final artifact checks",
        "limitations": [
            "Two synthetic cases, one observation per condition; no statistical superiority",
            "Provider sampling and host load are uncontrolled; reported cache usage is recorded",
            "Fixed templates exclude planning variability; planning observations are separate",
            "Finite behavioral checks, not proof against adversarial test-aware code",
        ],
    }
    protocol["protocol_sha256"] = digest(protocol)
    (root / "runs").mkdir(mode=0o700)
    save(root / "protocol.json", protocol)
    return protocol


def verify(root):
    protocol = json.loads((root / "protocol.json").read_text())
    original = protocol.pop("protocol_sha256")
    if digest(protocol) != original or protocol["source_hashes"] != source_hashes():
        raise ConfigurationError("Frozen protocol or executable sources changed")
    if protocol["cases"] != cases() or protocol["max_total_model_calls"] != 72:
        raise ConfigurationError("Frozen evaluator/call cap changed")
    image = subprocess.check_output(
        ["sudo", "-n", "docker", "image", "inspect", protocol["image"], "--format", "{{.Id}}"],
        text=True,
    ).strip()
    if image != protocol["image_id"]:
        raise ConfigurationError("Frozen local image identity changed")
    protocol["protocol_sha256"] = original
    return protocol


def evaluate(directory, case, image, parent_id):
    checks = []
    before = artifact_manifest(directory, include_git=True)
    unexpected = sorted(
        str(p.relative_to(directory))
        for p in directory.rglob("*")
        if p.is_file()
        and str(p.relative_to(directory)) not in case["files"]
        and "__pycache__" not in p.parts
    )
    with WorkspaceSandbox(
        directory,
        image,
        uuid.uuid4().hex,
        POLICY["tool_timeout_seconds"],
        read_only=True,
        parent_run_id=parent_id,
    ) as sandbox:
        for command, marker in zip(case["checks"], case["markers"], strict=True):
            result = sandbox.execute(command)
            checks.append(
                {
                    "command_sha256": digest(command),
                    "exit_code": result.exit_code,
                    "output": result.output[:4000],
                    "marker_present": marker in result.output,
                }
            )
    after = artifact_manifest(directory, include_git=True)
    passed = (
        not unexpected
        and before == after
        and all(check["exit_code"] == 0 and check["marker_present"] for check in checks)
    )
    return {
        "passed": passed,
        "checks": checks,
        "before": before,
        "after": after,
        "unexpected_artifacts": unexpected,
    }


def slot_observation(root, protocol, item, started, worker_error, *, skip_evaluation=False):
    slot = root / "runs" / item["slot"]
    case = next(case for case in protocol["cases"] if case["id"] == item["case_id"])
    worker_path = slot / "worker-result.json"
    worker = json.loads(worker_path.read_text()) if worker_path.exists() else {}
    error = worker_error or worker.get("error_type")
    receipt_path = slot / "receipt.json"
    evaluation, events = None, None
    try:
        receipt = json.loads(receipt_path.read_text())
        if not re.fullmatch(r"[a-f0-9]{32}", receipt["run_id"]):
            raise ConfigurationError("Invalid benchmark run receipt")
        AgentManager.cleanup(receipt["run_id"])
        store = AgentStore(slot / "data")
        directory = store.directory(receipt["workspace_id"])
        with store.connect() as db:
            events = [
                dict(row) | {"payload": json.loads(row["payload"])}
                for row in db.execute(
                    "SELECT * FROM events WHERE run_id=? ORDER BY sequence", (receipt["run_id"],)
                )
            ]
        save(slot / "events.json", events)
        if not skip_evaluation:
            evaluation = evaluate(directory, case, protocol["image"], receipt["run_id"])
        if worker_error:
            store.orchestration.stop(receipt["run_id"], "interrupted", worker_error)
    except Exception as exc:
        error = error or type(exc).__name__
    if not worker and events is not None:
        starts = {
            e["payload"]["call_id"] for e in events if e["kind"] == "evaluation.model_started"
        }
        worker["model_calls"] = len(starts)
    event_rows = events or []
    passed = evaluation["passed"] if evaluation is not None else None
    reported = worker.get("reported_passed")
    result = item | {
        "protocol_sha256": protocol["protocol_sha256"],
        "case_sha256": case["case_sha256"],
        "accepted": error is None and passed is True and reported is True,
        "artifact_checks_passed": passed,
        "reported_passed": reported,
        "error_type": error,
        "seconds": round(time.monotonic() - started, 6),
        "active_seconds": worker.get("active_seconds"),
        "model_calls": worker.get("model_calls"),
        "total_tokens": worker.get("total_tokens"),
        "cached_input_tokens": worker.get("cached_input_tokens"),
        "usage_complete": worker.get("usage_complete", False),
        "host_load_after": os.getloadavg(),
        "tool_calls": sum(event["kind"] == "tool.started" for event in event_rows)
        if events is not None
        else None,
        "tool_errors": sum(
            event["kind"] == "tool.failed"
            or (event["kind"] == "tool.finished" and event["payload"].get("failed") is True)
            for event in event_rows
        )
        if events is not None
        else None,
        "repair_events": [
            event["payload"] for event in event_rows if event["kind"] == "orchestration.repair"
        ],
        "reused_tasks": sum(
            event["kind"] == "orchestration.task" and event["payload"].get("status") == "reused"
            for event in event_rows
        ),
        "evaluation": evaluation,
    }
    result["observation_sha256"] = digest(result)
    save(slot / "result.json", result)
    return result


def observations(root, protocol):
    records = []
    for item in protocol["order"]:
        path = root / "runs" / item["slot"] / "result.json"
        if not path.exists():
            continue
        row = json.loads(path.read_text())
        expected = row.pop("observation_sha256")
        if digest(row) != expected or row["protocol_sha256"] != protocol["protocol_sha256"]:
            raise ConfigurationError("Benchmark observation integrity check failed")
        if any(row[key] != item[key] for key in ("case_id", "variant", "repeat", "slot")):
            raise ConfigurationError("Observation does not match its frozen slot")
        records.append(row | {"observation_sha256": expected})
    return records


def terminate(process):
    if process.poll() is not None:
        return
    os.killpg(process.pid, signal.SIGTERM)
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=5)


def run_suite(root):
    protocol = verify(root)
    with (root / "suite.lock").open("a") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        for index, item in enumerate(protocol["order"]):
            verify(root)
            slot = root / "runs" / item["slot"]
            if (slot / "result.json").exists():
                observations(root, protocol)
                continue
            if slot.exists():
                raise ConfigurationError(
                    "Unfinished benchmark slot exists; inspect it, never replay"
                )
            slot.mkdir(mode=0o700)
            save(
                slot / "state.json",
                {"status": "starting", "started_at": datetime.now(UTC).isoformat()},
            )
            started, worker_error, interrupted = time.monotonic(), None, False
            with (slot / "worker.log").open("w") as log:
                process = subprocess.Popen(
                    [
                        sys.executable,
                        "-m",
                        "myself_agent.evaluation_cli",
                        "worker",
                        "--root",
                        str(root),
                        "--index",
                        str(index),
                    ],
                    stdout=log,
                    stderr=log,
                    start_new_session=True,
                    env={
                        **os.environ,
                        "PYTHONPATH": str(Path(__file__).resolve().parents[1]),
                        "LANGSMITH_TRACING": "false",
                        "LANGCHAIN_TRACING_V2": "false",
                    },
                )
                save(
                    slot / "state.json",
                    {
                        "status": "running",
                        "pid": process.pid,
                        "started_at": datetime.now(UTC).isoformat(),
                    },
                )
                try:
                    if process.wait(timeout=POLICY["timeout_seconds"]) != 0:
                        worker_error = "WorkerProcessError"
                except subprocess.TimeoutExpired:
                    worker_error = "ActiveTimeBudgetExceeded"
                    terminate(process)
                except KeyboardInterrupt:
                    worker_error, interrupted = "OperatorInterrupted", True
                    terminate(process)
            result = slot_observation(
                root, protocol, item, started, worker_error, skip_evaluation=interrupted
            )
            save(slot / "state.json", {"status": "completed", "accepted": result["accepted"]})
            if interrupted:
                raise KeyboardInterrupt
            print(
                json.dumps(
                    {
                        key: result[key]
                        for key in (
                            "case_id",
                            "variant",
                            "accepted",
                            "model_calls",
                            "total_tokens",
                            "error_type",
                        )
                    }
                ),
                flush=True,
            )
        records = observations(root, protocol)
        report = {
            "protocol_sha256": protocol["protocol_sha256"],
            "observations": records,
            "summary": summarize(records),
            "complete": len(records) == len(protocol["order"]),
        }
        save(root / "report.json", report)
        return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["freeze", "run", "worker", "report"])
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--index", type=int)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    args = parser.parse_args()
    root = args.root.resolve()
    if args.action == "freeze":
        value = freeze(root, args.config)
        print(
            json.dumps({"protocol_sha256": value["protocol_sha256"], "max_total_model_calls": 72})
        )
    elif args.action == "run":

        def stop(signum, frame):
            raise KeyboardInterrupt

        signal.signal(signal.SIGTERM, stop)
        run_suite(root)
    elif args.action == "worker":
        protocol = verify(root)
        if args.index is None or not 0 <= args.index < len(protocol["order"]):
            parser.error("worker index is required and must name a frozen slot")
        run_worker(root, args.index)
    else:
        protocol = verify(root)
        rows = observations(root, protocol)
        print(
            json.dumps(
                {
                    "complete": len(rows) == len(protocol["order"]),
                    "expected_runs": len(protocol["order"]),
                    "observed_runs": len(rows),
                    "summary": summarize(rows),
                },
                ensure_ascii=False,
                indent=2,
            )
        )


if __name__ == "__main__":
    main()
