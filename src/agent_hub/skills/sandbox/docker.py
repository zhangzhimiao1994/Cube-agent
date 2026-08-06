from __future__ import annotations

import asyncio
import contextlib
import re
from dataclasses import dataclass
from pathlib import Path

from agent_hub.skills.sandbox.base import (
    SkillInvocation,
    SkillResult,
    run_subprocess_with_limits,
    validate_execution_id,
)


@dataclass(frozen=True, slots=True)
class DockerSandboxSettings:
    image: str = "agent-hub-skill-runner:latest"
    user: str = "65532:65532"
    pids_limit: int = 64
    isolated_network_name: str = "agent-hub-skill-net"
    container_package_path: str = "/package/skill.zip"
    container_workdir: str = "/workspace"

    def __post_init__(self) -> None:
        if self.isolated_network_name in {"host", "none"}:
            raise ValueError("isolated network name cannot be host or none")
        if re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}", self.isolated_network_name) is None:
            raise ValueError("isolated network name must be a safe Docker network identifier")


class DockerSkillSandbox:
    def __init__(self, settings: DockerSandboxSettings | None = None) -> None:
        self._settings = settings or DockerSandboxSettings()
        self._processes: dict[str, asyncio.subprocess.Process] = {}

    async def run(self, invocation: SkillInvocation) -> SkillResult:
        argv = build_docker_command(invocation, self._settings)
        try:
            return await run_subprocess_with_limits(
                argv,
                invocation,
                process_started=lambda process: self._processes.__setitem__(
                    invocation.execution_id, process
                ),
                on_forced_terminate=lambda: self._terminate_backend(invocation.execution_id),
            )
        finally:
            self._processes.pop(invocation.execution_id, None)

    async def terminate(self, execution_id: str) -> None:
        process = self._processes.get(execution_id)
        if process is not None and process.returncode is None:
            process.kill()
        with contextlib.suppress(FileNotFoundError, TimeoutError):
            await self._terminate_backend(execution_id)

    async def _terminate_backend(self, execution_id: str) -> None:
        killer = await asyncio.create_subprocess_exec(
            *build_docker_terminate_command(execution_id),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(killer.wait(), timeout=5)


def build_docker_command(
    invocation: SkillInvocation,
    settings: DockerSandboxSettings | None = None,
) -> tuple[str, ...]:
    settings = settings or DockerSandboxSettings()
    cpus = f"{invocation.cpu_quota_percent / 100:.2f}".rstrip("0").rstrip(".")
    command: list[str] = [
        "docker",
        "run",
        "--rm",
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
        settings.isolated_network_name if invocation.network_allowlist else "none",
        "--tmpfs",
        "/tmp:rw,noexec,nosuid,nodev,size=64m",
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
    ]
    for index, input_path in enumerate(invocation.read_only_inputs):
        command.extend(
            (
                "--mount",
                _bind_mount(input_path, f"/inputs/{index}", readonly=True),
            )
        )
    command.extend((settings.image, "python", "-m", "agent_hub_skill_runner"))
    return tuple(command)


def build_docker_terminate_command(execution_id: str) -> tuple[str, ...]:
    validate_execution_id(execution_id)
    return ("docker", "kill", f"agent-hub-skill-{execution_id}")


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
