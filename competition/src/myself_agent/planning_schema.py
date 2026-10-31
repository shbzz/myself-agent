"""Model-facing shape hints; runtime compilation remains the authority boundary."""

from copy import deepcopy

from myself_agent.orchestration_models import WorkflowInput


def proposal_schema(max_steps: int, require_human_review: bool = False) -> dict:
    """Narrow fields by kind, without replacing DAG/team/command/cost validation."""
    if type(max_steps) is not int or not 2 <= max_steps <= 96:
        raise ValueError("Planning step budget must be an integer from 2 to 96")
    if require_human_review and max_steps < 3:
        raise ValueError("Human-reviewed plans need at least three steps")
    schema = deepcopy(WorkflowInput.model_json_schema())
    definitions = schema["$defs"]
    original = definitions.pop("Node")["properties"]
    common = ["id", "kind", "depends_on", "priority"]
    task = ["prompt", "required_skills", "required_tools", "access"]
    kinds = {
        "agent": ("PlannedAgent", task, ["prompt", "required_skills", "access"]),
        "verify": (
            "PlannedVerify",
            task + ["checks", "repair_targets"],
            ["prompt", "required_skills", "checks"],
        ),
        "condition": (
            "PlannedCondition",
            ["source", "on_true", "on_false"],
            ["source", "on_true", "on_false"],
        ),
        "loop": (
            "PlannedLoop",
            ["body", "until", "max_iterations"],
            ["body", "until", "max_iterations"],
        ),
        "subworkflow": ("PlannedSubworkflow", ["body"], ["body"]),
    }
    references, mapping = [], {}
    for kind, (name, fields, required) in kinds.items():
        properties = {key: deepcopy(original[key]) for key in common + fields}
        properties["kind"] = {"type": "string", "const": kind}
        for key in required:
            value = properties[key]
            if "anyOf" in value:
                properties[key] = value = next(
                    option for option in value["anyOf"] if option.get("type") != "null"
                )
            value.pop("default", None)
        if kind in ("agent", "verify"):
            properties["prompt"]["minLength"] = 1
            properties["required_skills"]["minItems"] = 1
        if kind == "verify":
            properties["checks"]["minItems"] = 1
            properties["access"] = {"type": "string", "const": "read", "default": "read"}
        definitions[name] = {
            "type": "object",
            "title": name,
            "additionalProperties": False,
            "properties": properties,
            "required": ["id", "kind", *required],
        }
        reference = "#/$defs/" + name
        references.append({"$ref": reference})
        mapping[kind] = reference
    definitions["Flow"]["properties"]["nodes"]["items"] = {
        "oneOf": references,
        "discriminator": {"propertyName": "kind", "mapping": mapping},
    }
    top = definitions["TopPlanningFlow"] = deepcopy(definitions["Flow"])
    top["title"] = "TopPlanningFlow"
    top["properties"]["nodes"]["maxItems"] = 14 if require_human_review else 15
    schema["properties"].pop("id")
    schema["properties"]["flow"] = {"$ref": "#/$defs/TopPlanningFlow"}
    budget = schema["properties"]["max_steps"]
    budget.pop("default", None)
    budget["maximum"] = max_steps
    budget["description"] = (
        "Required step budget including generated tasks and host acceptance gate"
        + (" plus host human-review gate." if require_human_review else ".")
    )
    schema["required"] = ["name", "flow", "max_steps"]
    return schema
