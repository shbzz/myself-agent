"""Frozen-case and comparison harness checks; all model responses here are fixtures."""

import base64
import json
import subprocess
import uuid
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage
from test_general import ScriptModel, call

from myself_agent import evaluation_cli as cli
from myself_agent.evaluation import (
    POLICY,
    EvaluationBudget,
    configuration_identity,
    definitions,
    run_worker,
    source_hashes,
)
from myself_agent.evaluation_cases import cases
from myself_agent.general_settings import ConfigurationError, PolicyInput
from myself_agent.general_store import AgentStore
from myself_agent.recovery_store import digest

PARSER = """import csv, io
from decimal import Decimal, InvalidOperation

def parse_orders(csv_text):
    reader=csv.DictReader(io.StringIO(csv_text))
    if not reader.fieldnames: raise ValueError('header')
    reader.fieldnames=[name.strip().lstrip('\\ufeff').strip() for name in reader.fieldnames]
    if not {'order_id','amount'} <= set(reader.fieldnames): raise ValueError('header')
    result=[]; seen=set()
    for row in reader:
        identifier=(row.get('order_id') or '').strip()
        currency=(row.get('currency','CNY') or '').strip().upper()
        try: value=Decimal((row.get('amount') or '').strip())
        except InvalidOperation as error: raise ValueError('amount') from error
        if not value.is_finite() or value<0: raise ValueError('amount')
        cents=value*100
        if cents!=cents.to_integral_value(): raise ValueError('precision')
        if not identifier or identifier in seen or currency not in {'CNY','USD','EUR'}:
            raise ValueError('identity/currency')
        seen.add(identifier)
        result.append({'order_id':identifier,'amount_cents':int(cents),'currency':currency})
    return result
"""
STATS = """def summarize(orders):
    totals={}
    for row in orders:
        totals[row['currency']]=totals.get(row['currency'],0)+row['amount_cents']
    return {'count':len(orders),'totals':totals}
"""
NORMALIZE = """from datetime import datetime, timezone

def normalize_events(records):
    result=[]
    for row in records:
        if not isinstance(row,dict) or not {'user','time','tags'} <= row.keys():
            raise ValueError('fields')
        if not isinstance(row['user'],str) or not row['user'].strip(): raise ValueError('user')
        if not isinstance(row['time'],str): raise ValueError('time')
        parsed=datetime.fromisoformat(row['time'].replace('Z','+00:00'))
        if parsed.tzinfo is None: raise ValueError('timezone')
        if not isinstance(row['tags'],list) or any(not isinstance(t,str) for t in row['tags']):
            raise ValueError('tags')
        tags=sorted({tag.strip().lower() for tag in row['tags'] if tag.strip()})
        result.append({'user':row['user'].strip(),'date':parsed.astimezone(timezone.utc).date().isoformat(),
                       'tags':tags})
    return result
"""
REPORT = """def summarize_events(events):
    groups={}
    for row in events:
        group=groups.setdefault(row['date'],{'count':0,'users':set(),'tags':{}})
        group['count']+=1; group['users'].add(row['user'])
        for tag in row['tags']:
            group['tags'][tag]=group['tags'].get(tag,0)+1
    return [{'date':date,'count':group['count'],'unique_users':len(group['users']),
             'tag_counts':group['tags']} for date,group in sorted(groups.items())]
"""
REFERENCES = {
    "parser.py": PARSER,
    "stats.py": STATS,
    "normalize.py": NORMALIZE,
    "report.py": REPORT,
}


def frozen(tmp_path, monkeypatch, *, real_image=False):
    monkeypatch.setattr(cli, "revision", lambda: "0" * 40)
    if not real_image:
        monkeypatch.setattr(cli.subprocess, "check_output", lambda *a, **k: "sha256:" + "a" * 64)
    root = tmp_path / "experiment"
    protocol = cli.freeze(root)
    return root, protocol


