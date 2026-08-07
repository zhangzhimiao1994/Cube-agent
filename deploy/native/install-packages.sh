#!/usr/bin/env bash
set -Eeuo pipefail

usage() {
  cat <<'EOF'
Usage:
  deploy/native/install-packages.sh [--detect OS_RELEASE] [--dry-run] [--local-db] [--local-redis]
EOF
}

detect_manager() {
  local os_release="${1:-/etc/os-release}"
  [[ -r "$os_release" ]] || { echo "unsupported: missing os-release" >&2; return 1; }
  # shellcheck disable=SC1090
  source "$os_release"
  case "${ID:-}" in
    ubuntu|debian)
      case "${VERSION_ID:-}" in
        22.04|24.04|12|13) echo "apt" ;;
        *) echo "unsupported: ${ID:-unknown} ${VERSION_ID:-unknown}" >&2; return 1 ;;
      esac
      ;;
    rocky|almalinux)
      case "${VERSION_ID%%.*}" in
        9) echo "dnf" ;;
        *) echo "unsupported: ${ID:-unknown} ${VERSION_ID:-unknown}" >&2; return 1 ;;
      esac
      ;;
    *)
      echo "unsupported: ${ID:-unknown}; use Docker mode for broad Linux compatibility" >&2
      return 1
      ;;
  esac
}

DRY_RUN=0
DETECT_ONLY=""
LOCAL_DB=0
LOCAL_REDIS=0
while (($#)); do
  case "$1" in
    --detect) DETECT_ONLY="${2:?missing os-release path}"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    --local-db) LOCAL_DB=1; shift ;;
    --local-redis) LOCAL_REDIS=1; shift ;;
    --help) usage; exit 0 ;;
    *) echo "unknown argument: $1" >&2; usage; exit 2 ;;
  esac
done

if [[ -n "$DETECT_ONLY" ]]; then
  detect_manager "$DETECT_ONLY"
  exit 0
fi

manager="$(detect_manager /etc/os-release)"
packages=(ca-certificates curl openssl tar gzip coreutils)
if [[ "$LOCAL_DB" -eq 1 ]]; then
  packages+=(postgresql postgresql-client)
else
  packages+=(postgresql-client)
fi
if [[ "$LOCAL_REDIS" -eq 1 ]]; then
  packages+=(redis)
fi
packages+=(caddy)

if [[ "$DRY_RUN" -eq 1 ]]; then
  printf 'manager=%s packages=%s\n' "$manager" "${packages[*]}"
  exit 0
fi

if [[ "$manager" == "apt" ]]; then
  apt-get update
  DEBIAN_FRONTEND=noninteractive apt-get install -y "${packages[@]}"
elif [[ "$manager" == "dnf" ]]; then
  dnf install -y "${packages[@]}"
fi
