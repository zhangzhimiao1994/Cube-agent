#!/usr/bin/env bash

# Sourced by installer modules; shellcheck checks files independently.
# shellcheck disable=SC2034
AGENT_HUB_SOURCE_DIR="${AGENT_HUB_SOURCE_DIR:-$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)}"
INSTALL_ROOT="${AGENT_HUB_INSTALL_ROOT:-/opt/agent-hub}"
STATE_DIR="${AGENT_HUB_STATE_DIR:-/var/lib/agent-hub}"
CONFIG_DIR="${AGENT_HUB_CONFIG_DIR:-/etc/agent-hub}"
# Sourced by installer modules; shellcheck checks files independently.
# shellcheck disable=SC2034
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
  local command="${3:-unknown}"
  local safe_command last_stage
  safe_command="$(printf '%s' "$command" | redact)"
  last_stage="none"
  if [[ -f "$JOURNAL_FILE" ]]; then
    last_stage="$(tail -n 1 "$JOURNAL_FILE" 2>/dev/null || printf 'none')"
    last_stage="${last_stage:-none}"
  fi
  warn "installer failed"
  warn "Line: $line"
  warn "Exit status: $status"
  warn "Failed command: $safe_command"
  warn "Failed stage: after $last_stage"
  explain_installer_failure "$safe_command"
  warn "running automatic diagnostics"
  run_doctor || true
  exit "$status"
}

explain_installer_failure() {
  local command="$1"
  warn "Common causes and checks:"
  case "$command" in
    *apt*|*dnf*|*yum*|*install-packages.sh*)
      warn "- Package installation failed. Check mirror reachability, DNS, and distro package manager locks."
      warn "- Try: apt-get update or dnf makecache"
      ;;
    *uv*|*pip*|*npm*)
      warn "- Dependency installation failed. Check access to PyPI/npm mirrors and available disk space."
      warn "- Try: df -h && env | grep AGENT_HUB_"
      ;;
    *alembic*|*createdb*|*psql*)
      warn "- Database setup or migration failed. Check PostgreSQL status and credentials."
      warn "- Try: systemctl status postgresql && journalctl -u postgresql --no-pager -n 100"
      ;;
    *caddy*)
      warn "- Caddy failed to start or reload. Check port 80/443, Caddyfile syntax, and TLS files."
      warn "- Try: systemctl status caddy && journalctl -u caddy --no-pager -n 100"
      ;;
    *systemctl*)
      warn "- A systemd service failed. Inspect the specific service logs."
      warn "- Try: systemctl status agent-hub.target && journalctl -u agent-hub-api --no-pager -n 100"
      ;;
    *curl*)
      warn "- Health check failed. API may still be starting or cannot reach database/Redis."
      warn "- Try: curl -v http://127.0.0.1:8000/health/ready"
      ;;
    *)
      warn "- Review the failed command above, then inspect service and installer logs."
      ;;
  esac
  warn "- API logs: journalctl -u agent-hub-api --no-pager -n 100"
  warn "- Worker logs: journalctl -u agent-hub-worker --no-pager -n 100"
  warn "- Caddy logs: journalctl -u caddy --no-pager -n 100"
  warn "- Service status: systemctl status caddy agent-hub-api agent-hub-worker"
  warn "- Readiness probe: curl -v http://127.0.0.1:8000/health/ready"
}

run_doctor() {
  if [[ -f "$AGENT_HUB_SOURCE_DIR/scripts/agent-hub" ]]; then
    bash "$AGENT_HUB_SOURCE_DIR/scripts/agent-hub" doctor || return 0
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
