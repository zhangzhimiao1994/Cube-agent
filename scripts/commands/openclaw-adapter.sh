#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
SOURCE_DIR="${AGENT_HUB_SOURCE_DIR:-$(cd -- "$SCRIPT_DIR/../.." && pwd)}"
PYTHON_BIN="${AGENT_HUB_PYTHON:-$SOURCE_DIR/.venv/bin/python}"

usage() {
  cat <<'EOF'
Usage: scripts/agent-hub openclaw-adapter

Starts a local OpenClaw Adapter for a Linux, Windows, or macOS host.

Required environment:
  OPENCLAW_ADAPTER_TOKEN                  Bearer token configured as a sealed secret ref in Agent Hub.
  OPENCLAW_ADAPTER_ALLOWED_COMMANDS_JSON  JSON array of exact argv arrays allowed on this machine.

Optional environment:
  OPENCLAW_ADAPTER_PLATFORM               linux, windows, or macos. Defaults to the current OS.
  OPENCLAW_ADAPTER_HOST                   Bind host. Defaults to 127.0.0.1.
  OPENCLAW_ADAPTER_PORT                   Bind port. Defaults to 8765.
  OPENCLAW_ADAPTER_COMMAND_TIMEOUT_SECONDS Command timeout. Defaults to 15.
  OPENCLAW_ADAPTER_ALLOWED_FILE_ROOTS_JSON JSON array of absolute roots allowed for file_read.
  OPENCLAW_ADAPTER_FILE_READ_LIMIT_BYTES   Maximum bytes returned by file_read. Defaults to 64000.
EOF
}

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
  usage
  exit 0
fi

if [[ -z "${OPENCLAW_ADAPTER_TOKEN:-}" ]]; then
  usage >&2
  printf '\n[agent-hub] error: OPENCLAW_ADAPTER_TOKEN is required\n' >&2
  exit 2
fi

if [[ -z "${OPENCLAW_ADAPTER_ALLOWED_COMMANDS_JSON:-}" ]]; then
  usage >&2
  printf '\n[agent-hub] error: OPENCLAW_ADAPTER_ALLOWED_COMMANDS_JSON is required\n' >&2
  exit 2
fi

if [[ ! -x "$PYTHON_BIN" ]]; then
  if command -v python3 >/dev/null 2>&1; then
    PYTHON_BIN="$(command -v python3)"
  elif command -v python >/dev/null 2>&1; then
    PYTHON_BIN="$(command -v python)"
  else
    printf '[agent-hub] error: Python runtime not found\n' >&2
    exit 2
  fi
fi

if [[ -n "${PYTHONPATH:-}" ]]; then
  export PYTHONPATH="$SOURCE_DIR/src:$PYTHONPATH"
else
  export PYTHONPATH="$SOURCE_DIR/src"
fi
exec "$PYTHON_BIN" -m agent_hub.openclaw.local_adapter