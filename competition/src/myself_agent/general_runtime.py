"""Real Deep Agents tool loop with persistent LangGraph conversation checkpoints."""

import argparse
import difflib
import re
import threading
import time
from pathlib import Path

from deepagents import create_deep_agent
from langchain.agents.middleware import ModelCallLimitMiddleware
from langchain_core.callbacks import BaseCallbackHandler, UsageMetadataCallbackHandler
from langchain_core.messages import ToolMessage, message_to_dict
from langgraph.checkpoint.sqlite import SqliteSaver

from myself_agent.config import DEFAULT_CONFIG
from myself_agent.general_context import CallBudget, ContextMiddleware
from myself_agent.general_model import (
    general_model_session,
    model_base_settings,
    provider_credential,
)
from myself_agent.general_sandbox import WorkspaceSandbox
from myself_agent.general_settings import ConfigurationError
from myself_agent.general_store import AgentStore
from myself_agent.provider import platform_owned_delegation


def snapshot(directory):
    result = {}
    for path in directory.rglob("*"):
        if len(result) >= 1000:
            break
        if path.is_symlink() or not path.is_file() or ".git" in path.parts:
            continue
        try:
            if path.stat().st_size <= 128 * 1024:
                result[str(path.relative_to(directory))] = path.read_text(encoding="utf-8")
        except (UnicodeError, OSError):
            pass
    return result


class Trace(BaseCallbackHandler):
    def __init__(self, store, run_id, directory):
        self.store, self.run_id, self.directory = store, run_id, directory
        self.before = snapshot(directory)
        self.lock = threading.Lock()
        self.started = {}

    def on_tool_start(self, serialized, input_str, *, run_id, **kwargs):
        self.started[str(run_id)] = time.monotonic()
        self.store.event(
            self.run_id,
            "tool.started",
            {
                "tool_call_id": str(run_id),
                "name": serialized.get("name", "tool"),
                "input": input_str[:16000],
                "inputs": kwargs.get("inputs"),
            },
        )

    def on_tool_end(self, output, *, run_id, **kwargs):
        content = getattr(output, "content", output)
        exit_match = re.search(r"\[Command failed with exit code (-?\d+)\]", str(content))
        failed = getattr(output, "status", None) == "error" or exit_match is not None
        self.store.event(
            self.run_id,
            "tool.finished",
            {
                "tool_call_id": str(run_id),
                "output": str(content)[:16000],
                "failed": failed,
                "exit_code": int(exit_match.group(1)) if exit_match else None,
                "elapsed_ms": round(
                    (time.monotonic() - self.started.get(str(run_id), time.monotonic())) * 1000
                ),
            },
        )
        self.changes()

    def on_tool_error(self, error, *, run_id, **kwargs):
        self.store.event(
            self.run_id,
            "tool.failed",
            {
                "tool_call_id": str(run_id),
                "error_type": type(error).__name__,
                "elapsed_ms": round(
                    (time.monotonic() - self.started.get(str(run_id), time.monotonic())) * 1000
                ),
            },
        )

    def changes(self):
        with self.lock:
            after = snapshot(self.directory)
            for name in sorted(self.before.keys() | after.keys()):
                old, new = self.before.get(name, ""), after.get(name, "")
                if old != new or (name in self.before) != (name in after):
                    diff = "".join(
                        difflib.unified_diff(
                            old.splitlines(True), new.splitlines(True), fromfile=name, tofile=name
                        )
                    )
                    self.store.event(
                        self.run_id, "file.changed", {"path": name, "diff": diff[:32000]}
                    )
            self.before = after


