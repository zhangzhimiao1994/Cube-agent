#!/usr/bin/env bash
set -Eeuo pipefail

failures=0

check() {
  local name="$1"
  shift
  if "$@"; then
    printf 'ok: %s\n' "$name"
  else
    printf 'fail: %s\n' "$name"
    failures=$((failures + 1))
  fi
}

has_command() { command -v "$1" >/dev/null 2>&1; }
port_free() {
  local port="$1"
  ! command -v ss >/dev/null 2>&1 || ! ss -ltn "( sport = :$port )" | grep -q ":$port"
}
health_live() {
  ! has_command curl || curl -fsS http://127.0.0.1:8000/health/live >/dev/null 2>&1
}

check "linux kernel" test "$(uname -s)" = "Linux"
check "disk has 2GB free" bash -c 'df -Pk / | awk "NR==2 { exit !(\$4 > 2097152) }"'
check "memory has 1GB total" bash -c 'awk "/MemTotal/ { exit !(\\$2 > 1048576) }" /proc/meminfo'
check "port 80 free or deliberately occupied by proxy" port_free 80
check "port 443 free or deliberately occupied by proxy" port_free 443
check "docker available for universal install path" has_command docker
check "systemd available for native path" has_command systemctl
check "curl available for health checks" has_command curl
check "api live health" health_live

if [[ "$failures" -gt 0 ]]; then
  cat <<'EOF'
Suggested fixes:
  - New server: rerun `sudo bash install.sh --mode docker --yes`.
  - Port conflict: stop the existing web server or set HTTP_PORT/HTTPS_PORT.
  - Missing Docker: install Docker Engine, then rerun installer.
  - Native mode unsupported: rerun with `--mode docker`.
EOF
  exit 1
fi
