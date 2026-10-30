"""Versioned, declarative multi-Agent contracts; no executable condition expressions."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

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
    max_parallel: int = Field(default=1, ge=1, le=2)
    enabled: bool = True

    @model_validator(mode="after")
    def valid(self):
        if any(tool not in TOOL_NAMES for tool in self.tools):
            raise ValueError("Unknown Agent tool")
        if any(not skill.strip() or len(skill) > 80 for skill in self.skills):
            raise ValueError("Skills must be nonempty and at most 80 characters")
        self.skills = list(dict.fromkeys(skill.strip() for skill in self.skills))
        self.tools = list(dict.fromkeys(self.tools))
        if self.role == "verifier" and not set(self.tools) <= READ_TOOLS:
            raise ValueError("Verifier file tools must be read-only")
        return self


class TeamInput(Contract):
    id: str | None = Field(default=None, pattern=ID)
    name: str = Field(min_length=1, max_length=80)
    agent_ids: list[Identifier] = Field(min_length=2, max_length=8)
    max_parallel: int = Field(default=2, ge=1, le=4)

    @model_validator(mode="after")
    def distinct(self):
        if len(set(self.agent_ids)) != len(self.agent_ids):
            raise ValueError("Duplicate team members")
        return self


class Node(Contract):
    id: str = Field(pattern=ID)
    kind: Literal["agent", "verify", "condition", "loop", "subworkflow"] = "agent"
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
    max_iterations: int = Field(default=3, ge=1, le=4)

    @field_validator("required_skills")
    @classmethod
    def skills(cls, values):
        if any(not value.strip() or len(value) > 80 for value in values):
            raise ValueError("Skills must be nonempty and bounded")
        return list(dict.fromkeys(value.strip() for value in values))

    @model_validator(mode="after")
    def shape(self):
        if len(self.depends_on) != len(set(self.depends_on)):
            raise ValueError("Duplicate dependencies")
        if any(tool not in TOOL_NAMES for tool in self.required_tools):
            raise ValueError("Unknown required tool")
        if any(not command.strip() or len(command) > 8000 for command in self.checks):
            raise ValueError("Verification command must be nonempty and bounded")
        if self.kind in ("agent", "verify"):
            if not self.prompt.strip() or not self.required_skills:
                raise ValueError("Agent tasks require a prompt and skills")
            if any((self.body, self.on_true, self.on_false, self.source, self.until)):
                raise ValueError("Agent task cannot contain control fields")
        elif self.kind == "condition":
            if not self.source or not self.on_true or not self.on_false or self.body or self.until:
                raise ValueError("Condition requires a source and both branches")
            if self.source not in self.depends_on:
                raise ValueError("Condition source must be an explicit dependency")
        elif self.kind == "loop":
            if not self.body or not self.until or self.on_true or self.on_false or self.source:
                raise ValueError("Loop requires a body and verifier stop condition")
            target = next((n for n in self.body.nodes if n.id == self.until), None)
            if target is None or target.kind != "verify":
                raise ValueError("Loop stop condition must name a verifier in its body")
        elif not self.body or any((self.on_true, self.on_false, self.source, self.until)):
            raise ValueError("Subworkflow requires only a body")
        if self.kind not in ("agent", "verify") and (
            self.prompt or self.required_skills or self.required_tools or self.access != "read"
        ):
            raise ValueError("Control nodes cannot silently ignore Agent task fields")
        if self.kind != "loop" and self.max_iterations != 3:
            raise ValueError("Iteration limits belong to loop nodes")
        if self.access == "read" and not set(self.required_tools) <= READ_TOOLS:
            raise ValueError("Read tasks cannot require mutating tools")
        if self.kind == "verify":
            if self.access != "read" or not self.checks:
                raise ValueError("Verification requires read-only access and frozen commands")
        elif self.checks or self.repair_targets:
            raise ValueError("Checks and repair targets belong to verification nodes")
        return self


class Flow(Contract):
    nodes: list[Node] = Field(min_length=1, max_length=16)

    @model_validator(mode="after")
    def graph(self):
        nodes = {node.id: node for node in self.nodes}
        if len(nodes) != len(self.nodes):
            raise ValueError("Node IDs must be unique within a flow")
        pending, visited = set(nodes), set()
        while pending:
            ready = [name for name in pending if set(nodes[name].depends_on) <= visited]
            if not ready:
                raise ValueError("Flow has a cycle or an unknown dependency")
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
                raise ValueError("Repair targets must be verification ancestors")
            if node.kind == "condition" and nodes[node.source].kind not in (
                "verify",
                "loop",
                "condition",
                "subworkflow",
            ):
                raise ValueError("Conditions inspect verification outcomes, not arbitrary code")
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
                raise ValueError("Workflow nesting exceeds three levels")
            for node in flow.nodes:
                count += 1
                for child in (node.body, node.on_true, node.on_false):
                    if child:
                        visit(child, depth + 1)

        visit(self.flow, 0)
        if count > 48:
            raise ValueError("Workflow definition exceeds 48 nodes")
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
