import hashlib
import importlib.metadata
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tomllib
import zipfile
from pathlib import Path

import pytest

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
    assert "AGENT_HUB_RUNTIME_TIMEOUT_SECONDS=" in secrets
    assert "AGENT_HUB_RUNTIME_TOKEN_BUDGET=" in secrets
    assert "normalize_secret_file_format" in secrets
    assert "ensure_numeric_secret_default" in secrets
    assert "AGENT_HUB_SECRET_KEY=" not in secrets


def test_generated_secrets_do_not_create_divergent_jwt_keys() -> None:
    secrets = read("scripts/lib/secrets.sh")

    assert 'jwt_signing_key="$(rand_secret)"' in secrets
    assert 'AGENT_HUB_JWT_SIGNING_KEY=base64url:%s\\n\' "$jwt_signing_key"' in secrets
    assert "\n    printf 'JWT_SIGNING_KEY=base64url:%s\\n' \"$jwt_signing_key\"" not in secrets
    assert "sanitize_legacy_secrets" in secrets
    assert secrets.count("JWT_SIGNING_KEY=base64url:%s") == 1


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
    assert 'set_env_value "$INSTALL_ROOT/compose/.env" \\\n    DATABASE_URL' not in installer
    assert 'set_env_value "$INSTALL_ROOT/compose/.env" REDIS_URL' not in installer


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
    assert '--index-url "${AGENT_HUB_PYPI_MIRROR}"' in dockerfile
    assert '--registry="${AGENT_HUB_NPM_MIRROR}"' in dockerfile
    assert "ghcr.io/astral-sh/uv" not in dockerfile
    assert "-e ." not in dockerfile
    assert "AGENT_HUB_PYPI_MIRROR:" in compose
    assert "AGENT_HUB_NPM_MIRROR:" in compose
    assert "COPY --from=python-build --chown=10001:10001 /opt/agent-hub/.venv ./.venv" in dockerfile


def test_dockerfile_defines_non_root_skill_runner_target() -> None:
    dockerfile = read("Dockerfile")

    assert "FROM ${PYTHON_IMAGE} AS skill-runner" in dockerfile
    runner = dockerfile.split("FROM ${PYTHON_IMAGE} AS skill-runner", 1)[1].split(
        "FROM ${PYTHON_IMAGE} AS runtime", 1
    )[0]
    assert "--uid 65532" in runner
    assert "--gid 65532" in runner
    assert "USER 65532:65532" in runner
    assert "mkdir -p /package /workspace" in runner
    assert 'CMD ["python", "-m", "agent_hub.skills.runner"]' in runner


def test_dockerfile_uses_builtin_frontend_for_offline_base_image_builds() -> None:
    assert not re.search(r"(?m)^#\s*syntax=", read("Dockerfile"))


def test_skill_runner_module_remains_importable_when_sandbox_changes_workdir() -> None:
    assert 'PYTHONPATH=/opt/agent-hub' in _docker_stage("skill-runner")


def _docker_stage(name: str) -> str:
    stages = re.split(r"(?m)^FROM \S+ AS (\S+)\s*\n", read("Dockerfile"))
    by_name = dict(zip(stages[1::2], stages[2::2], strict=True))
    assert name in by_name, f"Docker build stage {name} is missing"
    return by_name[name]


def _runner_requirements() -> dict[str, str]:
    path = ROOT / "deploy/compose/skill-runner-requirements.txt"
    assert path.is_file(), "the runner needs its own pinned dependency manifest"
    requirements: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        match = re.fullmatch(r"([a-z][a-z0-9-]*)==([^\s;]+)", line)
        assert match, f"runner dependencies must be exact pins: {line}"
        name, version = match.groups()
        assert name not in requirements, f"duplicate runner dependency: {name}"
        requirements[name] = version
    return requirements


def test_skill_runner_build_is_independent_of_platform_and_web() -> None:
    assert "FROM ${PYTHON_IMAGE} AS skill-runner-build" in read("Dockerfile")
    runner = _docker_stage("skill-runner")
    assert "--from=python-build" not in runner, "runner must not copy the platform virtualenv"
    builder = _docker_stage("skill-runner-build")
    for stage in (runner, builder):
        assert "--from=python-build" not in stage
        assert "--from=web-build" not in stage
        assert "uv sync" not in stage
        assert "pyproject.toml" not in stage
    assert "deploy/compose/skill-runner-requirements.txt" in builder
    assert "--no-deps" in builder
    assert "--no-cache-dir" in builder
    assert "--only-binary=:all:" in builder
    assert "--from=skill-runner-build" in runner
    assert "PYTHONDONTWRITEBYTECODE=1" in runner
    assert "HOME=/workspace" in runner


def test_skill_runner_dependencies_are_only_the_lockfile_runtime_closure() -> None:
    requirements = _runner_requirements()
    lock = tomllib.loads(read("uv.lock"))
    packages = {package["name"]: package for package in lock["package"]}
    pending = ["pydantic", "pyyaml"]
    expected: dict[str, str] = {}
    while pending:
        name = pending.pop()
        if name in expected:
            continue
        package = packages[name]
        expected[name] = package["version"]
        pending.extend(dependency["name"] for dependency in package.get("dependencies", []))

    assert requirements == expected


