#!/usr/bin/env bash

native_python() {
  if command -v uv >/dev/null 2>&1; then
    native_uv_env uv python find 3.12
    return 0
  fi
  die "uv runtime not found after native bootstrap"
}

native_secret_value() {
  local name="$1"
  local value
  if [[ -f "$SECRETS_FILE" ]]; then
    value="$(grep "^${name}=" "$SECRETS_FILE" | cut -d= -f2- || true)"
    if [[ -z "$value" && "$name" != AGENT_HUB_* ]]; then
      value="$(grep "^AGENT_HUB_${name}=" "$SECRETS_FILE" | cut -d= -f2- || true)"
    fi
    printf '%s\n' "$value"
  else
    value="${!name:-}"
    if [[ -z "$value" && "$name" != AGENT_HUB_* ]]; then
      local prefixed="AGENT_HUB_${name}"
      value="${!prefixed:-}"
    fi
    printf '%s\n' "$value"
  fi
}

native_mirror_mode() {
  printf '%s\n' "${AGENT_HUB_MIRROR_MODE:-auto}"
}

native_uv_env() {
  env \
    UV_PYTHON_INSTALL_DIR="${AGENT_HUB_UV_PYTHON_INSTALL_DIR:-$INSTALL_ROOT/uv-python}" \
    UV_CACHE_DIR="${AGENT_HUB_UV_CACHE_DIR:-$INSTALL_ROOT/uv-cache}" \
    "$@"
}

python_mirror_env() {
  native_uv_env env \
    UV_DEFAULT_INDEX="${AGENT_HUB_PYPI_MIRROR:-https://pypi.tuna.tsinghua.edu.cn/simple}" \
    PIP_INDEX_URL="${AGENT_HUB_PYPI_MIRROR:-https://pypi.tuna.tsinghua.edu.cn/simple}" \
    UV_PYTHON_INSTALL_MIRROR="${AGENT_HUB_UV_PYTHON_INSTALL_MIRROR:-https://registry.npmmirror.com/-/binary/python-build-standalone}" \
    "$@"
}

run_python_with_mirror_fallback() {
  local mode
  mode="$(native_mirror_mode)"
  if [[ "$mode" == "china" ]]; then
    python_mirror_env "$@"
    return
  fi
  if "$@"; then
    return
  fi
  [[ "$mode" == "official" ]] && return 1
  warn "official Python package source failed; retrying with China PyPI mirror"
  python_mirror_env "$@"
}

run_npm_with_mirror_fallback() {
  local mode
  mode="$(native_mirror_mode)"
  if [[ "$mode" == "china" ]]; then
    npm --prefix web ci --registry="${AGENT_HUB_NPM_MIRROR:-https://registry.npmmirror.com}"
    return
  fi
  if npm --prefix web ci; then
    return
  fi
  [[ "$mode" == "official" ]] && return 1
  warn "official npm registry failed; retrying with China npm mirror"
  npm --prefix web ci --registry="${AGENT_HUB_NPM_MIRROR:-https://registry.npmmirror.com}"
}

run_uv_python_install_with_mirror_fallback() {
  local mode
  mode="$(native_mirror_mode)"
  if [[ "$mode" == "china" ]]; then
    python_mirror_env uv python install 3.12
    return
  fi
  if native_uv_env uv python install 3.12; then
    return
  fi
  [[ "$mode" == "official" ]] && return 1
  warn "official uv Python download failed; retrying with China python-build-standalone mirror"
  python_mirror_env uv python install 3.12
}

pypi_mirror_url() {
  printf '%s\n' "${AGENT_HUB_PYPI_MIRROR:-https://pypi.tuna.tsinghua.edu.cn/simple}"
}

ensure_native_uv_dirs() {
  mkdir -p \
    "${AGENT_HUB_UV_PYTHON_INSTALL_DIR:-$INSTALL_ROOT/uv-python}" \
    "${AGENT_HUB_UV_CACHE_DIR:-$INSTALL_ROOT/uv-cache}"
  chmod 0755 \
    "${AGENT_HUB_UV_PYTHON_INSTALL_DIR:-$INSTALL_ROOT/uv-python}" \
    "${AGENT_HUB_UV_CACHE_DIR:-$INSTALL_ROOT/uv-cache}"
}

install_python_project_from_mirror() {
  local mirror="$1"
  warn "locked uv sync is skipped in China mirror mode or after official lock install fails"
  python_mirror_env uv pip install --python .venv/bin/python --index-url "$mirror" .
}

sync_python_project_with_lock_or_mirror() {
  local mode mirror
  mode="$(native_mirror_mode)"
  mirror="$(pypi_mirror_url)"

  if [[ "$mode" == "china" ]]; then
    install_python_project_from_mirror "$mirror"
    return
  fi

  if native_uv_env uv sync --frozen --no-dev; then
    return 0
  fi

  [[ "$mode" == "official" ]] && return 1
  warn "official locked uv sync failed; installing project from China PyPI mirror without lock file URLs"
  install_python_project_from_mirror "$mirror"
}

