#!/usr/bin/env bash

verify_url() {
  local url="$1"
  if command -v curl >/dev/null 2>&1; then
    curl -fsS "$url" >/dev/null
  else
    warn "curl unavailable; skipping $url"
  fi
}

installation_health_base_url() {
  if [[ "${MODE:-}" == "docker" ]]; then
    local public_url
    public_url="${AGENT_HUB_PUBLIC_URL:-}"
    if [[ -z "$public_url" && -f "${SECRETS_FILE:-}" ]]; then
      public_url="$(grep '^AGENT_HUB_PUBLIC_URL=' "$SECRETS_FILE" | cut -d= -f2- || true)"
    fi
    if [[ -n "$public_url" ]]; then
      printf '%s\n' "${public_url%/}"
      return 0
    fi
  fi
  printf 'http://127.0.0.1:%s\n' "${AGENT_HUB_API_PORT:-8000}"
}

verify_native_service() {
  local unit="$1"
  [[ "${MODE:-}" == "native" ]] || return 0
  if command -v systemctl >/dev/null 2>&1; then
    systemctl is-active --quiet "$unit" || warn "$unit is not active; inspect with: journalctl -u $unit -n 200 --no-pager"
  fi
}

verify_native_litellm_proxy() {
  local current python_bin litellm_bin
  [[ "${MODE:-}" == "native" ]] || return 0
  current="${INSTALL_ROOT:-/opt/agent-hub}/current"
  python_bin="$current/.litellm-venv/bin/python"
  litellm_bin="$current/.litellm-venv/bin/litellm"

  [[ -x "$litellm_bin" ]] || {
    warn "LiteLLM CLI missing or not executable at $litellm_bin; model gateway will fail"
    return 0
  }
  [[ -x "$python_bin" ]] || {
    warn "LiteLLM Python interpreter missing or not executable at $python_bin; model gateway will fail"
    return 0
  }
  if ! "$python_bin" - <<'PY'
import importlib.util

required_modules = ("litellm", "litellm.proxy.proxy_server")
missing = [name for name in required_modules if importlib.util.find_spec(name) is None]
if missing:
    raise SystemExit("missing modules: " + ", ".join(missing))
PY
  then
    warn "LiteLLM proxy_server module is missing; reinstall native mode so litellm[proxy] is installed in .litellm-venv"
  fi
}

verify_installation() {
  local base_url
  base_url="$(installation_health_base_url)"
  log "verifying installation"
  verify_native_service agent-hub-api.service
  verify_native_service agent-hub-worker.service
  verify_native_service agent-hub-litellm.service
  verify_native_litellm_proxy
  verify_url "$base_url/health/live" || warn "live health not reachable yet at $base_url"
  verify_url "$base_url/health/ready" || warn "readiness not reachable yet at $base_url; run scripts/agent-hub doctor"
}
