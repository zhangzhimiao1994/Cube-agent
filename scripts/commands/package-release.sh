#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
SOURCE_DIR="${AGENT_HUB_SOURCE_DIR:-$(cd -- "$SCRIPT_DIR/../.." && pwd)}"
output=""

usage() {
  cat <<'EOF'
Usage: scripts/agent-hub package-release --output file.tar

Builds the Web UI and creates a deployable source archive that includes web/dist.
EOF
}

while (($#)); do
  case "$1" in
    --output)
      output="${2:?missing output}"
      shift 2
      ;;
    --help)
      usage
      exit 0
      ;;
    *)
      printf 'unknown argument: %s\n' "$1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if [[ -z "$output" ]]; then
  usage >&2
  exit 2
fi

npm --prefix "$SOURCE_DIR/web" run build

if ! [[ -f "$SOURCE_DIR/web/dist/index.html" ]]; then
  printf 'web build did not produce web/dist/index.html\n' >&2
  exit 1
fi

mkdir -p "$(dirname -- "$output")"
tar -cf "$output" \
  --exclude='./.git' \
  --exclude='./.venv' \
  --exclude='./.litellm-venv' \
  --exclude='./.worktrees' \
  --exclude='./.tmp' \
  --exclude='./.pytest_cache' \
  --exclude='./.mypy_cache' \
  --exclude='./.ruff_cache' \
  --exclude='./.uv-cache' \
  --exclude='./node_modules' \
  --exclude='./web/node_modules' \
  --exclude='./web/test-results' \
  -C "$SOURCE_DIR" .
