from __future__ import annotations

from subprocess import CompletedProcess
from typing import Any

import pytest

from agent_hub.execution_backends import (
    clear_execution_backend_probe_cache,
    execution_backend_unavailable_reason,
    probe_execution_backends,
)


def setup_function() -> None:
    clear_execution_backend_probe_cache()


def teardown_function() -> None:
    clear_execution_backend_probe_cache()


def test_probe_reports_systemd_and_docker_as_real_runtime_capabilities(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executables = {"docker": "/usr/bin/docker"}
    monkeypatch.setattr(
        "agent_hub.execution_backends.shutil.which",
        lambda name: executables.get(name),
    )
    monkeypatch.setattr(
        "agent_hub.execution_backends.subprocess.run",
        lambda *args, **kwargs: CompletedProcess(args=args[0], returncode=0, stdout="image-id\n", stderr=""),
    )
    monkeypatch.setattr("agent_hub.execution_backends.probe_systemd_broker", lambda: None)

    statuses = {item.id: item for item in probe_execution_backends()}

    assert statuses["systemd"].available is True
    assert statuses["systemd"].adapter == "SystemdSkillSandbox"
    assert statuses["docker"].available is True
    assert statuses["docker"].adapter == "DockerSkillSandbox"


def test_probe_explains_missing_docker_instead_of_advertising_placeholder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "agent_hub.execution_backends.shutil.which",
        lambda name: None,
    )
    monkeypatch.setattr("agent_hub.execution_backends.probe_systemd_broker", lambda: None)

    statuses = {item.id: item for item in probe_execution_backends()}

    assert statuses["docker"].available is False
    assert statuses["docker"].reason == "docker_cli_not_found"


def test_probe_rejects_systemd_when_privileged_broker_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executables = {"docker": "/usr/bin/docker"}
    monkeypatch.setattr(
        "agent_hub.execution_backends.shutil.which",
        lambda name: executables.get(name),
    )

    monkeypatch.setattr(
        "agent_hub.execution_backends.probe_systemd_broker",
        lambda: "systemd_broker_unavailable",
    )

    statuses = {item.id: item for item in probe_execution_backends()}

    assert statuses["systemd"].available is False
    assert statuses["systemd"].reason == "systemd_broker_unavailable"


def test_systemd_probe_exercises_the_runtime_isolation_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def probe() -> None:
        nonlocal calls
        calls += 1

    monkeypatch.setattr("agent_hub.execution_backends.probe_systemd_broker", probe)

    probe_execution_backends()

    assert calls == 1


def test_selected_backend_probe_is_cached_without_probing_other_backends(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = {"systemd": 0, "docker": 0}

    def systemd_reason() -> None:
        calls["systemd"] += 1

    def docker_reason() -> None:
        calls["docker"] += 1

    monkeypatch.setattr(
        "agent_hub.execution_backends._systemd_unavailable_reason",
        systemd_reason,
    )
    monkeypatch.setattr(
        "agent_hub.execution_backends._docker_unavailable_reason",
        docker_reason,
    )

    assert execution_backend_unavailable_reason("systemd") is None
    assert execution_backend_unavailable_reason("systemd") is None
    assert calls == {"systemd": 1, "docker": 0}


def test_docker_probe_starts_the_hardened_runner_image(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "agent_hub.execution_backends.shutil.which",
        lambda name: "/usr/bin/docker" if name == "docker" else None,
    )
    commands: list[tuple[str, ...]] = []

    def run(args: list[str], **kwargs: Any) -> CompletedProcess[str]:
        del kwargs
        commands.append(tuple(args))
        return CompletedProcess(args=args, returncode=0, stdout="image-id\n", stderr="")

    monkeypatch.setattr("agent_hub.execution_backends.subprocess.run", run)

    statuses = {item.id: item for item in probe_execution_backends()}

    assert statuses["docker"].available is True
    runner_command = commands[-1]
    assert runner_command[:3] == ("/usr/bin/docker", "run", "--rm")
    assert "--read-only" in runner_command
    assert "no-new-privileges" in runner_command
    assert "none" in runner_command
    assert runner_command[-3:] == ("python", "-c", "import agent_hub.skills.runner")
