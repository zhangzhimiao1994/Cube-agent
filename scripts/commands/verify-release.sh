#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/lib/common.sh
# shellcheck disable=SC1091
source "$SCRIPT_DIR/../lib/common.sh"

install_root="${AGENT_HUB_INSTALL_ROOT:-/opt/agent-hub}"
expect_revision="${AGENT_HUB_EXPECT_REVISION:-}"
check_services=1

usage() {
  cat <<'EOF'
Usage: scripts/agent-hub verify-release [--install-root dir] [--expect-revision sha] [--skip-services]

Verifies the native current release pointer, REVISION file, and systemd service
state when systemctl is available.
EOF
}

while (($#)); do
  case "$1" in
    --install-root)
      install_root="${2:?missing install root}"
      shift 2
      ;;
    --expect-revision)
      expect_revision="${2:?missing revision}"
      shift 2
      ;;
    --skip-services)
      check_services=0
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

release_dir="$install_root/releases"
current_link="$install_root/current"

if [[ ! -d "$release_dir" ]]; then
  die "release directory does not exist: $release_dir"
fi

if [[ ! -e "$current_link" && ! -L "$current_link" ]]; then
  die "current release link does not exist: $current_link"
fi

release_dir_real="$(cd -- "$release_dir" && pwd -P)"
current_real="$(readlink -f -- "$current_link")"

case "$current_real" in
  "$release_dir_real"/*)
    ;;
  *)
    die "current must point inside release directory: $current_link -> $current_real"
    ;;
esac

if [[ ! -d "$current_real" ]]; then
  die "current release target is not a directory: $current_real"
fi

if [[ ! -f "$current_real/REVISION" ]]; then
  die "current release REVISION file is missing: $current_real/REVISION"
fi

revision="$(tr -d '\r\n' < "$current_real/REVISION")"
if [[ -z "$revision" ]]; then
  die "current release REVISION file is empty: $current_real/REVISION"
fi

if [[ -n "$expect_revision" && "$revision" != "$expect_revision" ]]; then
  die "current release revision mismatch: expected $expect_revision got $revision"
fi

printf 'ok: current release %s revision=%s\n' "$current_real" "$revision"

if [[ "$check_services" -eq 1 ]]; then
  if command -v systemctl >/dev/null 2>&1; then
    for service in agent-hub-api.service agent-hub-worker.service agent-hub-litellm.service; do
      if systemctl is-active --quiet "$service"; then
        printf 'ok: service active %s\n' "$service"
      else
        die "service is not active: $service"
      fi
    done
  else
    printf 'skip: systemctl unavailable\n'
  fi
fi
