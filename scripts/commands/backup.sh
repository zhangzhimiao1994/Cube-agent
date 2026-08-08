#!/usr/bin/env bash
set -Eeuo pipefail

STATE_DIR="${AGENT_HUB_STATE_DIR:-/var/lib/agent-hub}"
mode="${1:-create}"
output="${2:-agent-hub-backup.tar.gz}"

if [[ "$mode" == "verify" ]]; then
  tar -tzf "$output" >/dev/null
  echo "backup verified"
  exit 0
fi

if [[ "$mode" != "create" ]]; then
  output="$mode"
fi

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
mkdir -p "$tmp/payload"
cp -R "$STATE_DIR" "$tmp/payload/state" 2>/dev/null || true
find "$tmp/payload" -type f -print0 | sort -z | xargs -0 sha256sum > "$tmp/manifest.sha256" 2>/dev/null || true
tar -czf "$output" -C "$tmp" payload manifest.sha256
echo "backup written: $output"
