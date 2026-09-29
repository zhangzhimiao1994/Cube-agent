"""Probe and describe the execution backends implemented by Agent Hub."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Literal, cast

from agent_hub.skills.sandbox.broker import probe_skill_broker
from agent_hub.skills.sandbox.remote import remote_backend_unavailable_reason

ExecutionBackendId = Literal["systemd", "docker", "ssh", "modal", "daytona", "vercel"]
EXECUTION_BACKEND_IDS = frozenset({"systemd", "docker", "ssh", "modal", "daytona", "vercel"})
DEFAULT_EXECUTION_BACKEND: ExecutionBackendId = "systemd"
DOCKER_SKILL_RUNNER_IMAGE = "agent-hub-skill-runner:latest"
_PROBE_CACHE_TTL_SECONDS = 10.0
_PROBE_CACHE: dict[ExecutionBackendId, tuple[float, str | None]] = {}
_PROBE_LOCK = threading.Lock()


@dataclass(frozen=True, slots=True)
class ExecutionBackendStatus:
    id: ExecutionBackendId
    name: str
    adapter: str
    description: str
    isolation: str
    cost: str
    available: bool
    reason: str | None
    supported_sandbox_profiles: tuple[str, ...]


def normalize_execution_backend(value: str | None) -> ExecutionBackendId:
    normalized = (value or DEFAULT_EXECUTION_BACKEND).strip().casefold()
    if normalized not in EXECUTION_BACKEND_IDS:
        raise ValueError(
            "execution_backend must be one of systemd, docker, ssh, modal, daytona, vercel"
        )
    return cast(ExecutionBackendId, normalized)


def probe_execution_backends() -> tuple[ExecutionBackendStatus, ...]:
    systemd_reason = _cached_unavailable_reason("systemd")
    docker_reason = _cached_unavailable_reason("docker")
    ssh_reason = _cached_unavailable_reason("ssh")
    modal_reason = _cached_unavailable_reason("modal")
    daytona_reason = _cached_unavailable_reason("daytona")
    vercel_reason = _cached_unavailable_reason("vercel")
    return (
        ExecutionBackendStatus(
            id="systemd",
            name="本机 systemd 隔离",
            adapter="SystemdSkillSandbox",
            description="通过最小权限 broker 在当前 Linux 服务器上运行 systemd 隔离技能。",
            isolation="固定严格隔离：DynamicUser + 只读系统 + 私有网络",
            cost="本机资源",
            available=systemd_reason is None,
            reason=systemd_reason,
            supported_sandbox_profiles=("none", "read_only", "restricted", "workspace_write"),
        ),
        ExecutionBackendStatus(
            id="ssh",
            name="SSH 远程隔离",
            adapter="SshSkillSandbox",
            description="通过主机密钥校验连接远程 runner，并传输技能包与受限工作区。",
            isolation="远端 runner 强制执行的 systemd 或容器隔离",
            cost="远程主机资源",
            available=ssh_reason is None,
            reason=ssh_reason,
            supported_sandbox_profiles=("none", "read_only", "restricted", "workspace_write"),
        ),
        *_cloud_backend_statuses(
            (("modal", modal_reason), ("daytona", daytona_reason), ("vercel", vercel_reason))
        ),
        ExecutionBackendStatus(
            id="docker",
            name="Docker 容器隔离",
            adapter="DockerSkillSandbox",
            description="在只读、降权、受限资源的临时容器中运行技能。",
            isolation="固定严格隔离：容器 + drop capabilities + no-new-privileges",
            cost="本机容器资源",
            available=docker_reason is None,
            reason=docker_reason,
            supported_sandbox_profiles=("none", "read_only", "restricted", "workspace_write"),
        ),
    )


def execution_backend_unavailable_reason(
    value: str | None,
    sandbox_profile: str | None = None,
) -> str | None:
    backend = normalize_execution_backend(value)
    reason = _cached_unavailable_reason(backend)
    if reason is not None:
        return reason
    if sandbox_profile is not None and sandbox_profile not in {
        "none",
        "read_only",
        "restricted",
        "workspace_write",
    }:
        return "sandbox_profile_not_supported"
    return None


def clear_execution_backend_probe_cache() -> None:
    with _PROBE_LOCK:
        _PROBE_CACHE.clear()


def _cached_unavailable_reason(backend: ExecutionBackendId) -> str | None:
    now = time.monotonic()
    with _PROBE_LOCK:
        cached = _PROBE_CACHE.get(backend)
        if cached is not None and now - cached[0] < _PROBE_CACHE_TTL_SECONDS:
            return cached[1]
        if backend == "systemd":
            reason = _systemd_unavailable_reason()
        elif backend == "docker":
            reason = _docker_unavailable_reason()
        else:
            reason = remote_backend_unavailable_reason(backend)
        _PROBE_CACHE[backend] = (time.monotonic(), reason)
        return reason


def _systemd_unavailable_reason() -> str | None:
    return probe_skill_broker("systemd")


def _docker_unavailable_reason() -> str | None:
    return probe_skill_broker("docker")


def _cloud_backend_statuses(
    values: tuple[tuple[str, str | None], ...],
) -> tuple[ExecutionBackendStatus, ...]:
    names = {"modal": "Modal 云沙箱", "daytona": "Daytona 云工作区", "vercel": "Vercel Sandbox"}
    return tuple(
        ExecutionBackendStatus(
            id=cast(ExecutionBackendId, backend),
            name=names[backend],
            adapter="HttpRemoteSkillSandbox",
            description="通过版本化 HTTPS 协议执行技能，并回传受限工作区产物。",
            isolation="由云端 runner 声明并通过健康协议验证",
            cost="云端按量资源",
            available=reason is None,
            reason=reason,
            supported_sandbox_profiles=("none", "read_only", "restricted", "workspace_write"),
        )
        for backend, reason in values
    )
