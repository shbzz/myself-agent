#!/usr/bin/env python3
"""Check staged files or a Git revision for common credentials and private files."""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import PurePosixPath

PATTERNS = (
    ("private-key", re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----")),
    ("github-token", re.compile(rb"(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})")),
    ("aws-access-key", re.compile(rb"AKIA[0-9A-Z]{16}")),
    ("provider-key", re.compile(rb"sk-[A-Za-z0-9]{24,}")),
    (
        "credential-header",
        re.compile(rb"(?im)^\s*authorization\s*:\s*(?:basic|bearer)\s+\S+"),
    ),
    (
        "credential-assignment",
        re.compile(
            rb"(?im)^\s*(?:DEEPSEEK_API_KEY|OPENAI_API_KEY|ANTHROPIC_API_KEY|"
            rb"GITHUB_TOKEN|ALIYUN_ACCESS_KEY_SECRET|AWS_SECRET_ACCESS_KEY)\s*[:=]\s*"
            rb"(?!\$\{|<|your_|placeholder|test-placeholder)[^\s#]{12,}"
        ),
    ),
)
PRIVATE_NAMES = {"id_rsa", "id_ed25519", "credentials.json", "web-login.json"}


def git_bytes(*args: str) -> bytes:
    return subprocess.check_output(["git", *args])


def list_paths(revision: str | None) -> list[str]:
    if revision:
        raw = git_bytes("ls-tree", "-r", "-z", "--name-only", revision)
    else:
        raw = git_bytes("diff", "--cached", "--diff-filter=ACMR", "--name-only", "-z")
    return [os.fsdecode(path) for path in raw.split(b"\0") if path]


def contents(path: str, revision: str | None) -> bytes:
    if revision:
        return git_bytes("cat-file", "blob", f"{revision}:{path}")
    return git_bytes("show", f":{path}")


def private_path(path: str) -> bool:
    name = PurePosixPath(path).name.lower()
    return (
        name in PRIVATE_NAMES
        or name == ".env"
        or (name.startswith(".env.") and name != ".env.example")
        or name.endswith((".pem", ".p12", ".pfx"))
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--revision", help="scan blobs from a Git revision instead of the index")
    parser.add_argument("--path", action="append", default=[], help="limit revision scan to a path")
    args = parser.parse_args()

    paths = list_paths(args.revision)
    if args.path:
        paths = [path for path in paths if any(path == prefix or path.startswith(prefix + "/") for prefix in args.path)]
    problems: list[tuple[str, str, int]] = []
    for path in paths:
        if private_path(path):
            problems.append((path, "private-file", 1))
            continue
        data = contents(path, args.revision)
        if b"\0" in data:
            continue
        for label, pattern in PATTERNS:
            for match in pattern.finditer(data):
                line = data.count(b"\n", 0, match.start()) + 1
                problems.append((path, label, line))

    if problems:
        for path, label, line in problems:
            print(f"credential hygiene: {label} in {path}:{line}", file=sys.stderr)
        return 1
    print(f"Credential hygiene passed ({len(paths)} files scanned).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