@pytest.mark.parametrize("manifest_name", ["skill.json", "skill.yaml"])
@pytest.mark.parametrize("scenario", ["success", "hash_mismatch", "dependencies"])
def test_minimal_skill_runner_artifact_preserves_execution_contract(
    tmp_path: Path,
    manifest_name: str,
    scenario: str,
) -> None:
    artifact = tmp_path / "runner"
    artifact.mkdir()
    copied: set[str] = set()
    for line in _docker_stage("skill-runner").splitlines():
        if not line.startswith("COPY ") or "--from=" in line:
            continue
        tokens = [token for token in shlex.split(line)[1:] if not token.startswith("--")]
        destination = artifact / tokens[-1]
        destination.mkdir(parents=True, exist_ok=True)
        for source in tokens[:-1]:
            assert (ROOT / source).is_file(), (
                "runner must copy individual modules, not the full src"
            )
            shutil.copy2(ROOT / source, destination / Path(source).name)
            copied.add(source)
    assert copied == {
        "src/agent_hub/__init__.py",
        "src/agent_hub/skills/__init__.py",
        "src/agent_hub/skills/runner.py",
        "src/agent_hub/skills/package.py",
        "src/agent_hub/skills/manifest.py",
    }
    # Run without site-packages so the platform installation cannot hide missing dependencies.
    for name, version in _runner_requirements().items():
        distribution = importlib.metadata.distribution(name)
        assert distribution.version == version
        for file in distribution.files or ():
            if "__pycache__" in file.parts:
                continue
            assert not file.is_absolute() and ".." not in file.parts
            destination = artifact / file
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(distribution.locate_file(file)), destination)

    requirements = b"pydantic==2.12.5\n" if scenario == "dependencies" else b""
    manifest = {
        "name": "demo_skill",
        "version": "1.0.0",
        "entry_point": "main.py",
        "compatible_runtime": "python3.12",
        "dependency_lock_hash": hashlib.sha256(requirements).hexdigest(),
    }
    package = tmp_path / "skill.zip"
    with zipfile.ZipFile(package, "w") as archive:
        contents = (
            json.dumps(manifest)
            if manifest_name.endswith(".json")
            else "\n".join(f"{key}: {json.dumps(value)}" for key, value in manifest.items())
        )
        archive.writestr(manifest_name, contents)
        archive.writestr(
            "main.py",
            "import json, sys\n"
            "print('hello ' + json.load(sys.stdin)['name'])\n"
            "print('skill stderr', file=sys.stderr)\n",
        )
        if requirements:
            archive.writestr("requirements.txt", requirements)
    env = os.environ.copy()
    env.update(
        PYTHONPATH=str(artifact),
        PYTHONDONTWRITEBYTECODE="1",
        AGENT_HUB_PACKAGE_PATH=str(package),
        AGENT_HUB_PACKAGE_SHA256=(
            "0" * 64
            if scenario == "hash_mismatch"
            else hashlib.sha256(package.read_bytes()).hexdigest()
        ),
        AGENT_HUB_WORKDIR=str(tmp_path / "workspace"),
        AGENT_HUB_TIMEOUT_SECONDS="5",
        AGENT_HUB_SANDBOX_PROFILE="workspace_write",
    )
    result = subprocess.run(
        (sys.executable, "-S", "-m", "agent_hub.skills.runner"),
        cwd=artifact,
        env=env,
        input='{"name":"agent"}',
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
    )

    if scenario == "success":
        assert result.returncode == 0, result.stderr
        assert result.stdout == "hello agent\n"
        assert result.stderr == "skill stderr\n"
    else:
        assert result.returncode == 78, result.stderr
        assert result.stdout == ""
        expected = (
            "sha256 does not match"
            if scenario == "hash_mismatch"
            else "dependencies are not installed"
        )
        assert expected in result.stderr


def test_skill_runner_builder_is_registered_and_uses_safe_docker_argv() -> None:
    launcher = read("scripts/agent-hub")
    command_path = ROOT / "scripts/commands/build-skill-runner.sh"

    assert command_path.is_file(), "build-skill-runner command is missing"
    command = command_path.read_text(encoding="utf-8")
    assert "build-skill-runner  Build the isolated Docker Skill runner image." in launcher
    assert "build-skill-runner" in launcher
    assert 'image="agent-hub-skill-runner:latest"' in command
    assert "--source" in command
    assert "--image" in command
    assert "build_args=(" in command
    assert "build --target skill-runner --tag" in command
    assert 'exec "$docker_bin" "${build_args[@]}"' in command
    assert "eval" not in command


def test_skill_runner_build_command_is_documented() -> None:
    readme = read("README.md")
    operations = read("docs/operations.md")

    assert "scripts/agent-hub build-skill-runner" in readme
    assert "scripts/agent-hub build-skill-runner" in operations
    assert "--source" in operations
    assert "--image" in operations


