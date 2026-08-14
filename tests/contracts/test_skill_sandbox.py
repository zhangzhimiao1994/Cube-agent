from __future__ import annotations

import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from agent_hub.skills.sandbox.base import (
    SkillInvocation,
    SkillResult,
    decode_bounded,
    run_subprocess_with_limits,
)
from agent_hub.skills.sandbox.docker import (
    DockerSandboxSettings,
    build_docker_command,
    build_docker_terminate_command,
)
from agent_hub.skills.sandbox.systemd import (
    build_systemd_run_command,
    build_systemd_terminate_command,
)


def invocation(**changes: object) -> SkillInvocation:
    values: dict[str, object] = {
        "execution_id": "exec_1",
        "package_path": Path("/srv/agent-hub/packages/pkg.zip"),
        "package_sha256": "a" * 64,
        "input": {"prompt": "safe"},
        "timeout_seconds": 3,
        "output_limit_bytes": 1024,
        "memory_limit_bytes": 128 * 1024 * 1024,
        "cpu_quota_percent": 50,
        "network_allowlist": (),
        "read_only_inputs": (Path("/srv/agent-hub/input.txt"),),
        "writable_tmp_path": Path("/srv/agent-hub/tmp/exec_1"),
        "selected_secret_refs": ("secret://deepseek",),
    }
    values.update(changes)
    return SkillInvocation.model_validate(values, strict=True)


def test_invocation_rejects_non_json_input_and_unsafe_identity() -> None:
    with pytest.raises(ValidationError):
        invocation(input={"value": object()})
    with pytest.raises(ValidationError):
        invocation(input={"value": float("nan")})
    with pytest.raises(ValidationError):
        invocation(execution_id="../escape")
    with pytest.raises(ValidationError):
        invocation(package_sha256="not-a-hash")


def test_skill_result_is_bounded_and_strict() -> None:
    assert decode_bounded("你好abcdef".encode(), 8) == "你好ab"
    assert len(decode_bounded("你".encode(), 2).encode()) <= 2
    result = SkillResult(exit_code=None, stdout="x", stderr="", timed_out=True)
    with pytest.raises(ValidationError):
        result.timed_out = False


def test_docker_command_enforces_default_hardening_and_no_network() -> None:
    command = build_docker_command(invocation())

    assert command[:3] == ("docker", "run", "--rm")
    assert "--user" in command
    assert "65532:65532" in command
    assert "--read-only" in command
    assert _value_after(command, "--cap-drop") == "ALL"
    assert _value_after(command, "--security-opt") == "no-new-privileges"
    assert _value_after(command, "--pids-limit") == "64"
    assert _value_after(command, "--memory") == str(128 * 1024 * 1024)
    assert _value_after(command, "--cpus") == "0.5"
    assert _value_after(command, "--network") == "none"
    assert _value_after(command, "--workdir") == "/workspace"
    assert all("docker.sock" not in part for part in command)
    assert any("target=/package/skill.zip,readonly" in part for part in command)
    assert any("target=/inputs/0,readonly" in part for part in command)
    assert any("target=/workspace" in part and "readonly" not in part for part in command)
    assert command[-4:] == (
        "agent-hub-skill-runner:latest",
        "python",
        "-m",
        "agent_hub.skills.runner",
    )


def test_docker_command_uses_isolated_network_when_network_policy_allows() -> None:
    command = build_docker_command(
        invocation(network_allowlist=("api.example.com",)),
        DockerSandboxSettings(isolated_network_name="agent-hub-isolated-egress"),
    )

    assert _value_after(command, "--network") == "agent-hub-isolated-egress"
    assert "host" not in command
    assert all("api.example.com" not in part for part in command)


def test_docker_command_rejects_host_network_and_mount_option_injection() -> None:
    with pytest.raises(ValueError, match="host"):
        DockerSandboxSettings(isolated_network_name="host")
    with pytest.raises(ValueError, match="mount paths"):
        build_docker_command(invocation(package_path=Path("/srv/pkg,readonly=false.zip")))


def test_docker_terminate_command_targets_named_container_without_shell() -> None:
    assert build_docker_terminate_command("exec_1") == (
        "docker",
        "kill",
        "agent-hub-skill-exec_1",
    )
    with pytest.raises(ValueError):
        build_docker_terminate_command("../escape")


def test_systemd_command_enforces_process_filesystem_and_network_restrictions() -> None:
    command = build_systemd_run_command(invocation())
    properties = _properties(command)

    assert command[:5] == ("systemd-run", "--wait", "--collect", "--pipe", "--quiet")
    assert properties["DynamicUser"] == "yes"
    assert properties["NoNewPrivileges"] == "yes"
    assert properties["ProtectSystem"] == "strict"
    assert properties["PrivateTmp"] == "yes"
    assert properties["PrivateDevices"] == "yes"
    assert properties["RestrictSUIDSGID"] == "yes"
    assert properties["PrivateNetwork"] == "yes"
    assert properties["IPAddressDeny"] == "any"
    assert properties["MemoryMax"] == str(128 * 1024 * 1024)
    assert properties["CPUQuota"] == "50%"
    assert properties["RuntimeMaxSec"] == "3s"
    assert properties["ReadWritePaths"] == "/srv/agent-hub/tmp/exec_1"
    assert properties["WorkingDirectory"] == "/srv/agent-hub/tmp/exec_1"
    assert "PYTHONPATH=/opt/agent-hub/current/src" in _environment_values(command)
    assert any(item == "ReadOnlyPaths=/srv/agent-hub/packages/pkg.zip" for item in _property_values(command))
    assert any(item == "ReadOnlyPaths=/srv/agent-hub/input.txt" for item in _property_values(command))
    assert command[-3:] == (
        "/opt/agent-hub/current/.venv/bin/python",
        "-m",
        "agent_hub.skills.runner",
    )


async def test_subprocess_runner_enforces_timeout_and_bounded_output() -> None:
    slow = await run_subprocess_with_limits(
        (sys.executable, "-c", "import time; time.sleep(10)"),
        invocation(timeout_seconds=1, output_limit_bytes=32),
    )
    assert slow.timed_out is True
    assert slow.exit_code is None

    cleanup_calls = 0

    async def cleanup() -> None:
        nonlocal cleanup_calls
        cleanup_calls += 1

    noisy = await run_subprocess_with_limits(
        (sys.executable, "-c", "import sys; sys.stdout.write('x' * 100000); sys.stdout.flush()"),
        invocation(timeout_seconds=10, output_limit_bytes=32),
        on_forced_terminate=cleanup,
    )
    assert len(noisy.stdout.encode()) <= 32
    assert cleanup_calls == 1


def test_systemd_terminate_command_targets_unit_without_shell() -> None:
    assert build_systemd_terminate_command("exec_1") == (
        "systemctl",
        "kill",
        "--kill-who=all",
        "agent-hub-skill-exec_1.service",
    )
    with pytest.raises(ValueError):
        build_systemd_terminate_command("../escape")


def _environment_values(command: tuple[str, ...]) -> list[str]:
    return [command[index + 1] for index, value in enumerate(command) if value == "-E"]


def _value_after(command: tuple[str, ...], flag: str) -> str:
    return command[command.index(flag) + 1]


def _property_values(command: tuple[str, ...]) -> list[str]:
    return [command[index + 1] for index, value in enumerate(command) if value == "-p"]


def _properties(command: tuple[str, ...]) -> dict[str, str]:
    result: dict[str, str] = {}
    for value in _property_values(command):
        if "=" in value:
            key, item = value.split("=", 1)
            result[key] = item
    return result