def test_case_hashes_commands_and_matrix_are_frozen(tmp_path, monkeypatch):
    root, protocol = frozen(tmp_path, monkeypatch)
    assert protocol["max_total_model_calls"] == 72
    assert len(protocol["order"]) == 6
    assert len({(v["case_id"], v["variant"]) for v in protocol["order"]}) == 6
    assert protocol["automatic_retries"] == 0
    assert cli.verify(root)["protocol_sha256"] == protocol["protocol_sha256"]
    for case in protocol["cases"]:
        original = case["case_sha256"]
        assert digest({k: v for k, v in case.items() if k != "case_sha256"}) == original
        for command in case["checks"]:
            source = base64.b64decode(command.split("'")[1]).decode()
            compile(source, "<frozen-evaluator>", "exec")
    with pytest.raises(FileExistsError):
        cli.freeze(root)


def test_protocol_and_source_mutation_refuse_execution(tmp_path, monkeypatch):
    root, protocol = frozen(tmp_path, monkeypatch)
    changed = protocol | {"max_total_model_calls": 100}
    (root / "protocol.json").write_text(json.dumps(changed))
    with pytest.raises(ConfigurationError, match="changed"):
        cli.verify(root)
    (root / "protocol.json").write_text(json.dumps(protocol))
    monkeypatch.setattr(cli, "source_hashes", lambda: {"changed": "hash"})
    with pytest.raises(ConfigurationError, match="changed"):
        cli.verify(root)


def test_variant_definitions_share_tools_policy_and_fixed_node_contracts(tmp_path):
    store = AgentStore(tmp_path)
    store.config.save_policy("global", PolicyInput(**POLICY))
    config = store.config.resolve()
    case = cases()[0]
    single = definitions(case, "single", config)
    fixed = definitions(case, "fixed_team", config)
    custom = definitions(case, "custom", config)
    assert len(single["agents"]) == 1
    assert single["agents"][0]["definition"]["id"] == "solver"
    assert "task" not in single["agents"][0]["definition"]["tools"]
    assert fixed["agents"] == custom["agents"]
    assert fixed["workflow"]["flow"] == custom["workflow"]["flow"]["nodes"][0]["body"]
    assert fixed["workflow"]["flow"]["nodes"][-1]["checks"] == case["checks"]
    assert all(
        configuration_identity(value["configuration"]) == configuration_identity(config)
        for value in (single, fixed, custom)
    )
    worker_tools = single["agents"][0]["definition"]["tools"]
    assert all(
        a["definition"]["tools"] == worker_tools
        for a in fixed["agents"]
        if a["definition"]["role"] == "worker"
    )


def test_usage_deduplicates_callbacks_and_keeps_missing_tokens_unknown():
    events = []
    store = SimpleNamespace(event=lambda *args: events.append(args))
    budget = EvaluationBudget(store, "parent", 2)
    identifier = uuid.uuid4()
    budget.on_chat_model_start(None, [], run_id=identifier)
    budget.on_chat_model_start(None, [], run_id=identifier)
    message = AIMessage(
        content="fixture",
        usage_metadata={
            "input_tokens": 10,
            "output_tokens": 2,
            "total_tokens": 12,
            "input_token_details": {"cache_read": 4},
        },
    )
    response = SimpleNamespace(generations=[[SimpleNamespace(message=message)]])
    budget.on_llm_end(response, run_id=identifier)
    budget.on_llm_end(response, run_id=identifier)
    assert budget.totals()["model_calls"] == 1
    assert budget.totals()["total_tokens"] == 12
    assert budget.totals()["cached_input_tokens"] == 4
    budget.on_chat_model_start(None, [], run_id=uuid.uuid4())
    assert budget.totals()["total_tokens"] is None
    assert budget.totals()["observed_partial_tokens"] == 12
    assert budget.totals()["usage_complete"] is False
    with pytest.raises(ConfigurationError, match="预算"):
        budget.on_chat_model_start(None, [], run_id=uuid.uuid4())
    assert len([e for e in events if e[1] == "evaluation.model_started"]) == 2


