#!/usr/bin/env bash

docker_compose_up() {
  docker compose -f "$INSTALL_ROOT/compose/docker-compose.yml" --env-file "$INSTALL_ROOT/compose/.env" up -d
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
  [[ -f "$daemon_config" ]] && cp -n "$daemon_config" "$daemon_config.agent-hub.bak" 2>/dev/null || true
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
  cp -R "$SCRIPT_DIR/deploy/compose" "$INSTALL_ROOT/compose"
  cp "$SECRETS_FILE" "$INSTALL_ROOT/compose/.env"
  if ! grep -q '^DATABASE_URL=' "$INSTALL_ROOT/compose/.env"; then
    # shellcheck disable=SC2016
    printf 'DATABASE_URL=postgresql+asyncpg://agent_hub:${POSTGRES_PASSWORD}@postgres:5432/agent_hub\n' >> "$INSTALL_ROOT/compose/.env"
    printf 'REDIS_URL=redis://redis:6379/0\n' >> "$INSTALL_ROOT/compose/.env"
  fi

  if [[ "${AGENT_HUB_MIRROR_MODE:-auto}" == "china" ]]; then
    configure_china_docker_mirror || true
  fi
  if docker_compose_up; then
    mark_stage "docker-up"
    return
  fi
  if [[ "${AGENT_HUB_MIRROR_MODE:-auto}" == "official" ]]; then
    return 1
  fi
  warn "official Docker image pull/build failed; retrying after configuring China docker.io registry mirror"
  configure_china_docker_mirror || true
  docker_compose_up
  mark_stage "docker-up"
}

print_bootstrap_output() {
  local url code
  url="$(grep '^AGENT_HUB_PUBLIC_URL=' "$SECRETS_FILE" | cut -d= -f2-)/setup"
  code="$(grep '^AGENT_HUB_SETUP_CODE=' "$SECRETS_FILE" | cut -d= -f2-)"
  printf '\nManagement URL: %s\n' "$url"
  printf 'One-time setup code: %s\n' "$code"
  printf 'Run diagnostics: scripts/agent-hub doctor\n'
}
