#!/usr/bin/env bash

rand_secret() {
  openssl rand -base64 32 | tr '+/' '-_' | tr -d '='
}

is_private_ipv4() {
  local ip="$1" a b c d
  IFS=. read -r a b c d <<< "$ip"
  [[ "$a" =~ ^[0-9]+$ && "$b" =~ ^[0-9]+$ && "$c" =~ ^[0-9]+$ && "$d" =~ ^[0-9]+$ ]] || return 1
  ((a >= 0 && a <= 255 && b >= 0 && b <= 255 && c >= 0 && c <= 255 && d >= 0 && d <= 255)) || return 1

  if ((a == 10)); then return 0; fi
  if ((a == 127)); then return 0; fi
  if ((a == 169 && b == 254)); then return 0; fi
  if ((a == 172 && b >= 16 && b <= 31)); then return 0; fi
  if ((a == 192 && b == 168)); then return 0; fi
  if ((a == 100 && b >= 64 && b <= 127)); then return 0; fi
  if ((a == 0)); then return 0; fi
  if ((a >= 224)); then return 0; fi
  return 1
}

detect_public_url() {
  if [[ -n "${AGENT_HUB_PUBLIC_URL:-}" ]]; then
    validate_public_url "$AGENT_HUB_PUBLIC_URL"
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
    for ip in $(hostname -I 2>/dev/null || true); do
      [[ "$ip" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] || continue
      if is_private_ipv4 "$ip"; then
        warn "detected private or loopback address $ip; set AGENT_HUB_PUBLIC_URL=http(s)://your-public-host for external access"
        continue
      fi
      printf 'http://%s\n' "$ip"
      return 0
    done
  fi

  printf 'http://127.0.0.1\n'
}

validate_public_url() {
  local url="$1"
  case "$url" in
    http://*|https://*) ;;
    *) die "AGENT_HUB_PUBLIC_URL must start with http:// or https://" ;;
  esac
}

sanitize_legacy_secrets() {
  [[ -f "$SECRETS_FILE" ]] || return 0
  local tmp
  tmp="$(mktemp "$CONFIG_DIR/secrets.env.sanitized.XXXXXX")"
  grep -Ev '^(DATABASE_URL|REDIS_URL|JWT_SIGNING_KEY|AGENT_HUB_SECRET_KEY)=' "$SECRETS_FILE" > "$tmp" || true
  if ! cmp -s "$SECRETS_FILE" "$tmp"; then
    chmod 0600 "$tmp"
    mv "$tmp" "$SECRETS_FILE"
    log "removed legacy unprefixed secrets from $SECRETS_FILE"
  else
    rm -f "$tmp"
  fi
}

normalize_secret_file_format() {
  [[ -f "$SECRETS_FILE" ]] || return 0
  if grep -q 'nAGENT_HUB_' "$SECRETS_FILE"; then
    sed -i 's/nAGENT_HUB_/\nAGENT_HUB_/g' "$SECRETS_FILE"
  fi
  if [[ -s "$SECRETS_FILE" ]] && [[ "$(tail -c 1 "$SECRETS_FILE")" != "" ]]; then
    printf '\n' >> "$SECRETS_FILE"
  fi
  chmod 0600 "$SECRETS_FILE"
}

ensure_secret_default() {
  local name="$1" value="$2"
  [[ -f "$SECRETS_FILE" ]] || return 0
  if grep -q "^${name}=" "$SECRETS_FILE"; then
    return 0
  fi
  printf '%s=%s\n' "$name" "$value" >> "$SECRETS_FILE"
  chmod 0600 "$SECRETS_FILE"
}

ensure_numeric_secret_default() {
  local name="$1" value="$2" current
  [[ -f "$SECRETS_FILE" ]] || return 0
  current="$(grep "^${name}=" "$SECRETS_FILE" | tail -n 1 | cut -d= -f2- || true)"
  if [[ "$current" =~ ^[0-9]+([.][0-9]+)?$ ]]; then
    return 0
  fi
  local tmp
  tmp="$(mktemp "$CONFIG_DIR/secrets.env.${name}.XXXXXX")"
  grep -v "^${name}=" "$SECRETS_FILE" > "$tmp" || true
  printf '%s=%s\n' "$name" "$value" >> "$tmp"
  chmod 0600 "$tmp"
  mv "$tmp" "$SECRETS_FILE"
}

