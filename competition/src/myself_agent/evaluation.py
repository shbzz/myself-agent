"""Private, synthetic comparison primitives. Never exposed as a production API."""

import hashlib
import json
import os
import threading
import time
import uuid
from pathlib import Path

from myself_agent.evaluation_cases import cases
from myself_agent.general_context import CallBudget
from myself_agent.general_settings import ConfigurationError, PolicyInput
from myself_agent.general_store import AgentStore
from myself_agent.orchestration_engine import Orchestrator, TaskRequest
from myself_agent.orchestration_models import AgentInput, Flow, Node, WorkflowInput
from myself_agent.orchestration_runtime import DeepAgentRunner, artifact_manifest

POLICY = {
    "model_profile_id": "deepseek-flash",
    "max_model_calls": 12,
    "timeout_seconds": 90,
    "output_reserve_tokens": 1800,
    "effective_context_tokens": 32768,
    "tool_timeout_seconds": 15,
    "web_enabled": False,
    "browser_enabled": False,
    "mcp_server_ids": [],
}
EFFORT = "off"
TOOLS = ["read_file", "write_file", "edit_file", "execute", "ls", "glob", "grep", "write_todos"]
BUILDER_PROMPT = (
    "先读取分配的文件，再实现完整契约；可自行用execute检查。仅修改分配文件，不委派。交接简短如实。"
)
REVIEW_PROMPT = (
    "独立核对需求、对应文件和交接；最多读取对应文件一次，再输出严格JSON布尔判断。"
    "冻结命令由平台执行。"
)


