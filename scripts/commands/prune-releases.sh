#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/lib/common.sh
# shellcheck disable=SC1091
source "$SCRIPT_DIR/../lib/common.sh"

keep="${AGENT_HUB_RELEASES_TO_KEEP:-2}"
execute=0
install_root="${AGENT_HUB_INSTALL_ROOT:-/opt/agent-hub}"

usage() {
  cat <<'EOF'
Usage: scripts/agent-hub prune-releases [--install-root dir] [--keep count] [--execute]

Previews old native release directories by default. Pass --execute to remove
releases older than the newest count while always protecting the active current
release target.
EOF
}

while (($#)); do
  case "$1" in
    --keep)
      keep="${2:?missing keep count}"
      shift 2
      ;;
    --install-root)
      install_root="${2:?missing install root}"
      shift 2
      ;;
    --execute)
      execute=1
      shift
      ;;
    --yes)
      execute=1
      shift
      ;;
    --dry-run)
      execute=0
      shift
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

if ! [[ "$keep" =~ ^[0-9]+$ ]] || [[ "$keep" -lt 1 ]]; then
  die "--keep must be a positive integer"
fi

release_dir="$install_root/releases"
current_link="$install_root/current"

if [[ ! -d "$release_dir" ]]; then
  die "release directory does not exist: $release_dir"
fi

release_dir_real="$(cd -- "$release_dir" && pwd -P)"

if [[ ! -e "$current_link" && ! -L "$current_link" ]]; then
  die "current release link does not exist: $current_link"
fi

current_real="$(readlink -f -- "$current_link")"
case "$current_real" in
  "$release_dir_real"/*)
    ;;
  *)
    die "current must point inside release directory: $current_link -> $current_real"
    ;;
esac

mapfile -t releases < <(find "$release_dir_real" -mindepth 1 -maxdepth 1 -type d -printf '%f\n' | sort -r)

printf 'mode=%s keep=%s release_dir=%s current=%s\n' \
  "$([[ "$execute" -eq 1 ]] && printf 'execute' || printf 'dry-run')" \
  "$keep" \
  "$release_dir_real" \
  "$current_real"

declare -A protected=()
protected["$(basename -- "$current_real")"]="current"

recent_count=0
for release_name in "${releases[@]}"; do
  if [[ "$recent_count" -ge "$keep" ]]; then
    continue
  fi
  if [[ -n "${protected[$release_name]:-}" ]]; then
    continue
  fi
  protected["$release_name"]="recent"
  recent_count=$((recent_count + 1))
done

for release_name in "${releases[@]}"; do
  release_path="$release_dir_real/$release_name"
  reason="${protected[$release_name]:-}"
  if [[ -n "$reason" ]]; then
    printf 'keep %s reason=%s\n' "$release_path" "$reason"
    continue
  fi
  if [[ "$execute" -eq 1 ]]; then
    rm -rf -- "$release_path"
    printf 'removed %s reason=older\n' "$release_path"
  else
    printf 'remove %s reason=older\n' "$release_path"
  fi
done
