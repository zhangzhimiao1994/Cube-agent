#!/usr/bin/env bash

AGENT_HUB_SOURCE_DIR="${AGENT_HUB_SOURCE_DIR:-$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)}"

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

run_with_timeout() {
  local seconds="$1"
  shift
  if command -v timeout >/dev/null 2>&1; then
    timeout "$seconds" "$@"
    return
  fi
  "$@"
}

native_uv_sync_locked() {
  run_with_timeout \
    "${AGENT_HUB_UV_SYNC_TIMEOUT_SECONDS:-900}" \
    env \
    UV_PYTHON_INSTALL_DIR="${AGENT_HUB_UV_PYTHON_INSTALL_DIR:-$INSTALL_ROOT/uv-python}" \
    UV_CACHE_DIR="${AGENT_HUB_UV_CACHE_DIR:-$INSTALL_ROOT/uv-cache}" \
    uv sync --frozen --no-dev
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

native_node_version_ok() {
  local version major minor
  version="$("${1:-node}" -v 2>/dev/null || true)"
  version="${version#v}"
  major="${version%%.*}"
  minor="${version#*.}"
  minor="${minor%%.*}"
  [[ "$major" =~ ^[0-9]+$ && "$minor" =~ ^[0-9]+$ ]] || return 1
  (( major > 20 )) && return 0
  (( major == 20 && minor >= 19 )) && return 0
  return 1
}

native_node_arch() {
  case "$(uname -m)" in
    x86_64|amd64) printf 'x64\n' ;;
    aarch64|arm64) printf 'arm64\n' ;;
    *)
      die "unsupported CPU architecture for bundled Node.js: $(uname -m)"
      ;;
  esac
}

native_node_env() {
  local node_home
  node_home="${AGENT_HUB_NODE_HOME:-$INSTALL_ROOT/node}"
  env PATH="$node_home/bin:$PATH" "$@"
}

