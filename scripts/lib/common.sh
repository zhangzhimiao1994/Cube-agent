#!/usr/bin/env bash

INSTALL_ROOT="${AGENT_HUB_INSTALL_ROOT:-/opt/agent-hub}"
STATE_DIR="${AGENT_HUB_STATE_DIR:-/var/lib/agent-hub}"
CONFIG_DIR="${AGENT_HUB_CONFIG_DIR:-/etc/agent-hub}"
SECRETS_FILE="$CONFIG_DIR/secrets.env"
JOURNAL_FILE="$STATE_DIR/install-journal"

log() { printf '[agent-hub] %s\n' "$*"; }
warn() { printf '[agent-hub] warning: %s\n' "$*" >&2; }
die() { printf '[agent-hub] error: %s\n' "$*" >&2; exit 1; }

redact() {
  sed -E 's/(password|secret|token|key)=([^ ]+)/\1=[REDACTED]/Ig'
}

mark_stage() {
  mkdir -p "$STATE_DIR"
  printf '%s\n' "$1" >> "$JOURNAL_FILE"
}

stage_done() {
  [[ -f "$JOURNAL_FILE" ]] && grep -qx "$1" "$JOURNAL_FILE"
}

installer_failed() {
  local line="$1"
  local status="$2"
  warn "installer failed at line $line with status $status"
  warn "running automatic diagnostics"
  run_doctor || true
  exit "$status"
}

run_doctor() {
  if [[ -x "$SCRIPT_DIR/scripts/agent-hub" ]]; then
    "$SCRIPT_DIR/scripts/agent-hub" doctor || return 0
  else
    check_port 80 || true
    check_port 443 || true
    command -v docker >/dev/null 2>&1 || warn "docker not installed; docker mode will install it or native mode must be supported"
    command -v systemctl >/dev/null 2>&1 || warn "systemd not detected; native mode unavailable"
  fi
}

check_port() {
  local port="$1"
  if command -v ss >/dev/null 2>&1 && ss -ltn "( sport = :$port )" | grep -q ":$port"; then
    warn "port $port is already in use"
    return 1
  fi
}
