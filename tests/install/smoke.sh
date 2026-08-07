#!/usr/bin/env bash
set -Eeuo pipefail

MODE="docker"
DRY_RUN=0
while (($#)); do
  case "$1" in
    --mode) MODE="${2:?missing mode}"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

root="$(mktemp -d)"
trap 'rm -rf "$root"' EXIT
export AGENT_HUB_TEST=1
export AGENT_HUB_INSTALL_ROOT="$root/opt"
export AGENT_HUB_STATE_DIR="$root/state"
export AGENT_HUB_CONFIG_DIR="$root/etc"
if [[ "$DRY_RUN" -eq 1 ]]; then
  bash install.sh --mode "$MODE" --dry-run --yes
else
  bash install.sh --mode "$MODE" --yes
fi
echo "AGENT_HUB_SMOKE_OK"
