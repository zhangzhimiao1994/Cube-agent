from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


def read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def test_native_installer_deploys_release_before_starting_services() -> None:
    script = read("scripts/lib/install_native.sh")

    assert "deploy_native_release" in script
    assert "normalize_native_release_line_endings" in script
    assert "ln -sfn" in script
    assert '"$INSTALL_ROOT/current"' in script
    assert "uv sync --frozen --no-dev" in script

    deploy = script.index("deploy_native_release")
    start = script.index("systemctl enable --now agent-hub.target")
    assert deploy < start


def test_native_installer_creates_runtime_dirs_and_migrates_before_services() -> None:
    script = read("scripts/lib/install_native.sh")

    tmpfiles = script.index("systemd-tmpfiles --create")
    database = script.index("configure_native_database")
    migrations = script.index("alembic upgrade head")
    start = script.index("systemctl enable --now agent-hub.target")

    assert tmpfiles < start
    assert database < migrations
    assert migrations < start


def test_native_installer_starts_local_dependencies_and_writes_runtime_urls() -> None:
    script = read("scripts/lib/install_native.sh")
    secrets = read("scripts/lib/secrets.sh")

    assert "DATABASE_URL=" in secrets
    assert "REDIS_URL=" in secrets
    assert "systemctl enable --now postgresql" in script
    assert "systemctl enable --now redis" in script
    assert "createdb" in script


def test_native_database_bootstrap_avoids_psql_variable_identifier_interpolation() -> None:
    script = read("scripts/lib/install_native.sh")

    assert ':"role"' not in script
    assert ":'password'" not in script
    assert "sql_literal" in script
    assert 'CREATE ROLE \\"${postgres_user}\\" LOGIN PASSWORD' in script
    assert 'ALTER ROLE \\"${postgres_user}\\" WITH LOGIN PASSWORD' in script


def test_native_installer_normalizes_release_and_systemd_line_endings() -> None:
    script = read("scripts/lib/install_native.sh")

    assert "normalize_native_release_line_endings" in script
    assert "normalize_native_systemd_units" in script
    assert "sed -i 's/\\r$//'" in script
    assert "-name '*.sh'" in script
    assert "-name '*.service'" in script


def test_installer_defaults_management_url_to_external_address() -> None:
    secrets = read("scripts/lib/secrets.sh")

    assert "detect_public_url" in secrets
    assert "api.ipify.org" in secrets
    assert "hostname -I" in secrets


def test_native_api_stays_private_and_caddy_exposes_management_ui() -> None:
    api_unit = read("deploy/native/systemd/agent-hub-api.service")
    caddyfile = read("deploy/native/Caddyfile")
    installer = read("scripts/lib/install_native.sh")

    assert "--host ${AGENT_HUB_API_BIND_HOST:-127.0.0.1}" in api_unit
    assert "reverse_proxy 127.0.0.1:8000" in caddyfile
    assert "AGENT_HUB_WEB_DIR=/opt/agent-hub/current/web/dist" in api_unit
    assert "http://*)\n      printf ':80" in installer
    assert "handle /setup*" in caddyfile
    assert "handle /setup*" in installer
    assert 'chmod 0755 "$release" "$release/web" "$release/web/dist"' in installer
    assert 'chmod -R a+rX "$release/web/dist"' in installer
    assert "chown -R agent-hub:agent-hub" in installer


def test_native_caddy_supports_user_supplied_tls_certificate() -> None:
    script = read("scripts/lib/install_native.sh")
    secrets = read("scripts/lib/secrets.sh")

    assert "AGENT_HUB_TLS_CERT_FILE" in secrets
    assert "AGENT_HUB_TLS_KEY_FILE" in secrets
    assert "tls $cert_file $key_file" in script


def test_native_install_packages_installs_uv_runtime_dependencies() -> None:
    packages = read("deploy/native/install-packages.sh")
    installer = read("scripts/lib/install_native.sh")

    assert "python3-venv" in packages
    assert "nodejs" in packages
    assert "npm" in packages
    assert "uv python install 3.12" in installer
    assert "uv venv --python" in installer
    assert "UV_PYTHON_INSTALL_DIR" in installer
    assert "${AGENT_HUB_UV_PYTHON_INSTALL_DIR:-$INSTALL_ROOT/uv-python}" in installer
    assert "native_uv_env uv python install 3.12" in installer
    assert "native_uv_env uv python find 3.12" in installer


def test_native_installer_falls_back_to_china_mirrors_when_official_sources_fail() -> None:
    packages = read("deploy/native/install-packages.sh")
    installer = read("scripts/lib/install_native.sh")
    docker = read("scripts/lib/install_docker.sh")

    assert "AGENT_HUB_MIRROR_MODE" in packages
    assert "configure_china_package_mirror" in packages
    assert "install_with_mirror_fallback" in packages
    assert "pypi.tuna.tsinghua.edu.cn" in installer
    assert "UV_DEFAULT_INDEX" in installer
    assert "registry.npmmirror.com" in installer
    assert "docker.io" in docker
    assert "registry.cn-hangzhou.aliyuncs.com" in docker


def test_native_installer_uses_mirror_install_without_locked_wheel_urls_in_china_mode() -> None:
    script = read("scripts/lib/install_native.sh")

    assert "sync_python_project_with_lock_or_mirror" in script
    assert 'if [[ "$mode" == "china" ]]; then\n    install_python_project_from_mirror "$mirror"\n    return\n  fi' in script
    assert "locked uv sync is skipped in China mirror mode" in script
    assert "uv pip install --python .venv/bin/python" in script
    assert "--index-url" in script


def test_native_installer_falls_back_from_locked_uv_sync_to_mirror_pip_install() -> None:
    script = read("scripts/lib/install_native.sh")

    assert "sync_python_project_with_lock_or_mirror" in script
    assert "uv sync --frozen --no-dev" in script
    assert "uv pip install --python .venv/bin/python" in script
    assert "--index-url" in script
    assert "official locked uv sync failed" in script
