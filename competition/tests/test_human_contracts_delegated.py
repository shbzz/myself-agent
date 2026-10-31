import pytest
from pydantic import ValidationError

from myself_agent.recovery_models import HumanDecision

BASE = {
    "request_id": "a" * 32,
    "checkpoint_id": "b" * 32,
    "digest": "c" * 64,
    "decision": "approve",
    "note": "reviewed",
}


@pytest.mark.parametrize("decision", ["approve", "reject"])
def test_valid_human_decision(decision):
    decision_obj = HumanDecision(**{**BASE, "decision": decision})
    assert decision_obj.decision == decision
    assert decision_obj.note == "reviewed"


@pytest.mark.parametrize(
    "field,value",
    [
        ("request_id", "a" * 31),
        ("digest", "c" * 63),
        ("decision", "maybe"),
        ("note", ""),
    ],
)
def test_invalid_human_decision_fields(field, value):
    with pytest.raises(ValidationError):
        HumanDecision(**{**BASE, field: value})


def test_extra_actor_field_rejected():
    with pytest.raises(ValidationError):
        HumanDecision(**BASE, actor="someone")
