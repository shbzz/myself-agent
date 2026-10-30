import base64
import io
import os
import stat
import zipfile
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage
from test_general import ScriptModel, call, new_run

from myself_agent.general_runtime import run_agent
from myself_agent.general_store import AgentStore
from myself_agent.general_web import create_app
from myself_agent.workspace_files import MAX_ARCHIVE_BYTES, WorkspaceFiles


def make_zip(files):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path, content in files.items():
            archive.writestr(path, content)
    return output.getvalue()


@pytest.fixture
def client(tmp_path):
    class Manager:
        def __init__(self, root, config):
            self.store = AgentStore(root)

        def close(self):
            pass

    with TestClient(create_app(tmp_path, manager_factory=Manager)) as client:
        yield client


def import_project(client, files=None, **overrides):
    body = {
        "name": "已有项目",
        "data_base64": base64.b64encode(
            make_zip(
                files
                if files is not None
                else {"src/app.py": "print('你好')", "README.md": "project"}
            )
        ).decode(),
    }
    return client.post("/next/api/workspaces/import", json=body | overrides)


def test_import_preview_binary_download_export_and_persistence(client):
    response = import_project(client, {"src/app.py": "print('你好')", "data.bin": b"\x00\xff"})
    assert response.status_code == 201
    data = response.json()
    assert data["file_count"] == 2
    identifier = data["workspace"]["id"]
    base = "/next/api/workspaces/" + identifier
    listing = client.get(base + "/files").json()["entries"]
    assert [(e["name"], e["kind"]) for e in listing] == [("src", "directory"), ("data.bin", "file")]
    assert (
        client.get(base + "/file", params={"path": "src/app.py"}).json()["text"] == "print('你好')"
    )
    assert client.get(base + "/file", params={"path": "data.bin"}).json()["text"] is None
    download = client.get(base + "/download", params={"path": "src/app.py"})
    assert download.content.decode() == "print('你好')"
    assert download.headers["content-disposition"].endswith("filename*=UTF-8''app.py")
    exported = client.get(base + "/archive")
    assert exported.headers["content-type"] == "application/zip"
    with zipfile.ZipFile(io.BytesIO(exported.content)) as archive:
        assert archive.read("src/app.py").decode() == "print('你好')"
        assert archive.read("data.bin") == b"\x00\xff"
    store = client.app.state.manager.store
    reopened = AgentStore(store.root)
    assert reopened.workspaces()[0]["id"] == identifier
    second = import_project(client, {"other.txt": "separate"}).json()["workspace"]["id"]
    assert (
        client.get(f"/next/api/workspaces/{second}/file", params={"path": "src/app.py"}).status_code
        == 404
    )


@pytest.mark.parametrize(
    "path",
    [
        "../outside",
        "/tmp/outside",
        "a/../../outside",
        "a/./b",
        "a//b",
        "a\\b",
        "C:/outside",
        "a/../b",
        "a\nb",
    ],
)
def test_invalid_import_paths_leave_no_workspace(client, path):
    response = import_project(client, {path: "blocked"})
    assert response.status_code == 422
    assert client.get("/next/api/workspaces").json()["workspaces"] == []


def test_null_filenames_and_duplicate_members_are_rejected(client):
    raw = make_zip({"axb": "bad"}).replace(b"axb", b"a\x00b")
    response = client.post(
        "/next/api/workspaces/import",
        json={"name": "null", "data_base64": base64.b64encode(raw).decode()},
    )
    assert response.status_code == 422
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("same", "first")
        with pytest.warns(UserWarning, match="Duplicate"):
            archive.writestr("same", "second")
    response = client.post(
        "/next/api/workspaces/import",
        json={"name": "duplicate", "data_base64": base64.b64encode(output.getvalue()).decode()},
    )
    assert response.status_code == 422
    assert client.get("/next/api/workspaces").json()["workspaces"] == []


def test_archive_links_duplicates_collisions_corruption_and_limits(client):
    raw = io.BytesIO()
    with zipfile.ZipFile(raw, "w") as archive:
        link = zipfile.ZipInfo("escape")
        link.create_system = 3
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive.writestr(link, "/etc/passwd")
    response = client.post(
        "/next/api/workspaces/import",
        json={"name": "link", "data_base64": base64.b64encode(raw.getvalue()).decode()},
    )
    assert response.status_code == 422
    for files in [
        {"a": "file", "a/b": "conflict"},
        {".git/config": "metadata"},
        {},
        {f"file-{i}": "x" for i in range(1001)},
        {"bomb": b"0" * (17 * 1024 * 1024)},
    ]:
        assert import_project(client, files).status_code == 422
    assert import_project(client, data_base64="not base64!").status_code == 422
    assert (
        import_project(client, data_base64=base64.b64encode(b"not a zip").decode()).status_code
        == 422
    )
    assert import_project(client, name="   ").status_code == 422
    assert (
        import_project(
            client, data_base64=base64.b64encode(b"x" * (MAX_ARCHIVE_BYTES + 1)).decode()
        ).status_code
        == 422
    )
    assert client.get("/next/api/workspaces").json()["workspaces"] == []


