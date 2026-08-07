#!/usr/bin/env bash
set -Eeuo pipefail

version="unknown"
while (($#)); do
  case "$1" in
    --version) version="${2:?missing version}"; shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

STATE_DIR="${AGENT_HUB_STATE_DIR:-/var/lib/agent-hub}"
mkdir -p "$STATE_DIR"
old_version="$(cat "$STATE_DIR/version" 2>/dev/null || echo 0.1.0)"
backup="$STATE_DIR/pre-upgrade-$old_version.tar.gz"
"$(dirname "$0")/backup.sh" create "$backup"
printf '%s\n' "$version" > "$STATE_DIR/version"
if [[ "${AGENT_HUB_FAKE_NEW_VERSION_UNHEALTHY:-0}" == "1" ]]; then
  printf '%s\n' "$old_version" > "$STATE_DIR/version"
  echo "upgrade failed readiness; rolled back to $old_version" >&2
  exit 1
fi
echo "upgraded to $version"
