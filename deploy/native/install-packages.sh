#!/usr/bin/env bash
set -Eeuo pipefail

usage() {
  cat <<'EOF'
Usage:
  deploy/native/install-packages.sh [--detect OS_RELEASE] [--dry-run] [--local-db] [--local-redis]

Environment:
  AGENT_HUB_MIRROR_MODE=auto|official|china
    auto: try official sources first; APT switches only on confirmed source/network failure.
    official: never rewrite package sources.
    china: configure China mirrors before installing packages.
  AGENT_HUB_APT_LOCK_TIMEOUT_SECONDS=120
    Wait for confirmed lists/dpkg locks for 1-600 seconds; never interrupt dpkg.
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
  packages+=(python3 python3-venv python3-pip build-essential xz-utils bubblewrap apparmor apparmor-utils)
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
  packages+=(python3 python3-pip gcc gcc-c++ make xz bubblewrap)
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

apt_lock_timeout() {
  apt_wait_seconds="${AGENT_HUB_APT_LOCK_TIMEOUT_SECONDS-120}"
  if [[ ! $apt_wait_seconds =~ ^[1-9][0-9]{0,2}$ ]] || (( apt_wait_seconds > 600 )); then
    printf 'APT_FAILURE=invalid_lock_timeout\n' >&2
    return 2
  fi
}

apt_lock_error() {
  local line found=0 pattern
  if [[ $2 == update ]]; then
    pattern='^E: Could not get lock /var/lib/apt/lists/lock'
  else
    pattern='^E: Could not get lock (/var/lib/dpkg/lock(-frontend)?|/var/cache/apt/archives/lock)'
  fi
  pattern+='(\. It is held by process [1-9][0-9]*( \([^()]*\))?| - open \(11: Resource temporarily unavailable\))$'
  # Unknown or mixed diagnostics must not become a lock retry or a mirror change.
  while IFS= read -r line || [[ -n $line ]]; do
    if [[ $line =~ $pattern ]]; then
      found=1
    elif [[ -z $line || $line == 'N: Be aware that removing the lock file is not a solution and may break your system.' ]]; then
      continue
    elif [[ $2 == update && $line == 'E: Unable to lock directory /var/lib/apt/lists/' ]]; then
      continue
    elif [[ $2 == install && (
      $line == 'E: Unable to acquire the dpkg frontend lock (/var/lib/dpkg/lock-frontend), is another process using it?' ||
      $line == 'E: Unable to lock the administration directory (/var/lib/dpkg/), is another process using it?' ||
      $line == 'E: Unable to lock directory /var/cache/apt/archives/'
    ) ]]; then
      continue
    else
      return 1
    fi
  done < "$1" || return 1
  [[ $found == 1 ]]
}

apt_source_error() {
  local line host base reason pattern network_pattern found=0 terminal=0
  pattern='^E: Failed to fetch https?://(archive\.ubuntu\.com|security\.ubuntu\.com|deb\.debian\.org|security\.debian\.org)/(ubuntu|debian|debian-security)/[^[:space:]]+[[:space:]]+(.+)$'
  while IFS= read -r line || [[ -n $line ]]; do
    if [[ -z $line ]]; then
      continue
    elif [[ $line =~ $pattern && $terminal == 0 ]]; then
      host=${BASH_REMATCH[1]}
      base=${BASH_REMATCH[2]}
      reason=${BASH_REMATCH[3]}
      case "$host:$base" in
        archive.ubuntu.com:ubuntu|security.ubuntu.com:ubuntu|deb.debian.org:debian|security.debian.org:debian-security) ;;
        *) return 1 ;;
      esac
      network_pattern="^Could not connect to ${host//./\\.}:(80|443) \\([0-9a-fA-F:.]+\\)\\. - connect \\((101: Network is unreachable|110: Connection timed out|111: Connection refused)\\)$"
      if [[ $reason != "Temporary failure resolving '$host'" && $reason != "Could not resolve '$host'" && ! $reason =~ $network_pattern ]]; then
        return 1
      fi
      found=1
    elif [[ $2 == update && $line == 'E: Some index files failed to download. They have been ignored, or old ones used instead.' ]] ||
         [[ $2 == install && $line == 'E: Unable to fetch some archives, maybe run apt-get update or try with --fix-missing?' ]]; then
      [[ $found == 1 && $terminal == 0 ]] || return 1
      terminal=1
    else
      return 1
    fi
  done < "$1" || return 1
  [[ $found == 1 && $terminal == 1 ]]
}

