#!/usr/bin/env bash
set -Eeuo pipefail

backup="${1:?usage: scripts/agent-hub restore BACKUP --target DIR}"
shift
[[ "${1:-}" == "--target" ]] || { echo "restore requires --target" >&2; exit 2; }
target="${2:?missing target}"
mkdir -p "$target"
tar -xzf "$backup" -C "$target"
echo "backup restored to $target"