postgres_exec() {
  if command -v sudo >/dev/null 2>&1; then
    sudo -u postgres "$@"
  else
    runuser -u postgres -- "$@"
  fi
}

sql_literal() {
  printf "%s" "$1" | sed "s/'/''/g"
}

start_native_dependencies() {
  if command -v postgresql-setup >/dev/null 2>&1; then
    postgresql-setup --initdb 2>/dev/null || true
  fi
  systemctl enable --now postgresql \
    || systemctl enable --now postgresql-16 \
    || systemctl enable --now postgresql@16-main
  systemctl enable --now redis \
    || systemctl enable --now redis-server
}

append_secret_if_missing() {
  local name="$1"
  local value="$2"
  if ! grep -q "^${name}=" "$SECRETS_FILE"; then
    printf '%s=%s\n' "$name" "$value" >> "$SECRETS_FILE"
  fi
}

ensure_native_runtime_urls() {
  local postgres_db postgres_user postgres_password
  postgres_db="$(native_secret_value POSTGRES_DB)"
  postgres_user="$(native_secret_value POSTGRES_USER)"
  postgres_password="$(native_secret_value POSTGRES_PASSWORD)"
  postgres_db="${postgres_db:-agent_hub}"
  postgres_user="${postgres_user:-agent_hub}"
  [[ -n "$postgres_password" ]] || die "POSTGRES_PASSWORD is missing from $SECRETS_FILE"

  append_secret_if_missing \
    AGENT_HUB_DATABASE_URL \
    "postgresql+asyncpg://${postgres_user}:${postgres_password}@127.0.0.1:5432/${postgres_db}"
  append_secret_if_missing AGENT_HUB_REDIS_URL "redis://127.0.0.1:6379/0"
  append_secret_if_missing \
    DATABASE_URL \
    "postgresql+asyncpg://${postgres_user}:${postgres_password}@127.0.0.1:5432/${postgres_db}"
  append_secret_if_missing REDIS_URL "redis://127.0.0.1:6379/0"
}

write_litellm_config() {
  mkdir -p "$CONFIG_DIR"
  cat > "$CONFIG_DIR/litellm.yaml" <<'EOF'
model_list: []
litellm_settings:
  drop_params: true
  request_timeout: 600
EOF
  chown root:agent-hub "$CONFIG_DIR/litellm.yaml" 2>/dev/null || true
  chmod 0640 "$CONFIG_DIR/litellm.yaml"
}

