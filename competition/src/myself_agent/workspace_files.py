"""Bounded project imports and descriptor-relative workspace file access."""

import io
import os
import stat
import zipfile
import zlib
from contextlib import contextmanager

MAX_ARCHIVE_BYTES = 8 * 1024 * 1024
MAX_FILE_BYTES = 16 * 1024 * 1024
MAX_TOTAL_BYTES = 32 * 1024 * 1024
MAX_ENTRIES = 1000
PREVIEW_BYTES = 256 * 1024


class WorkspaceFileError(ValueError):
    pass


def path_parts(path, *, root=False):
    if root and path == "":
        return []
    parts = path.split("/")
    try:
        encoded_size = len(path.encode("utf-8"))
    except UnicodeError:
        raise WorkspaceFileError("文件路径含无效字符") from None
    if (
        not path
        or encoded_size > 1024
        or len(parts) > 20
        or "\\" in path
        or ":" in path
        or any(ord(c) < 32 or ord(c) == 127 for c in path)
        or any(p in ("", ".", "..") or len(p.encode("utf-8")) > 255 for p in parts)
    ):
        raise WorkspaceFileError("请使用工作区内的相对路径")
    return parts


def archive_files(data):
    if len(data) > MAX_ARCHIVE_BYTES:
        raise WorkspaceFileError("ZIP 文件不能超过 8 MiB")
    result = {}
    total = 0
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            entries = archive.infolist()
            if len(entries) > MAX_ENTRIES:
                raise WorkspaceFileError("ZIP 内最多包含 1000 个文件或目录")
            for item in entries:
                path = item.filename[:-1] if item.is_dir() else item.filename
                parts = path_parts(path)
                if item.orig_filename != item.filename:
                    raise WorkspaceFileError("ZIP 文件名含无效字符")
                kind = stat.S_IFMT(item.external_attr >> 16)
                if kind not in (0, stat.S_IFREG, stat.S_IFDIR) or item.flag_bits & 1:
                    raise WorkspaceFileError("ZIP 不支持链接、特殊文件或加密内容")
                if ".git" in parts:
                    raise WorkspaceFileError("请移除 ZIP 中的 .git 目录后再导入")
                if item.is_dir():
                    continue
                if path in result:
                    raise WorkspaceFileError("ZIP 中存在重复文件路径")
                total += item.file_size
                if item.file_size > MAX_FILE_BYTES or total > MAX_TOTAL_BYTES:
                    raise WorkspaceFileError("解压后单文件最多 16 MiB，合计最多 32 MiB")
                with archive.open(item) as stream:
                    content = stream.read(MAX_FILE_BYTES + 1)
                if len(content) != item.file_size:
                    raise WorkspaceFileError("ZIP 文件大小校验失败")
                result[path] = content
    except (
        zipfile.BadZipFile,
        NotImplementedError,
        RuntimeError,
        OSError,
        EOFError,
        UnicodeError,
        zlib.error,
    ) as error:
        raise WorkspaceFileError("无法读取 ZIP，请使用未加密的普通 ZIP 文件") from error
    if not result:
        raise WorkspaceFileError("ZIP 中没有可导入的普通文件")
    for path in result:
        parts = path.split("/")
        if any("/".join(parts[:i]) in result for i in range(1, len(parts))):
            raise WorkspaceFileError("ZIP 中的文件和目录路径冲突")
    return result


