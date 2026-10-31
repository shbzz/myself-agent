import copy
import json
from contextlib import contextmanager

import pytest
from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError as SchemaError
from langchain_core.messages import AIMessage
from test_general import ScriptModel
from test_planning import planning_case

from myself_agent.general_settings import ConfigurationError
from myself_agent.orchestration_models import WorkflowInput
from myself_agent.planning_models import compile_plan
from myself_agent.planning_runtime import run_planning
from myself_agent.planning_schema import proposal_schema


def task(**changes):
    return {
        "id": "build",
        "kind": "agent",
        "prompt": "Build",
        "required_skills": ["build"],
        "access": "write",
        **changes,
    }


def proposal(nodes=None, **changes):
    return {
        "name": "Bounded plan",
        "max_steps": 12,
        "flow": {"nodes": nodes or [task()]},
        **changes,
    }


def validate(value, **options):
    schema = proposal_schema(12, **options)
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(value)
    WorkflowInput.model_validate(value)


def test_schema_has_only_per_kind_fields_and_never_exposes_human():
    schema = proposal_schema(12)
    assert "Node" not in schema["$defs"]
    assert "id" not in schema["properties"]
    assert schema["properties"]["max_steps"]["maximum"] == 12
    assert "default" not in schema["properties"]["max_steps"]
    assert "max_steps" in schema["required"]
    definitions = schema["$defs"]
    expected = {
        "PlannedAgent",
        "PlannedVerify",
        "PlannedCondition",
        "PlannedLoop",
        "PlannedSubworkflow",
    }
    mapping = definitions["Flow"]["properties"]["nodes"]["items"]
    assert len(mapping["oneOf"]) == 5 and mapping["discriminator"]["propertyName"] == "kind"
    assert {item["$ref"].split("/")[-1] for item in mapping["oneOf"]} == expected
    for name in expected:
        item = definitions[name]
        assert item["additionalProperties"] is False
        assert {"id", "kind"} <= set(item["required"])
        assert item["properties"]["kind"]["const"] != "human"
        assert "default" not in item["properties"]["kind"]
    assert set(definitions["PlannedAgent"]["properties"]) == {
        "id",
        "kind",
        "depends_on",
        "priority",
        "prompt",
        "required_skills",
        "required_tools",
        "access",
    }
    assert definitions["TopPlanningFlow"]["properties"]["nodes"]["maxItems"] == 15
    assert (
        proposal_schema(12, True)["$defs"]["TopPlanningFlow"]["properties"]["nodes"]["maxItems"]
        == 14
    )
    assert definitions["Flow"]["properties"]["nodes"]["maxItems"] == 16


@pytest.mark.parametrize("value", [True, 1, 97, 2.0, "12", None])
def test_bad_schema_budget_refused(value):
    with pytest.raises((TypeError, ValueError)):
        proposal_schema(value)


def test_schema_does_not_mutate_runtime_contract_or_other_calls():
    before = WorkflowInput.model_json_schema()
    first = proposal_schema(12)
    first["$defs"]["Flow"]["properties"]["nodes"]["maxItems"] = 0
    assert WorkflowInput.model_json_schema() == before
    assert proposal_schema(20)["$defs"]["Flow"]["properties"]["nodes"]["maxItems"] == 16
    with pytest.raises(ValueError):
        proposal_schema(2, True)


@pytest.mark.parametrize(
    "mutation",
    [
        "human",
        "missing_kind",
        "irrelevant",
        "missing_budget",
        "budget",
        "empty_skills",
        "empty_prompt",
        "top_id",
    ],
)
def test_generated_schema_rejects_known_bad_shapes(mutation):
    value = proposal()
    node = value["flow"]["nodes"][0]
    if mutation == "human":
        node["kind"] = "human"
    elif mutation == "missing_kind":
        del node["kind"]
    elif mutation == "irrelevant":
        node["max_iterations"] = 1
    elif mutation == "missing_budget":
        del value["max_steps"]
    elif mutation == "budget":
        value["max_steps"] = 13
    elif mutation == "empty_skills":
        node["required_skills"] = []
    elif mutation == "empty_prompt":
        node["prompt"] = ""
    else:
        value["id"] = "invented"
    with pytest.raises(SchemaError):
        validate(value)


def test_all_five_kinds_and_nested_bounds_use_the_real_runtime_validator():
    verify = {
        "id": "check",
        "kind": "verify",
        "prompt": "Review",
        "required_skills": ["test"],
        "checks": ["python -V"],
        "depends_on": ["build"],
    }
    body = {"nodes": [task(), verify]}
    validate(proposal([task(), verify]))
    validate(
        proposal(
            [{"id": "repeat", "kind": "loop", "body": body, "until": "check", "max_iterations": 2}]
        )
    )
    validate(proposal([{"id": "nested", "kind": "subworkflow", "body": body}]))
    validate(
        proposal(
            [
                task(),
                verify,
                {
                    "id": "branch",
                    "kind": "condition",
                    "source": "check",
                    "depends_on": ["check"],
                    "on_true": {"nodes": [task()]},
                    "on_false": {"nodes": [task()]},
                },
            ]
        )
    )
    for field in ("body", "until", "max_iterations"):
        bad = {"id": "repeat", "kind": "loop", "body": body, "until": "check", "max_iterations": 2}
        bad[field] = None
        with pytest.raises(SchemaError):
            validate(proposal([bad]))
    bad_verify = copy.deepcopy(verify)
    bad_verify["access"] = "write"
    with pytest.raises(SchemaError):
        validate(proposal([task(), bad_verify]))


def test_runtime_rejects_unrequested_model_human_gate(tmp_path):
    manager, request, value, _ = planning_case(tmp_path)
    run = manager.submit_plan(request)
    snapshot = manager.store.planning.snapshot(run["id"])
    value["flow"]["nodes"].extend(
        [
            {
                "id": "review",
                "kind": "verify",
                "depends_on": [value["flow"]["nodes"][0]["id"]],
                "prompt": "Check",
                "required_skills": ["test"],
                "checks": request["acceptance_checks"],
            },
            {
                "id": "unexpected_human",
                "kind": "human",
                "source": "review",
                "depends_on": ["review"],
            },
        ]
    )
    with pytest.raises(ConfigurationError, match="owned by the platform"):
        compile_plan(value, request, snapshot["team_snapshot"], run["id"])
    assert manager.store.planning.view(run["id"])["digest"] is None


def test_planner_receives_narrowed_schema_and_approval_is_still_required(tmp_path):
    manager, request, value, _ = planning_case(tmp_path)
    request.update(max_steps=12, require_human_review=True)
    value["max_steps"] = 12
    run = manager.submit_plan(request)
    manager.store.status(run["id"], "running")
    model = ScriptModel(responses=[AIMessage(content=json.dumps(value))])

    @contextmanager
    def factory():
        yield model

    result = run_planning(manager.store, run["id"], model_factory=factory)
    human = next(message for message in model.seen[0] if message.type == "human")
    given = json.loads(human.content)
    assert given["schema"] == proposal_schema(12, True)
    assert result["executed_run_id"] is None and model.names == []
    assert result["workflow"]["flow"]["nodes"][-1]["id"] == "human_gate"
    assert result["usage"]["model_calls"] == 1
