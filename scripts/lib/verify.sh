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

verify_installation() {
  local base_url
  base_url="$(installation_health_base_url)"
  log "verifying installation"
  verify_url "$base_url/health/live" || warn "live health not reachable yet at $base_url"
  verify_url "$base_url/health/ready" || warn "readiness not reachable yet at $base_url; run scripts/agent-hub doctor"
}
