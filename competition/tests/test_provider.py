from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from pydantic import Field
from typing_extensions import TypedDict

from myself_agent.provider import invoke_role
from myself_agent.schema import Plan


class FixedPlanModel(BaseChatModel):
    bound_tool_names: list[str] = Field(default_factory=list)

    @property
    def _llm_type(self):
        return "offline-fixed-plan"

    def bind_tools(self, tools, **kwargs):
        for tool in tools:
            if isinstance(tool, dict):
                name = tool.get("name") or tool.get("function", {}).get("name")
            else:
                name = tool.name
            self.bound_tool_names.append(name)
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        plan = {
            "tasks": [
                {
                    "filename": "parser.py",
                    "instruction": "Implement the CSV parser contract",
                    "depends_on": [],
                },
                {
                    "filename": "stats.py",
                    "instruction": "Implement order summary statistics",
                    "depends_on": [],
                },
            ]
        }
        message = AIMessage(
            content="",
            tool_calls=[
                {"name": "Plan", "args": plan, "id": "offline-plan", "type": "tool_call"},
            ],
        )
        return ChatResult(generations=[ChatGeneration(message=message)])


def test_stateless_harness_inside_synchronously_checkpointed_parent(tmp_path):
    class State(TypedDict):
        plan: dict

    model = FixedPlanModel()

    def plan_node(state):
        plan, _ = invoke_role(model, "planner", Plan, "Return the two-module plan")
        return {"plan": plan.model_dump()}

    builder = StateGraph(State)
    builder.add_node("plan", plan_node)
    builder.add_edge(START, "plan")
    builder.add_edge("plan", END)
    with SqliteSaver.from_conn_string(str(tmp_path / "parent.sqlite")) as saver:
        graph = builder.compile(checkpointer=saver)
        result = graph.invoke({}, {"configurable": {"thread_id": "regression"}}, durability="sync")
        assert len(result["plan"]["tasks"]) == 2
        assert graph.get_state({"configurable": {"thread_id": "regression"}}).next == ()
    assert model.bound_tool_names
    assert "task" not in model.bound_tool_names
