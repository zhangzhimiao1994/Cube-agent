#!/usr/bin/env bash
set -Eeuo pipefail

usage() {
  cat <<'EOF'
Usage: scripts/agent-hub real-user-four-scale-acceptance [options]

Run sequential small, medium, large, and ultra capability acceptance through
a logged-in user's HTTP path. The script creates an independent project and
conversation per scale, handles approvals through project_scale_runner, and
cross-checks public files, individual downloads, and the public workspace ZIP.

Required environment:
  AGENT_HUB_ACCEPTANCE_BEARER_TOKEN, or
  AGENT_HUB_ACCEPTANCE_USERNAME / AGENT_HUB_ACCEPTANCE_PASSWORD
  (LOGIN_USERNAME / LOGIN_PASSWORD aliases are also accepted.)

The Python script always emits a structured JSON report. Exit 2 means the core
matrix passed but dynamic website preview remains explicitly pending.
EOF
}

detect_python() {
  if [[ -n "${AGENT_HUB_ACCEPTANCE_PYTHON:-}" ]]; then
    printf '%s\n' "$AGENT_HUB_ACCEPTANCE_PYTHON"
    return 0
  fi
  if [[ -x "$SOURCE_DIR/.venv/bin/python" ]]; then
    printf '%s\n' "$SOURCE_DIR/.venv/bin/python"
    return 0
  fi
  if command -v python3 >/dev/null 2>&1; then
    command -v python3
    return 0
  fi
  if command -v python >/dev/null 2>&1; then
    command -v python
    return 0
  fi
  printf 'python is required for real-user four-scale acceptance\n' >&2
  return 1
}

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
SOURCE_DIR="$(cd -- "$SCRIPT_DIR/../.." && pwd -P)"

if [[ "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi

python_bin="$(detect_python)"
export PYTHONPATH="$SOURCE_DIR/src:${PYTHONPATH:-}"
exec "$python_bin" "$SOURCE_DIR/scripts/real_user_four_scale_acceptance.py" "$@"
