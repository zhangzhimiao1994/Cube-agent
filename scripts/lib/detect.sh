#!/usr/bin/env bash

HOST_ID="unknown"
HOST_VERSION="unknown"
HOST_MANAGER="unknown"
ARCH="$(uname -m)"
HAS_DOCKER=0
HAS_SYSTEMD=0
# Reserved for repair/upgrade branching.
# shellcheck disable=SC2034
EXISTING_INSTALL=0

detect_host() {
  if [[ -r /etc/os-release ]]; then
    # shellcheck disable=SC1091
    source /etc/os-release
    HOST_ID="${ID:-unknown}"
    HOST_VERSION="${VERSION_ID:-unknown}"
  fi
  command -v docker >/dev/null 2>&1 && HAS_DOCKER=1
  command -v systemctl >/dev/null 2>&1 && HAS_SYSTEMD=1
  case "$HOST_ID" in
    ubuntu|debian) HOST_MANAGER="apt" ;;
    rocky|almalinux|rhel|centos|fedora) HOST_MANAGER="dnf" ;;
    *) HOST_MANAGER="unknown" ;;
  esac
  log "detected os=$HOST_ID version=$HOST_VERSION arch=$ARCH manager=$HOST_MANAGER docker=$HAS_DOCKER systemd=$HAS_SYSTEMD"
}

detect_existing_install() {
  if [[ -d "$INSTALL_ROOT" || -f "$SECRETS_FILE" ]]; then
    EXISTING_INSTALL=1
    log "existing installation detected; repair/upgrade mode will preserve data and secrets"
  fi
}

choose_mode() {
  if [[ -z "$MODE" || "$MODE" == "auto" ]]; then
    if [[ "$HAS_DOCKER" -eq 1 || "$HOST_MANAGER" == "unknown" ]]; then
      MODE="docker"
    elif [[ "$HAS_SYSTEMD" -eq 1 ]]; then
      MODE="native"
    else
      MODE="docker"
    fi
  fi
  if [[ "$MODE" == "native" && "$HOST_MANAGER" == "unknown" ]]; then
    die "native mode does not support this Linux distribution; rerun with --mode docker"
  fi
  log "selected mode=$MODE"
}
