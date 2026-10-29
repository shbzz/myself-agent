import os
import threading
import time
import uuid
from contextlib import nullcontext

import pytest
from conftest import PARSER, STATS
from myself_agent.scenarios import SCENARIOS, get_scenario
from myself_agent.scheduler import affected_tasks, ready_batch
from myself_agent.schema import Plan, SourceFile
from myself_agent.workflow import Workflow
from pydantic import ValidationError

SOURCES = {
    "parser.py": PARSER,
    "stats.py": STATS,
    "tokenize_text.py": """import re

def tokenize(text):
    return [word.lower() for word in re.findall(r"[A-Za-z]+", text)]
""",
    "metrics.py": """from collections import Counter
from tokenize_text import tokenize

def analyze(text):
    words = tokenize(text)
    return {"words": len(words), "unique": len(set(words)),
            "frequencies": dict(Counter(words))}
""",
    "report.py": """from parser import parse_orders
from stats import summarize

def build_report(csv_text):
    result = summarize(parse_orders(csv_text))
    return (f"# Order report\\n\\nOrders: {result['count']}\\n"
            f"Total: {result['total_cents']} cents\\n")
""",
}


def make_plan(scenario_id):
    return Plan(
        tasks=[
            {
                "filename": name,
                "instruction": "Implement the approved module contract",
                "depends_on": list(deps),
            }
            for name, deps in get_scenario(scenario_id).dependencies.items()
        ]
    )


def test_dependency_readiness_and_transitive_repair():
    plan = make_plan("order-report")
    assert set(
        ready_batch(plan, list(SOURCES.keys() & {t.filename for t in plan.tasks}), [], 2)
    ) == {"parser.py", "stats.py"}
    assert ready_batch(plan, [t.filename for t in plan.tasks], ["parser.py"], 2) == ["stats.py"]
    assert ready_batch(plan, [t.filename for t in plan.tasks], ["parser.py", "stats.py"], 2) == [
        "report.py"
    ]
    assert affected_tasks(plan, {"parser.py"}) == ["parser.py", "report.py"]
    assert affected_tasks(plan, {"report.py"}) == ["report.py"]
    with pytest.raises(ValidationError):
        Plan(
            tasks=[
                {
                    "filename": "parser.py",
                    "instruction": "Implement parser module",
                    "depends_on": ["report.py"],
                },
                {
                    "filename": "stats.py",
                    "instruction": "Implement statistics module",
                    "depends_on": [],
                },
            ]
        )
    with pytest.raises(ValueError, match="contracts"):
        get_scenario("text-insights").validate_plan(make_plan("csv-orders"))


@pytest.mark.docker
@pytest.mark.parametrize(
    "scenario_id,concurrency", [(name, 2) for name in SCENARIOS] + [("order-report", 1)]
)
def test_scenario_graph_and_trusted_acceptance(
    monkeypatch, settings, tmp_path, scenario_id, concurrency
):
    lock = threading.Lock()
    running = 0
    peak = 0
    prompts = {}

    def fake_role(model, role, schema, prompt):
        nonlocal running, peak
        if schema is Plan:
            return make_plan(scenario_id), {}
        name = role.replace("_developer", ".py")
        with lock:
            running += 1
            peak = max(peak, running)
            prompts[name] = prompt
        time.sleep(0.05)  # Overlap independent model calls so concurrency is observable.
        with lock:
            running -= 1
        return SourceFile(filename=name, content=SOURCES[name]), {}

    monkeypatch.setattr("myself_agent.workflow.invoke_role", fake_role)
    monkeypatch.setattr("myself_agent.workflow.model_session", lambda _: nullcontext(None))
    directory = tmp_path / "run"
    directory.mkdir()
    workflow = Workflow(
        settings, directory, uuid.uuid4().hex, scenario_id=scenario_id, concurrency=concurrency
    )
    previous_umask = os.umask(0o027)
    try:
        result = workflow.run()
    finally:
        os.umask(previous_umask)
    assert result["outcome"] == "passed", result["verification"]
    assert result["verification"]["report"]["tests_run"] == get_scenario(scenario_id).tests
    assert peak == (1 if scenario_id == "text-insights" or concurrency == 1 else 2)
    for name, dependencies in get_scenario(scenario_id).dependencies.items():
        for dependency in dependencies:
            assert SOURCES[dependency].splitlines()[0] in prompts[name]
            completed = next(
                i
                for i, e in enumerate(workflow.events)
                if e["kind"] == "agent_completed" and e["role"] == dependency[:-3] + "_developer"
            )
            started = next(
                i
                for i, e in enumerate(workflow.events)
                if e["kind"] == "agent_started" and e["role"] == name[:-3] + "_developer"
            )
            assert completed < started


@pytest.mark.docker
def test_repair_regenerates_downstream_without_rerunning_independent_module(
    monkeypatch, settings, tmp_path
):
    def fake_role(model, role, schema, prompt):
        if schema is Plan:
            return make_plan("order-report"), {}
        name = role.replace("_developer", ".py")
        return SourceFile(filename=name, content=SOURCES[name]), {}

    monkeypatch.setattr("myself_agent.workflow.invoke_role", fake_role)
    monkeypatch.setattr("myself_agent.workflow.model_session", lambda _: nullcontext(None))
    directory = tmp_path / "run"
    directory.mkdir()
    workflow = Workflow(
        settings, directory, uuid.uuid4().hex, inject_failure=True, scenario_id="order-report"
    )
    result = workflow.run()
    assert result["outcome"] == "passed"
    roles = [e["role"] for e in workflow.events if e["kind"] == "agent_completed"]
    assert roles.count("parser_developer") == 1
    assert roles.count("stats_developer") == 2
    assert roles.count("report_developer") == 2
