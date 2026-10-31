"""Versioned, declarative multi-Agent contracts; no executable condition expressions."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic_core import PydanticCustomError

from myself_agent.general_settings import ID, ConfigurationError

Identifier = Annotated[str, Field(pattern=ID)]
READ_TOOLS = {
    "ls",
    "read_file",
    "glob",
    "grep",
    "execute",
    "write_todos",
    "web_read",
    "browser_read",
    "mcp_list_tools",
}

TOOL_NAMES = (
    "ls",
    "read_file",
    "write_file",
    "edit_file",
    "glob",
    "grep",
    "execute",
    "write_todos",
    "web_read",
    "browser_read",
    "mcp_list_tools",
    "mcp_call",
)


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AgentInput(Contract):
    id: str | None = Field(default=None, pattern=ID)
    name: str = Field(min_length=1, max_length=80)
    role: Literal["planner", "worker", "verifier"] = "worker"
    skills: list[str] = Field(min_length=1, max_length=24)
    system_prompt: str = Field(default="", max_length=8000)
    model_profile_id: str | None = Field(default=None, pattern=ID)
    tools: list[str] = Field(default_factory=list, max_length=12)
    knowledge_ids: list[Identifier] = Field(default_factory=list, max_length=8)
    memory_ids: list[Identifier] = Field(default_factory=list, max_length=8)
    max_parallel: int = Field(default=1, ge=1, le=2)
    enabled: bool = True

    @model_validator(mode="after")
    def valid(self):
        for identifiers in (self.knowledge_ids, self.memory_ids):
            if len(identifiers) != len(set(identifiers)):
                raise PydanticCustomError(
                    "contract_duplicate_knowledge_or_memory_references",
                    "Duplicate knowledge or memory references",
                )
        if any(tool not in TOOL_NAMES for tool in self.tools):
            raise PydanticCustomError("contract_unknown_agent_tool", "Unknown Agent tool")
        if any(not skill.strip() or len(skill) > 80 for skill in self.skills):
            raise PydanticCustomError(
                "contract_skills_must_be_nonempty_and_at_most_80_characters",
                "Skills must be nonempty and at most 80 characters",
            )
        self.skills = list(dict.fromkeys(skill.strip() for skill in self.skills))
        self.tools = list(dict.fromkeys(self.tools))
        if self.role == "verifier" and not set(self.tools) <= READ_TOOLS:
            raise PydanticCustomError(
                "contract_verifier_file_tools_must_be_read_only",
                "Verifier file tools must be read-only",
            )
        return self


class TeamInput(Contract):
    id: str | None = Field(default=None, pattern=ID)
    name: str = Field(min_length=1, max_length=80)
    agent_ids: list[Identifier] = Field(min_length=2, max_length=8)
    max_parallel: int = Field(default=2, ge=1, le=4)

    @model_validator(mode="after")
    def distinct(self):
        if len(set(self.agent_ids)) != len(self.agent_ids):
            raise PydanticCustomError("contract_duplicate_team_members", "Duplicate team members")
        return self


class Node(Contract):
    id: str = Field(pattern=ID)
    kind: Literal["agent", "verify", "condition", "loop", "subworkflow", "human"] = "agent"
    depends_on: list[Identifier] = Field(default_factory=list, max_length=16)
    priority: int = Field(default=0, ge=-20, le=20)
    prompt: str = Field(default="", max_length=8000)
    required_skills: list[str] = Field(default_factory=list, max_length=24)
    required_tools: list[str] = Field(default_factory=list, max_length=12)
    access: Literal["read", "write"] = "read"
    checks: list[str] = Field(default_factory=list, max_length=8)
    repair_targets: list[Identifier] = Field(default_factory=list, max_length=16)
    source: str | None = Field(default=None, pattern=ID)
    body: "Flow | None" = None
    on_true: "Flow | None" = None
    on_false: "Flow | None" = None
    until: str | None = Field(default=None, pattern=ID)
    max_iterations: int = Field(
        default=3,
        ge=1,
        le=4,
        description="Only loop nodes may customize this; omit for other kinds.",
    )

    @field_validator("required_skills")
    @classmethod
    def skills(cls, values):
        if any(not value.strip() or len(value) > 80 for value in values):
            raise PydanticCustomError(
                "contract_skills_must_be_nonempty_and_bounded",
                "Skills must be nonempty and bounded",
            )
        return list(dict.fromkeys(value.strip() for value in values))

    @model_validator(mode="after")
    def shape(self):
        if len(self.depends_on) != len(set(self.depends_on)):
            raise PydanticCustomError("contract_duplicate_dependencies", "Duplicate dependencies")
        if any(tool not in TOOL_NAMES for tool in self.required_tools):
            raise PydanticCustomError("contract_unknown_required_tool", "Unknown required tool")
        if any(not command.strip() or len(command) > 8000 for command in self.checks):
            raise PydanticCustomError(
                "contract_verification_command_must_be_nonempty_and_bounded",
                "Verification command must be nonempty and bounded",
            )
        if self.kind in ("agent", "verify"):
            if not self.prompt.strip() or not self.required_skills:
                raise PydanticCustomError(
                    "node_prompt_skills_required", "Agent tasks require a prompt and skills"
                )
            if any((self.body, self.on_true, self.on_false, self.source, self.until)):
                raise PydanticCustomError(
                    "node_task_control_fields", "Agent task cannot contain control fields"
                )
        elif self.kind == "human":
            if not self.source or self.source not in self.depends_on:
                raise PydanticCustomError(
                    "contract_human_gate_requires_an_explicit_verification_dependency",
                    "Human gate requires an explicit verification dependency",
                )
            if any((self.body, self.on_true, self.on_false, self.until)):
                raise PydanticCustomError(
                    "contract_human_gate_cannot_contain_control_bodies",
                    "Human gate cannot contain control bodies",
                )
        elif self.kind == "condition":
            if not self.source or not self.on_true or not self.on_false or self.body or self.until:
                raise PydanticCustomError(
                    "contract_condition_requires_a_source_and_both_branches",
                    "Condition requires a source and both branches",
                )
            if self.source not in self.depends_on:
                raise PydanticCustomError(
                    "contract_condition_source_must_be_an_explicit_dependency",
                    "Condition source must be an explicit dependency",
                )
        elif self.kind == "loop":
            if not self.body or not self.until or self.on_true or self.on_false or self.source:
                raise PydanticCustomError(
                    "contract_loop_requires_a_body_and_verifier_stop_condition",
                    "Loop requires a body and verifier stop condition",
                )
            target = next((n for n in self.body.nodes if n.id == self.until), None)
            if target is None or target.kind != "verify":
                raise PydanticCustomError(
                    "contract_loop_stop_condition_must_name_a_verifier_in_its_body",
                    "Loop stop condition must name a verifier in its body",
                )
        elif not self.body or any((self.on_true, self.on_false, self.source, self.until)):
            raise PydanticCustomError(
                "contract_subworkflow_requires_only_a_body", "Subworkflow requires only a body"
            )
        if self.kind not in ("agent", "verify") and (
            self.prompt or self.required_skills or self.required_tools or self.access != "read"
        ):
            raise PydanticCustomError(
                "node_control_task_fields", "Control nodes cannot silently ignore Agent task fields"
            )
        if self.kind != "loop" and self.max_iterations != 3:
            raise PydanticCustomError(
                "node_iteration_scope", "Iteration limits belong to loop nodes"
            )
        if self.access == "read" and not set(self.required_tools) <= READ_TOOLS:
            raise PydanticCustomError(
                "node_readonly_tools", "Read tasks cannot require mutating tools"
            )
        if self.kind == "verify":
            if self.access != "read" or not self.checks:
                raise PydanticCustomError(
                    "node_verification_requirements",
                    "Verification requires read-only access and frozen commands",
                )
        elif self.checks or self.repair_targets:
            raise PydanticCustomError(
                "node_verifier_only_fields",
                "Checks and repair targets belong to verification nodes",
            )
        return self


class Flow(Contract):
    nodes: list[Node] = Field(min_length=1, max_length=16)

    @model_validator(mode="after")
    def graph(self):
        nodes = {node.id: node for node in self.nodes}
        if len(nodes) != len(self.nodes):
            raise PydanticCustomError(
                "contract_node_ids_must_be_unique_within_a_flow",
                "Node IDs must be unique within a flow",
            )
        pending, visited = set(nodes), set()
        while pending:
            ready = [name for name in pending if set(nodes[name].depends_on) <= visited]
            if not ready:
                raise PydanticCustomError(
                    "contract_flow_has_a_cycle_or_an_unknown_dependency",
                    "Flow has a cycle or an unknown dependency",
                )
            visited.update(ready)
            pending.difference_update(ready)
        for node in self.nodes:
            ancestors, stack = set(), list(node.depends_on)
            while stack:
                name = stack.pop()
                if name not in ancestors:
                    ancestors.add(name)
                    stack.extend(nodes[name].depends_on)
            if not set(node.repair_targets) <= ancestors:
                raise PydanticCustomError(
                    "contract_repair_targets_must_be_verification_ancestors",
                    "Repair targets must be verification ancestors",
                )
            if node.kind == "human" and nodes[node.source].kind != "verify":
                raise PydanticCustomError(
                    "contract_human_source_must_be_an_independent_verification_node",
                    "Human source must be an independent verification node",
                )
            if node.kind == "condition" and nodes[node.source].kind not in (
                "verify",
                "loop",
                "condition",
                "subworkflow",
            ):
                raise PydanticCustomError(
                    "contract_conditions_inspect_verification_outcomes_not_arbitrary_code",
                    "Conditions inspect verification outcomes, not arbitrary code",
                )
        return self


class WorkflowInput(Contract):
    id: str | None = Field(default=None, pattern=ID)
    name: str = Field(min_length=1, max_length=80)
    flow: Flow
    max_steps: int = Field(default=48, ge=1, le=96)

    @model_validator(mode="after")
    def bounds(self):
        count = 0

        def visit(flow, depth):
            nonlocal count
            if depth > 3:
                raise PydanticCustomError(
                    "contract_workflow_nesting_exceeds_three_levels",
                    "Workflow nesting exceeds three levels",
                )
            for node in flow.nodes:
                if node.kind == "human" and depth != 0:
                    raise PydanticCustomError(
                        "contract_human_gates_are_supported_only_at_top_level_quiescent_boundaries",
                        "Human gates are supported only at top-level quiescent boundaries",
                    )
                count += 1
                for child in (node.body, node.on_true, node.on_false):
                    if child:
                        visit(child, depth + 1)

        visit(self.flow, 0)
        if count > 48:
            raise PydanticCustomError(
                "contract_workflow_definition_exceeds_48_nodes",
                "Workflow definition exceeds 48 nodes",
            )
        return self


def task_nodes(flow):
    for node in flow.nodes:
        if node.kind in ("agent", "verify"):
            yield node
        for child in (node.body, node.on_true, node.on_false):
            if child:
                yield from task_nodes(child)


def validate_team(flow, agents, configuration):
    policy = configuration["policy"]
    unavailable = set()
    if not policy["web_enabled"]:
        unavailable.update(("web_read", "browser_read"))
    elif not policy["browser_enabled"]:
        unavailable.add("browser_read")
    if not configuration.get("mcp_servers"):
        unavailable.update(("mcp_call", "mcp_list_tools"))
    for node in task_nodes(flow):
        if set(node.required_tools) & unavailable:
            raise ConfigurationError("A required external tool is disabled by session policy")
        candidates = [
            agent
            for agent in agents
            if agent["enabled"]
            and (agent["role"] == "verifier") == (node.kind == "verify")
            and set(node.required_skills) <= set(agent["skills"])
            and set(node.required_tools) <= set(agent["tools"])
        ]
        if not candidates:
            raise ConfigurationError(f"No eligible Agent for task {node.id}")
