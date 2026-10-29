#!/usr/bin/env bash
set -euo pipefail

ROOT="$(git -C "$(dirname "$0")/../.." rev-parse --show-toplevel)"
APP="$ROOT/competition"
PYTHON="${MYSELF_AGENT_PYTHON:-/home/dsh/projects/myself-agent-research/venv/bin/python}"

if [[ ! -x "$PYTHON" ]]; then
  printf 'Home project Python is unavailable: %s\n' "$PYTHON" >&2
  printf 'Set MYSELF_AGENT_PYTHON to the home project interpreter.\n' >&2
  exit 2
fi

"$PYTHON" -m ruff --config "$APP/pyproject.toml" check "$APP/src" "$APP/tests"
"$PYTHON" -m ruff --config "$APP/pyproject.toml" format --check "$APP/src" "$APP/tests"
"$PYTHON" -m pytest -c "$APP/pyproject.toml" -q "$APP/tests"

if command -v node >/dev/null 2>&1; then
  node --check "$APP/src/myself_agent/static/chat.js"
  node --check "$APP/src/myself_agent/static/app.js"
else
  printf 'Node.js is required for static JavaScript syntax checks.\n' >&2
  exit 2
fi

if [[ -f "$APP/frontend/package.json" ]]; then
  npm --prefix "$APP/frontend" run build
fi