def write_json(path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def configuration_identity(config):
    return {
        "profile": config["profile"],
        "policy": config["policy"],
        "provider": {
            key: config["provider"][key] for key in ("id", "version", "protocol", "base_url")
        },
    }


def definitions(case, variant, config):
    if variant not in {"single", "fixed_team", "custom"}:
        raise ValueError("Unknown evaluation variant")
    if variant == "single":
        agents = [
            AgentInput(
                id="solver",
                name="Single worker",
                skills=["implement"],
                tools=TOOLS,
                system_prompt=BUILDER_PROMPT,
            ).model_dump()
        ]
        nodes = [
            Node(
                id="solve",
                prompt="实现完整需求的两个模块。最终仅输出JSON："
                '{"passed":true或false,"summary":"有证据的交接"}。',
                required_skills=["implement"],
                required_tools=["read_file", "write_file"],
                access="write",
            )
        ]
    else:
        agents, nodes = [], []
        for module in case["modules"]:
            name = module["id"]
            agents.append(
                AgentInput(
                    id=name + "_worker",
                    name=name,
                    skills=[name],
                    tools=TOOLS,
                    system_prompt=BUILDER_PROMPT,
                ).model_dump()
            )
            nodes.append(
                Node(
                    id="build_" + name,
                    prompt=module["task"],
                    required_skills=[name],
                    required_tools=["read_file", "write_file"],
                    access="write",
                )
            )
            nodes.append(
                Node(
                    id="check_" + name,
                    kind="verify",
                    depends_on=["build_" + name],
                    prompt="独立核对完整需求中" + module["file"] + "的契约，不修改文件。",
                    required_skills=["review"],
                    checks=[module["check"]],
                    repair_targets=["build_" + name],
                )
            )
        agents.extend(
            [
                AgentInput(
                    id="reviewer",
                    name="Module review",
                    role="verifier",
                    skills=["review"],
                    tools=["read_file"],
                    system_prompt=REVIEW_PROMPT,
                    max_parallel=2,
                ).model_dump(),
                AgentInput(
                    id="final_reviewer",
                    name="Final evidence gate",
                    role="verifier",
                    skills=["final"],
                    tools=[],
                    system_prompt="依据独立模块交接作JSON判断；"
                    "平台随后执行全部冻结命令，模型判断不能覆盖命令失败。",
                ).model_dump(),
            ]
        )
        nodes.append(
            Node(
                id="final",
                kind="verify",
                depends_on=["check_" + m["id"] for m in case["modules"]],
                prompt="核对所有模块交接，最终JSON判断；平台会独立检查全部模块及组合行为。",
                required_skills=["final"],
                checks=case["checks"],
                repair_targets=["build_" + m["id"] for m in case["modules"]],
            )
        )
        if variant == "custom":
            nodes = [
                Node(
                    id="repair",
                    kind="loop",
                    body=Flow(nodes=nodes),
                    until="final",
                    max_iterations=2,
                )
            ]
    workflow = WorkflowInput(
        id="benchmark", name="Frozen " + variant, flow=Flow(nodes=nodes), max_steps=36
    )
    return {
        "configuration": config,
        "team": {
            "id": "benchmark",
            "name": variant,
            "agent_ids": [a["id"] for a in agents],
            "max_parallel": 2 if variant == "custom" else 1,
        },
        "agents": [{"definition": agent, "configuration": config} for agent in agents],
        "workflow": workflow.model_dump(),
        "benchmark_only": True,
    }


class EvaluationBudget(CallBudget):
    """Persist per-call accounting including incomplete/failed observations."""

    def __init__(self, store, run_id, limit):
        super().__init__(limit)
        self.store, self.run_id = store, run_id
        self.observed, self.logged_starts, self.observation_lock = {}, set(), threading.Lock()

    def on_chat_model_start(self, serialized, messages, *, run_id, **kwargs):
        super().on_chat_model_start(serialized, messages, run_id=run_id, **kwargs)
        with self.observation_lock:
            if str(run_id) in self.logged_starts:
                return
            self.logged_starts.add(str(run_id))
        self.store.event(self.run_id, "evaluation.model_started", {"call_id": str(run_id)})

    def on_llm_end(self, response, *, run_id, **kwargs):
        generations = response.generations
        message = generations[0][0].message if generations and generations[0] else None
        usage = getattr(message, "usage_metadata", None)
        known = isinstance(usage, dict) and all(
            type(usage.get(key)) is int and usage[key] >= 0
            for key in ("input_tokens", "output_tokens", "total_tokens")
        )
        record = {"call_id": str(run_id), "usage": usage if known else None}
        if message is not None:
            record["model_id"] = message.response_metadata.get("model_name")
        with self.observation_lock:
            if str(run_id) in self.observed:
                if self.observed[str(run_id)] != record:
                    self.observed[str(run_id)]["usage"] = None
                return
            self.observed[str(run_id)] = record
        self.store.event(self.run_id, "evaluation.model_finished", record)

    def totals(self):
        known = [
            record["usage"] for record in self.observed.values() if record["usage"] is not None
        ]
        complete = len(known) == self.calls
        total = sum(value["total_tokens"] for value in known)
        cache = [v.get("input_token_details", {}).get("cache_read") for v in known]
        cache_known = complete and all(type(value) is int and value >= 0 for value in cache)
        return {
            "model_calls": self.calls,
            "usage_complete": complete,
            "total_tokens": total if complete else None,
            "observed_partial_tokens": total,
            "input_tokens": sum(v["input_tokens"] for v in known) if complete else None,
            "output_tokens": sum(v["output_tokens"] for v in known) if complete else None,
            "cached_input_tokens": sum(cache) if cache_known else None,
            "accounted_calls": len(known),
        }


def setup(slot, case, variant, identity):
    data = slot / "data"
    if data.exists():
        raise ConfigurationError("Benchmark slot already contains data; never replay it")
    store = AgentStore(data)
    workspace = store.create_workspace("Synthetic benchmark " + case["id"])
    directory = store.directory(workspace["id"])
    for name, content in case["files"].items():
        (directory / name).write_text(content)
    session = store.create_session(workspace["id"], variant)
    store.config.save_policy("session:" + session["id"], PolicyInput(**POLICY))
    config = store.config.resolve(session_id=session["id"])
    if configuration_identity(config) != identity:
        raise ConfigurationError("Model or policy differs from frozen comparison")
    snapshot = definitions(case, variant, config)
    # Intentional private fixture: single baseline does not satisfy the public team
    # minimum. It uses exactly one bounded worker, not the multi-Agent scheduler/API.
    run, _ = store.submit(
        session["id"],
        uuid.uuid4().hex,
        case["spec"],
        reasoning_effort=EFFORT,
        action="orchestrate",
        orchestration=snapshot,
    )
    store.status(run["id"], "running")
    write_json(slot / "receipt.json", {"run_id": run["id"], "workspace_id": workspace["id"]})
    return store, run, directory, snapshot


def run_worker(root, index, *, model_factory=None):
    protocol = json.loads((root / "protocol.json").read_text())
    item = protocol["order"][index]
    case = next(case for case in cases() if case["id"] == item["case_id"])
    slot = root / "runs" / item["slot"]
    store, run, directory, snapshot = setup(slot, case, item["variant"], protocol["configuration"])
    runner = DeepAgentRunner(
        store,
        run["id"],
        config_path=Path(protocol["config_path"]),
        image=protocol["image"],
        model_factory=model_factory,
    )
    runner.budget = EvaluationBudget(store, run["id"], POLICY["max_model_calls"])
    before, started = artifact_manifest(directory, include_git=True), time.monotonic()
    error_type, claimed, outcome = None, None, None
    try:
        if item["variant"] == "single":

            def check():
                if time.monotonic() - started >= POLICY["timeout_seconds"]:
                    raise ConfigurationError("Benchmark active-time budget exhausted")

            node = Node.model_validate(snapshot["workflow"]["flow"]["nodes"][0])
            request = TaskRequest(
                node,
                "root/solve",
                uuid.uuid4().hex,
                snapshot["agents"][0]["definition"],
                snapshot["configuration"],
                {},
                {},
                check,
            )
            outcome = runner(request)
            try:
                value = json.loads(outcome["text"])
                if (
                    not isinstance(value, dict)
                    or type(value.get("passed")) is not bool
                    or not isinstance(value.get("summary"), str)
                ):
                    raise ValueError
                claimed = value["passed"]
            except (ValueError, AttributeError):
                raise ConfigurationError(
                    "Single baseline must return a strict JSON verdict"
                ) from None
        else:
            outcome = Orchestrator(store, run["id"], runner).run()
            claimed = outcome["passed"]
    except Exception as error:
        error_type = type(error).__name__
    finally:
        store.status(
            run["id"],
            "succeeded" if error_type is None and claimed is True else "failed",
            error_type,
        )
        result = {
            "error_type": error_type,
            "reported_passed": claimed,
            "active_seconds": round(time.monotonic() - started, 6),
            "before": before,
            "outcome": outcome,
            "provider_usage": runner.usage,
            **runner.budget.totals(),
        }
        write_json(slot / "worker-result.json", result)
    return result


def source_hashes():
    app = Path(__file__).resolve().parents[2]
    paths = sorted((app / "src").rglob("*.py")) + [app / "pyproject.toml"]
    return {
        str(path.relative_to(app)): hashlib.sha256(path.read_bytes()).hexdigest() for path in paths
    }
