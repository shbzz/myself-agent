"""Explicit, bounded knowledge ingestion and human-reviewed experience contracts."""

from typing import Annotated, Literal

from pydantic import Field, field_validator, model_validator

from myself_agent.orchestration_models import Contract, Identifier

WorkspaceID = Annotated[str, Field(pattern=r"^[a-f0-9]{32}$")]


class SourceInput(Contract):
    workspace_id: WorkspaceID
    id: Identifier
    expected_version: int = Field(ge=0)
    title: str = Field(min_length=1, max_length=120)
    text: str = Field(min_length=1, max_length=24000)
    reference: str = Field(default="", max_length=300)
    active: bool = True

    @field_validator("title", "text", "reference")
    @classmethod
    def nonblank(cls, value, info):
        value.encode("utf-8")
        if (info.field_name != "reference" and not value.strip()) or "\x00" in value:
            raise ValueError("Knowledge text must be nonblank and contain no NUL")
        return value


class MemoryProposal(Contract):
    workspace_id: WorkspaceID
    run_id: WorkspaceID
    verification_path: str = Field(min_length=1, max_length=600)
    statement: str = Field(min_length=1, max_length=1800)

    @field_validator("statement", "verification_path")
    @classmethod
    def nonblank(cls, value):
        value.encode("utf-8")
        if not value.strip() or "\x00" in value:
            raise ValueError("Experience must be nonblank and contain no NUL")
        return value


class MemoryDecision(Contract):
    workspace_id: WorkspaceID
    expected_version: int = Field(ge=1)
    action: Literal["approve", "correct", "withdraw"]
    reason: str = Field(min_length=1, max_length=600)
    statement: str | None = Field(default=None, min_length=1, max_length=1800)

    @model_validator(mode="after")
    def valid(self):
        self.reason.encode("utf-8")
        if self.statement is not None:
            self.statement.encode("utf-8")
        if not self.reason.strip() or "\x00" in self.reason:
            raise ValueError("A review reason is required")
        if self.action == "correct":
            if not self.statement or not self.statement.strip() or "\x00" in self.statement:
                raise ValueError("A correction requires a nonblank replacement")
        elif self.statement is not None:
            raise ValueError("Only corrections may replace an experience")
        return self


class RetrievalInput(Contract):
    session_id: WorkspaceID
    agent_id: Identifier
    query: str = Field(min_length=1, max_length=2000)

    @field_validator("query")
    @classmethod
    def query_text(cls, value):
        value.encode("utf-8")
        if not value.strip() or "\x00" in value:
            raise ValueError("Retrieval query must be nonblank and contain no NUL")
        return value