class WorkspaceFiles:
    def __init__(self, store):
        self.store = store

    def import_archive(self, name, data):
        if not name.strip():
            raise WorkspaceFileError("请输入工作区名称")
        files = archive_files(data)
        workspace = self.store.create_workspace(name.strip(), files=files)
        return {
            "workspace": workspace,
            "file_count": len(files),
            "total_bytes": sum(map(len, files.values())),
        }

    @contextmanager
    def opened(self, workspace_id, path="", *, directory=False):
        parts = path_parts(path, root=directory)
        workspace = self.store.directory(workspace_id)
        descriptors = []
        try:
            descriptor = os.open(workspace, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            descriptors.append(descriptor)
            for index, part in enumerate(parts):
                flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
                if index < len(parts) - 1 or directory:
                    flags |= os.O_DIRECTORY
                descriptor = os.open(part, flags, dir_fd=descriptor)
                descriptors.append(descriptor)
            mode = os.fstat(descriptor).st_mode
            if not (stat.S_ISDIR(mode) if directory else stat.S_ISREG(mode)):
                raise WorkspaceFileError("仅支持工作区内的普通文件与目录")
            yield descriptor
        except FileNotFoundError:
            raise KeyError(path) from None
        except OSError as error:
            raise WorkspaceFileError("无法访问该路径，不支持链接或特殊文件") from error
        finally:
            for descriptor in reversed(descriptors):
                os.close(descriptor)

    @staticmethod
    def entries(descriptor):
        result = []
        with os.scandir(descriptor) as entries:
            for entry in entries:
                if len(result) >= MAX_ENTRIES:
                    raise WorkspaceFileError("单个目录最多显示 1000 项，请缩小工作区范围")
                try:
                    info = entry.stat(follow_symlinks=False)
                except FileNotFoundError:
                    continue
                kind = (
                    "directory"
                    if stat.S_ISDIR(info.st_mode)
                    else "file"
                    if stat.S_ISREG(info.st_mode)
                    else "blocked"
                )
                result.append(
                    {
                        "name": entry.name,
                        "kind": kind,
                        "size": info.st_size,
                        "modified": info.st_mtime,
                    }
                )
        return sorted(result, key=lambda entry: (entry["kind"] != "directory", entry["name"]))

    def listing(self, workspace_id, path=""):
        with self.opened(workspace_id, path, directory=True) as descriptor:
            entries = self.entries(descriptor)
        return {"path": path, "entries": entries}

    def read(self, workspace_id, path, *, preview=False):
        with self.opened(workspace_id, path) as descriptor:
            size = os.fstat(descriptor).st_size
            if preview and size > PREVIEW_BYTES:
                return {
                    "path": path,
                    "size": size,
                    "text": None,
                    "reason": "文件超过 256 KiB，请下载查看",
                }
            if size > MAX_FILE_BYTES:
                raise WorkspaceFileError("单文件下载上限为 16 MiB")
            with os.fdopen(os.dup(descriptor), "rb") as stream:
                data = stream.read((PREVIEW_BYTES if preview else MAX_FILE_BYTES) + 1)
        if len(data) > (PREVIEW_BYTES if preview else MAX_FILE_BYTES):
            raise WorkspaceFileError("文件正在变化或超过读取上限，请稍后重试")
        if not preview:
            return data
        try:
            text = data.decode("utf-8")
            if "\x00" in text:
                raise UnicodeError
        except UnicodeError:
            return {
                "path": path,
                "size": len(data),
                "text": None,
                "reason": "此文件不支持文本预览，请下载查看",
            }
        return {"path": path, "size": len(data), "text": text, "reason": None}

    def export(self, workspace_id):
        output = io.BytesIO()
        pending = [""]
        total = 0
        count = 0
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            while pending:
                path = pending.pop()
                for entry in self.listing(workspace_id, path)["entries"]:
                    count += 1
                    if count > MAX_ENTRIES:
                        raise WorkspaceFileError("工作区超过 1000 项，请单独下载所需文件")
                    relative = path + "/" + entry["name"] if path else entry["name"]
                    if entry["kind"] == "directory":
                        if relative.count("/") >= 20:
                            raise WorkspaceFileError("目录层级过深，请单独下载所需文件")
                        pending.append(relative)
                        archive.writestr(relative + "/", b"")
                    elif entry["kind"] == "file":
                        if total + entry["size"] > MAX_TOTAL_BYTES:
                            raise WorkspaceFileError("工作区超过 32 MiB，请单独下载所需文件")
                        data = self.read(workspace_id, relative)
                        total += len(data)
                        if total > MAX_TOTAL_BYTES:
                            raise WorkspaceFileError("工作区超过 32 MiB，请单独下载所需文件")
                        archive.writestr(relative, data)
        if output.tell() > MAX_TOTAL_BYTES:
            raise WorkspaceFileError("导出的 ZIP 超过 32 MiB，请单独下载所需文件")
        return output.getvalue()
