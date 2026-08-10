from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def test_generated_secrets_use_agent_hub_prefixed_application_environment() -> None:
    secrets = read("scripts/lib/secrets.sh")

    assert "AGENT_HUB_ENVIRONMENT=production" in secrets
    assert "AGENT_HUB_DATABASE_URL=" in secrets
    assert "AGENT_HUB_REDIS_URL=" in secrets
    assert "AGENT_HUB_JWT_SIGNING_KEY=base64url:" in secrets
    assert "AGENT_HUB_MASTER_KEY=" in secrets
    assert "AGENT_HUB_SECRET_KEY=" not in secrets


def test_generated_secrets_do_not_create_divergent_jwt_keys() -> None:
    secrets = read("scripts/lib/secrets.sh")

    assert 'jwt_signing_key="$(rand_secret)"' in secrets
    assert 'AGENT_HUB_JWT_SIGNING_KEY=base64url:%s\\n\' "$jwt_signing_key"' in secrets
    assert 'JWT_SIGNING_KEY=base64url:%s\\n\' "$jwt_signing_key"' in secrets
    assert secrets.count("JWT_SIGNING_KEY=base64url:%s") == 2


def test_docker_install_overrides_container_internal_service_urls() -> None:
    installer = read("scripts/lib/install_docker.sh")

    assert "AGENT_HUB_DATABASE_URL" in installer
    assert "postgresql+asyncpg://agent_hub:" in installer
    assert "@postgres:5432/agent_hub" in installer
    assert "AGENT_HUB_REDIS_URL" in installer
    assert "redis://redis:6379/0" in installer
    assert "@127.0.0.1:5432" not in installer
    assert "AGENT_HUB_LITELLM_HEALTH_URL" in installer
    assert "http://litellm:4000/health/liveliness" in installer


def test_docker_install_prefers_china_mirrors_unless_official_mode() -> None:
    installer = read("scripts/lib/install_docker.sh")

    assert 'if [[ "${AGENT_HUB_MIRROR_MODE:-auto}" != "official" ]]; then' in installer
    assert "configure_china_docker_mirror || true" in installer
    assert "configure_docker_build_mirrors" in installer
    assert "pypi.tuna.tsinghua.edu.cn" in installer
    assert "registry.npmmirror.com" in installer
    assert installer.index("configure_china_docker_mirror || true") < installer.index(
        "if docker_compose_up"
    )


def test_dockerfile_builds_virtualenv_at_runtime_path_without_editable_install() -> None:
    dockerfile = read("Dockerfile")
    compose = read("deploy/compose/docker-compose.yml")

    assert "WORKDIR /opt/agent-hub" in dockerfile
    assert "uv sync --frozen --no-dev --no-editable" in dockerfile
    assert "AGENT_HUB_PYPI_MIRROR" in dockerfile
    assert "AGENT_HUB_NPM_MIRROR" in dockerfile
    assert "uv pip install --python .venv/bin/python" in dockerfile
    assert "--index-url \"${AGENT_HUB_PYPI_MIRROR}\"" in dockerfile
    assert "--registry=\"${AGENT_HUB_NPM_MIRROR}\"" in dockerfile
    assert "ghcr.io/astral-sh/uv" not in dockerfile
    assert "-e ." not in dockerfile
    assert "AGENT_HUB_PYPI_MIRROR:" in compose
    assert "AGENT_HUB_NPM_MIRROR:" in compose
    assert "COPY --from=python-build --chown=10001:10001 /opt/agent-hub/.venv ./.venv" in dockerfile


def test_compose_runs_migrations_and_bootstrap_before_application_services() -> None:
    compose = read("deploy/compose/docker-compose.yml")

    assert "migrate:" in compose
    assert 'command: ["alembic", "upgrade", "head"]' in compose
    assert "bootstrap:" in compose
    assert "service_completed_successfully" in compose
    assert "AGENT_HUB_SETUP_CODE" in compose


def test_compose_exposes_feishu_on_main_api_instead_of_second_app_process() -> None:
    compose = read("deploy/compose/docker-compose.yml")
    native_target = read("deploy/native/systemd/agent-hub.target")

    assert "\n  feishu:" not in compose
    assert "agent-hub-feishu.service" not in native_target
    assert "reverse_proxy api:8000" in read("deploy/compose/Caddyfile")


def test_compose_and_native_litellm_use_config_file() -> None:
    compose = read("deploy/compose/docker-compose.yml")
    native_service = read("deploy/native/systemd/agent-hub-litellm.service")
    docker_installer = read("scripts/lib/install_docker.sh")
    native_installer = read("scripts/lib/install_native.sh")

    assert "./litellm.yaml:/etc/litellm/config.yaml:ro" in compose
    assert '"--config", "/etc/litellm/config.yaml"' in compose
    assert "--config /etc/agent-hub/litellm.yaml" in native_service
    assert "write_litellm_config" in docker_installer
    assert "write_litellm_config" in native_installer


def test_native_litellm_proxy_uses_isolated_verified_virtualenv() -> None:
    native_service = read("deploy/native/systemd/agent-hub-litellm.service")
    native_installer = read("scripts/lib/install_native.sh")

    assert "/opt/agent-hub/current/.litellm-venv/bin/litellm" in native_service
    assert "/opt/agent-hub/current/.venv/bin/litellm" not in native_service
    assert "install_litellm_proxy_venv" in native_installer
    assert "verify_litellm_proxy_venv" in native_installer
    assert "--exclude='.litellm-venv'" in native_installer
    assert "uv pip install" in native_installer
    assert "--python .litellm-venv/bin/python" in native_installer
    assert "'litellm[proxy]>=1.75,<2'" in native_installer
    assert "litellm.proxy.proxy_server" in native_installer
    assert ".litellm-venv/bin/litellm --help" in native_installer
    assert "proxy_server module is missing" in native_installer


def test_compose_litellm_is_health_checked_and_can_reach_provider_apis() -> None:
    compose = read("deploy/compose/docker-compose.yml")

    assert "litellm:\n        condition: service_healthy" in compose
    assert "networks: [backend, egress]" in compose
    assert "  egress:\n" in compose
    assert "socket.create_connection(('127.0.0.1', 4000), 3)" in compose


def test_compose_caddy_requires_explicit_public_url_without_localhost_default() -> None:
    caddyfile = read("deploy/compose/Caddyfile")

    assert "{$AGENT_HUB_PUBLIC_URL}" in caddyfile
    assert "{$AGENT_HUB_PUBLIC_URL:localhost}" not in caddyfile


def test_compose_env_example_uses_prefixed_application_environment() -> None:
    example = read("deploy/compose/.env.example")

    assert "AGENT_HUB_DATABASE_URL=" in example
    assert "AGENT_HUB_REDIS_URL=" in example
    assert "AGENT_HUB_JWT_SIGNING_KEY=" in example
    assert "AGENT_HUB_MASTER_KEY=" in example
    assert "AGENT_HUB_LITELLM_HEALTH_URL=http://litellm:4000/health/liveliness" in example
    assert "\nDATABASE_URL=" not in f"\n{example}"
    assert "\nJWT_SIGNING_KEY=" not in f"\n{example}"
    assert "${POSTGRES_PASSWORD}" not in example


def test_readme_uses_repository_checkout_instead_of_placeholder_install_url() -> None:
    readme = read("README.md")

    assert "example.invalid" not in readme
    assert "git clone https://github.com/zhangzhimiao1994/mix-agent.git" in readme
    assert "cd mix-agent" in readme
