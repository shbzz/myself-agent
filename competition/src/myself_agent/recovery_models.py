"""Explicit, idempotent human decisions; no model-supplied reviewer identity."""

from typing import Literal

from pydantic import Field

from myself_agent.orchestration_models import Contract


class HumanDecision(Contract):
    request_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    checkpoint_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    decision: Literal["approve", "reject"]
    note: str = Field(min_length=1, max_length=2000)


class ArtifactSnapshotInput(Contract):
    request_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    note: str = Field(min_length=1, max_length=2000)


class ArtifactRollbackInput(ArtifactSnapshotInput):
    snapshot_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    expected_current_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    confirm: Literal["restore"]
