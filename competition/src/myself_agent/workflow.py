import hashlib
import json
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

from myself_agent.config import Settings
from myself_agent.provider import invoke_role, model_session
from myself_agent.sandbox import run_tests
from myself_agent.scenarios import get_scenario
from myself_agent.scheduler import affected_tasks, ready_batch
from myself_agent.schema import Plan, RunState, SourceFile


def write_json(path: Path, data):
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


class Workflow:
    def __init__(
        self,
        settings: Settings,
        run_dir: Path,
        run_id: str,
        inject_failure=False,
        scenario_id="csv-orders",
        brief="",
        concurrency=2,
        base_artifacts=None,
    ):
        self.settings = settings
        self.run_dir = run_dir
        self.run_id = run_id
        self.inject_failure = inject_failure
        self.scenario = get_scenario(scenario_id)
        self.brief = brief
        self.base_artifacts = dict(base_artifacts or {})
        for name, content in self.base_artifacts.items():
            if name not in self.scenario.dependencies:
                raise ValueError("Baseline must contain only this scenario's approved modules")
            SourceFile(filename=name, content=content)
        if concurrency not in (1, 2):
            raise ValueError("Concurrency must be one or two")
        self.concurrency = concurrency
        self.lock = threading.Lock()
        self.events = []

    def event(self, kind, **data):
        with self.lock:
            row = {
                "time": datetime.now(UTC).isoformat(),
                "kind": kind,
                "sequence": len(self.events),
                **data,
            }
            self.events.append(row)
            with (self.run_dir / "events.jsonl").open("a", encoding="utf-8") as output:
                output.write(json.dumps(row, ensure_ascii=False) + "\n")

    def role(self, model, name, schema, prompt, repair_round):
        started = time.monotonic()
        self.event("agent_started", role=name, repair_round=repair_round)
        try:
            result, usage = invoke_role(model, name, schema, prompt)
        except Exception as error:
            self.event(
                "agent_failed",
                role=name,
                repair_round=repair_round,
                error_type=type(error).__name__,
            )
            raise
        write_json(self.run_dir / f"{name}-round-{repair_round}.json", result.model_dump())
        self.event(
            "agent_completed",
            role=name,
            repair_round=repair_round,
            usage=usage,
            elapsed_seconds=round(time.monotonic() - started, 3),
        )
        return result

    def build(self, model, saver):
        requirement = self.scenario.requirement
        if self.brief:
            requirement += (
                "\nUser implementation preferences (must preserve every contract, "
                "filename and acceptance rule above):\n" + self.brief
            )

        def plan(state):
            result = self.role(
                model,
                "planner",
                Plan,
                requirement + "\nReturn a Plan structured response describing each module task. "
                "Use exactly this module dependency map: "
                + json.dumps(self.scenario.dependencies)
                + ". Do not implement code yet.",
                0,
            )
            self.scenario.validate_plan(result)
            write_json(self.run_dir / "plan.json", result.model_dump())
            self.event("plan_created", tasks=result.model_dump()["tasks"])
            return {
                "plan": result.model_dump(),
                "repair_round": 0,
                "targets": [task.filename for task in result.tasks],
                "feedback": "",
                "completed": [],
            }

        def schedule(state):
            plan = Plan.model_validate(state["plan"])
            batch = ready_batch(plan, state["targets"], state["completed"], self.concurrency)
            self.event(
                "batch_scheduled",
                repair_round=state["repair_round"],
                tasks=batch,
                completed=state["completed"],
                concurrency=self.concurrency,
                reason="依赖已完成；按剩余依赖路径长度优先，在并发额度内派发",
            )
            return {"batch": batch}

        def dispatch(state):
            tasks = {task["filename"]: task for task in state["plan"]["tasks"]}
            return [
                Send(
                    "worker",
                    {
                        "task": tasks[filename],
                        "feedback": state["feedback"],
                        "previous": state.get("artifacts", {}).get(filename, ""),
                        "dependencies": {
                            name: state["artifacts"][name] for name in tasks[filename]["depends_on"]
                        },
                        "repair_round": state["repair_round"],
                    },
                )
                for filename in state["batch"]
            ]

        def worker(state):
            filename = state["task"]["filename"]
            prompt = (
                requirement + f"\nYour assigned file is ONLY {filename}. "
                f"Task: {state['task']['instruction']}\n"
                "Return a SourceFile structured response with filename and Python source. "
                "Do not modify tests, create other files, or execute code yourself."
            )
            if state["dependencies"]:
                prompt += "\nCompleted dependency source:\n" + json.dumps(state["dependencies"])
            if state["previous"]:
                prompt += (
                    f"\nPrevious source for this module:\n{state['previous']}\n"
                    "Continue from this source, apply the user's current request, and "
                    "preserve existing working behavior and all public contracts."
                )
            if state["feedback"]:
                prompt += (
                    f"Independent acceptance feedback:\n{state['feedback']}\n"
                    "Fix this module while preserving its required public contract."
                )
            result = self.role(
                model,
                filename.removesuffix(".py") + "_developer",
                SourceFile,
                prompt,
                state["repair_round"],
            )
            if result.filename != filename:
                raise ValueError("Worker attempted to return an unassigned file")
            return {"artifacts": {filename: result.content}}

        def collect(state):
            return {"completed": sorted(set(state["completed"]) | set(state["batch"]))}

        def after_batch(state):
            return "verify" if set(state["targets"]) <= set(state["completed"]) else "schedule"

        def verify(state):
            repair_round = state["repair_round"]
            directory = self.run_dir / f"round-{repair_round}"
            directory.mkdir(exist_ok=True)
            workspace = directory / "workspace"
            workspace.mkdir(mode=0o755, exist_ok=True)
            # systemd's UMask=0027 must not block the container's unprivileged UID.
            workspace.chmod(0o755)
            if set(state["artifacts"]) != set(self.scenario.dependencies):
                raise ValueError("The full approved artifact set is required")
            artifacts = dict(state["artifacts"])
            if self.inject_failure and repair_round == 0:
                write_json(directory / "before-injection.json", artifacts)
                faulty = "stats.py" if "stats.py" in artifacts else "tokenize_text.py"
                artifacts[faulty] = (
                    "def summarize(orders):\n    return {}\n"
                    if faulty == "stats.py"
                    else "def tokenize(text):\n    return []\n"
                )
                self.event("controlled_fault_injected", module=faulty, repair_round=0)
            for filename, content in artifacts.items():
                destination = workspace / filename
                if destination.is_symlink():
                    raise ValueError("Artifact destination must not be a symlink")
                destination.write_text(content, encoding="utf-8")
                destination.chmod(0o644)
            write_json(
                directory / "artifact-hashes.json",
                {
                    name: hashlib.sha256(content.encode()).hexdigest()
                    for name, content in artifacts.items()
                },
            )
            self.event("verification_started", repair_round=repair_round)
            result = run_tests(
                self.settings,
                workspace,
                f"myself-agent-{self.run_id}-{repair_round}",
                self.scenario.id,
            )
            write_json(directory / "verification.json", result)
            self.event(
                "verification_completed",
                repair_round=repair_round,
                passed=result["passed"],
                failure_kind=result["failure_kind"],
                report=result["report"],
                elapsed_seconds=result["elapsed_seconds"],
            )
            return {"verification": result, "artifacts": artifacts}

        def route(state):
            result = state["verification"]
            if (
                result["passed"]
                or result["failure_kind"] == "infrastructure"
                or state["repair_round"] >= self.settings.max_repairs
            ):
                return END
            return "repair"

        def repair(state):
            result = state["verification"]
            report = result.get("report") or {}
            cases = report.get("failed_cases", [])
            roots = set()
            for case in cases:
                for prefix, name in self.scenario.test_modules.items():
                    if prefix in case["test"]:
                        roots.add(name)
            plan = Plan.model_validate(state["plan"])
            if not roots:
                roots = set(self.scenario.dependencies)
            targets = affected_tasks(plan, roots)
            feedback = json.dumps(
                {
                    "failure_kind": result["failure_kind"],
                    "report": report,
                    "output": result["output"][-12_000:] if not report else "",
                }
            )
            next_round = state["repair_round"] + 1
            self.event(
                "repair_scheduled",
                repair_round=next_round,
                roots=sorted(roots),
                targets=targets,
                reason="失败用例定位模块，并沿依赖图更新受影响的下游模块；随后全量验收",
            )
            return {
                "repair_round": next_round,
                "targets": targets,
                "feedback": feedback,
                "completed": [],
            }

        builder = StateGraph(RunState)
        for name, function in [
            ("plan", plan),
            ("schedule", schedule),
            ("worker", worker),
            ("collect", collect),
            ("verify", verify),
            ("repair", repair),
        ]:
            builder.add_node(name, function)
        builder.add_edge(START, "plan")
        builder.add_edge("plan", "schedule")
        builder.add_conditional_edges("schedule", dispatch, ["worker"])
        builder.add_edge("worker", "collect")
        builder.add_conditional_edges(
            "collect", after_batch, {"schedule": "schedule", "verify": "verify"}
        )
        builder.add_conditional_edges("verify", route, {END: END, "repair": "repair"})
        builder.add_edge("repair", "schedule")
        return builder.compile(checkpointer=saver)

    def run(self):
        started = time.monotonic()
        self.event("run_started", scenario=self.scenario.id, concurrency=self.concurrency)
        with (
            model_session(self.settings) as model,
            SqliteSaver.from_conn_string(str(self.run_dir / "checkpoints.sqlite")) as saver,
        ):
            graph = self.build(model, saver)
            (self.run_dir / "workflow.mmd").write_text(graph.get_graph().draw_mermaid())
            state = graph.invoke(
                {"requirement": self.scenario.requirement, "artifacts": self.base_artifacts},
                {
                    "configurable": {"thread_id": self.run_id},
                    "max_concurrency": self.concurrency,
                    "recursion_limit": 100,
                },
                durability="sync",
            )
        totals = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
        for event in self.events:
            for usage in event.get("usage", {}).values():
                for key in totals:
                    totals[key] += usage.get(key, 0)
        summary = {
            "outcome": "passed" if state["verification"]["passed"] else "failed",
            "run_id": self.run_id,
            "run_directory": str(self.run_dir),
            "scenario": self.scenario.id,
            "provider": "DeepSeek official",
            "model": self.settings.model,
            "image": self.settings.image,
            "thinking": "disabled",
            "controlled_fault_injection": self.inject_failure,
            "repair_rounds": state["repair_round"],
            "based_on_previous_task": bool(self.base_artifacts),
            "verification": state["verification"],
            "usage": totals,
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "concurrency": self.concurrency,
            "scheduling_policy": "dependency-ready, critical-path-first, bounded batches",
            "scope": "three trusted software scenarios; template-constrained task DAG",
        }
        write_json(self.run_dir / "summary.json", summary)
        self.event("run_completed", outcome=summary["outcome"], usage=totals)
        return summary
