#!/usr/bin/env bash

verify_url() {
  local url="$1"
  if command -v curl >/dev/null 2>&1; then
    curl -fsS "$url" >/dev/null
  else
    warn "curl unavailable; skipping $url"
  fi
}

verify_installation() {
  log "verifying installation"
  verify_url "http://127.0.0.1:8000/health/live" || warn "live health not reachable yet"
  verify_url "http://127.0.0.1:8000/health/ready" || warn "readiness not reachable yet; run scripts/agent-hub doctor"
}
