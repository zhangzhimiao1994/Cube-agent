from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from agent_hub.skills.sandbox.base import (
    SkillInvocation,
    SkillResult,
    validate_execution_id,
)
from agent_hub.skills.sandbox.broker import (
    BROKER_SOCKET_PATH,
    BrokerRequest,
    request_skill_broker,
)


@dataclass(frozen=True, slots=True)
class DockerSandboxSettings:
    executable: str = "docker"
    image: str = "agent-hub-skill-runner:latest"
    user: str = "10001:10001"
    pids_limit: int = 64
    isolated_network_name: str = "agent-hub-skill-net"
    container_package_path: str = "/package/skill.zip"
    container_workdir: str = "/workspace"
    broker_socket_path: Path = BROKER_SOCKET_PATH

    def __post_init__(self) -> None:
        if self.isolated_network_name in {"host", "none"}:
            raise ValueError("isolated network name cannot be host or none")
        if re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}", self.isolated_network_name) is None:
            raise ValueError("isolated network name must be a safe Docker network identifier")


class DockerSkillSandbox:
    def __init__(self, settings: DockerSandboxSettings | None = None) -> None:
        self._settings = settings or DockerSandboxSettings()

    async def run(self, invocation: SkillInvocation) -> SkillResult:
        response = await request_skill_broker(
            BrokerRequest.run(invocation, backend="docker"),
            socket_path=self._settings.broker_socket_path,
        )
        if not response.ok or response.result is None:
            raise OSError(response.error or "Docker Skill broker returned no result")
        return response.result

    async def terminate(self, execution_id: str) -> None:
        response = await request_skill_broker(
            BrokerRequest.terminate(execution_id, backend="docker"),
            socket_path=self._settings.broker_socket_path,
        )
        if not response.ok:
            raise OSError(response.error or "Docker Skill broker termination failed")


def build_docker_command(
    invocation: SkillInvocation,
    settings: DockerSandboxSettings | None = None,
) -> tuple[str, ...]:
    settings = settings or DockerSandboxSettings()
    if invocation.network_allowlist:
        raise ValueError("network allowlist requires a configured Docker egress policy")
    cpus = f"{invocation.cpu_quota_percent / 100:.2f}".rstrip("0").rstrip(".")
    command: list[str] = [
        settings.executable,
        "run",
        "--rm",
        "--interactive",
        "--name",
        f"agent-hub-skill-{invocation.execution_id}",
        "--user",
        settings.user,
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--pids-limit",
        str(settings.pids_limit),
        "--memory",
        str(invocation.memory_limit_bytes),
        "--cpus",
        cpus,
        "--network",
        "none",
        "--tmpfs",
        "/tmp:rw,noexec,nosuid,nodev,size=64m",
        "--workdir",
        settings.container_workdir,
        "--mount",
        _bind_mount(invocation.package_path, settings.container_package_path, readonly=True),
        "--mount",
        _bind_mount(invocation.writable_tmp_path, settings.container_workdir, readonly=False),
        "--env",
        f"AGENT_HUB_EXECUTION_ID={invocation.execution_id}",
        "--env",
        f"AGENT_HUB_PACKAGE_SHA256={invocation.package_sha256}",
        "--env",
        f"AGENT_HUB_TIMEOUT_SECONDS={invocation.timeout_seconds}",
        "--env",
        f"AGENT_HUB_OUTPUT_LIMIT_BYTES={invocation.output_limit_bytes}",
        "--env",
        f"AGENT_HUB_SANDBOX_PROFILE={invocation.sandbox_profile}",
        "--env",
        f"AGENT_HUB_WORKDIR={settings.container_workdir}",
    ]
    for index, input_path in enumerate(invocation.read_only_inputs):
        command.extend(
            (
                "--mount",
                _bind_mount(input_path, f"/inputs/{index}", readonly=True),
            )
        )
    command.extend((settings.image, "python", "-m", "agent_hub.skills.runner"))
    return tuple(command)


def build_docker_terminate_command(
    execution_id: str,
    *,
    executable: str = "docker",
) -> tuple[str, ...]:
    validate_execution_id(execution_id)
    return (executable, "kill", f"agent-hub-skill-{execution_id}")


def _bind_mount(source: object, target: str, *, readonly: bool) -> str:
    mount = f"type=bind,source={_path_arg(source)},target={target}"
    if readonly:
        mount += ",readonly"
    return mount


def _path_arg(value: object) -> str:
    if isinstance(value, Path):
        text = value.as_posix()
    else:
        text = str(value)
    if not text or "," in text or any(ord(ch) < 32 or ord(ch) == 127 for ch in text):
        raise ValueError("Docker mount paths must not contain commas or control characters")
    return text
