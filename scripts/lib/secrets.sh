#!/usr/bin/env bash

rand_secret() {
  openssl rand -base64 32 | tr '+/' '-_' | tr -d '='
}

generate_or_keep_secrets() {
  mkdir -p "$CONFIG_DIR" "$STATE_DIR"
  if [[ -f "$SECRETS_FILE" ]]; then
    log "keeping existing secrets at $SECRETS_FILE"
    return 0
  fi
  local tmp
  tmp="$(mktemp "$CONFIG_DIR/secrets.env.XXXXXX")"
  {
    printf 'AGENT_HUB_SECRET_KEY=%s\n' "$(rand_secret)"
    printf 'JWT_SIGNING_KEY=base64url:%s\n' "$(rand_secret)"
    printf 'POSTGRES_DB=agent_hub\n'
    printf 'POSTGRES_USER=agent_hub\n'
    printf 'POSTGRES_PASSWORD=%s\n' "$(rand_secret)"
    printf 'LITELLM_MASTER_KEY=%s\n' "$(rand_secret)"
    printf 'AGENT_HUB_SETUP_CODE=%s\n' "$(rand_secret)"
    printf 'AGENT_HUB_PUBLIC_URL=%s\n' "${AGENT_HUB_PUBLIC_URL:-http://127.0.0.1}"
  } > "$tmp"
  chmod 0600 "$tmp"
  mv "$tmp" "$SECRETS_FILE"
  mark_stage "secrets"
  log "generated secrets at $SECRETS_FILE"
}
