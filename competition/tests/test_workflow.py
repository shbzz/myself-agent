import uuid
from contextlib import nullcontext

import pytest
from conftest import PARSER, STATS

from myself_agent.schema import Plan, SourceFile
from myself_agent.workflow import Workflow


@pytest.mark.docker
def test_graph_repairs_only_failed_module(monkeypatch, settings, tmp_path):
    def fake_role(model, role, schema, prompt):
        if schema is Plan:
            result = Plan(
                tasks=[
                    {
                        "filename": "parser.py",
                        "instruction": "Implement the CSV parsing contract",
                        "depends_on": [],
                    },
                    {
                        "filename": "stats.py",
                        "instruction": "Implement integer order statistics",
                        "depends_on": [],
                    },
                ]
            )
        else:
            filename = "parser.py" if role == "parser_developer" else "stats.py"
            result = SourceFile(
                filename=filename, content=PARSER if filename == "parser.py" else STATS
            )
        return result, {}

    monkeypatch.setattr("myself_agent.workflow.invoke_role", fake_role)
    monkeypatch.setattr("myself_agent.workflow.model_session", lambda _: nullcontext(None))
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    workflow = Workflow(settings, run_dir, uuid.uuid4().hex, inject_failure=True)
    summary = workflow.run()
    assert summary["outcome"] == "passed"
    assert summary["repair_rounds"] == 1
    completed = [event["role"] for event in workflow.events if event["kind"] == "agent_completed"]
    assert completed.count("parser_developer") == 1
    assert completed.count("stats_developer") == 2
    assert (run_dir / "checkpoints.sqlite").exists()
