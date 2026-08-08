#!/usr/bin/env bash

rand_secret() {
  openssl rand -base64 32 | tr '+/' '-_' | tr -d '='
}

detect_public_url() {
  if [[ -n "${AGENT_HUB_PUBLIC_URL:-}" ]]; then
    printf '%s\n' "$AGENT_HUB_PUBLIC_URL"
    return 0
  fi

  local ip
  if command -v curl >/dev/null 2>&1; then
    ip="$(curl -fsS --max-time 3 https://api.ipify.org 2>/dev/null || true)"
    if [[ "$ip" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
      printf 'http://%s\n' "$ip"
      return 0
    fi
  fi

  if command -v hostname >/dev/null 2>&1; then
    ip="$(hostname -I 2>/dev/null | awk '{print $1}' || true)"
    if [[ -n "$ip" ]]; then
      printf 'http://%s\n' "$ip"
      return 0
    fi
  fi

  printf 'http://127.0.0.1\n'
}

generate_or_keep_secrets() {
  mkdir -p "$CONFIG_DIR" "$STATE_DIR"
  if [[ -f "$SECRETS_FILE" ]]; then
    log "keeping existing secrets at $SECRETS_FILE"
    return 0
  fi
  local tmp postgres_password
  tmp="$(mktemp "$CONFIG_DIR/secrets.env.XXXXXX")"
  postgres_password="$(rand_secret)"
  {
    printf 'AGENT_HUB_ENVIRONMENT=production\n'
    printf 'AGENT_HUB_SECRET_KEY=%s\n' "$(rand_secret)"
    printf 'AGENT_HUB_MASTER_KEY=%s\n' "$(openssl rand -base64 32)"
    printf 'AGENT_HUB_JWT_SIGNING_KEY=base64url:%s\n' "$(rand_secret)"
    printf 'JWT_SIGNING_KEY=base64url:%s\n' "$(rand_secret)"
    printf 'POSTGRES_DB=agent_hub\n'
    printf 'POSTGRES_USER=agent_hub\n'
    printf 'POSTGRES_PASSWORD=%s\n' "$postgres_password"
    printf 'AGENT_HUB_DATABASE_URL=postgresql+asyncpg://agent_hub:%s@127.0.0.1:5432/agent_hub\n' "$postgres_password"
    printf 'AGENT_HUB_REDIS_URL=redis://127.0.0.1:6379/0\n'
    printf 'DATABASE_URL=postgresql+asyncpg://agent_hub:%s@127.0.0.1:5432/agent_hub\n' "$postgres_password"
    printf 'REDIS_URL=redis://127.0.0.1:6379/0\n'
    printf 'LITELLM_MASTER_KEY=%s\n' "$(rand_secret)"
    printf 'AGENT_HUB_SETUP_CODE=%s\n' "$(rand_secret)"
    printf 'AGENT_HUB_PUBLIC_URL=%s\n' "$(detect_public_url)"
    printf 'AGENT_HUB_TLS_CERT_FILE=%s\n' "${AGENT_HUB_TLS_CERT_FILE:-}"
    printf 'AGENT_HUB_TLS_KEY_FILE=%s\n' "${AGENT_HUB_TLS_KEY_FILE:-}"
    printf 'AGENT_HUB_API_BIND_HOST=%s\n' "${AGENT_HUB_API_BIND_HOST:-127.0.0.1}"
    printf 'AGENT_HUB_API_PORT=%s\n' "${AGENT_HUB_API_PORT:-8000}"
    printf 'AGENT_HUB_FEISHU_BIND_HOST=%s\n' "${AGENT_HUB_FEISHU_BIND_HOST:-127.0.0.1}"
    printf 'AGENT_HUB_FEISHU_PORT=%s\n' "${AGENT_HUB_FEISHU_PORT:-8001}"
    printf 'LITELLM_BIND_HOST=%s\n' "${LITELLM_BIND_HOST:-127.0.0.1}"
    printf 'LITELLM_PORT=%s\n' "${LITELLM_PORT:-4000}"
    printf 'AGENT_HUB_LOG_LEVEL=%s\n' "${AGENT_HUB_LOG_LEVEL:-WARNING}"
  } > "$tmp"
  chmod 0600 "$tmp"
  mv "$tmp" "$SECRETS_FILE"
  mark_stage "secrets"
  log "generated secrets at $SECRETS_FILE"
}
