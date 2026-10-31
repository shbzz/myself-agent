"""Contract tests for knowledge ingestion and Agent memory references."""

import pytest
from pydantic import ValidationError

from myself_agent.knowledge_models import MemoryDecision, SourceInput
from myself_agent.orchestration_models import AgentInput

WORKSPACE_ID = "a" * 32


def source_data(**changes):
    data = {
        "workspace_id": WORKSPACE_ID,
        "id": "manual",
        "expected_version": 0,
        "title": "规范",
        "text": "边界检查",
    }
    data.update(changes)
    return data


def decision_data(**changes):
    data = {
        "workspace_id": WORKSPACE_ID,
        "expected_version": 1,
        "action": "approve",
        "reason": "确认",
    }
    data.update(changes)
    return data


def agent_data(**changes):
    data = {"name": "worker", "skills": ["build"]}
    data.update(changes)
    return data


def test_source_input_accepts_valid_boundary_values():
    source = SourceInput(**source_data())

    assert source.workspace_id == WORKSPACE_ID
    assert source.id == "manual"
    assert source.expected_version == 0
    assert source.title == "规范"
    assert source.text == "边界检查"


@pytest.mark.parametrize(
    "text",
    [" \t\r\n\u3000", "正常\x00文本", "x" * 24001],
)
def test_source_input_rejects_invalid_text(text):
    with pytest.raises(ValidationError):
        SourceInput(**source_data(text=text))


def test_source_input_rejects_negative_expected_version():
    with pytest.raises(ValidationError):
        SourceInput(**source_data(expected_version=-1))


@pytest.mark.parametrize("statement", [None, " \t\n\u3000"])
def test_correction_requires_a_nonblank_statement(statement):
    with pytest.raises(ValidationError):
        MemoryDecision(**decision_data(action="correct", statement=statement))


@pytest.mark.parametrize("action", ["approve", "withdraw"])
def test_non_correction_rejects_statement(action):
    with pytest.raises(ValidationError):
        MemoryDecision(**decision_data(action=action, statement="不应携带"))


def test_memory_decision_rejects_blank_reason():
    with pytest.raises(ValidationError):
        MemoryDecision(**decision_data(reason=" \t\n\u3000"))


@pytest.mark.parametrize("field", ["knowledge_ids", "memory_ids"])
def test_agent_input_rejects_duplicate_references(field):
    with pytest.raises(ValidationError):
        AgentInput(**agent_data(**{field: ["reference", "reference"]}))


def test_agent_input_rejects_more_than_eight_knowledge_ids():
    with pytest.raises(ValidationError):
        AgentInput(**agent_data(knowledge_ids=[f"k{i}" for i in range(1, 10)]))
