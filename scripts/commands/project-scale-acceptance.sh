#!/usr/bin/env bash
set -Eeuo pipefail

usage() {
  cat <<'EOF'
Usage: scripts/agent-hub project-scale-acceptance [options]

Build or execute the project-scale acceptance fixture plan.

Options are passed through to:
  python -m agent_hub.harness.project_scale_runner

Common options:
  --scale small|medium|large|ultra     Limit to one scale; repeatable.
  --flow direct|dispatch|hybrid|multi_agent|plugin|model_failure|self_repair|artifact_production
                                       Limit to one flow; repeatable.
  --json                               Print machine-readable JSON.
  --execute                            Execute real server probes; requires AGENT_HUB_ACCEPTANCE_BEARER_TOKEN.
  --wait-seconds SECONDS               Wait for terminal run status before evidence checks.
  --poll-interval SECONDS              Poll interval while waiting for terminal run status.
  --help                               Show Python runner help.
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
  printf 'python is required for project-scale acceptance\n' >&2
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
exec "$python_bin" -m agent_hub.harness.project_scale_runner "$@"
