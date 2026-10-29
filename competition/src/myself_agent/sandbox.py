import json
import os
import re
import selectors
import subprocess
import time
from pathlib import Path

from myself_agent.config import Settings
from myself_agent.scenarios import get_scenario

PREFIX = "MYSELF_AGENT_TEST_RESULT:"
MAX_OUTPUT = 128 * 1024
ACCEPTANCE = Path(__file__).parent / "acceptance"


def docker_command(
    settings: Settings, workspace: Path, name: str, scenario_id="csv-orders"
) -> list[str]:
    scenario = get_scenario(scenario_id)
    if not re.fullmatch(r"myself-agent-[a-f0-9]{32}-[0-3]", name):
        raise ValueError("Invalid project container name")
    if not re.fullmatch(r"python@sha256:[a-f0-9]{64}", settings.image):
        raise ValueError("Test image must be a pinned official Python digest")
    return [
        "sudo",
        "-n",
        "docker",
        "run",
        "--rm",
        "--pull=never",
        "--name",
        name,
        "--network=none",
        "--read-only",
        "--user=65534:65534",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges:true",
        "--memory=512m",
        "--memory-swap=512m",
        "--cpus=0.5",
        "--pids-limit=64",
        "--ulimit=nofile=128:128",
        "--tmpfs=/tmp:rw,noexec,nosuid,size=16m",
        "--mount",
        f"type=bind,src={workspace.resolve()},dst=/workspace,readonly",
        "--mount",
        f"type=bind,src={ACCEPTANCE.resolve()},dst=/acceptance,readonly",
        "--workdir=/workspace",
        settings.image,
        "python",
        "-B",
        "-I",
        f"/acceptance/{scenario.runner}",
    ]


def run_tests(settings: Settings, workspace: Path, name: str, scenario_id="csv-orders") -> dict:
    scenario = get_scenario(scenario_id)
    command = docker_command(settings, workspace, name, scenario_id)
    started = time.monotonic()
    output = bytearray()
    stop_reason = None
    proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    assert proc.stdout is not None
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(proc.stdout, selectors.EVENT_READ)
            while True:
                remaining = settings.timeout_seconds - (time.monotonic() - started)
                if remaining <= 0:
                    stop_reason = "timeout"
                    break
                if not selector.select(min(remaining, 0.2)):
                    if proc.poll() is not None:
                        break
                    continue
                chunk = os.read(proc.stdout.fileno(), 4096)
                if not chunk:
                    break
                if len(output) + len(chunk) > MAX_OUTPUT:
                    stop_reason = "output_limit"
                    break
                output.extend(chunk)
        if stop_reason:
            proc.kill()
        return_code = proc.wait(timeout=5)
    finally:
        # Only this invocation's validated, unique project container may be removed.
        subprocess.run(
            ["sudo", "-n", "docker", "rm", "-f", name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
            check=False,
        )
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
        proc.stdout.close()
    text = output.decode("utf-8", errors="replace")
    report = None
    for line in reversed(text.splitlines()):
        if line.startswith(PREFIX):
            try:
                report = json.loads(line[len(PREFIX) :])
            except json.JSONDecodeError:
                pass
            break
    passed = (
        stop_reason is None
        and return_code == 0
        and isinstance(report, dict)
        and report.get("passed") is True
        and report.get("tests_run") == scenario.tests
        and report.get("suite") == scenario.suite
    )
    return {
        "passed": passed,
        "failure_kind": stop_reason
        or (
            "infrastructure"
            if return_code in (125, 126, 127)
            else "acceptance"
            if not passed
            else None
        ),
        "return_code": return_code,
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "report": report,
        "output": text,
        "image": settings.image,
        "container": name,
    }
