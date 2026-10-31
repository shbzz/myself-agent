"""Bounded DAG interpreter with capability/load scheduling and evidence-scoped repair."""

import threading
import time
import uuid
from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass

from myself_agent.general_settings import ConfigurationError
from myself_agent.orchestration_models import Flow, Node


class OrchestrationStopped(ConfigurationError):
    pass


class OrchestrationPaused(Exception):
    def __init__(self, checkpoint):
        self.checkpoint = checkpoint
        super().__init__("Human verification required")


@dataclass
class TaskRequest:
    node: Node
    path: str
    attempt_id: str
    agent: dict
    configuration: dict
    inputs: dict
    feedback: dict
    check: Callable


def merge_evidence(values):
    values = list(values)
    result = {}
    for key in (
        "agent_ids",
        "producer_agents",
        "producer_paths",
        "verified_paths",
        "verification_ids",
        "resolved_rejections",
        "human_decision_ids",
    ):
        result[key] = sorted({item for value in values for item in value.get(key, [])})
    result["rejections"] = {
        key: item for value in values for key, item in value.get("rejections", {}).items()
    }
    for path in result["resolved_rejections"]:
        result["rejections"].pop(path, None)
    return result


def graph_result(results):
    evidence = merge_evidence(results.values())
    covered = set(evidence["producer_paths"]) <= set(evidence["verified_paths"])
    passed = (
        False
        if evidence["rejections"]
        else True
        if evidence["verification_ids"] and covered
        else None
    )
    return evidence | {
        "passed": passed,
        "nodes": results,
        "unverified_paths": sorted(
            set(evidence["producer_paths"]) - set(evidence["verified_paths"])
        ),
    }


class Resources:
    def __init__(self, agents, limit):
        self.agents, self.limit = agents, limit
        self.loads = {a["definition"]["id"]: 0 for a in agents}
        self.assigned = dict.fromkeys(self.loads, 0)
        self.active, self.writer = 0, False
        self.condition = threading.Condition()

    def reserve(self, node):
        with self.condition:
            candidates = []
            for member in self.agents:
                agent = member["definition"]
                identifier = agent["id"]
                reason = None
                if not agent["enabled"]:
                    reason = "disabled"
                elif (agent["role"] == "verifier") != (node.kind == "verify"):
                    reason = "role"
                elif not set(node.required_skills) <= set(agent["skills"]):
                    reason = "skills"
                elif not set(node.required_tools) <= set(agent["tools"]):
                    reason = "tools"
                eligible = reason is None
                if eligible and self.loads[identifier] >= agent["max_parallel"]:
                    reason = "agent_capacity"
                candidates.append(
                    {
                        "agent_id": identifier,
                        "eligible": eligible,
                        "reason": reason,
                        "load": self.loads[identifier],
                        "assigned": self.assigned[identifier],
                        "extra_skills": len(set(agent["skills"]) - set(node.required_skills)),
                    }
                )
            if not any(c["eligible"] for c in candidates):
                raise ConfigurationError(f"No eligible Agent for {node.id}")
            if self.active >= self.limit or self.writer or (node.access == "write" and self.active):
                return None
            available = [c for c in candidates if c["reason"] is None]
            if not available:
                return None
            selected = min(
                available,
                key=lambda c: (c["load"], c["assigned"], c["extra_skills"], c["agent_id"]),
            )
            identifier = selected["agent_id"]
            self.loads[identifier] += 1
            self.assigned[identifier] += 1
            self.active += 1
            self.writer = node.access == "write"
            member = next(a for a in self.agents if a["definition"]["id"] == identifier)
            return member, candidates

    def release(self, identifier, access):
        with self.condition:
            self.loads[identifier] -= 1
            self.active -= 1
            if access == "write":
                self.writer = False
            self.condition.notify_all()


