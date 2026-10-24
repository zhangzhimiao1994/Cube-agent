from pathlib import Path
from types import SimpleNamespace
from typing import cast
from uuid import UUID

from agent_hub.skills.sandbox.broker import BrokerPolicy
from agent_hub.skills.sandbox.python_input_supply_broker import build_input_supply_root_command
from agent_hub.skills.sandbox.python_input_supply_catalog import RootSupplyConfig
from agent_hub.skills.sandbox.python_input_supply_worker import build_fetch_command
from agent_hub.skills.sandbox.python_project_backend import RootConfig
from agent_hub.skills.sandbox.python_project_protocol import (
    PythonProjectProbeInvocation,
    base_python_invocation,
)


def invocation() -> PythonProjectProbeInvocation:
    return PythonProjectProbeInvocation.model_validate({"execution_id": "supply_" + "1" * 32,
        "tenant_id": UUID(int=1), "run_id": UUID(int=2), "project_id": "project", "session_id": "session",
        "sandbox_profile": "workspace_write", "timeout_seconds": 30,
        "original_reference": {"schema_version": 1, "contract_sha256": "1" * 64,
                            "input_source_sha256": "2" * 64, "file_count": 6}})


def settings() -> tuple[SimpleNamespace, SimpleNamespace, SimpleNamespace]:
    policy = SimpleNamespace(python="/opt/app/bin/python", systemd_run="/usr/bin/systemd-run",
        systemctl="/usr/bin/systemctl", service_group="agent_hub", project_workspace_root=Path("/var/lib/workspaces"))
    config = SimpleNamespace(stage_root=Path("/var/lib/stages"), environment_root=Path("/var/lib/environments"),
                             wheel_root=Path("/var/lib/wheels"))
    supply = SimpleNamespace(state_root=Path("/var/lib/input-supply"), fetch_timeout_seconds=15)
    return policy, config, supply


def test_root_worker_is_offline_and_only_writes_explicit_supply_roots() -> None:
    policy, config, supply = settings()
    command = build_input_supply_root_command(base_python_invocation(invocation()), cast(BrokerPolicy, policy),
        configuration=cast(RootConfig, config), supply=cast(RootSupplyConfig, supply), stage=config.stage_root / "owned")
    assert "PrivateNetwork=yes" in command and "IPAddressDeny=any" in command
    assert "RestrictAddressFamilies=AF_UNIX" in command
    assert "agent_hub.skills.sandbox.python_input_supply_worker" in command
    assert "ReadWritePaths=/var/lib/stages /var/lib/wheels /var/lib/input-supply" in command
    assert not any("ReadWritePaths=" in item and "environments" in item for item in command)


def test_fetch_unit_has_no_project_cache_secret_or_broker_access() -> None:
    policy, config, supply = settings()
    command = build_fetch_command(invocation(), cast(BrokerPolicy, policy),
        configuration=cast(RootConfig, config), supply=cast(RootSupplyConfig, supply))
    assert "DynamicUser=yes" in command and "CapabilityBoundingSet=" in command
    assert "SupplementaryGroups=agent_hub" in command
    assert "ProtectSystem=strict" in command and "ProtectHome=yes" in command
    assert "ProtectProc=invisible" in command and "LimitFSIZE=0" in command
    assert "PrivateNetwork=yes" not in command
    assert "agent_hub.skills.sandbox.python_input_fetch" in command and "--worker-batch" in command
    masked = next(item for item in command if item.startswith("InaccessiblePaths="))
    assert masked == "InaccessiblePaths=/etc/agent-hub"
    filesystems = next(item for item in command if item.startswith("TemporaryFileSystem="))
    assert "/run:rw,nosuid,nodev,noexec,size=1048576,mode=0755" in filesystems
    assert "/var:ro,nosuid,nodev,noexec,size=1048576" in filesystems
    assert "/tmp:rw,nosuid,nodev,noexec,size=8388608" in filesystems
    assert "PrivateTmp=no" in command and "BindReadOnlyPaths=/etc/resolv.conf" in command
    assert "-i" in command and "-I" in command and "-B" in command


def test_fetch_still_masks_a_project_outside_empty_runtime_filesystems() -> None:
    policy, config, supply = settings()
    policy.project_workspace_root = Path("/srv/private-projects")
    config.wheel_root = Path("/mnt/private-wheels")
    command = build_fetch_command(invocation(), cast(BrokerPolicy, policy),
        configuration=cast(RootConfig, config), supply=cast(RootSupplyConfig, supply))
    masked = next(item for item in command if item.startswith("InaccessiblePaths="))
    assert set(masked.split("=", 1)[1].split()) == {
        "/etc/agent-hub", "/srv/private-projects", "/mnt/private-wheels"}
