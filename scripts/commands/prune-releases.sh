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

protect_release_path() {
  local release_path="$1"
  local reason="$2"
  local release_relative
  local release_name
  local release_real

  case "$release_path" in
    "$release_dir_real"/*)
      ;;
    *)
      return 0
      ;;
  esac

  release_relative="${release_path#"$release_dir_real"/}"
  release_name="${release_relative%%/*}"
  release_real="$release_dir_real/$release_name"
  if [[ -d "$release_real" && -z "${protected[$release_name]:-}" ]]; then
    protected["$release_name"]="$reason"
  fi
}

protect_runtime_release_chain() {
  local runtime_name="$1"
  local runtime_path="$2"
  local runtime_cursor="$runtime_path"
  local link_target
  local link_target_dir
  local link_target_base
  local link_target_parent_real
  local link_relative
  local link_release_name
  local link_release_real
  local hop=0

  while [[ -L "$runtime_cursor" ]]; do
    ((hop += 1))
    ((hop <= 16)) || return 0

    link_target="$(readlink -- "$runtime_cursor")" || return 0
    case "$link_target" in
      /*)
        ;;
      *)
        link_target="$(dirname -- "$runtime_cursor")/$link_target"
        ;;
    esac

    link_target_dir="$(dirname -- "$link_target")"
    link_target_base="$(basename -- "$link_target")"
    link_target_parent_real="$(cd -P -- "$link_target_dir" 2>/dev/null && pwd -P)" || return 0
    link_target="$link_target_parent_real/$link_target_base"
    case "$link_target" in
      "$release_dir_real"/*)
        ;;
      *)
        return 0
        ;;
    esac

    link_relative="${link_target#"$release_dir_real"/}"
    link_release_name="${link_relative%%/*}"
    link_release_real="$release_dir_real/$link_release_name"
    protect_release_path "$link_release_real" "current-runtime-link:$runtime_name"
    runtime_cursor="$link_target"
  done
}

protect_runtime_release() {
  local runtime_name="$1"
  local runtime_path="$current_real/$runtime_name"
  local runtime_real
  local runtime_relative
  local runtime_release_name
  local runtime_release_real
  local runtime_release_reason

  if [[ ! -e "$runtime_path" && ! -L "$runtime_path" ]]; then
    return 0
  fi

  protect_runtime_release_chain "$runtime_name" "$runtime_path"

  runtime_real="$(readlink -f -- "$runtime_path")"
  case "$runtime_real" in
    "$release_dir_real"/*)
      ;;
    *)
      return 0
      ;;
  esac

  runtime_relative="${runtime_real#"$release_dir_real"/}"
  runtime_release_name="${runtime_relative%%/*}"
  runtime_release_real="$release_dir_real/$runtime_release_name"
  runtime_release_reason="${protected[$runtime_release_name]:-}"
  if [[ -d "$runtime_release_real" && ( -z "$runtime_release_reason" || "$runtime_release_reason" == current-runtime-link:* ) ]]; then
    protected["$(basename -- "$runtime_release_real")"]="current-runtime:$runtime_name"
  fi
}

protect_runtime_release ".venv"
protect_runtime_release ".litellm-venv"

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
