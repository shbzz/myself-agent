"""Deep Agents filesystem and Shell tools backed by a workspace-only container."""

import base64
import json
import os
import re
import selectors
import shlex
import subprocess
import time

from deepagents.backends.protocol import ExecuteResponse, FileDownloadResponse, FileUploadResponse
from deepagents.backends.sandbox import BaseSandbox


class WorkspaceSandbox(BaseSandbox):
    def __init__(
        self, workspace, image, run_id, tool_timeout=30, *, read_only=False, parent_run_id=None
    ):
        if not re.fullmatch(r"[a-f0-9]{32}", run_id):
            raise ValueError("Invalid run ID")
        if not re.fullmatch(r"python@sha256:[a-f0-9]{64}", image):
            raise ValueError("A pinned Python image is required")
        if parent_run_id and not re.fullmatch(r"[a-f0-9]{32}", parent_run_id):
            raise ValueError("Invalid parent run ID")
        self.read_only, self.parent_run_id = read_only, parent_run_id
        self.workspace = workspace.resolve(strict=True)
        self.image = image
        self.name = "myself-agent-general-" + run_id
        self.tool_timeout = tool_timeout

    @property
    def id(self):
        return self.name

    def __enter__(self):
        subprocess.run(
            [
                "sudo",
                "-n",
                "docker",
                "run",
                "-d",
                "--rm",
                "--pull=never",
                "--name",
                self.name,
                "--network=none",
                "--read-only",
                f"--user={os.getuid()}:{os.getgid()}",
                "--cap-drop=ALL",
                "--security-opt=no-new-privileges:true",
                "--memory=512m",
                "--memory-swap=512m",
                "--cpus=0.5",
                "--pids-limit=64",
                "--ulimit=nofile=128:128",
                "--ulimit=fsize=16777216:16777216",
                "--tmpfs=/tmp:rw,nosuid,size=32m",
                "--mount",
                f"type=bind,src={self.workspace},dst=/workspace"
                + (",readonly" if self.read_only else ""),
                "--workdir=/workspace",
                *(
                    ["--label", "myself-agent.parent=" + self.parent_run_id]
                    if self.parent_run_id
                    else []
                ),
                self.image,
                "sleep",
                "infinity",
            ],
            check=True,
            capture_output=True,
            timeout=20,
        )
        return self

    def close(self):
        subprocess.run(
            ["sudo", "-n", "docker", "rm", "-f", self.name],
            capture_output=True,
            timeout=15,
            check=False,
        )

    def __exit__(self, *args):
        self.close()

    def execute(self, command, *, timeout=None):
        limit = min(timeout or self.tool_timeout, 60)
        process = subprocess.Popen(
            ["sudo", "-n", "docker", "exec", "-w", "/workspace", self.name, "sh", "-lc", command],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        output = bytearray()
        started = time.monotonic()
        exceeded = None
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                while True:
                    if time.monotonic() - started > limit:
                        exceeded = "Command timed out"
                        break
                    if not selector.select(0.1):
                        if process.poll() is not None:
                            break
                        continue
                    data = os.read(process.stdout.fileno(), 4096)
                    if not data:
                        break
                    output.extend(data)
                    if len(output) > 128 * 1024:
                        exceeded = "Command output limit reached"
                        break
            if exceeded:
                self.close()  # Stop descendants too; this run must not reuse the container.
                raise RuntimeError(exceeded)
            return ExecuteResponse(
                output.decode("utf-8", errors="replace"), exit_code=process.wait(timeout=5)
            )
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
            process.stdout.close()

    def upload_files(self, files):
        results = []
        for path, content in files:
            payload = json.dumps([path, base64.b64encode(content).decode()])
            script = (
                "import json,base64,pathlib; p,v=json.loads(" + repr(payload) + "); "
                "pathlib.Path(p).write_bytes(base64.b64decode(v))"
            )
            result = self.execute("python -c " + shlex.quote(script))
            results.append(
                FileUploadResponse(
                    path=path, error=None if result.exit_code == 0 else "permission_denied"
                )
            )
        return results

    def download_files(self, paths):
        results = []
        for path in paths:
            script = (
                "import pathlib,base64; print(base64.b64encode(pathlib.Path("
                + repr(path)
                + ").read_bytes()).decode())"
            )
            result = self.execute("python -c " + shlex.quote(script))
            results.append(
                FileDownloadResponse(
                    path=path,
                    content=base64.b64decode(result.output) if result.exit_code == 0 else None,
                    error=None if result.exit_code == 0 else "file_not_found",
                )
            )
        return results