@pytest.mark.docker
@pytest.mark.parametrize("case_index", [0, 1])
def test_real_frozen_evaluator_accepts_reference_rejects_bad_and_no_sentinel(
    tmp_path, settings, case_index
):
    case = cases()[case_index]
    directory = tmp_path / "workspace"
    directory.mkdir()
    for name in case["files"]:
        (directory / name).write_text(REFERENCES[name])
    good = cli.evaluate(directory, case, settings.image, uuid.uuid4().hex)
    assert good["passed"], good
    assert good["before"] == good["after"]
    first = case["modules"][0]["file"]
    (directory / first).write_text(case["files"][first])
    bad = cli.evaluate(directory, case, settings.image, uuid.uuid4().hex)
    assert not bad["passed"]
    # Exit code alone is not proof that the host assertions reached completion.
    (directory / first).write_text("import os\nos._exit(0)\n")
    deceptive = cli.evaluate(directory, case, settings.image, uuid.uuid4().hex)
    assert not deceptive["passed"]
    assert deceptive["checks"][0]["exit_code"] == 0
    assert deceptive["checks"][0]["marker_present"] is False


def with_usage(message):
    message.usage_metadata = {
        "input_tokens": 10,
        "output_tokens": 1,
        "total_tokens": 11,
        "input_token_details": {"cache_read": 5},
    }
    message.response_metadata = {"model_name": "offline-fixture"}
    return message


@pytest.mark.docker
@pytest.mark.parametrize("variant", ["single", "fixed_team", "custom"])
def test_real_bounded_runner_variants_use_same_host_evaluator(tmp_path, monkeypatch, variant):
    root, protocol = frozen(tmp_path, monkeypatch, real_image=True)
    index = next(
        i
        for i, v in enumerate(protocol["order"])
        if v["case_id"] == "orders-v1" and v["variant"] == variant
    )
    item = protocol["order"][index]
    slot = root / "runs" / item["slot"]
    slot.mkdir()
    seen = []
    case = cases()[0]

    @contextmanager
    def factory(request):
        seen.append(request.agent["id"])
        if request.node.kind == "verify":
            responses = [AIMessage(content='{"passed":true,"summary":"offline review fixture"}')]
        else:
            files = (
                list(case["files"])
                if variant == "single"
                else [request.agent["id"].replace("_worker", "") + ".py"]
            )
            responses = [
                call(
                    "edit_file",
                    {
                        "file_path": "/workspace/" + name,
                        "old_string": case["files"][name],
                        "new_string": REFERENCES[name],
                    },
                )
                for name in files
            ]
            responses.append(
                AIMessage(content='{"passed":true,"summary":"offline implementation fixture"}')
            )
        yield ScriptModel(responses=[with_usage(message) for message in responses])

    worker = run_worker(root, index, model_factory=factory)
    assert worker["error_type"] is None, worker
    assert worker["reported_passed"] is True
    assert worker["usage_complete"]
    assert worker["total_tokens"] == 11 * worker["model_calls"]
    receipt = json.loads((slot / "receipt.json").read_text())
    store = AgentStore(slot / "data")
    evaluation = cli.evaluate(
        store.directory(receipt["workspace_id"]), case, protocol["image"], receipt["run_id"]
    )
    assert evaluation["passed"], evaluation
    if variant == "single":
        assert seen == ["solver"] and worker["model_calls"] == 3
    else:
        assert len(seen) == 5 and worker["model_calls"] == 7
    with pytest.raises(ConfigurationError, match="never replay"):
        run_worker(root, index, model_factory=factory)


def test_supervisor_timeout_uses_same_process_and_never_replays_slots(tmp_path, monkeypatch):
    root, protocol = frozen(tmp_path, monkeypatch)
    processes = []
    signals = []

    class Process:
        def __init__(self, *args, **kwargs):
            self.pid = 10000 + len(processes)
            self.waits = 0
            processes.append(self)

        def poll(self):
            return None if self.waits < 2 else -15

        def wait(self, timeout):
            self.waits += 1
            if self.waits == 1:
                raise subprocess.TimeoutExpired("fixture", timeout)
            return -15

    monkeypatch.setattr(cli.subprocess, "Popen", Process)
    monkeypatch.setattr(cli.os, "killpg", lambda pid, signal: signals.append(pid))
    result = cli.run_suite(root)
    assert result["complete"] and len(processes) == 6 and len(signals) == 6
    assert all(r["error_type"] == "ActiveTimeBudgetExceeded" for r in result["observations"])
    assert all(
        r["model_calls"] is None and r["total_tokens"] is None for r in result["observations"]
    )
    cli.run_suite(root)
    assert len(processes) == 6
    row = protocol["order"][0]
    path = root / "runs" / row["slot"] / "result.json"
    value = json.loads(path.read_text())
    value["accepted"] = True
    path.write_text(json.dumps(value))
    with pytest.raises(ConfigurationError, match="integrity"):
        cli.observations(root, protocol)


