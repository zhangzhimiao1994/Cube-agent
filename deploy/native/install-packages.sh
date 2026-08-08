#!/usr/bin/env bash
set -Eeuo pipefail

usage() {
  cat <<'EOF'
Usage:
  deploy/native/install-packages.sh [--detect OS_RELEASE] [--dry-run] [--local-db] [--local-redis]

Environment:
  AGENT_HUB_MIRROR_MODE=auto|official|china
    auto: try official package sources first, then switch to China mirrors on failure.
    official: never rewrite package sources.
    china: configure China mirrors before installing packages.
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
packages=(ca-certificates curl openssl tar gzip coreutils nodejs npm)
if [[ "$manager" == "apt" ]]; then
  packages+=(python3 python3-venv python3-pip build-essential)
  if [[ "$LOCAL_DB" -eq 1 ]]; then
    packages+=(postgresql postgresql-client)
  else
    packages+=(postgresql-client)
  fi
  if [[ "$LOCAL_REDIS" -eq 1 ]]; then
    packages+=(redis-server)
  fi
  packages+=(caddy)
elif [[ "$manager" == "dnf" ]]; then
  packages+=(python3 python3-pip gcc gcc-c++ make)
  if [[ "$LOCAL_DB" -eq 1 ]]; then
    packages+=(postgresql-server postgresql)
  else
    packages+=(postgresql)
  fi
  if [[ "$LOCAL_REDIS" -eq 1 ]]; then
    packages+=(redis)
  fi
  packages+=(caddy)
fi

# Python 3.12 is installed by uv in scripts/lib/install_native.sh when the host
# Python is older than the application runtime requirement.

mirror_mode="${AGENT_HUB_MIRROR_MODE:-auto}"
[[ "$mirror_mode" == "auto" || "$mirror_mode" == "official" || "$mirror_mode" == "china" ]] || {
  echo "unsupported AGENT_HUB_MIRROR_MODE=$mirror_mode" >&2
  exit 2
}

configure_china_package_mirror() {
  if [[ "$manager" == "apt" ]]; then
    local apt_files=()
    [[ -f /etc/apt/sources.list ]] && apt_files+=(/etc/apt/sources.list)
    if compgen -G "/etc/apt/sources.list.d/*.list" >/dev/null; then
      apt_files+=(/etc/apt/sources.list.d/*.list)
    fi
    if compgen -G "/etc/apt/sources.list.d/*.sources" >/dev/null; then
      apt_files+=(/etc/apt/sources.list.d/*.sources)
    fi
    for file in "${apt_files[@]}"; do
      cp -n "$file" "$file.agent-hub.bak" 2>/dev/null || true
      sed -i \
        -e 's#https\?://archive.ubuntu.com/ubuntu#https://mirrors.aliyun.com/ubuntu#g' \
        -e 's#https\?://security.ubuntu.com/ubuntu#https://mirrors.aliyun.com/ubuntu#g' \
        -e 's#https\?://deb.debian.org/debian#https://mirrors.aliyun.com/debian#g' \
        -e 's#https\?://security.debian.org/debian-security#https://mirrors.aliyun.com/debian-security#g' \
        "$file"
    done
  elif [[ "$manager" == "dnf" ]]; then
    for file in /etc/yum.repos.d/*.repo; do
      [[ -f "$file" ]] || continue
      cp -n "$file" "$file.agent-hub.bak" 2>/dev/null || true
      # Keep dnf's $contentdir literal in repo URLs.
      # shellcheck disable=SC2016
      sed -i \
        -e 's#https\?://download.rockylinux.org/\$contentdir#https://mirrors.aliyun.com/rockylinux#g' \
        -e 's#https\?://repo.almalinux.org/almalinux#https://mirrors.aliyun.com/almalinux#g' \
        "$file"
    done
  fi
}

run_package_install() {
  if [[ "$manager" == "apt" ]]; then
    apt-get update
    DEBIAN_FRONTEND=noninteractive apt-get install -y "${packages[@]}"
  elif [[ "$manager" == "dnf" ]]; then
    dnf install -y "${packages[@]}"
  fi
}

install_with_mirror_fallback() {
  if [[ "$mirror_mode" == "china" ]]; then
    configure_china_package_mirror
    run_package_install
    return
  fi

  if run_package_install; then
    return
  fi

  if [[ "$mirror_mode" == "official" ]]; then
    return 1
  fi

  echo "official package sources failed; switching to China mirrors" >&2
  configure_china_package_mirror
  run_package_install
}

if [[ "$DRY_RUN" -eq 1 ]]; then
  printf 'manager=%s mirror_mode=%s packages=%s\n' "$manager" "$mirror_mode" "${packages[*]}"
  exit 0
fi

install_with_mirror_fallback
