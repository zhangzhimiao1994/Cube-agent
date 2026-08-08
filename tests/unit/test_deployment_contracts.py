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


def test_docker_install_overrides_container_internal_service_urls() -> None:
    installer = read("scripts/lib/install_docker.sh")

    assert "AGENT_HUB_DATABASE_URL" in installer
    assert "postgresql+asyncpg://agent_hub:" in installer
    assert "@postgres:5432/agent_hub" in installer
    assert "AGENT_HUB_REDIS_URL" in installer
    assert "redis://redis:6379/0" in installer
    assert "@127.0.0.1:5432" not in installer


def test_dockerfile_builds_virtualenv_at_runtime_path_without_editable_install() -> None:
    dockerfile = read("Dockerfile")

    assert "WORKDIR /opt/agent-hub" in dockerfile
    assert "uv sync --frozen --no-dev --no-editable" in dockerfile
    assert "uv pip install --no-deps -e ." not in dockerfile
    assert "COPY --from=python-build --chown=10001:10001 /opt/agent-hub/.venv ./.venv" in dockerfile


def test_compose_runs_migrations_and_bootstrap_before_application_services() -> None:
    compose = read("deploy/compose/docker-compose.yml")

    assert "migrate:" in compose
    assert 'command: ["alembic", "upgrade", "head"]' in compose
    assert "bootstrap:" in compose
    assert "service_completed_successfully" in compose
    assert "AGENT_HUB_SETUP_CODE" in compose


def test_readme_uses_repository_checkout_instead_of_placeholder_install_url() -> None:
    readme = read("README.md")

    assert "example.invalid" not in readme
    assert "git clone git@github.com:zhangzhimiao1994/mix-agent-.git" in readme
