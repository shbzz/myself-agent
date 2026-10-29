import pytest
from myself_agent.config import load_settings
from myself_agent.sandbox import docker_command
from myself_agent.schema import Plan, SourceFile, merge_files
from pydantic import ValidationError


def test_artifact_allowlist_and_result_replacement():
    with pytest.raises(ValidationError):
        SourceFile(filename="../../.ssh/config", content="x" * 30)
    assert merge_files({"stats.py": "old"}, {"stats.py": "fixed"}) == {"stats.py": "fixed"}


def test_plan_rejects_duplicate_and_cyclic_dependencies():
    task = {
        "filename": "parser.py",
        "instruction": "Implement the parsing contract",
        "depends_on": [],
    }
    with pytest.raises(ValidationError):
        Plan(tasks=[task, task])
    with pytest.raises(ValidationError):
        Plan(
            tasks=[
                {**task, "depends_on": ["stats.py"]},
                {**task, "filename": "stats.py", "depends_on": ["parser.py"]},
            ]
        )


def test_config_rejects_permissions_and_other_provider(tmp_path):
    path = tmp_path / "settings.env"
    path.write_text(
        "DEEPSEEK_API_KEY=unit-test-placeholder\n"
        "DEEPSEEK_BASE_URL=https://api.deepseek.com\nDEEPSEEK_MODEL=deepseek-flash\n"
    )
    path.chmod(0o644)
    with pytest.raises(ValueError, match="mode 600"):
        load_settings(path)
    path.chmod(0o600)
    config = load_settings(path)
    assert "unit-test-placeholder" not in repr(config)
    path.write_text(path.read_text().replace("api.deepseek.com", "example.invalid"))
    with pytest.raises(ValueError, match="official endpoint"):
        load_settings(path)


def test_sandbox_arguments_do_not_mount_host_credentials(settings, workspace, container_name):
    command = docker_command(settings, workspace, container_name)
    assert "--network=none" in command
    assert "--read-only" in command
    assert "--user=65534:65534" in command
    assert "--cap-drop=ALL" in command
    assert "--memory=512m" in command
    assert not any("docker.sock" in value or "deepseek.env" in value for value in command)
    assert "--env" not in command and "--privileged" not in command
    with pytest.raises(ValueError, match="container name"):
        docker_command(settings, workspace, "an-unrelated-container")
