#!/usr/bin/env bash

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
  docker compose -f "$INSTALL_ROOT/compose/docker-compose.yml" --env-file "$INSTALL_ROOT/compose/.env" up -d
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
