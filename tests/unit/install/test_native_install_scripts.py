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


def test_auto_mode_prefers_native_on_supported_systemd_hosts() -> None:
    detect = read("scripts/lib/detect.sh")
    install = read("install.sh")
    readme = read("README.md")
    installation = read("docs/installation.md")

    assert (
        'if [[ "$HAS_SYSTEMD" -eq 1 && "$HOST_MANAGER" != "unknown" ]]; then\n'
        '      MODE="native"'
    ) in detect
    assert 'elif [[ "$HAS_DOCKER" -eq 1 || "$HOST_MANAGER" == "unknown" ]]; then' in detect
    assert "chooses native on supported systemd apt/dnf hosts" in install
    assert "prefers native mode" in readme
    assert "Native mode when systemd plus apt/dnf support are detected" in installation


def test_native_installer_prunes_old_releases_after_successful_deploy() -> None:
    script = read("scripts/lib/install_native.sh")

    assert "prune_native_releases" in script
    assert "AGENT_HUB_RELEASES_TO_KEEP" in script
    assert "readlink -f \"$INSTALL_ROOT/current\"" in script
    assert 'resolved_release="$(readlink -f "$release"' in script
    assert 'case "$resolved_release" in' in script
    assert '"$INSTALL_ROOT/releases/"*)' in script
    assert 'rm -rf -- "$resolved_release"' in script

    link_current = script.index('ln -sfn "$release" "$INSTALL_ROOT/current"')
    prune = script.index("prune_native_releases")
    assert link_current < prune


def test_installer_failure_output_includes_context_and_hints() -> None:
    common = read("scripts/lib/common.sh")
    install = read("install.sh")

    assert 'installer_failed "$LINENO" "$?" "$BASH_COMMAND"' in install
    assert "Failed stage:" in common
    assert "Failed command:" in common
    assert "Common causes and checks:" in common
    assert "journalctl -u agent-hub-api" in common
    assert "systemctl status caddy" in common
    assert "curl -v http://127.0.0.1:8000/health/ready" in common


def test_installer_preflights_required_support_files_before_sourcing() -> None:
    install = read("install.sh")
    common = read("scripts/lib/common.sh")
    docker = read("scripts/lib/install_docker.sh")

    assert "AGENT_HUB_SOURCE_DIR" in install
    assert "AGENT_HUB_SOURCE_DIR" in common
    assert "AGENT_HUB_SOURCE_DIR" in docker
    assert "normalize_installer_tree" in install
    assert "sed -i 's/\\r$//'" in install
    assert "chmod 0755" in install
    assert "require_installer_files" in install
    assert "installation package is incomplete" in install
    assert install.index("require_installer_files") < install.index(
        'source "$SCRIPT_DIR/scripts/lib/common.sh"'
    )
    assert '"$SCRIPT_DIR/scripts/lib/install_docker.sh"' in install
    assert '"$SCRIPT_DIR/scripts/lib/install_native.sh"' in install
    assert 'bash "$AGENT_HUB_SOURCE_DIR/scripts/agent-hub" doctor' in common
    assert 'cp -R "$AGENT_HUB_SOURCE_DIR/deploy/compose"' in docker


def test_readme_archive_install_extracts_into_isolated_source_directory() -> None:
    readme = read("README.md")

    assert "mktemp -d /tmp/agent-hub-install" in readme
    assert 'mkdir -p "$tmp/source"' in readme
    assert 'tar -xzf "$tmp/source.tar.gz" --strip-components=1 -C "$tmp/source"' in readme
    assert 'cd "$tmp/source"' in readme
    assert "Do not extract the archive directly into `/root`" in readme


def test_install_verification_uses_public_url_for_docker_mode() -> None:
    verify = read("scripts/lib/verify.sh")

    assert "installation_health_base_url" in verify
    assert '[[ "${MODE:-}" == "docker" ]]' in verify
    assert "AGENT_HUB_PUBLIC_URL" in verify
    assert "verify_native_service agent-hub-api.service" in verify
    assert "verify_native_service agent-hub-worker.service" in verify
    assert "verify_native_service agent-hub-litellm.service" in verify
    assert "verify_native_litellm_proxy" in verify
    assert ".litellm-venv/bin/litellm" in verify
    assert "litellm.proxy.proxy_server" in verify
    assert 'verify_url "$base_url/health/live"' in verify
    assert 'verify_url "$base_url/health/ready"' in verify


def test_native_installer_creates_runtime_dirs_and_migrates_before_services() -> None:
    script = read("scripts/lib/install_native.sh")

    tmpfiles = script.index("systemd-tmpfiles --create")
    database = script.index("configure_native_database")
    migrations = script.index("alembic upgrade head")
    start = script.index("systemctl enable --now agent-hub.target")

    assert tmpfiles < start
    assert database < migrations
    assert migrations < start


def test_native_installer_fails_fast_when_core_services_do_not_become_active() -> None:
    script = read("scripts/lib/install_native.sh")

    assert "require_native_service_active" in script
    assert "systemctl status \"$unit\" --no-pager -l" in script
    assert "journalctl -u \"$unit\" -n 120 --no-pager" in script
    assert "did not become active after install" in script
    assert "require_native_service_active caddy.service" in script
    assert "require_native_service_active agent-hub-api.service" in script
    assert "require_native_service_active agent-hub-worker.service" in script
    assert "require_native_service_active agent-hub-litellm.service" in script
    start = script.index("systemctl enable --now agent-hub.target")
    litellm_check = script.index("require_native_service_active agent-hub-litellm.service")
    mark = script.index('mark_stage "native-up"')
    assert start < litellm_check < mark


