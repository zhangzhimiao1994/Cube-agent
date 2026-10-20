"""Fixed Python outer-unit command; trusted paths are never model arguments."""

from __future__ import annotations

from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

from agent_hub.skills.sandbox.python_project_protocol import PythonProjectInvocation

if TYPE_CHECKING:
    from agent_hub.skills.sandbox.broker import BrokerPolicy


def _escaped(path: Path) -> str:
    value = path.as_posix()
    if (
        not PurePosixPath(value).is_absolute()
        or ".." in PurePosixPath(value).parts
        or ":" in value
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        raise ValueError("python_project_command_rejected")
    return "".join(
        "%%" if char == "%" else f"\\x{ord(char):02x}" if char in "\\ '\"" else char
        for char in value
    )


def build_python_project_systemd_command(
    invocation: PythonProjectInvocation,
    policy: BrokerPolicy,
    *,
    snapshot: Path,
    original: Path,
    plan: Path,
    runtime: Path,
    wheels: Path,
    export: Path,
    environment: Path | None = None,
) -> tuple[str, ...]:
    if type(invocation) is not PythonProjectInvocation:
        raise ValueError("python_project_command_rejected")
    invocation = PythonProjectInvocation.model_validate(invocation.model_dump(), strict=True)
    binds = (
        (snapshot, "source"),
        (original, "original"),
        (plan, "plan.json"),
        (runtime, "runtime"),
        (wheels, "wheels"),
        (policy.source_path, "trusted-source"),
    )
    properties = [
        "DynamicUser=yes",
        f"SupplementaryGroups={policy.service_group}",
        "UMask=0077",
        "NoNewPrivileges=yes",
        "ProtectSystem=strict",
        "ProtectHome=yes",
        "PrivateTmp=yes",
        "PrivateDevices=yes",
        "PrivateNetwork=yes",
        "IPAddressDeny=any",
        "RestrictAddressFamilies=AF_UNIX AF_NETLINK",
        "ProtectKernelModules=yes",
        "ProtectControlGroups=yes",
        "RestrictSUIDSGID=yes",
        "LockPersonality=yes",
        "CapabilityBoundingSet=",
        "TasksMax=128",
        "Slice=system.slice",
        "KillMode=control-group",
        "TimeoutStopSec=2s",
        f"MemoryMax={invocation.memory_limit_bytes}",
        f"CPUQuota={invocation.cpu_quota_percent}%",
        f"RuntimeMaxSec={invocation.timeout_seconds}s",
        (
            f"TemporaryFileSystem=/run/agent-hub-python-work:rw,nosuid,nodev,size={invocation.workspace_limit_bytes},"
            f"nr_inodes={invocation.file_limit * 10},mode=1777"
        ),
        "InaccessiblePaths=/var/lib/agent-hub",
    ]
    properties.extend(
        f"BindReadOnlyPaths={_escaped(path)}:/run/agent-hub-python-{name}" for path, name in binds
    )
    properties.append(f"BindPaths={_escaped(export)}:/run/agent-hub-python-export")
    if environment is not None:
        properties.append(
            f"BindReadOnlyPaths={_escaped(environment)}:/run/agent-hub-python-environment"
        )
    command = [
        policy.systemd_run,
        "--wait",
        "--collect",
        "--pipe",
        "--quiet",
        "--unit",
        f"agent-hub-project-{invocation.execution_id}",
    ]
    for value in properties:
        command.extend(("-p", value))
    # -I ignores PYTHONPATH and user startup files; only this fixed read-only code
    # mount is added to sys.path. Project source/env is never on the outer path.
    bootstrap = (
        "import sys;sys.path.insert(0,'/run/agent-hub-python-trusted-source');"
        "from agent_hub.skills.sandbox.python_project_runner import main;raise SystemExit(main())"
    )
    _escaped(Path(policy.python))
    command.extend(
        (
            "/usr/bin/env",
            "-i",
            "PATH=/usr/bin:/bin",
            "LANG=C.UTF-8",
            "AGENT_HUB_GENERATED_PROJECT_VALIDATION_SANDBOX=systemd",
            Path(policy.python).as_posix().replace("%", "%%"),
            "-I",
            "-B",
            "-c",
            bootstrap,
        )
    )
    return tuple(command)
