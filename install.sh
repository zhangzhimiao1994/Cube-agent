#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/scripts/lib/common.sh"
source "$SCRIPT_DIR/scripts/lib/detect.sh"
source "$SCRIPT_DIR/scripts/lib/prompts.sh"
source "$SCRIPT_DIR/scripts/lib/secrets.sh"
source "$SCRIPT_DIR/scripts/lib/database.sh"
source "$SCRIPT_DIR/scripts/lib/tls.sh"
source "$SCRIPT_DIR/scripts/lib/install_docker.sh"
source "$SCRIPT_DIR/scripts/lib/install_native.sh"
source "$SCRIPT_DIR/scripts/lib/verify.sh"

MODE=""
# Reserved for future config-file driven installs.
# shellcheck disable=SC2034
CONFIG_FILE=""
DRY_RUN=0
ASSUME_YES=0

usage() {
  cat <<'EOF'
Agent Hub one-click installer

Usage:
  sudo bash install.sh [--mode docker|native|auto] [--config file] [--dry-run] [--yes]

Defaults:
  --mode auto chooses Docker for broad Linux compatibility, and native only on supported apt/dnf hosts.
  New servers are bootstrapped from zero: dependencies, secrets, database, services, health, setup code.
  Existing installs are repaired or upgraded; data and secrets are never overwritten.
EOF
}

while (($#)); do
  case "$1" in
    --mode) MODE="${2:?missing mode}"; shift 2 ;;
    --config) CONFIG_FILE="${2:?missing config}"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    --yes|-y) ASSUME_YES=1; shift ;;
    --help) usage; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
done

if [[ -n "$CONFIG_FILE" ]]; then
  [[ -r "$CONFIG_FILE" ]] || die "config file is not readable: $CONFIG_FILE"
  # shellcheck disable=SC1090
  source "$CONFIG_FILE"
fi

[[ -z "$MODE" || "$MODE" == "auto" || "$MODE" == "docker" || "$MODE" == "native" ]] || die "mode must be auto, docker, or native"
if [[ "${AGENT_HUB_TEST:-0}" != "1" ]]; then
  [[ "${EUID:-$(id -u)}" -eq 0 ]] || die "run with sudo bash install.sh"
fi

trap 'installer_failed "$LINENO" "$?" "$BASH_COMMAND"' ERR

main() {
  log "starting Agent Hub installer"
  detect_host
  detect_existing_install
  choose_mode
  generate_or_keep_secrets
  select_tls_mode
  if [[ "$DRY_RUN" -eq 1 ]]; then
    log "dry-run mode=$MODE install_root=$INSTALL_ROOT"
    run_doctor || true
    return 0
  fi
  if [[ "$MODE" == "docker" ]]; then
    install_docker_mode
  else
    install_native_mode
  fi
  verify_installation
  print_bootstrap_output
}

main
