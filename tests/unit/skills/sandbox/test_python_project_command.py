"""Only the trusted outer unit can see the export; hooks have separate mounts."""

from __future__ import annotations

import importlib
import importlib.util
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from agent_hub.skills.sandbox.broker import BrokerPolicy
from agent_hub.skills.sandbox.project import OriginalProjectReference
from agent_hub.skills.sandbox.python_project_protocol import PythonProjectInvocation


@pytest.fixture
def api() -> Any:
    name = "agent_hub.skills.sandbox.python_project_command"
    assert importlib.util.find_spec(name) is not None, "Python systemd launcher missing"
    return importlib.import_module(name)


def invocation() -> PythonProjectInvocation:
    return PythonProjectInvocation(
        execution_id="python_1",
        tenant_id=UUID(int=1),
        run_id=UUID(int=2),
        project_id="billing",
        session_id="one",
        sandbox_profile="workspace_write",
        timeout_seconds=180,
        install_allowed=True,
        source_kind="original",
        recipe_sha256="c" * 64,
        environment_input_sha256="d" * 64,
        original_reference=OriginalProjectReference(
            contract_sha256="a" * 64, input_source_sha256="b" * 64, file_count=6
        ),
    )


def locations() -> dict[str, Path]:
    return {
        name: Path("/private") / name
        for name in ("snapshot", "original", "plan", "runtime", "wheels", "export")
    }


def test_unit_is_offline_quota_bounded_and_only_export_is_host_writable(api: Any) -> None:
    args = api.build_python_project_systemd_command(invocation(), BrokerPolicy(), **locations())
    properties = [args[index + 1] for index, value in enumerate(args) if value == "-p"]
    assert args[:7] == (
        "/usr/bin/systemd-run",
        "--wait",
        "--collect",
        "--pipe",
        "--quiet",
        "--unit",
        "agent-hub-project-python_1",
    )
    assert "PrivateNetwork=yes" in properties and "IPAddressDeny=any" in properties
    # bwrap configures its private loopback through netlink, never AF_INET sockets.
    assert "RestrictAddressFamilies=AF_UNIX AF_NETLINK" in properties
    assert "DynamicUser=yes" in properties and "KillMode=control-group" in properties
    assert "ProtectSystem=strict" in properties and "CapabilityBoundingSet=" in properties
    assert "RuntimeMaxSec=180s" in properties
    assert any(
        value.startswith("TemporaryFileSystem=/run/agent-hub-python-work:") for value in properties
    )
    assert [value for value in properties if value.startswith("BindPaths=")] == [
        "BindPaths=/private/export:/run/agent-hub-python-export"
    ]
    assert not any("project-environments" in value for value in properties)
    assert "/usr/bin/env" in args and "-i" in args and "-I" in args
    assert "AGENT_HUB_GENERATED_PROJECT_VALIDATION_SANDBOX=systemd" in args
    assert "PYTHONPATH=" not in " ".join(args)


def test_reuse_mount_is_readonly_and_missing_reuse_does_not_mount_parent_cache(api: Any) -> None:
    args = api.build_python_project_systemd_command(
        invocation(),
        BrokerPolicy(),
        **locations(),
        environment=Path("/private/one-cache/environment"),
    )
    assert (
        "BindReadOnlyPaths=/private/one-cache/environment:/run/agent-hub-python-environment" in args
    )
    assert all(not value.startswith("BindPaths=/private/one-cache") for value in args)


@pytest.mark.parametrize("bad", ["/private/../else", "/private/x\nother", "/private/x:other"])
def test_paths_cannot_escape_or_inject_systemd_property_words(api: Any, bad: str) -> None:
    with pytest.raises(ValueError):
        api.build_python_project_systemd_command(
            invocation(), BrokerPolicy(), **(locations() | {"runtime": Path(bad)})
        )


def test_percent_spaces_and_quotes_are_literal_not_specifiers(api: Any) -> None:
    args = api.build_python_project_systemd_command(
        invocation(), BrokerPolicy(), **(locations() | {"runtime": Path("/private/a %x'quoted")})
    )
    assert "BindReadOnlyPaths=/private/a\\x20%%x\\x27quoted:/run/agent-hub-python-runtime" in args
