#!/usr/bin/env bash

select_tls_mode() {
  local public_url="${AGENT_HUB_PUBLIC_URL:-http://127.0.0.1}"
  if [[ "$public_url" == https://* ]]; then
    log "TLS mode=https via reverse proxy"
  else
    warn "IP-only or HTTP install; bind privately and use SSH tunnel or deliberate firewall exposure"
  fi
}