def test_native_installer_starts_local_dependencies_and_writes_runtime_urls() -> None:
    script = read("scripts/lib/install_native.sh")
    secrets = read("scripts/lib/secrets.sh")

    assert "AGENT_HUB_DATABASE_URL=" in secrets
    assert "AGENT_HUB_REDIS_URL=" in secrets
    assert "\nDATABASE_URL=" not in f"\n{secrets}"
    assert "\nREDIS_URL=" not in f"\n{secrets}"
    assert "sanitize_legacy_secrets" in secrets
    assert "DATABASE_URL|REDIS_URL|JWT_SIGNING_KEY|AGENT_HUB_SECRET_KEY" in secrets
    assert 'database_url="$(native_secret_value AGENT_HUB_DATABASE_URL)"' in script
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
    assert "chmod 0755" in script
    assert "-name '*.sh'" in script
    assert "-name '*.service'" in script


def test_installer_defaults_management_url_to_external_address() -> None:
    secrets = read("scripts/lib/secrets.sh")

    assert "detect_public_url" in secrets
    assert "api.ipify.org" in secrets
    assert "hostname -I" in secrets
    assert "is_private_ipv4" in secrets
    assert "private or loopback address" in secrets


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
    assert "fix_native_web_permissions" in installer
    assert 'chmod 0755 "$INSTALL_ROOT" "$INSTALL_ROOT/releases"' in installer
    assert 'chmod 0755 "$release"' in installer
    assert 'chmod 0755 "$release/web"' in installer
    assert 'chmod 0755 "$release/web/dist"' in installer
    assert 'chmod -R a+rX "$release/web/dist"' in installer
    assert 'chmod -R u+rwX,g+rX,o-rwx "$release/.venv"' in installer
    assert 'chmod -R u+rwX,g+rX,o-rwx "$release/.litellm-venv"' in installer
    assert "fix_native_uv_permissions" in installer
    assert 'chmod -R a+rX "$python_dir"' in installer
    assert "chown -R agent-hub:agent-hub" in installer
    assert "systemctl reload-or-restart caddy" in installer


def test_doctor_diagnoses_web_asset_permission_failures() -> None:
    doctor = read("scripts/commands/doctor.sh")

    assert "web ui assets readable by Caddy" in doctor
    assert "Caddy cannot read Web UI asset" in doctor
    assert "namei -l" in doctor
    assert "chmod -R a+rX" in doctor


def test_doctor_accepts_caddy_owned_public_ports_after_native_install() -> None:
    doctor = read("scripts/commands/doctor.sh")

    assert "port_available_or_expected_proxy" in doctor
    assert "systemd_unit_active caddy.service" in doctor
    assert "port 80 free or served by Caddy" in doctor
    assert "port 443 free or served by Caddy" in doctor


def test_doctor_diagnoses_runtime_services_and_litellm_proxy_environment() -> None:
    doctor = read("scripts/commands/doctor.sh")

    assert "systemd_unit_active_if_present" in doctor
    assert "api systemd service active when installed" in doctor
    assert "worker systemd service active when installed" in doctor
    assert "litellm systemd service active when installed" in doctor
    assert "native LiteLLM proxy environment" in doctor
    assert ".litellm-venv/bin/litellm" in doctor
    assert "litellm.proxy.proxy_server" in doctor
    assert "journalctl -u agent-hub-litellm" in doctor
    assert "model gateway failed" in doctor


def test_doctor_does_not_require_docker_when_native_stack_is_installed() -> None:
    doctor = read("scripts/commands/doctor.sh")

    assert "docker_available_or_native_installed" in doctor
    assert "native_stack_installed" in doctor
    assert "docker available for docker install path" in doctor


def test_native_caddy_supports_user_supplied_tls_certificate() -> None:
    script = read("scripts/lib/install_native.sh")
    secrets = read("scripts/lib/secrets.sh")

    assert "AGENT_HUB_TLS_CERT_FILE" in secrets
    assert "AGENT_HUB_TLS_KEY_FILE" in secrets
    assert "tls $cert_file $key_file" in script


def test_native_install_packages_installs_uv_runtime_dependencies() -> None:
    packages = read("deploy/native/install-packages.sh")
    installer = read("scripts/lib/install_native.sh")

    assert "AGENT_HUB_SOURCE_DIR" in installer
    assert 'bash "$AGENT_HUB_SOURCE_DIR/deploy/native/install-packages.sh"' in installer
    assert '"$SCRIPT_DIR/deploy/native/install-packages.sh"' not in installer
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
    assert "install_litellm_proxy_venv" in script
    assert 'if [[ "$mode" == "china" ]]; then\n    install_python_project_from_mirror "$mirror"\n    return\n  fi' in script
    assert "locked uv sync is skipped in China mirror mode" in script
    assert "uv pip install --python .venv/bin/python" in script
    assert "uv pip install \\\n    --python .litellm-venv/bin/python" in script
    assert "--index-url" in script


def test_native_installer_falls_back_from_locked_uv_sync_to_mirror_pip_install() -> None:
    script = read("scripts/lib/install_native.sh")

    assert "sync_python_project_with_lock_or_mirror" in script
    assert "run_with_timeout" in script
    assert "${AGENT_HUB_UV_SYNC_TIMEOUT_SECONDS:-900}" in script
    assert "uv sync --frozen --no-dev" in script
    assert "uv pip install --python .venv/bin/python" in script
    assert "verify_litellm_proxy_venv" in script
    assert "litellm.proxy.proxy_server" in script
    assert "--index-url" in script
    assert "official locked uv sync failed" in script