ensure_secret_defaults() {
  normalize_secret_file_format
  ensure_numeric_secret_default \
    AGENT_HUB_RUNTIME_TIMEOUT_SECONDS \
    "${AGENT_HUB_RUNTIME_TIMEOUT_SECONDS:-300}"
  ensure_numeric_secret_default \
    AGENT_HUB_RUNTIME_TOKEN_BUDGET \
    "${AGENT_HUB_RUNTIME_TOKEN_BUDGET:-1000000}"
}

generate_or_keep_secrets() {
  mkdir -p "$CONFIG_DIR" "$STATE_DIR"
  if [[ -f "$SECRETS_FILE" ]]; then
    log "keeping existing secrets at $SECRETS_FILE"
    sanitize_legacy_secrets
    ensure_secret_defaults
    return 0
  fi
  local tmp postgres_password jwt_signing_key public_url
  tmp="$(mktemp "$CONFIG_DIR/secrets.env.XXXXXX")"
  postgres_password="$(rand_secret)"
  jwt_signing_key="$(rand_secret)"
  public_url="$(detect_public_url)"
  {
    printf 'AGENT_HUB_ENVIRONMENT=production\n'
    printf 'AGENT_HUB_MASTER_KEY=%s\n' "$(openssl rand -base64 32)"
    printf 'AGENT_HUB_JWT_SIGNING_KEY=base64url:%s\n' "$jwt_signing_key"
    printf 'POSTGRES_DB=agent_hub\n'
    printf 'POSTGRES_USER=agent_hub\n'
    printf 'POSTGRES_PASSWORD=%s\n' "$postgres_password"
    printf 'AGENT_HUB_DATABASE_URL=postgresql+asyncpg://agent_hub:%s@127.0.0.1:5432/agent_hub\n' "$postgres_password"
    printf 'AGENT_HUB_REDIS_URL=redis://127.0.0.1:6379/0\n'
    printf 'AGENT_HUB_LITELLM_HEALTH_URL=http://127.0.0.1:%s/health/liveliness\n' "${LITELLM_PORT:-4000}"
    printf 'LITELLM_MASTER_KEY=%s\n' "$(rand_secret)"
    printf 'AGENT_HUB_SETUP_CODE=%s\n' "$(rand_secret)"
    printf 'AGENT_HUB_PUBLIC_URL=%s\n' "$public_url"
    printf 'AGENT_HUB_TLS_CERT_FILE=%s\n' "${AGENT_HUB_TLS_CERT_FILE:-}"
    printf 'AGENT_HUB_TLS_KEY_FILE=%s\n' "${AGENT_HUB_TLS_KEY_FILE:-}"
    printf 'AGENT_HUB_API_BIND_HOST=%s\n' "${AGENT_HUB_API_BIND_HOST:-127.0.0.1}"
    printf 'AGENT_HUB_API_PORT=%s\n' "${AGENT_HUB_API_PORT:-8000}"
    printf 'AGENT_HUB_FEISHU_BIND_HOST=%s\n' "${AGENT_HUB_FEISHU_BIND_HOST:-127.0.0.1}"
    printf 'AGENT_HUB_FEISHU_PORT=%s\n' "${AGENT_HUB_FEISHU_PORT:-8001}"
    printf 'LITELLM_BIND_HOST=%s\n' "${LITELLM_BIND_HOST:-127.0.0.1}"
    printf 'LITELLM_PORT=%s\n' "${LITELLM_PORT:-4000}"
    printf 'AGENT_HUB_LOG_LEVEL=%s\n' "${AGENT_HUB_LOG_LEVEL:-WARNING}"
    printf 'AGENT_HUB_RUNTIME_TIMEOUT_SECONDS=%s\n' "${AGENT_HUB_RUNTIME_TIMEOUT_SECONDS:-300}"
    printf 'AGENT_HUB_RUNTIME_TOKEN_BUDGET=%s\n' "${AGENT_HUB_RUNTIME_TOKEN_BUDGET:-1000000}"
  } > "$tmp"
  chmod 0600 "$tmp"
  mv "$tmp" "$SECRETS_FILE"
  sanitize_legacy_secrets
  ensure_secret_defaults
  mark_stage "secrets"
  log "generated secrets at $SECRETS_FILE"
}
