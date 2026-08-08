#!/usr/bin/env bash

select_tls_mode() {
  local public_url="${AGENT_HUB_PUBLIC_URL:-http://127.0.0.1}"
  local cert_file="${AGENT_HUB_TLS_CERT_FILE:-}"
  local key_file="${AGENT_HUB_TLS_KEY_FILE:-}"
  if [[ -f "$SECRETS_FILE" ]]; then
    public_url="$(grep '^AGENT_HUB_PUBLIC_URL=' "$SECRETS_FILE" | cut -d= -f2- || true)"
    public_url="${public_url:-http://127.0.0.1}"
  fi
  if [[ -z "$cert_file" && -f "$SECRETS_FILE" ]]; then
    cert_file="$(grep '^AGENT_HUB_TLS_CERT_FILE=' "$SECRETS_FILE" | cut -d= -f2- || true)"
    key_file="$(grep '^AGENT_HUB_TLS_KEY_FILE=' "$SECRETS_FILE" | cut -d= -f2- || true)"
  fi

  if [[ -n "$cert_file" || -n "$key_file" ]]; then
    [[ -n "$cert_file" && -n "$key_file" ]] || die "set both AGENT_HUB_TLS_CERT_FILE and AGENT_HUB_TLS_KEY_FILE"
    [[ "$public_url" == https://* ]] || die "custom TLS certificates require AGENT_HUB_PUBLIC_URL=https://your-domain"
    log "TLS mode=https with user-supplied certificate"
  elif [[ "$public_url" == https://* ]]; then
    log "TLS mode=https via Caddy automatic certificate"
  else
    warn "IP-only or HTTP install; bind privately and use SSH tunnel or deliberate firewall exposure"
  fi
}