def test_unfinished_slot_refuses_retry_and_no_production_root_is_initialized(tmp_path, monkeypatch):
    root, protocol = frozen(tmp_path, monkeypatch)
    slot = root / "runs" / protocol["order"][0]["slot"]
    slot.mkdir()
    with pytest.raises(ConfigurationError, match="never replay"):
        cli.run_suite(root)
    unrelated = tmp_path / "existing"
    unrelated.mkdir()
    (unrelated / "user-data").write_text("preserve")
    with pytest.raises(FileExistsError):
        cli.freeze(unrelated)
    assert list(unrelated.iterdir()) == [unrelated / "user-data"]
    assert source_hashes()


@pytest.mark.docker
def test_custom_repair_reuses_unaffected_module_without_new_budget(tmp_path, monkeypatch):
    root, protocol = frozen(tmp_path, monkeypatch, real_image=True)
    index = next(
        i
        for i, row in enumerate(protocol["order"])
        if row["case_id"] == "orders-v1" and row["variant"] == "custom"
    )
    item = protocol["order"][index]
    slot = root / "runs" / item["slot"]
    slot.mkdir()
    case = cases()[0]
    bad = "def parse_orders(csv_text):\n    return []\n"
    visited = []

    @contextmanager
    def factory(request):
        visited.append(request.path)
        if request.node.kind == "verify":
            responses = [AIMessage(content='{"passed":true,"summary":"fixture model opinion"}')]
        else:
            name = request.agent["id"].replace("_worker", "") + ".py"
            old = case["files"][name]
            new = REFERENCES[name]
            if name == "parser.py":
                if "iteration-1/" in request.path:
                    new = bad
                else:
                    old = bad
            responses = [
                call(
                    "edit_file",
                    {"file_path": "/workspace/" + name, "old_string": old, "new_string": new},
                ),
                AIMessage(content="fixture handoff"),
            ]
        yield ScriptModel(responses=[with_usage(message) for message in responses])

    result = run_worker(root, index, model_factory=factory)
    assert result["reported_passed"] is True and result["error_type"] is None, result
    assert result["model_calls"] == 10 and result["total_tokens"] == 110
    assert sum(path.endswith("/build_stats") for path in visited) == 1
    assert sum(path.endswith("/build_parser") for path in visited) == 2
    receipt = json.loads((slot / "receipt.json").read_text())
    store = AgentStore(slot / "data")
    with store.connect() as db:
        repairs = [
            json.loads(row[0])
            for row in db.execute(
                "SELECT payload FROM events WHERE run_id=? AND kind='orchestration.repair'",
                (receipt["run_id"],),
            )
        ]
    assert repairs[0]["reused"] == ["build_stats", "check_stats"]
    assert cli.evaluate(
        store.directory(receipt["workspace_id"]), case, protocol["image"], receipt["run_id"]
    )["passed"]


def test_operator_interrupt_stops_owned_worker_and_does_not_start_next_slot(tmp_path, monkeypatch):
    root, protocol = frozen(tmp_path, monkeypatch)
    processes, signals = [], []

    class Process:
        pid = 21001
        waits = 0

        def __init__(self, *args, **kwargs):
            processes.append(self)
            assert kwargs["env"]["LANGSMITH_TRACING"] == "false"
            assert kwargs["env"]["LANGCHAIN_TRACING_V2"] == "false"

        def poll(self):
            return None if self.waits < 2 else -15

        def wait(self, timeout):
            self.waits += 1
            if self.waits == 1:
                raise KeyboardInterrupt
            return -15

    monkeypatch.setattr(cli.subprocess, "Popen", Process)
    monkeypatch.setattr(cli.os, "killpg", lambda pid, signum: signals.append(pid))
    with pytest.raises(KeyboardInterrupt):
        cli.run_suite(root)
    assert len(processes) == 1 and signals == [21001]
    rows = cli.observations(root, protocol)
    assert len(rows) == 1 and rows[0]["error_type"] == "OperatorInterrupted"
    assert rows[0]["artifact_checks_passed"] is None
