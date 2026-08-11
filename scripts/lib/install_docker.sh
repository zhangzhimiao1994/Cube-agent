#!/usr/bin/env bash

AGENT_HUB_SOURCE_DIR="${AGENT_HUB_SOURCE_DIR:-$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)}"

docker_compose_up() {
  docker compose -f "$INSTALL_ROOT/compose/docker-compose.yml" --env-file "$INSTALL_ROOT/compose/.env" up -d
}

set_env_value() {
  local file="$1"
  local name="$2"
  local value="$3"
  local tmp
  tmp="$(mktemp "$file.XXXXXX")"
  grep -v "^${name}=" "$file" > "$tmp" || true
  printf '%s=%s\n' "$name" "$value" >> "$tmp"
  mv "$tmp" "$file"
  chmod 0600 "$file"
}

write_litellm_config() {
  cat > "$INSTALL_ROOT/compose/litellm.yaml" <<'EOF'
model_list: []
litellm_settings:
  drop_params: true
  request_timeout: 600
EOF
  chmod 0644 "$INSTALL_ROOT/compose/litellm.yaml"
}

configure_docker_build_mirrors() {
  if [[ "${AGENT_HUB_MIRROR_MODE:-auto}" == "official" ]]; then
    set_env_value "$INSTALL_ROOT/compose/.env" \
      AGENT_HUB_PYPI_MIRROR \
      "${AGENT_HUB_PYPI_MIRROR:-https://pypi.org/simple}"
    set_env_value "$INSTALL_ROOT/compose/.env" \
      AGENT_HUB_NPM_MIRROR \
      "${AGENT_HUB_NPM_MIRROR:-https://registry.npmjs.org}"
    return
  fi
  set_env_value "$INSTALL_ROOT/compose/.env" \
    AGENT_HUB_PYPI_MIRROR \
    "${AGENT_HUB_PYPI_MIRROR:-https://pypi.tuna.tsinghua.edu.cn/simple}"
  set_env_value "$INSTALL_ROOT/compose/.env" \
    AGENT_HUB_NPM_MIRROR \
    "${AGENT_HUB_NPM_MIRROR:-https://registry.npmmirror.com}"
}

configure_china_docker_mirror() {
  local mirror daemon_config
  mirror="${AGENT_HUB_DOCKER_REGISTRY_MIRROR:-https://registry.cn-hangzhou.aliyuncs.com}"
  daemon_config=/etc/docker/daemon.json

  if [[ -f "$daemon_config" && "${AGENT_HUB_DOCKER_MIRROR_FORCE:-0}" != "1" ]]; then
    warn "Docker daemon config exists; not overwriting $daemon_config automatically"
    warn "Set AGENT_HUB_DOCKER_REGISTRY_MIRROR to your docker.io mirror and AGENT_HUB_DOCKER_MIRROR_FORCE=1 to let installer rewrite it"
    return 1
  fi

  mkdir -p /etc/docker
  if [[ -f "$daemon_config" ]]; then
    cp -n "$daemon_config" "$daemon_config.agent-hub.bak" 2>/dev/null || true
  fi
  cat > "$daemon_config" <<EOF
{
  "registry-mirrors": ["$mirror"]
}
EOF
  if command -v systemctl >/dev/null 2>&1; then
    systemctl restart docker
  else
    service docker restart
  fi
}

install_docker_mode() {
  if ! command -v docker >/dev/null 2>&1; then
    warn "docker missing; install Docker Engine first or use a cloud image with Docker preinstalled"
    die "automatic Docker Engine installation is intentionally not performed without distro-specific package trust setup"
  fi
  mkdir -p "$INSTALL_ROOT"
  cp -R "$AGENT_HUB_SOURCE_DIR/deploy/compose" "$INSTALL_ROOT/compose"
  cp "$SECRETS_FILE" "$INSTALL_ROOT/compose/.env"
  write_litellm_config
  local postgres_password
  postgres_password="$(grep '^POSTGRES_PASSWORD=' "$INSTALL_ROOT/compose/.env" | cut -d= -f2-)"
  [[ -n "$postgres_password" ]] || die "POSTGRES_PASSWORD is missing from compose environment"
  set_env_value "$INSTALL_ROOT/compose/.env" \
    AGENT_HUB_DATABASE_URL \
    "postgresql+asyncpg://agent_hub:${postgres_password}@postgres:5432/agent_hub"
  set_env_value "$INSTALL_ROOT/compose/.env" AGENT_HUB_REDIS_URL "redis://redis:6379/0"
  set_env_value "$INSTALL_ROOT/compose/.env" \
    AGENT_HUB_LITELLM_HEALTH_URL \
    "http://litellm:4000/health/liveliness"
  configure_docker_build_mirrors

  if [[ "${AGENT_HUB_MIRROR_MODE:-auto}" != "official" ]]; then
    configure_china_docker_mirror || true
  fi
  if docker_compose_up; then
    mark_stage "docker-up"
    return
  fi
  if [[ "${AGENT_HUB_MIRROR_MODE:-auto}" == "official" ]]; then
    return 1
  fi
  warn "Docker image pull/build failed; retrying after checking China docker.io registry mirror"
  configure_china_docker_mirror || true
  docker_compose_up
  mark_stage "docker-up"
}

print_bootstrap_output() {
  local public_url setup_url code
  public_url="$(grep '^AGENT_HUB_PUBLIC_URL=' "$SECRETS_FILE" | cut -d= -f2-)"
  setup_url="${public_url%/}/setup"
  code="$(grep '^AGENT_HUB_SETUP_CODE=' "$SECRETS_FILE" | cut -d= -f2-)"
  printf '\nExternal URL: %s\n' "$public_url"
  printf 'Management URL: %s\n' "$setup_url"
  printf '\nWebhook callback URLs for external platforms:\n'
  printf '  Feishu: %s/channels/feishu/events\n' "${public_url%/}"
  printf '  DingTalk: %s/channels/dingtalk/events\n' "${public_url%/}"
  printf '  WeCom group bot: %s/channels/wecom/bot/events\n' "${public_url%/}"
  printf '  WeCom app: %s/channels/wecom/app/events\n' "${public_url%/}"
  printf '  WeChat official account: %s/channels/wechatmp/events\n' "${public_url%/}"
  printf '  WeChat customer service: %s/channels/wechat-kf/events\n' "${public_url%/}"
  printf '  Telegram: %s/channels/telegram/events\n' "${public_url%/}"
  printf '  Slack: %s/channels/slack/events\n' "${public_url%/}"
  printf '  QQ bot: %s/channels/qq/events\n' "${public_url%/}"
  printf '  Custom webhook: %s/channels/custom/events\n' "${public_url%/}"
  printf 'One-time setup code: %s\n' "$code"
  printf 'Run diagnostics: scripts/agent-hub doctor\n'
}