apt_run() (
  local phase="$1" error_file status deadline
  shift
  umask 077
  error_file=$(mktemp 2>/dev/null) || { printf 'APT_FAILURE=stderr_file\n' >&2; return 1; }
  trap 'status=$?; if ! rm -f -- "$error_file" 2>/dev/null; then printf "APT_FAILURE=stderr_cleanup\n" >&2; status=1; fi; exit "$status"' EXIT
  deadline=$((SECONDS + apt_wait_seconds))
  while :; do
    status=0
    if [[ $phase == update ]]; then
      LC_ALL=C apt-get update 2>"$error_file" || status=$?
    else
      LC_ALL=C DEBIAN_FRONTEND=noninteractive \
        apt-get -o "DPkg::Lock::Timeout=$apt_wait_seconds" install -y "$@" 2>"$error_file" || status=$?
    fi
    if (( status == 0 )); then return 0; fi
    # Only APT's documented failure code can enter either diagnostic classifier.
    if (( status != 100 )); then printf 'APT_FAILURE=unexpected_status\n' >&2; return 100; fi
    if apt_lock_error "$error_file" "$phase" 2>/dev/null; then
      if [[ $phase == update ]]; then
        if (( SECONDS < deadline )); then
          sleep 1 2>/dev/null || { printf 'APT_FAILURE=lock_wait\n' >&2; return 1; }
          continue
        fi
        printf 'APT_FAILURE=lists_lock_timeout\n' >&2
      else
        printf 'APT_FAILURE=install_lock\n' >&2
      fi
    elif apt_source_error "$error_file" "$phase" 2>/dev/null; then
      printf 'APT_FAILURE=source_fetch\n' >&2
      # Private result, mapped back to 100 at the public installer boundary.
      return 125
    else
      printf 'APT_FAILURE=%s\n' "$phase" >&2
    fi
    return "$status"
  done
)

run_package_install() {
  if [[ "$manager" == "apt" ]]; then
    apt_lock_timeout || return $?
    apt_run update || return $?
    apt_run install "${packages[@]}"
  elif [[ "$manager" == "dnf" ]]; then
    dnf install -y "${packages[@]}"
  fi
}

install_with_mirror_fallback() {
  local status=0
  if [[ "$manager" == "apt" ]]; then apt_lock_timeout || return $?; fi
  if [[ "$mirror_mode" == "china" ]]; then
    configure_china_package_mirror
    run_package_install || status=$?
  else
    run_package_install || status=$?
    if (( status != 0 )); then
      if [[ "$mirror_mode" == "official" && "$manager" != "apt" ]]; then return 1; fi
      if [[ "$mirror_mode" == "auto" && ( "$manager" != "apt" || $status == 125 ) ]]; then
        echo "official package sources failed; switching to China mirrors" >&2
        configure_china_package_mirror
        status=0
        run_package_install || status=$?
      fi
    fi
  fi
  if [[ "$manager" == "apt" && $status == 125 ]]; then return 100; fi
  return "$status"
}

if [[ "$DRY_RUN" -eq 1 ]]; then
  printf 'manager=%s mirror_mode=%s packages=%s\n' "$manager" "$mirror_mode" "${packages[*]}"
  exit 0
fi

install_with_mirror_fallback
