"""Probe and describe the execution backends implemented by Agent Hub."""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

ExecutionBackendId = Literal["systemd", "docker"]
EXECUTION_BACKEND_IDS = frozenset({"systemd", "docker"})
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
        raise ValueError("execution_backend must be one of systemd, docker")
    return cast(ExecutionBackendId, normalized)


def probe_execution_backends() -> tuple[ExecutionBackendStatus, ...]:
    systemd_reason = _cached_unavailable_reason("systemd")
    docker_reason = _cached_unavailable_reason("docker")
    return (
        ExecutionBackendStatus(
            id="systemd",
            name="本机 systemd 隔离",
            adapter="SystemdSkillSandbox",
            description="在当前 Linux 服务器上通过 systemd transient unit 运行技能。",
            isolation="固定严格隔离：DynamicUser + 只读系统 + 私有网络",
            cost="本机资源",
            available=systemd_reason is None,
            reason=systemd_reason,
            supported_sandbox_profiles=("none", "read_only", "restricted", "workspace_write"),
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
        reason = (
            _systemd_unavailable_reason()
            if backend == "systemd"
            else _docker_unavailable_reason()
        )
        _PROBE_CACHE[backend] = (time.monotonic(), reason)
        return reason


def _systemd_unavailable_reason() -> str | None:
    systemd_run = shutil.which("systemd-run")
    if systemd_run is None or shutil.which("systemctl") is None:
        return "systemd_tools_not_found"
    unit = f"agent-hub-execution-probe-{uuid.uuid4().hex[:12]}"
    try:
        with tempfile.TemporaryDirectory(prefix="agent-hub-execution-probe-") as temp_dir:
            probe_workdir = Path(temp_dir)
            probe_workdir.chmod(0o777)
            workdir = probe_workdir.as_posix()
            source_root = Path(__file__).resolve().parents[1].as_posix()
            python = Path(sys.executable).resolve().as_posix()
            result = subprocess.run(
                (
                    systemd_run,
                    "--wait",
                    "--collect",
                    "--quiet",
                    "--unit",
                    unit,
                    "-p",
                    "DynamicUser=yes",
                    "-p",
                    "NoNewPrivileges=yes",
                    "-p",
                    "ProtectSystem=strict",
                    "-p",
                    "PrivateTmp=yes",
                    "-p",
                    "PrivateDevices=yes",
                    "-p",
                    "RestrictSUIDSGID=yes",
                    "-p",
                    "PrivateNetwork=yes",
                    "-p",
                    "IPAddressDeny=any",
                    "-p",
                    "MemoryMax=33554432",
                    "-p",
                    "CPUQuota=100%",
                    "-p",
                    "RuntimeMaxSec=5s",
                    "-p",
                    f"ReadOnlyPaths={source_root}",
                    "-p",
                    f"ReadOnlyPaths={python}",
                    "-p",
                    f"ReadWritePaths={workdir}",
                    "-p",
                    f"WorkingDirectory={workdir}",
                    "-E",
                    f"PYTHONPATH={source_root}",
                    python,
                    "-c",
                    "import agent_hub.skills.runner",
                ),
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
    except (OSError, subprocess.TimeoutExpired):
        return "systemd_transient_unit_unavailable"
    return None if result.returncode == 0 else "systemd_transient_unit_unavailable"


def _docker_unavailable_reason() -> str | None:
    docker = shutil.which("docker")
    if docker is None:
        return "docker_cli_not_found"
    try:
        result = subprocess.run(
            (docker, "image", "inspect", DOCKER_SKILL_RUNNER_IMAGE, "--format", "{{.Id}}"),
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "docker_daemon_unavailable"
    if result.returncode != 0:
        combined = f"{result.stdout}\n{result.stderr}".casefold()
        if "no such image" in combined or "not found" in combined:
            return "docker_runner_image_not_found"
        return "docker_daemon_unavailable"
    try:
        runner = subprocess.run(
            (
                docker,
                "run",
                "--rm",
                "--user",
                "65532:65532",
                "--read-only",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges",
                "--pids-limit",
                "16",
                "--memory",
                "33554432",
                "--cpus",
                "0.25",
                "--network",
                "none",
                "--tmpfs",
                "/tmp:rw,noexec,nosuid,nodev,size=16m",
                DOCKER_SKILL_RUNNER_IMAGE,
                "python",
                "-c",
                "import agent_hub.skills.runner",
            ),
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "docker_runner_unavailable"
    return None if runner.returncode == 0 else "docker_runner_unavailable"
