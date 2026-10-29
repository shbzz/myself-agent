"""Real Deep Agents tool loop with persistent LangGraph conversation checkpoints."""

import argparse
import difflib
import threading
from pathlib import Path

from deepagents import create_deep_agent
from langchain.agents.middleware import ModelCallLimitMiddleware
from langchain_core.callbacks import BaseCallbackHandler, UsageMetadataCallbackHandler
from langchain_core.messages import ToolMessage, message_to_dict
from langgraph.checkpoint.sqlite import SqliteSaver

from myself_agent.config import DEFAULT_CONFIG, load_settings
from myself_agent.general_sandbox import WorkspaceSandbox
from myself_agent.general_store import AgentStore
from myself_agent.provider import model_session, platform_owned_delegation


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

    def on_tool_start(self, serialized, input_str, *, run_id, **kwargs):
        self.store.event(
            self.run_id,
            "tool.started",
            {
                "tool_call_id": str(run_id),
                "name": serialized.get("name", "tool"),
                "input": input_str[:16000],
            },
        )

    def on_tool_end(self, output, *, run_id, **kwargs):
        content = getattr(output, "content", output)
        self.store.event(
            self.run_id,
            "tool.finished",
            {
                "tool_call_id": str(run_id),
                "output": str(content)[:16000],
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
    session = store.session(run["session_id"])
    directory = store.directory(session["workspace_id"])
    store.event(run_id, "run.started", {"model": getattr(model, "model_name", "configured")})
    trace, usage = Trace(store, run_id, directory), UsageMetadataCallbackHandler()
    with WorkspaceSandbox(directory, image, run_id) as backend:
        with SqliteSaver.from_conn_string(str(store.root / "checkpoints.sqlite")) as saver:
            agent = create_deep_agent(
                model=model,
                backend=backend,
                checkpointer=saver,
                subagents=[],
                middleware=[
                    platform_owned_delegation,
                    ModelCallLimitMiddleware(run_limit=24, exit_behavior="error"),
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
                "callbacks": [trace, usage],
            }
            previous = agent.get_state(config).values.get("messages", [])
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
            user = {"role": "user", "content": run["text"]}
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
    settings = load_settings(args.config)
    try:
        with model_session(settings) as model:
            run_agent(store, args.run_id, model, settings.image)
    except Exception as error:
        store.event(args.run_id, "run.error", {"error_type": type(error).__name__})
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