configure_native_database() {
  local database_url postgres_db postgres_user postgres_password postgres_password_sql role_exists db_exists
  start_native_dependencies
  ensure_native_runtime_urls

  database_url="$(native_secret_value DATABASE_URL)"
  case "$database_url" in
    *127.0.0.1*|*localhost*) ;;
    *)
      log "external DATABASE_URL configured; skipping local PostgreSQL bootstrap"
      return 0
      ;;
  esac

  postgres_db="$(native_secret_value POSTGRES_DB)"
  postgres_user="$(native_secret_value POSTGRES_USER)"
  postgres_password="$(native_secret_value POSTGRES_PASSWORD)"
  postgres_db="${postgres_db:-agent_hub}"
  postgres_user="${postgres_user:-agent_hub}"
  [[ "$postgres_db" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || die "POSTGRES_DB must be a simple SQL identifier"
  [[ "$postgres_user" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || die "POSTGRES_USER must be a simple SQL identifier"
  postgres_password_sql="$(sql_literal "$postgres_password")"

  role_exists="$(postgres_exec psql -tAc "SELECT 1 FROM pg_roles WHERE rolname='${postgres_user}'" | tr -d '[:space:]')"
  if [[ "$role_exists" == "1" ]]; then
    postgres_exec psql -v ON_ERROR_STOP=1 \
      -c "ALTER ROLE \"${postgres_user}\" WITH LOGIN PASSWORD '${postgres_password_sql}';"
  else
    postgres_exec psql -v ON_ERROR_STOP=1 \
      -c "CREATE ROLE \"${postgres_user}\" LOGIN PASSWORD '${postgres_password_sql}';"
  fi

  db_exists="$(postgres_exec psql -tAc "SELECT 1 FROM pg_database WHERE datname='${postgres_db}'" | tr -d '[:space:]')"
  if [[ "$db_exists" != "1" ]]; then
    postgres_exec createdb -O "$postgres_user" "$postgres_db"
  fi
}

install_uv_from_official() {
  local installer
  installer="$(mktemp)"
  curl -fsSL https://astral.sh/uv/install.sh -o "$installer" || {
    rm -f "$installer"
    return 1
  }
  UV_INSTALL_DIR=/usr/local/bin sh "$installer" || {
    rm -f "$installer"
    return 1
  }
  rm -f "$installer"
}

install_uv_from_pypi_mirror() {
  local bootstrap python_bin
  python_bin="$(command -v python3 || true)"
  [[ -n "$python_bin" ]] || die "python3 not found; cannot bootstrap uv from PyPI mirror"
  bootstrap="$INSTALL_ROOT/bootstrap-uv"
  mkdir -p "$bootstrap"
  "$python_bin" -m venv "$bootstrap"
  "$bootstrap/bin/python" -m pip install --upgrade pip
  "$bootstrap/bin/python" -m pip install \
    -i "${AGENT_HUB_PYPI_MIRROR:-https://pypi.tuna.tsinghua.edu.cn/simple}" \
    uv
  ln -sfn "$bootstrap/bin/uv" /usr/local/bin/uv
}

ensure_native_uv() {
  if command -v uv >/dev/null 2>&1; then
    return 0
  fi
  log "installing uv runtime manager"
  local mode
  mode="$(native_mirror_mode)"
  if [[ "$mode" == "china" ]]; then
    install_uv_from_pypi_mirror
  elif [[ "$mode" == "official" ]]; then
    install_uv_from_official
  elif ! install_uv_from_official; then
    warn "official uv installer failed; retrying with China PyPI mirror"
    install_uv_from_pypi_mirror
  fi
  command -v uv >/dev/null 2>&1 || die "uv installation failed"
}

normalize_native_release_line_endings() {
  local release="$1"
  find "$release" -type f \( \
    -name '*.sh' \
    -o -name '*.service' \
    -o -name '*.target' \
    -o -name '*.socket' \
    -o -name '*.timer' \
    -o -name 'Caddyfile' \
  \) -exec sed -i 's/\r$//' {} +
}

normalize_native_systemd_units() {
  find /etc/systemd/system -maxdepth 1 -type f \( \
    -name 'agent-hub*.service' \
    -o -name 'agent-hub*.target' \
    -o -name 'agent-hub*.socket' \
    -o -name 'agent-hub*.timer' \
  \) -exec sed -i 's/\r$//' {} +
}

install_native_systemd_units() {
  install -m 0644 "$SCRIPT_DIR"/deploy/native/systemd/* /etc/systemd/system/
  normalize_native_systemd_units
}

native_public_url() {
  if [[ -f "$SECRETS_FILE" ]]; then
    native_secret_value AGENT_HUB_PUBLIC_URL
  else
    printf '%s\n' "${AGENT_HUB_PUBLIC_URL:-http://127.0.0.1}"
  fi
}

native_caddy_site() {
  local public_url
  public_url="$(native_public_url)"
  case "$public_url" in
    http://127.0.0.1*|https://127.0.0.1*|http://localhost*|https://localhost*)
      printf ':80\n'
      ;;
    *)
      printf '%s\n' "$public_url"
      ;;
  esac
}

deploy_native_release() {
  local release_id release python_bin
  release_id="$(date -u +%Y%m%d%H%M%S)"
  release="$INSTALL_ROOT/releases/$release_id"
  ensure_native_uv
  ensure_native_uv_dirs
  run_uv_python_install_with_mirror_fallback
  python_bin="$(native_python)"

  log "deploying native release $release"
  mkdir -p "$release"
  tar \
    --exclude='.git' \
    --exclude='.venv' \
    --exclude='.worktrees' \
    --exclude='web/node_modules' \
    --exclude='web/dist' \
    --exclude='.pytest_cache' \
    --exclude='.mypy_cache' \
    --exclude='.ruff_cache' \
    -cf - -C "$SCRIPT_DIR" . | tar -xf - -C "$release"
  normalize_native_release_line_endings "$release"

  (
    cd "$release"
    run_python_with_mirror_fallback uv venv --python "$python_bin" .venv
    sync_python_project_with_lock_or_mirror
    run_python_with_mirror_fallback uv pip install --python .venv/bin/python 'litellm[proxy]>=1.75,<2'
    if command -v npm >/dev/null 2>&1; then
      run_npm_with_mirror_fallback
      npm --prefix web run build
    else
      warn "npm not found; Web UI assets were not built"
    fi
  )

  chown -R agent-hub:agent-hub "$release" 2>/dev/null || true
  ln -sfn "$release" "$INSTALL_ROOT/current"
}

install_native_tls_assets() {
  local cert_file key_file cert_target key_target
  cert_file="$(native_secret_value AGENT_HUB_TLS_CERT_FILE)"
  key_file="$(native_secret_value AGENT_HUB_TLS_KEY_FILE)"

  if [[ -z "$cert_file" && -z "$key_file" ]]; then
    return 0
  fi
  [[ -n "$cert_file" && -n "$key_file" ]] || die "set both AGENT_HUB_TLS_CERT_FILE and AGENT_HUB_TLS_KEY_FILE"
  [[ -r "$cert_file" ]] || die "TLS certificate not readable: $cert_file"
  [[ -r "$key_file" ]] || die "TLS private key not readable: $key_file"

  mkdir -p "$CONFIG_DIR/tls"
  cert_target="$CONFIG_DIR/tls/server.crt"
  key_target="$CONFIG_DIR/tls/server.key"
  install -m 0644 "$cert_file" "$cert_target"
  install -m 0640 "$key_file" "$key_target"
  if getent group caddy >/dev/null 2>&1; then
    chgrp caddy "$key_target"
  else
    chmod 0644 "$key_target"
    warn "caddy group not found; TLS key is readable by local users"
  fi
}

install_native_caddy() {
  local caddy_dir caddyfile site web_root api_port cert_file key_file tls_directive public_url
  caddy_dir="${AGENT_HUB_CADDY_DIR:-/etc/caddy}"
  caddyfile="$caddy_dir/Caddyfile"
  site="$(native_caddy_site)"
  public_url="$(native_public_url)"
  web_root="$INSTALL_ROOT/current/web/dist"
  api_port="$(native_secret_value AGENT_HUB_API_PORT)"
  api_port="${api_port:-8000}"
  install_native_tls_assets
  cert_file="$CONFIG_DIR/tls/server.crt"
  key_file="$CONFIG_DIR/tls/server.key"
  tls_directive=""
  if [[ -f "$cert_file" && -f "$key_file" ]]; then
    [[ "$public_url" == https://* ]] || die "user-supplied TLS certificates require AGENT_HUB_PUBLIC_URL=https://your-domain"
    tls_directive="  tls $cert_file $key_file"
  fi
  mkdir -p "$caddy_dir"
  cat > "$caddyfile" <<EOF
$site {
  encode gzip
$tls_directive

  handle /api/* {
    reverse_proxy 127.0.0.1:$api_port
  }

  handle /health/* {
    reverse_proxy 127.0.0.1:$api_port
  }

  handle /channels/* {
    reverse_proxy 127.0.0.1:$api_port
  }

  handle /metrics {
    reverse_proxy 127.0.0.1:$api_port
  }

  handle {
    root * $web_root
    try_files {path} /index.html
    file_server
  }
}
EOF
  chmod 0644 "$caddyfile"
}

run_native_migrations() {
  log "running native database migrations"
  (
    cd "$INSTALL_ROOT/current"
    set -a
    # shellcheck disable=SC1090
    source "$SECRETS_FILE"
    set +a
    .venv/bin/alembic upgrade head
  )
}

run_native_bootstrap_seed() {
  log "seeding one-time setup code"
  (
    cd "$INSTALL_ROOT/current"
    set -a
    # shellcheck disable=SC1090
    source "$SECRETS_FILE"
    set +a
    .venv/bin/python -m agent_hub.cli.bootstrap \
      --code-env AGENT_HUB_SETUP_CODE \
      --database-url-env AGENT_HUB_DATABASE_URL \
      --minutes 60
  )
}

install_native_mode() {
  "$SCRIPT_DIR/deploy/native/install-packages.sh" --local-db --local-redis
  mkdir -p "$INSTALL_ROOT/releases" "$STATE_DIR"
  install -m 0644 "$SCRIPT_DIR/deploy/native/agent-hub.sysusers" /usr/lib/sysusers.d/agent-hub.conf 2>/dev/null || true
  install -m 0644 "$SCRIPT_DIR/deploy/native/agent-hub.tmpfiles" /usr/lib/tmpfiles.d/agent-hub.conf 2>/dev/null || true
  if command -v systemd-sysusers >/dev/null 2>&1; then
    systemd-sysusers /usr/lib/sysusers.d/agent-hub.conf
  fi
  if command -v systemd-tmpfiles >/dev/null 2>&1; then
    systemd-tmpfiles --create /usr/lib/tmpfiles.d/agent-hub.conf
  else
    mkdir -p /run/agent-hub "$STATE_DIR" /var/log/agent-hub
    chown agent-hub:agent-hub /run/agent-hub "$STATE_DIR" /var/log/agent-hub 2>/dev/null || true
    chmod 0750 /run/agent-hub "$STATE_DIR" /var/log/agent-hub
  fi
  deploy_native_release
  write_litellm_config
  install_native_caddy
  install_native_systemd_units
  configure_native_database
  run_native_migrations
  run_native_bootstrap_seed
  systemctl daemon-reload
  systemctl enable --now caddy
  systemctl enable --now agent-hub.target
  mark_stage "native-up"
}