def test_skill_runner_builder_preserves_source_and_image_as_single_arguments(
    tmp_path: Path,
) -> None:
    shell = _posix_shell()
    fake_bin = tmp_path / "fake bin"
    fake_bin.mkdir()
    if os.name == "nt":
        shutil.copy2(shell, fake_bin / "bash.exe")
    docker = fake_bin / "docker"
    docker.write_text(
        "#!/usr/bin/env bash\n"
        "set -Eeuo pipefail\n"
        'printf \'%s\\0\' "$@" > "$AGENT_HUB_TEST_DOCKER_ARGS"\n',
        encoding="utf-8",
    )
    docker.chmod(0o755)
    source = tmp_path / "source with spaces;not-a-command"
    source.mkdir()
    (source / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
    marker = tmp_path / "must-not-exist"
    source_arg = _shell_path(source)
    image = f"registry.example/runner:test; touch {_shell_path(marker)}"
    captured = tmp_path / "docker-args.bin"
    env = os.environ.copy()
    env["AGENT_HUB_TEST_FAKE_BIN"] = _shell_path(fake_bin)
    env["AGENT_HUB_TEST_LAUNCHER"] = _shell_path(ROOT / "scripts/agent-hub")
    env["AGENT_HUB_TEST_DOCKER_ARGS"] = _shell_path(captured)

    result = subprocess.run(
        (
            str(shell),
            "-lc",
            (
                'PATH="$AGENT_HUB_TEST_FAKE_BIN:/usr/bin:/bin"; '
                'export PATH; exec "$AGENT_HUB_TEST_LAUNCHER" "$@"'
            ),
            "build-skill-runner-contract",
            "build-skill-runner",
            "--source",
            source_arg,
            "--image",
            image,
        ),
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert captured.read_bytes().split(b"\0")[:-1] == [
        b"build",
        b"--target",
        b"skill-runner",
        b"--tag",
        image.encode(),
        source_arg.encode(),
    ]
    assert not marker.exists()


def _posix_shell() -> Path:
    for name in ("bash", "sh"):
        candidate = shutil.which(name)
        if candidate and Path(candidate).name.casefold() != "bash.exe":
            return Path(candidate)
    git = shutil.which("git")
    if git:
        bundled = Path(git).resolve().parents[1] / "usr/bin/sh.exe"
        if bundled.is_file():
            return bundled
    pytest.skip("a POSIX shell is required for the build command contract")


def _shell_path(path: Path) -> str:
    resolved = path.resolve()
    if os.name != "nt":
        return resolved.as_posix()
    drive = resolved.drive.rstrip(":").lower()
    relative = resolved.as_posix().split(":", 1)[1].lstrip("/")
    return f"/{drive}/{relative}"


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
    assert 'chown root:agent-hub "$CONFIG_DIR"' in native_installer
    assert 'chmod 0750 "$CONFIG_DIR"' in native_installer


def test_installer_prints_external_callback_urls_for_supported_channels() -> None:
    installer = read("scripts/lib/install_docker.sh")

    assert "Webhook callback URLs for external platforms" in installer
    for path in (
        "/channels/feishu/events",
        "/channels/dingtalk/events",
        "/channels/wecom/bot/events",
        "/channels/wecom/app/events",
        "/channels/wechatmp/events",
        "/channels/wechat-kf/events",
        "/channels/telegram/events",
        "/channels/slack/events",
        "/channels/qq/events",
        "/channels/custom/events",
    ):
        assert path in installer


def test_native_litellm_proxy_uses_isolated_verified_virtualenv() -> None:
    native_service = read("deploy/native/systemd/agent-hub-litellm.service")
    native_installer = read("scripts/lib/install_native.sh")

    assert "/opt/agent-hub/current/.litellm-venv/bin/python" in native_service
    assert "from litellm import run_server" in native_service
    assert "/opt/agent-hub/current/.litellm-venv/bin/litellm" not in native_service
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


def test_compose_worker_does_not_inherit_api_http_healthcheck() -> None:
    compose = read("deploy/compose/docker-compose.yml")
    worker_block = compose.split("  worker:\n", 1)[1].split("\n  litellm:", 1)[0]

    assert 'command: ["python", "-m", "agent_hub.runtime.worker"]' in worker_block
    assert "healthcheck:" in worker_block
    assert "disable: true" in worker_block
    assert "agent-hub-healthcheck" not in worker_block


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
    assert "AGENT_HUB_RUNTIME_TIMEOUT_SECONDS=300" in example
    assert "AGENT_HUB_RUNTIME_TOKEN_BUDGET=1000000" in example
    assert "\nDATABASE_URL=" not in f"\n{example}"
    assert "\nJWT_SIGNING_KEY=" not in f"\n{example}"
    assert "${POSTGRES_PASSWORD}" not in example


def test_readme_uses_repository_checkout_instead_of_placeholder_install_url() -> None:
    readme = read("README.md")

    assert "example.invalid" not in readme
    assert "git clone https://github.com/zhangzhimiao1994/Cube-agent.git" in readme
    assert "cd Cube-agent" in readme
