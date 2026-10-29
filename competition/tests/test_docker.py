from dataclasses import replace

import pytest

from myself_agent.sandbox import run_tests


@pytest.mark.docker
def test_acceptance_and_actual_isolation(settings, workspace, container_name):
    (workspace / "stats.py").write_text("""import os
import socket
from pathlib import Path

def summarize(orders):
    assert os.getuid() == 65534
    assert "DEEPSEEK_API_KEY" not in os.environ
    assert not Path("/var/run/docker.sock").exists()
    assert not Path("/home/dsh/.config/myself-agent/deepseek.env").exists()
    try:
        Path("/workspace/forbidden").write_text("bad")
    except OSError as error:
        assert error.errno in (13, 30)
    else:
        raise AssertionError("workspace was writable")
    with socket.socket() as client:
        client.settimeout(0.1)
        try:
            client.connect(("1.1.1.1", 443))
        except OSError:
            pass
        else:
            raise AssertionError("unexpected external network")
    return {"count": len(orders), "total_cents": sum(item["amount_cents"] for item in orders)}
""")
    result = run_tests(settings, workspace, container_name)
    assert result["passed"], result
    assert result["report"]["tests_run"] == 12


@pytest.mark.docker
def test_wrong_code_is_rejected(settings, workspace, container_name):
    (workspace / "stats.py").write_text("def summarize(orders):\n    return {}\n")
    result = run_tests(settings, workspace, container_name)
    assert not result["passed"]
    assert result["failure_kind"] == "acceptance"
    assert result["report"]["failures"] > 0


@pytest.mark.docker
def test_zero_exit_without_acceptance_report_is_rejected(settings, workspace, container_name):
    (workspace / "stats.py").write_text("import os\nos._exit(0)\n")
    result = run_tests(settings, workspace, container_name)
    assert result["return_code"] == 0
    assert not result["passed"]
    assert result["report"] is None


@pytest.mark.docker
def test_timeout_and_output_limit(settings, workspace, container_name):
    (workspace / "parser.py").write_text("while True:\n    pass\n")
    result = run_tests(replace(settings, timeout_seconds=1), workspace, container_name)
    assert result["failure_kind"] == "timeout"
    (workspace / "parser.py").write_text('print("x" * 200000, flush=True)\n')
    result = run_tests(settings, workspace, container_name)
    assert result["failure_kind"] == "output_limit"
