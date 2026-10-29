"""Read approved source snapshots and compute real line changes without executing code."""

import difflib
import json


def file_versions(directory, scenario):
    result = {}
    for filename in scenario.dependencies:
        versions = []
        for number in range(4):
            content = None
            response = directory / f"{filename[:-3]}_developer-round-{number}.json"
            if response.is_file() and not response.is_symlink():
                data = json.loads(response.read_text(encoding="utf-8"))
                if data.get("filename") == filename and isinstance(data.get("content"), str):
                    content = data["content"][:24_000]
            source = directory / f"round-{number}" / "workspace" / filename
            if source.is_file() and not source.is_symlink():
                content = source.read_text(encoding="utf-8")[:24_000]
            if content is not None and (not versions or content != versions[-1]["content"]):
                versions.append({"round": number, "content": content})
        if versions:
            result[filename] = versions
    return result


def changes(versions, baseline):
    files = []
    for filename, snapshots in versions.items():
        previous = baseline.get(filename, "")
        comparisons = []
        for snapshot in snapshots:
            old_lines = previous.splitlines()
            new_lines = snapshot["content"].splitlines()
            operations = difflib.SequenceMatcher(a=old_lines, b=new_lines).get_opcodes()
            added = sum(j2 - j1 for op, _, _, j1, j2 in operations if op in {"insert", "replace"})
            removed = sum(i2 - i1 for op, i1, i2, _, _ in operations if op in {"delete", "replace"})
            comparisons.append(
                {
                    **snapshot,
                    "added": added,
                    "removed": removed,
                    "diff": list(
                        difflib.unified_diff(
                            old_lines,
                            new_lines,
                            fromfile="before/" + filename,
                            tofile="after/" + filename,
                            lineterm="",
                        )
                    ),
                }
            )
            previous = snapshot["content"]
        files.append(
            {
                "filename": filename,
                "versions": comparisons,
                "based_on_previous_task": filename in baseline,
            }
        )
    return files