@pytest.mark.parametrize("path", ["../secret", "/etc/passwd", "src/../README.md", "src\\app.py"])
def test_file_routes_reject_path_escape(client, path):
    identifier = import_project(client).json()["workspace"]["id"]
    for route in ["files", "file", "download"]:
        assert (
            client.get(
                f"/next/api/workspaces/{identifier}/{route}", params={"path": path}
            ).status_code
            == 422
        )


def test_file_and_parent_symlinks_fifo_and_preview_limits(client, tmp_path):
    identifier = import_project(client).json()["workspace"]["id"]
    store = client.app.state.manager.store
    directory = store.directory(identifier)
    secret = tmp_path / "secret"
    secret.write_text("host-private")
    (directory / "link").symlink_to(secret)
    (directory / "parent").symlink_to(tmp_path, target_is_directory=True)
    os.mkfifo(directory / "pipe")
    base = f"/next/api/workspaces/{identifier}"
    for path in ["link", "parent/secret", "pipe"]:
        assert client.get(base + "/download", params={"path": path}).status_code == 422
    assert client.get(base + "/files", params={"path": "parent"}).status_code == 422
    with zipfile.ZipFile(io.BytesIO(client.get(base + "/archive").content)) as archive:
        assert "link" not in archive.namelist() and "pipe" not in archive.namelist()
        assert "parent/secret" not in archive.namelist()
    (directory / "large.txt").write_bytes(b"x" * (256 * 1024 + 1))
    assert client.get(base + "/file", params={"path": "large.txt"}).json()["text"] is None
    with (directory / "huge.bin").open("wb") as stream:
        stream.truncate(17 * 1024 * 1024)
    assert client.get(base + "/download", params={"path": "huge.bin"}).status_code == 422
    assert client.get(base + "/archive").status_code == 422


def test_directory_swapped_to_link_during_open_cannot_escape(client, tmp_path):
    identifier = import_project(client).json()["workspace"]["id"]
    directory = client.app.state.manager.store.directory(identifier)
    (tmp_path / "app.py").write_text("private")
    original = os.open

    def swap(path, flags, *args, **kwargs):
        if path == "src":
            (directory / "src").rename(directory / "saved")
            (directory / "src").symlink_to(tmp_path, target_is_directory=True)
        return original(path, flags, *args, **kwargs)

    with patch("myself_agent.workspace_files.os.open", side_effect=swap):
        response = client.get(
            f"/next/api/workspaces/{identifier}/download", params={"path": "src/app.py"}
        )
    assert response.status_code == 422
    assert b"private" not in response.content


def test_failed_import_rolls_back_files_and_database(tmp_path):
    store = AgentStore(tmp_path)
    with patch("pathlib.Path.write_bytes", side_effect=OSError("full")):
        with pytest.raises(OSError):
            WorkspaceFiles(store).import_archive("rollback", make_zip({"file": "content"}))
    assert store.workspaces() == []
    assert list((tmp_path / "workspaces").iterdir()) == []


@pytest.mark.docker
def test_imported_project_is_used_by_real_tools_and_results_are_exported(tmp_path, settings):
    store = AgentStore(tmp_path)
    files = WorkspaceFiles(store)
    workspace = files.import_archive(
        "已有代码", make_zip({"app.py": "def answer():\n    return 41\n"})
    )["workspace"]
    session = store.create_session(workspace["id"], "修复现有项目")
    model = ScriptModel(
        responses=[
            call("read_file", {"file_path": "/workspace/app.py"}),
            call(
                "edit_file",
                {
                    "file_path": "/workspace/app.py",
                    "old_string": "return 41",
                    "new_string": "return 42",
                },
            ),
            call(
                "execute",
                {
                    "command": (
                        "python -c 'from app import answer; assert answer() == 42; "
                        'print("verified")\' > result.txt'
                    )
                },
            ),
            AIMessage(content="已修改导入项目并通过真实容器测试。"),
        ]
    )
    run = new_run(store, session)
    run_agent(store, run["id"], model, settings.image)
    assert files.read(workspace["id"], "result.txt") == b"verified\n"
    with zipfile.ZipFile(io.BytesIO(files.export(workspace["id"]))) as archive:
        assert b"return 42" in archive.read("app.py")
        assert archive.read("result.txt") == b"verified\n"