def run_agent(store, run_id, model, image):
    run = store.run(run_id)
    snapshot_config = store.run_configuration(run_id)
    policy = snapshot_config["policy"]
    session = store.session(run["session_id"])
    directory = store.directory(session["workspace_id"])
    store.event(run_id, "run.started", {"model": getattr(model, "model_name", "configured")})
    trace, usage = Trace(store, run_id, directory), UsageMetadataCallbackHandler()
    budget = CallBudget(policy["max_model_calls"])
    context = ContextMiddleware(
        model,
        snapshot_config,
        store,
        run_id,
        usage,
        force=run["action"] == "compact",
        budget=budget,
    )
    with WorkspaceSandbox(directory, image, run_id, policy["tool_timeout_seconds"]) as backend:
        with SqliteSaver.from_conn_string(str(store.root / "checkpoints.sqlite")) as saver:
            agent = create_deep_agent(
                model=model,
                backend=backend,
                checkpointer=saver,
                subagents=[],
                middleware=[
                    platform_owned_delegation,
                    context,
                    ModelCallLimitMiddleware(
                        run_limit=policy["max_model_calls"], exit_behavior="error"
                    ),
                ],
                system_prompt=(
                    "你是通用工作区 Agent。按用户自由提出的任务使用真实工具完成工作。"
                    "工作目录是 /workspace，项目文件必须放在该目录下；/tmp 可作临时目录。"
                    "先检查已有文件，再修改，尽可能执行真实测试。工具运行于无网络的 Python "
                    "容器，不提供浏览器或联网检索；如任务需要缺失能力，明确说明。"
                    "不要虚构工具结果、测试通过或任务完成。工具失败后检查反馈并修复。"
                    "回复使用中文，最终说明实际修改、验证结果和未完成部分。"
                ),
            )
            config = {
                "configurable": {"thread_id": session["id"]},
                "recursion_limit": 100,
                "callbacks": [trace, usage, budget],
            }
            previous = agent.get_state(config).values.get("messages", [])
            if run["action"] == "compact":
                result = context.before_model({"messages": previous}, None) if previous else None
                if result:
                    agent.update_state(config, result)
                else:
                    store.event(
                        run_id, "context.unchanged", {"reason": "当前可压缩历史不足，保留原文"}
                    )
                store.event(run_id, "run.finished", {"usage": usage.usage_metadata})
                return
            answered = {getattr(m, "tool_call_id", None) for m in previous}
            recovery = [
                ToolMessage(
                    content="上轮执行被中断，结果未知。检查文件状态后再决定是否重试。",
                    tool_call_id=call["id"],
                )
                for m in previous
                for call in getattr(m, "tool_calls", [])
                if call["id"] not in answered
            ]
            content = run["text"]
            if run["attachment_ids"]:
                content = [{"type": "text", "text": run["text"] or "请查看图片"}]
                content.extend(
                    store.image_block(identifier) for identifier in run["attachment_ids"]
                )
                store.event(run_id, "image.submitted", {"attachment_ids": run["attachment_ids"]})
            user = {
                "role": "user",
                "content": content,
                "additional_kwargs": {"attachment_ids": run["attachment_ids"]},
            }
            store.message(
                session["id"], run_id, {"type": "human", "data": {"content": run["text"]}}
            )
            for update in agent.stream(
                {"messages": [*recovery, user]}, config, stream_mode="updates", durability="sync"
            ):
                for value in update.values():
                    if not isinstance(value, dict):
                        continue
                    for message in value.get("messages", []):
                        if hasattr(message, "type"):
                            data = message_to_dict(message)
                            store.message(session["id"], run_id, data)
                            store.event(run_id, "message.created", data)
                    if "todos" in value:
                        store.event(run_id, "plan.updated", {"todos": value["todos"]})
            trace.changes()
    store.event(run_id, "run.finished", {"usage": usage.usage_metadata})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    args = parser.parse_args()
    store = AgentStore(args.root)
    try:
        configuration = store.run_configuration(args.run_id)
        settings = model_base_settings(args.config, configuration["provider"])
        credential = provider_credential(store, configuration["provider"], settings)
        with general_model_session(
            settings, store.run(args.run_id)["reasoning_effort"], configuration, credential
        ) as model:
            run_agent(store, args.run_id, model, settings.image)
    except Exception as error:
        store.event(
            args.run_id,
            "run.error",
            {
                "error_type": type(error).__name__,
                "detail": str(error)
                if isinstance(error, ConfigurationError)
                else "模型或工具执行失败，请检查配置和执行记录",
            },
        )
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
