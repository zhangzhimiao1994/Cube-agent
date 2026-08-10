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
systemd_unit_active_if_present() {
  local unit="$1"
  has_command systemctl || return 0
  systemctl list-unit-files "$unit" --no-legend 2>/dev/null | grep -q "^$unit" || return 0
  systemctl is-active --quiet "$unit"
}
native_current_release() {
  printf '%s/current\n' "${AGENT_HUB_INSTALL_ROOT:-/opt/agent-hub}"
}
native_litellm_proxy_ready() {
  local current python_bin litellm_bin
  current="$(native_current_release)"
  python_bin="$current/.litellm-venv/bin/python"
  litellm_bin="$current/.litellm-venv/bin/litellm"
  if [[ ! -d "$current" ]]; then
    printf 'detail: native release symlink is missing: %s\n' "$current" >&2
    return 1
  fi
  if [[ ! -x "$litellm_bin" ]]; then
    printf 'detail: LiteLLM CLI is missing or not executable: %s\n' "$litellm_bin" >&2
    return 1
  fi
  if [[ ! -x "$python_bin" ]]; then
    printf 'detail: LiteLLM Python interpreter is missing or not executable: %s\n' "$python_bin" >&2
    return 1
  fi
  if ! "$python_bin" - <<'PY'
import importlib.util

required_modules = ("litellm", "litellm.proxy.proxy_server")
missing = [name for name in required_modules if importlib.util.find_spec(name) is None]
if missing:
    raise SystemExit("missing modules: " + ", ".join(missing))
PY
  then
    printf 'detail: LiteLLM proxy_server module is missing; reinstall with litellm[proxy]\n' >&2
    return 1
  fi
}
disk_has_2gb_free() {
  df -Pk / | awk 'NR==2 { exit !($4 > 2097152) }'
}
memory_has_1gb_total() {
  awk '/MemTotal/ { exit !($2 > 1048576) }' /proc/meminfo
}
web_root() {
  printf '%s\n' "${AGENT_HUB_WEB_ROOT:-/opt/agent-hub/current/web/dist}"
}
readable_as_caddy() {
  local path="$1"
  if id caddy >/dev/null 2>&1 && [[ "${EUID:-$(id -u)}" -eq 0 ]] && command -v runuser >/dev/null 2>&1; then
    runuser -u caddy -- test -r "$path"
    return $?
  fi
  test -r "$path"
}
web_assets_readable() {
  local root index asset
  root="$(web_root)"
  index="$root/index.html"
  if [[ ! -d "$root" ]]; then
    printf 'detail: web root does not exist: %s\n' "$root" >&2
    return 1
  fi
  if ! readable_as_caddy "$index"; then
    printf 'detail: Caddy cannot read Web UI entry file: %s\n' "$index" >&2
    return 1
  fi
  asset="$(find "$root/assets" -type f \( -name '*.js' -o -name '*.css' \) -print -quit 2>/dev/null || true)"
  if [[ -z "$asset" ]]; then
    printf 'detail: Web UI assets are missing under %s/assets\n' "$root" >&2
    return 1
  fi
  if ! readable_as_caddy "$asset"; then
    printf 'detail: Caddy cannot read Web UI asset: %s\n' "$asset" >&2
    if command -v namei >/dev/null 2>&1; then
      namei -l "$asset" >&2 || true
    fi
    return 1
  fi
}

check "linux kernel" test "$(uname -s)" = "Linux"
check "disk has 2GB free" disk_has_2gb_free
check "memory has 1GB total" memory_has_1gb_total
check "port 80 free or deliberately occupied by proxy" port_free 80
check "port 443 free or deliberately occupied by proxy" port_free 443
check "docker available for universal install path" has_command docker
check "systemd available for native path" has_command systemctl
check "curl available for health checks" has_command curl
check "api live health" health_live
check "api systemd service active when installed" systemd_unit_active_if_present agent-hub-api.service
check "worker systemd service active when installed" systemd_unit_active_if_present agent-hub-worker.service
check "litellm systemd service active when installed" systemd_unit_active_if_present agent-hub-litellm.service
check "native LiteLLM proxy environment" native_litellm_proxy_ready
check "web ui assets readable by Caddy" web_assets_readable

if [[ "$failures" -gt 0 ]]; then
  cat <<'EOF'
Suggested fixes:
  - New server: rerun `sudo bash install.sh --mode docker --yes`.
  - Port conflict: stop the existing web server or set HTTP_PORT/HTTPS_PORT.
  - Missing Docker: install Docker Engine, then rerun installer.
  - Native mode unsupported: rerun with `--mode docker`.
  - LiteLLM proxy failed / model gateway failed:
      sudo journalctl -u agent-hub-litellm -n 200 --no-pager
      sudo bash install.sh --mode native --yes
  - Web UI white screen / asset 403:
      release="$(readlink -f /opt/agent-hub/current)" && test -n "$release"
      sudo chmod 0755 /opt/agent-hub /opt/agent-hub/releases "$release" "$release/web" "$release/web/dist"
      sudo chmod -R a+rX "$release/web/dist"
      sudo systemctl restart caddy
EOF
  exit 1
fi
