"""Probe and describe the execution backends implemented by Agent Hub."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Literal, cast

from agent_hub.skills.sandbox.broker import BROKER_PROBE_TIMEOUT_SECONDS, probe_skill_broker
from agent_hub.skills.sandbox.remote import remote_backend_unavailable_reason

ExecutionBackendId = Literal["systemd", "docker", "ssh", "modal", "daytona", "vercel"]
EXECUTION_BACKEND_IDS = frozenset({"systemd", "docker", "ssh", "modal", "daytona", "vercel"})
DEFAULT_EXECUTION_BACKEND: ExecutionBackendId = "systemd"
DOCKER_SKILL_RUNNER_IMAGE = "agent-hub-skill-runner:latest"
_PROBE_CACHE_TTL_SECONDS = 10.0
_PROBE_CACHE: dict[ExecutionBackendId, tuple[float, str | None]] = {}
_PROBE_LOCK = threading.Lock()
# Discovery must fit comfortably inside the ordinary GET's 20s caller budget.
# Selected admission instead waits for the same probe's full bounded protocol.
_DISCOVERY_WAIT_SECONDS = 1.0
_PROBE_TRANSIENT_TTL_SECONDS = 1.0
_PROBE_GENERATION = 0


@dataclass(slots=True)
class _Probe:
    generation: int
    done: threading.Event
    reason: str | None = None


_PROBE_IN_FLIGHT: dict[ExecutionBackendId, _Probe] = {}


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
    deadline = time.monotonic() + _DISCOVERY_WAIT_SECONDS
    order: tuple[ExecutionBackendId, ...] = ("systemd", "docker", "ssh", "modal", "daytona", "vercel")
    # Start all expired backends before waiting: one slow broker never holds
    # the cache lock or consumes the next backend's discovery window.
    for backend in order:
        with _PROBE_LOCK:
            if _fresh_cached_reason(backend) is None:
                _start_probe(backend)
    systemd_reason, docker_reason, ssh_reason, modal_reason, daytona_reason, vercel_reason = (
        _cached_unavailable_reason(backend, deadline=deadline) for backend in order
    )
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
    *,
    timeout_seconds: float | None = None,
) -> str | None:
    backend = normalize_execution_backend(value)
    wait_seconds = BROKER_PROBE_TIMEOUT_SECONDS + 1.0
    if timeout_seconds is not None:
        wait_seconds = max(0.0, min(wait_seconds, timeout_seconds))
    reason = _cached_unavailable_reason(backend, deadline=time.monotonic() + wait_seconds)
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
    global _PROBE_GENERATION
    with _PROBE_LOCK:
        _PROBE_GENERATION += 1
        _PROBE_CACHE.clear()
        # Keep ownership of running probes; invalidation cannot spawn duplicates
        # or allow a stale result to certify availability in the new generation.


def _transient_reason(reason: str | None) -> bool:
    return reason is not None and reason.endswith(("_probe_timeout", "_probe_failed"))


def _fresh_cached_reason(backend: ExecutionBackendId) -> tuple[float, str | None] | None:
    # Caller owns _PROBE_LOCK.
    cached = _PROBE_CACHE.get(backend)
    if cached is None:
        return None
    ttl = _PROBE_TRANSIENT_TTL_SECONDS if _transient_reason(cached[1]) else _PROBE_CACHE_TTL_SECONDS
    return cached if time.monotonic() - cached[0] < ttl else None


def _start_probe(backend: ExecutionBackendId) -> _Probe:
    # Caller owns _PROBE_LOCK. At most one daemon worker per fixed backend ID,
    # including across clear_cache; pending never certifies availability.
    existing = _PROBE_IN_FLIGHT.get(backend)
    if existing is not None:
        return existing
    probe = _Probe(_PROBE_GENERATION, threading.Event())
    _PROBE_IN_FLIGHT[backend] = probe

    def refresh() -> None:
        try:
            if backend == "systemd":
                reason = _systemd_unavailable_reason()
            elif backend == "docker":
                reason = _docker_unavailable_reason()
            else:
                reason = remote_backend_unavailable_reason(backend)
        except Exception:  # noqa: BLE001 - discovery errors never grant availability
            reason = f"{backend}_probe_failed"
        with _PROBE_LOCK:
            probe.reason = reason
            if probe.generation == _PROBE_GENERATION:
                _PROBE_CACHE[backend] = (time.monotonic(), reason)
            _PROBE_IN_FLIGHT.pop(backend, None)
            probe.done.set()

    try:
        threading.Thread(target=refresh, name=f"backend-probe-{backend}", daemon=True).start()
    except RuntimeError:
        _PROBE_IN_FLIGHT.pop(backend, None)
        probe.reason = f"{backend}_probe_failed"
        _PROBE_CACHE[backend] = (time.monotonic(), probe.reason)
        probe.done.set()
    return probe


def _cached_unavailable_reason(
    backend: ExecutionBackendId, *, deadline: float | None = None,
) -> str | None:
    if deadline is None:
        deadline = time.monotonic() + BROKER_PROBE_TIMEOUT_SECONDS + 1.0
    while True:
        with _PROBE_LOCK:
            cached = _fresh_cached_reason(backend)
            if cached is not None:
                return cached[1]
            probe = _start_probe(backend)
        if not probe.done.wait(max(0.0, deadline - time.monotonic())):
            return f"{backend}_probe_pending"
        with _PROBE_LOCK:
            if probe.generation == _PROBE_GENERATION:
                # Return this caller's actual result even when its retry TTL is
                # zero. Only subsequent callers should start a fresh attempt.
                return probe.reason
        if time.monotonic() >= deadline:
            return f"{backend}_probe_pending"


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