native_node_home() {
  local configured resolved protected
  configured="${AGENT_HUB_NODE_HOME:-$INSTALL_ROOT/node}"
  [[ "$configured" == /* && ! -L "$configured" ]] \
    || die "dedicated Node home must be an absolute directory, not a symlink"
  resolved="$(realpath -m -- "$configured")" || return 1
  case "$resolved" in
    /|/usr|/usr/local|/usr/bin|/usr/sbin|/usr/lib|/usr/lib64|/usr/local/bin|/usr/local/sbin|/usr/local/lib|/usr/local/lib64|/bin|/sbin|/lib|/lib64|/opt|/var|/var/lib|/srv|/home|/root|/tmp|/run)
      die "refusing shared system directory as dedicated Node home"
      ;;
  esac
  for protected in "$INSTALL_ROOT" "${STATE_DIR:-/var/lib/agent-hub}" "${CONFIG_DIR:-/etc/agent-hub}"; do
    protected="$(realpath -m -- "$protected")" || return 1
    case "$protected/" in
      "$resolved/"*) die "dedicated Node home must not contain installation, state or config roots" ;;
    esac
  done
  printf '%s\n' "$resolved"
}

normalize_native_node_ownership() {
  local node_home hardlinks
  node_home="$(native_node_home)" || return 1
  [[ -d "$node_home" ]] || die "dedicated Node home is not a directory"
  # Do not follow npm's symlinks, including links outside the dedicated tree.
  hardlinks="$(find -P "$node_home" -xdev -type f -links +1 -print -quit)" \
    || die "cannot inspect dedicated Node home"
  [[ -z "$hardlinks" ]] \
    || die "dedicated Node home contains hardlinked files"
  chown -hR -P root:root -- "$node_home" \
    || die "cannot normalize dedicated Node ownership"
}

download_native_node() {
  local version arch node_home archive tmp_dir url mirror_url
  version="${AGENT_HUB_NODE_VERSION:-22.12.0}"
  arch="$(native_node_arch)"
  node_home="$(native_node_home)" || return 1
  tmp_dir="$(mktemp -d)"
  archive="$tmp_dir/node.tar.xz"
  url="https://nodejs.org/dist/v${version}/node-v${version}-linux-${arch}.tar.xz"
  mirror_url="${AGENT_HUB_NODE_MIRROR:-https://npmmirror.com/mirrors/node}/v${version}/node-v${version}-linux-${arch}.tar.xz"

  mkdir -p "$node_home"
  if [[ "$(native_mirror_mode)" == "china" ]]; then
    curl -fsSL "$mirror_url" -o "$archive"
  elif ! curl -fsSL "$url" -o "$archive"; then
    [[ "$(native_mirror_mode)" == "official" ]] && return 1
    warn "official Node.js download failed; retrying with China Node.js mirror"
    curl -fsSL "$mirror_url" -o "$archive"
  fi

  rm -rf "${node_home:?}/"*
  tar -xJf "$archive" -C "$node_home" --strip-components=1 --no-same-owner
  rm -rf "$tmp_dir"
  normalize_native_node_ownership
  chmod -R a+rX "$node_home"
}

native_node_runtime_ok() {
  local node_home="$1"
  [[ -x "$node_home/bin/node" && -x "$node_home/bin/npm" \
    && -f "$node_home/lib/node_modules/npm/bin/npm-cli.js" ]] || return 1
  native_node_version_ok "$node_home/bin/node" \
    && "$node_home/bin/node" "$node_home/lib/node_modules/npm/bin/npm-cli.js" --version >/dev/null
}

ensure_native_nodejs() {
  local node_home
  node_home="$(native_node_home)" || return 1
  if [[ -d "$node_home" ]]; then
    normalize_native_node_ownership || return 1
  fi
  if native_node_runtime_ok "$node_home"; then
    export PATH="$node_home/bin:$PATH"
    return 0
  fi
  warn "dedicated Node.js 20.19+ is required for previews and the Web UI; installing bundled Node.js"
  download_native_node || return 1
  native_node_runtime_ok "$node_home" \
    || die "dedicated Node.js runtime is missing or does not satisfy version requirements"
  export PATH="$node_home/bin:$PATH"
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
  fix_native_uv_permissions
}

fix_native_uv_permissions() {
  local python_dir cache_dir
  python_dir="${AGENT_HUB_UV_PYTHON_INSTALL_DIR:-$INSTALL_ROOT/uv-python}"
  cache_dir="${AGENT_HUB_UV_CACHE_DIR:-$INSTALL_ROOT/uv-cache}"
  [[ -d "$python_dir" ]] && chmod -R a+rX "$python_dir"
  [[ -d "$cache_dir" ]] && chmod 0755 "$cache_dir"
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

  if native_uv_sync_locked; then
    return 0
  fi

  [[ "$mode" == "official" ]] && return 1
  warn "official locked uv sync failed; installing project from China PyPI mirror without lock file URLs"
  install_python_project_from_mirror "$mirror"
}

install_litellm_proxy_venv() {
  local python_bin="$1"
  log "installing LiteLLM proxy into isolated virtualenv"
  run_python_with_mirror_fallback uv venv --python "$python_bin" .litellm-venv
  run_python_with_mirror_fallback uv pip install \
    --python .litellm-venv/bin/python \
    'litellm[proxy]>=1.75,<2'
  verify_litellm_proxy_venv
}

verify_litellm_proxy_venv() {
  [[ -x .litellm-venv/bin/litellm ]] \
    || die "LiteLLM proxy install failed: .litellm-venv/bin/litellm is missing or not executable"

  if ! .litellm-venv/bin/python - <<'PY'
import importlib.util

required_modules = ("litellm", "litellm.proxy.proxy_server")
missing = [name for name in required_modules if importlib.util.find_spec(name) is None]
if missing:
    raise SystemExit("missing modules: " + ", ".join(missing))
PY
  then
    die "LiteLLM proxy install failed: proxy_server module is missing; ensure litellm[proxy] installed successfully"
  fi

  if ! .litellm-venv/bin/litellm --help >/dev/null; then
    die "LiteLLM proxy install failed: litellm CLI cannot start"
  fi
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
}

write_litellm_config() {
  mkdir -p "$CONFIG_DIR"
  chown root:agent-hub "$CONFIG_DIR" 2>/dev/null || true
  chmod 0750 "$CONFIG_DIR"
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

  database_url="$(native_secret_value AGENT_HUB_DATABASE_URL)"
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
  find "$release" -type f \( -name '*.sh' -o -path '*/scripts/agent-hub' \) \
    -exec chmod 0755 {} +
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
  remove_legacy_native_skill_unit
  install -m 0644 "$AGENT_HUB_SOURCE_DIR"/deploy/native/systemd/* /etc/systemd/system/
  normalize_native_systemd_units
}

remove_legacy_native_skill_unit() {
  systemctl stop 'agent-hub-skill@*.service' >/dev/null 2>&1 || true
  rm -f /etc/systemd/system/agent-hub-skill@.service
}

write_native_preview_broker_config() {
  local allowed_uid temporary workspace_root node_home
  allowed_uid="$(id -u agent-hub)" \
    || die "cannot resolve agent-hub UID for preview broker"
  [[ "$allowed_uid" =~ ^[1-9][0-9]*$ ]] \
    || die "preview broker requires a non-root agent-hub UID"
  node_home="$(native_node_home)" || return 1
  workspace_root="${AGENT_HUB_PROJECT_WORKSPACE_DIR-$STATE_DIR/workspaces}"
  # Match the API EnvironmentFile override without evaluating or loading secrets.
  if [[ -f "${SECRETS_FILE:-}" ]] \
    && grep -q '^AGENT_HUB_PROJECT_WORKSPACE_DIR=' "$SECRETS_FILE"; then
    workspace_root="$(native_secret_value AGENT_HUB_PROJECT_WORKSPACE_DIR)"
  fi
  mkdir -p "$CONFIG_DIR"
  temporary="$(mktemp "$CONFIG_DIR/.preview-broker.json.XXXXXX")" || return 1
  if ! env -i PATH=/usr/bin:/bin "$INSTALL_ROOT/current/.venv/bin/python" - \
    "$allowed_uid" "$workspace_root" \
    "$INSTALL_ROOT/current/src/agent_hub" "$node_home" > "$temporary" <<'PY'
import json
import shlex
import sys
from pathlib import PurePosixPath

uid = int(sys.argv[1])
if uid <= 0:
    raise SystemExit("preview broker requires a non-root console UID")
workspace = sys.argv[2].strip()
if workspace.startswith(("'", '"')):
    values = shlex.split(workspace)
    if len(values) != 1:
        raise SystemExit("invalid preview workspace setting")
    workspace = values[0]
if (not workspace.startswith("/") or ".." in PurePosixPath(workspace).parts
        or any(char.isspace() or char in ': %\\"' for char in workspace)):
    raise SystemExit("preview workspace must be an absolute systemd-safe path")
json.dump({
    "workspace_root": workspace,
    "allowed_uid": uid,
    "runtime_root": "/run/agent-hub-preview",
    "trusted_source_root": sys.argv[3],
    "node_root": sys.argv[4],
}, sys.stdout)
sys.stdout.write("\n")
PY
  then
    rm -f -- "$temporary"
    die "cannot generate preview broker policy"
  fi
  if ! chown root:root "$temporary" || ! chmod 0600 "$temporary" \
    || ! mv -fT -- "$temporary" "$CONFIG_DIR/preview-broker.json"; then
    rm -f -- "$temporary"
    die "cannot publish root-only preview broker policy"
  fi
}

require_native_preview_broker() {
  if ! runuser -u agent-hub -- env -i \
    PATH=/usr/bin:/bin PYTHONDONTWRITEBYTECODE=1 \
    "$INSTALL_ROOT/current/.venv/bin/python" - /run/agent-hub/preview-broker.sock <<'PY'
import json
import socket
import struct
import sys
import time

deadline = time.monotonic() + 60
with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
    connection.settimeout(60)
    connection.connect(sys.argv[1])
    request = b'{"version":1,"action":"probe"}'
    connection.sendall(struct.pack("!I", len(request)) + request)

    def read_exact(size):
        data = bytearray()
        while len(data) < size:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise SystemExit("preview probe timed out")
            connection.settimeout(remaining)
            part = connection.recv(size - len(data))
            if not part:
                raise SystemExit("preview probe connection closed")
            data.extend(part)
        return bytes(data)

    size = struct.unpack("!I", read_exact(4))[0]
    if not 0 < size <= 4096:
        raise SystemExit("invalid preview probe frame")
    response = json.loads(read_exact(size))
    if response != {"ok": True, "state": "probe"} or response.get("ok") is not True:
        raise SystemExit("preview isolation unavailable")
PY
  then
    die "native preview broker isolation probe failed"
  fi
  log "native preview broker isolation probe is ready"
}

probe_native_plugin_sandbox() {
  [[ -x /usr/bin/bwrap ]] || return 1
  command -v runuser >/dev/null 2>&1 || return 1
  runuser -u agent-hub -- /usr/bin/bwrap \
    --die-with-parent \
    --new-session \
    --unshare-net \
    --unshare-pid \
    --unshare-ipc \
    --unshare-uts \
    --ro-bind / / \
    --dev /dev \
    --proc /proc \
    -- /bin/true
}

install_native_plugin_sandbox_profile() {
  local restriction profile_source profile_target
  restriction="/proc/sys/kernel/apparmor_restrict_unprivileged_userns"
  profile_source="$AGENT_HUB_SOURCE_DIR/deploy/native/apparmor/agent-hub-bwrap"
  profile_target="/etc/apparmor.d/agent-hub-bwrap"

  [[ -x /usr/bin/bwrap ]] || die "bubblewrap is required for local process plugins"
  if [[ -r "$restriction" && "$(cat "$restriction")" == "1" ]]; then
    command -v apparmor_parser >/dev/null 2>&1 \
      || die "AppArmor parser is required for the bubblewrap sandbox"
    install -D -m 0644 "$profile_source" "$profile_target"
    apparmor_parser -r "$profile_target"
  fi
  probe_native_plugin_sandbox \
    || die "bubblewrap cannot create the required sandbox; check AppArmor and user namespace policy"
}

native_public_url() {
  if [[ -f "$SECRETS_FILE" ]]; then
    native_secret_value AGENT_HUB_PUBLIC_URL
  else
    detect_public_url
  fi
}

native_caddy_site() {
  local public_url
  public_url="$(native_public_url)"
  case "$public_url" in
    http://127.0.0.1*|https://127.0.0.1*|http://localhost*|https://localhost*)
      printf ':80\n'
      ;;
    http://*)
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
  fix_native_uv_permissions
  python_bin="$(native_python)"

  log "deploying native release $release"
  mkdir -p "$release"
  tar \
    --exclude='.git' \
    --exclude='.venv' \
    --exclude='.litellm-venv' \
    --exclude='.worktrees' \
    --exclude='web/node_modules' \
    --exclude='web/dist' \
    --exclude='.pytest_cache' \
    --exclude='.mypy_cache' \
    --exclude='.ruff_cache' \
    -cf - -C "$AGENT_HUB_SOURCE_DIR" . | tar -xf - -C "$release"
  normalize_native_release_line_endings "$release"

  (
    cd "$release" || exit
    run_python_with_mirror_fallback uv venv --python "$python_bin" .venv
    sync_python_project_with_lock_or_mirror
    install_litellm_proxy_venv "$python_bin"
    ensure_native_nodejs
    chown -R agent-hub:agent-hub web/node_modules 2>/dev/null || true
    run_npm_with_mirror_fallback
    npm --prefix web run build
  )

  fix_native_release_permissions "$release"
  ln -sfn "$release" "$INSTALL_ROOT/current"
  chown -h root:root "$INSTALL_ROOT/current" 2>/dev/null || true
  prune_native_releases
}

fix_native_release_permissions() {
  local release="${1:-}"
  [[ -n "$release" ]] || release="$(readlink -f "$INSTALL_ROOT/current" 2>/dev/null || true)"
  [[ -n "$release" && -d "$release" ]] || return 0

  chown -R root:agent-hub "$release"
  chmod 0755 "$INSTALL_ROOT" "$INSTALL_ROOT/releases"
  chmod -R u+rwX,g+rX,o-rwx "$release"
  chmod -R g-w,o-rwx "$release"
  chmod 0755 "$release"
  find "$release" -type f -exec chmod u-s,g-s {} +
  if [[ -d "$release/web" ]]; then
    chmod 0755 "$release/web"
  fi
  if [[ -d "$release/web/dist" ]]; then
    chmod 0755 "$release/web/dist"
    chmod -R a+rX "$release/web/dist"
  fi
}

prune_native_releases() {
  local keep current_release release resolved_release kept
  keep="${AGENT_HUB_RELEASES_TO_KEEP:-2}"
  [[ "$keep" =~ ^[0-9]+$ ]] || keep=2
  (( keep >= 1 )) || keep=1
  [[ -d "$INSTALL_ROOT/releases" ]] || return 0

  current_release="$(readlink -f "$INSTALL_ROOT/current" 2>/dev/null || true)"
  kept=0
  while IFS= read -r release; do
    [[ -n "$release" ]] || continue
    resolved_release="$(readlink -f "$release" 2>/dev/null || true)"
    [[ -n "$resolved_release" && -d "$resolved_release" ]] || continue
    case "$resolved_release" in
      "$INSTALL_ROOT/releases/"*) ;;
      *) continue ;;
    esac
    if [[ "$resolved_release" == "$current_release" ]]; then
      (( kept += 1 ))
      continue
    fi
    if (( kept < keep )); then
      (( kept += 1 ))
      continue
    fi
    log "pruning old native release $resolved_release"
    rm -rf -- "$resolved_release"
  done < <(
    find "$INSTALL_ROOT/releases" -mindepth 1 -maxdepth 1 -type d -printf '%T@ %p\n' \
      | sort -rn \
      | cut -d' ' -f2-
  )
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

  handle /health {
    reverse_proxy 127.0.0.1:$api_port
  }

  handle /health/* {
    reverse_proxy 127.0.0.1:$api_port
  }

  handle /openapi.json {
    reverse_proxy 127.0.0.1:$api_port
  }

  handle /setup* {
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

require_native_service_active() {
  local unit="$1"
  local attempt
  command -v systemctl >/dev/null 2>&1 || return 0
  for attempt in {1..15}; do
    if systemctl is-active --quiet "$unit"; then
      return 0
    fi
    [[ "$attempt" -lt 15 ]] && sleep 1
  done
  systemctl status "$unit" --no-pager -l >&2 || true
  journalctl -u "$unit" -n 120 --no-pager >&2 || true
  die "$unit did not become active after install; inspect the logs above"
}

require_native_readiness() {
  local ready_url="http://127.0.0.1:${AGENT_HUB_API_PORT:-8000}/health/ready"
  local timeout="${AGENT_HUB_NATIVE_READY_TIMEOUT_SECONDS:-120}"
  local poll_interval="${AGENT_HUB_NATIVE_READY_POLL_INTERVAL_SECONDS:-2}"
  local started="$SECONDS"
  local status=""
  if ! command -v curl >/dev/null 2>&1; then
    die "curl is required to verify native readiness"
  fi
  if [[ ! "$timeout" =~ ^[1-9][0-9]*$ || ! "$poll_interval" =~ ^[1-9][0-9]*$ ]]; then
    die "native readiness timeout and poll interval must be positive integers"
  fi
  while true; do
    status="$(curl --noproxy '*' \
      --connect-timeout 2 \
      --max-time 5 \
      -sS -o /dev/null -w '%{http_code}' \
      "$ready_url" 2>/dev/null || true)"
    if [[ "$status" == "200" ]]; then
      log "native readiness reached 200"
      return 0
    fi
    if ((SECONDS - started >= timeout)); then
      die "native readiness did not reach 200: ${status:-curl-error}"
    fi
    sleep "$poll_interval"
  done
}

require_native_plugin_package_runtime() {
  local runtime_url="http://127.0.0.1:${AGENT_HUB_API_PORT:-8000}/health/plugin-package-runtime"
  local payload=""
  if ! command -v curl >/dev/null 2>&1; then
    die "curl is required to verify native plugin package runtime registration"
  fi
  payload="$(curl --noproxy '*' \
    --connect-timeout 2 \
    --max-time 5 \
    -sS \
    "$runtime_url" 2>/dev/null || true)"
  if [[ "$payload" != *'"registration_status":"ready"'* ]]; then
    die "native plugin package runtime registration is not ready"
  fi
  log "native plugin package runtime registration is ready"
}

run_native_migrations() {
  log "running native database migrations"
  (
    cd "$INSTALL_ROOT/current" || exit
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
    cd "$INSTALL_ROOT/current" || exit
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
  bash "$AGENT_HUB_SOURCE_DIR/deploy/native/install-packages.sh" --local-db --local-redis
  mkdir -p "$INSTALL_ROOT/releases" "$STATE_DIR"
  install -m 0644 "$AGENT_HUB_SOURCE_DIR/deploy/native/agent-hub.sysusers" /usr/lib/sysusers.d/agent-hub.conf 2>/dev/null || true
  install -m 0644 "$AGENT_HUB_SOURCE_DIR/deploy/native/agent-hub.tmpfiles" /usr/lib/tmpfiles.d/agent-hub.conf 2>/dev/null || true
  if command -v systemd-sysusers >/dev/null 2>&1; then
    systemd-sysusers /usr/lib/sysusers.d/agent-hub.conf
  fi
  if command -v systemd-tmpfiles >/dev/null 2>&1; then
    systemd-tmpfiles --create /usr/lib/tmpfiles.d/agent-hub.conf
  else
    mkdir -p /run/agent-hub "$STATE_DIR" /var/log/agent-hub
    chown agent-hub:agent-hub /run/agent-hub "$STATE_DIR" /var/log/agent-hub 2>/dev/null || true
    chmod 0750 /run/agent-hub "$STATE_DIR" /var/log/agent-hub
    install -d -o root -g root -m 0700 /run/agent-hub-preview
  fi
  install_native_plugin_sandbox_profile
  deploy_native_release
  write_native_preview_broker_config
  write_litellm_config
  install_native_caddy
  install_native_systemd_units
  configure_native_database
  run_native_migrations
  run_native_bootstrap_seed
  systemctl daemon-reload
  systemctl enable caddy
  systemctl reload-or-restart caddy || systemctl restart caddy
  systemctl enable --now agent-hub-skill-broker.socket
  systemctl enable --now agent-hub-preview-broker.socket
  systemctl enable agent-hub.target
  systemctl restart agent-hub-skill-broker.service
  systemctl restart agent-hub-preview-broker.service
  systemctl restart agent-hub-litellm.service
  systemctl restart agent-hub-worker.service
  require_native_service_active agent-hub-skill-broker.socket
  require_native_service_active agent-hub-preview-broker.socket
  require_native_service_active agent-hub-preview-broker.service
  require_native_service_active caddy.service
  require_native_service_active agent-hub-worker.service
  require_native_service_active agent-hub-litellm.service
  # Start admissions after the new worker is active. Starting active target
  # members is idempotent; do not restart the worker a second time.
  systemctl restart agent-hub-api.service
  require_native_service_active agent-hub-api.service
  systemctl start agent-hub.target
  require_native_readiness
  require_native_plugin_package_runtime
  require_native_preview_broker
  mark_stage "native-up"
}