class Orchestrator:
    def __init__(self, store, run_id, runner, *, resume=None):
        self.store, self.run_id, self.runner = store, run_id, runner
        self.definition = store.orchestration.snapshot(run_id)
        self.policy = self.definition["configuration"]["policy"]
        self.resources = Resources(
            self.definition["agents"], self.definition["team"]["max_parallel"]
        )
        self.resume = resume
        self.steps = resume["steps"] if resume else 0
        if resume:
            self.resources.assigned.update(resume["assigned"])
        self.stop = threading.Event()
        self.failure = None
        self.failure_lock = threading.Lock()
        self.started = time.monotonic()
        self.elapsed_before = resume["runtime"]["active_seconds"] if resume else 0
        self.deadline = self.started + self.policy["timeout_seconds"] - self.elapsed_before
        self.pool = None

    def check(self):
        if self.failure is not None:
            raise self.failure
        if self.stop.is_set() or self.store.run(self.run_id)["status"] not in ("queued", "running"):
            raise OrchestrationStopped("Orchestration was stopped")
        if time.monotonic() >= self.deadline:
            raise OrchestrationStopped("Orchestration deadline reached")

    def step(self):
        self.check()
        if self.steps >= self.definition["workflow"]["max_steps"]:
            raise ConfigurationError("Workflow step budget exhausted")
        self.steps += 1

    def emit(self, kind, payload):
        self.store.event(self.run_id, "orchestration." + kind, payload)

    def run(self):
        self.emit(
            "started",
            {"team": self.definition["team"]["id"], "workflow": self.definition["workflow"]["id"]},
        )
        self.pool = ThreadPoolExecutor(max_workers=self.definition["team"]["max_parallel"])
        try:
            result = self.graph(Flow.model_validate(self.definition["workflow"]["flow"]), "root")
            summary = {key: value for key, value in result.items() if key != "nodes"}
            summary["steps"] = self.steps
            summary["status"] = "succeeded" if result["passed"] is True else "rejected"
            if not self.store.orchestration.finish(self.run_id, summary):
                raise OrchestrationStopped("Run stopped before completion")
            self.emit("finished", summary)
            return summary
        except OrchestrationPaused:
            raise
        except Exception as error:
            self.stop.set()
            self.store.orchestration.interrupt(self.run_id, "interrupted", type(error).__name__)
            self.emit("error", {"error_type": type(error).__name__})
            raise
        finally:
            self.pool.shutdown(wait=True, cancel_futures=True)

    @staticmethod
    def descendants(flow, targets):
        affected = set(targets)
        while True:
            before = len(affected)
            affected.update(node.id for node in flow.nodes if set(node.depends_on) & affected)
            if len(affected) == before:
                return affected

    def graph(self, flow, prefix, inherited=None, feedback=None, cache=None):
        inherited, feedback = inherited or {}, feedback or {}
        results, pending, futures, ready_since = {}, {n.id: n for n in flow.nodes}, {}, {}
        if prefix == "root" and self.resume:
            results = dict(self.resume["results"])
            node = pending[self.resume["node_id"]]
            evidence = merge_evidence(results[name] for name in node.depends_on)
            result = evidence | {
                "passed": True,
                "text": "Explicit human approval",
                "task_path": self.resume["path"],
            }
            result["human_decision_ids"] = sorted(
                set(result["human_decision_ids"]) | {self.resume["decision_id"]}
            )
            results[node.id] = result
            self.store.orchestration.task(
                self.run_id, self.resume["path"], node, "succeeded", result=result
            )
            pending = {key: value for key, value in pending.items() if key not in results}
            self.emit(
                "resumed",
                {
                    "checkpoint_id": self.resume["checkpoint_id"],
                    "completed_paths": sorted(results),
                    "steps": self.steps,
                },
            )
        while pending or futures:
            self.check()
            progressed = False
            ready = [node for node in pending.values() if set(node.depends_on) <= results.keys()]
            gates = [
                node
                for node in ready
                if node.kind == "human"
                and not merge_evidence(results[name] for name in node.depends_on)["rejections"]
            ]
            if gates:
                # Drain everything already in flight before persisting any continuation.
                if futures:
                    done, _ = wait(futures, timeout=0.05, return_when=FIRST_COMPLETED)
                    for future in done:
                        results[futures.pop(future)] = future.result()
                    continue
                if prefix != "root" or self.resources.active:
                    raise ConfigurationError("Human gate is not at a quiescent root boundary")
                node = sorted(gates, key=lambda value: value.id)[0]
                self.step()
                path = prefix + "/" + node.id
                self.store.orchestration.task(self.run_id, path, node, "waiting")
                raise OrchestrationPaused(
                    {
                        "path": path,
                        "node_id": node.id,
                        "results": results,
                        "verification": results[node.source],
                        "steps": self.steps,
                        "assigned": dict(self.resources.assigned),
                        "active_seconds": self.elapsed_before + time.monotonic() - self.started,
                    }
                )
            for node in ready:
                ready_since.setdefault(node.id, time.monotonic())

            def priority(node):
                criticality = len(self.descendants(flow, [node.id])) - 1
                age = min(20, time.monotonic() - ready_since[node.id]) / 10
                return node.priority + criticality + age

            ready.sort(key=lambda node: (-priority(node), node.id))
            for node in ready:
                path = prefix + "/" + node.id
                inputs = inherited | {name: results[name] for name in node.depends_on}
                evidence = merge_evidence(inputs.values())
                if cache and node.id in cache:
                    self.step()
                    results[node.id] = dict(
                        cache[node.id], reused_from=cache[node.id].get("task_path"), task_path=path
                    )
                    self.store.orchestration.task(
                        self.run_id, path, node, "reused", result=results[node.id]
                    )
                elif evidence["rejections"] and node.kind != "condition":
                    self.step()
                    results[node.id] = evidence | {
                        "passed": False,
                        "text": "Blocked by rejected dependency",
                    }
                    self.store.orchestration.task(
                        self.run_id, path, node, "skipped", result=results[node.id]
                    )
                elif node.kind in ("agent", "verify"):
                    reserved = self.resources.reserve(node)
                    if reserved is None:
                        continue
                    member, candidates = reserved
                    try:
                        self.step()
                        attempt = uuid.uuid4().hex
                        request = TaskRequest(
                            node,
                            path,
                            attempt,
                            member["definition"],
                            member["configuration"],
                            inputs,
                            feedback,
                            self.check,
                        )
                        self.emit(
                            "scheduled",
                            {
                                "path": path,
                                "agent_id": request.agent["id"],
                                "priority_score": round(priority(node), 4),
                                "candidates": candidates,
                                "access": node.access,
                                "reason": (
                                    "dependencies, role, skills and tools; "
                                    "then load, assignments and specialization"
                                ),
                            },
                        )
                        self.store.orchestration.task(
                            self.run_id,
                            path,
                            node,
                            "running",
                            agent_id=request.agent["id"],
                            attempt_id=attempt,
                        )
                        future = self.pool.submit(self.execute, request)
                    except BaseException:
                        self.resources.release(member["definition"]["id"], node.access)
                        raise
                    futures[future] = node.id
                else:
                    self.step()
                    self.store.orchestration.task(self.run_id, path, node, "running")
                    results[node.id] = self.control(node, path, inputs, feedback)
                    status = "rejected" if results[node.id]["passed"] is False else "succeeded"
                    self.store.orchestration.task(
                        self.run_id, path, node, status, result=results[node.id]
                    )
                pending.pop(node.id)
                progressed = True
            if futures:
                done, _ = wait(futures, timeout=0.05, return_when=FIRST_COMPLETED)
                for future in done:
                    identifier = futures.pop(future)
                    results[identifier] = future.result()
                    progressed = True
            if not progressed:
                if not futures and not self.resources.active:
                    raise ConfigurationError("Workflow cannot make progress")
                with self.resources.condition:
                    self.resources.condition.wait(timeout=0.05)
        return graph_result(results)

    def execute(self, request):
        node = request.node
        try:
            request.check()
            outcome = self.runner(request)
            request.check()
            evidence = merge_evidence(request.inputs.values())
            identifier = request.agent["id"]
            evidence["agent_ids"] = sorted(set(evidence["agent_ids"]) | {identifier})
            text = str(outcome.get("text", ""))[:6000]
            result = evidence | {
                "passed": None,
                "text": text,
                "agent_id": identifier,
                "task_path": request.path,
            }
            if node.kind == "agent":
                result["producer_agents"] = sorted(set(evidence["producer_agents"]) | {identifier})
                result["producer_paths"] = sorted(set(evidence["producer_paths"]) | {request.path})
            else:
                producers = {p for p in evidence["producer_agents"] if p == identifier}
                if producers:
                    raise ConfigurationError("Verifier cannot verify its own output")
                checks = outcome.get("checks", [])
                commands_match = len(checks) == len(node.checks) and all(
                    c.get("command") == expected
                    for c, expected in zip(checks, node.checks, strict=True)
                )
                passed = bool(
                    outcome.get("artifact_stable") is True
                    and outcome.get("review_passed") is True
                    and commands_match
                    and checks
                    and all(c.get("exit_code") == 0 for c in checks)
                )
                result.update(
                    passed=passed,
                    artifacts=outcome.get("artifacts"),
                    checks=checks,
                    review_passed=outcome.get("review_passed"),
                    repair_targets=node.repair_targets,
                )
                if passed:
                    result["verified_paths"] = sorted(
                        set(evidence["verified_paths"]) | set(evidence["producer_paths"])
                    )
                    result["verification_ids"] = sorted(
                        set(evidence["verification_ids"]) | {request.path}
                    )
                else:
                    result["rejections"] = evidence["rejections"] | {
                        request.path: {
                            "text": text,
                            "checks": checks,
                            "repair_targets": node.repair_targets,
                        }
                    }
                self.emit(
                    "verified",
                    {
                        "path": request.path,
                        "agent_id": identifier,
                        "passed": passed,
                        "checks": checks,
                    },
                )
            status = "rejected" if result["passed"] is False else "succeeded"
            self.store.orchestration.task(
                self.run_id,
                request.path,
                node,
                status,
                agent_id=identifier,
                attempt_id=request.attempt_id,
                result=result,
            )
            return result
        except Exception as error:
            with self.failure_lock:
                if self.failure is None:
                    self.failure = error
            self.stop.set()
            self.store.orchestration.task(
                self.run_id,
                request.path,
                node,
                "failed",
                agent_id=request.agent["id"],
                attempt_id=request.attempt_id,
                result={"error_type": type(error).__name__},
            )
            raise
        finally:
            self.resources.release(request.agent["id"], node.access)

    @staticmethod
    def compact_control(result, text):
        return {key: value for key, value in result.items() if key != "nodes"} | {"text": text}

    def control(self, node, path, inputs, feedback):
        if node.kind == "subworkflow":
            return self.compact_control(
                self.graph(node.body, path, inputs, feedback), "Subworkflow completed"
            )
        if node.kind == "condition":
            source = inputs[node.source]
            if source.get("passed") not in (True, False):
                raise ConfigurationError("Condition source has no verified boolean outcome")
            branch = "on_true" if source["passed"] else "on_false"
            clean_inputs = {
                key: dict(value, rejections={}) if key == node.source else value
                for key, value in inputs.items()
            }
            self.emit("branch", {"path": path, "source": node.source, "selected": branch})
            result = self.graph(
                getattr(node, branch),
                path + "/" + branch,
                clean_inputs,
                feedback | {"condition_source": source},
            )
            if result["passed"] is True:
                result["resolved_rejections"] = sorted(
                    set(result["resolved_rejections"]) | set(source.get("rejections", {}))
                )
            return self.compact_control(result, "Selected " + branch)
        cache = None
        for iteration in range(1, node.max_iterations + 1):
            result = self.graph(
                node.body, path + f"/iteration-{iteration}", inputs, feedback, cache
            )
            if result["nodes"][node.until]["passed"] is True and result["passed"] is True:
                return self.compact_control(
                    result, f"Loop converged after {iteration} iterations"
                ) | {"iterations": iteration}
            targets = {
                target
                for value in result["rejections"].values()
                for target in value.get("repair_targets", [])
            }
            if not targets:
                targets = {n.id for n in node.body.nodes if n.kind == "agent"}
            affected = self.descendants(node.body, targets) | {node.until}
            cache = {name: value for name, value in result["nodes"].items() if name not in affected}
            feedback = {
                "iteration": iteration,
                "rejections": result["rejections"],
                "unverified": sorted(set(result["producer_paths"]) - set(result["verified_paths"])),
            }
            self.emit(
                "repair",
                {
                    "path": path,
                    "iteration": iteration,
                    "targets": sorted(targets),
                    "affected": sorted(affected),
                    "reused": sorted(cache),
                    "will_repeat": iteration < node.max_iterations,
                },
            )
        output = self.compact_control(result, "Loop exhausted its iteration budget")
        output["passed"] = False
        output["rejections"] = output["rejections"] | {
            path: {"text": "Loop did not converge", "repair_targets": []}
        }
        return output | {"iterations": node.max_iterations}
