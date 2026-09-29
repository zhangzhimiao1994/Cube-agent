from __future__ import annotations

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
    monkeypatch.setattr("agent_hub.execution_backends.probe_skill_broker", lambda backend: None)

    statuses = {item.id: item for item in probe_execution_backends()}

    assert statuses["systemd"].available is True
    assert statuses["systemd"].adapter == "SystemdSkillSandbox"
    assert statuses["docker"].available is True
    assert statuses["docker"].adapter == "DockerSkillSandbox"
    assert statuses["ssh"].adapter == "SshSkillSandbox"
    assert statuses["modal"].adapter == "HttpRemoteSkillSandbox"
    assert statuses["daytona"].adapter == "HttpRemoteSkillSandbox"
    assert statuses["vercel"].adapter == "HttpRemoteSkillSandbox"
    assert statuses["ssh"].available is False
    assert statuses["modal"].available is False


def test_probe_explains_missing_docker_broker_instead_of_advertising_placeholder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "agent_hub.execution_backends.probe_skill_broker",
        lambda backend: "docker_broker_unavailable" if backend == "docker" else None,
    )

    statuses = {item.id: item for item in probe_execution_backends()}

    assert statuses["docker"].available is False
    assert statuses["docker"].reason == "docker_broker_unavailable"


def test_probe_rejects_systemd_when_privileged_broker_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "agent_hub.execution_backends.probe_skill_broker",
        lambda backend: "systemd_broker_unavailable" if backend == "systemd" else None,
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

    monkeypatch.setattr(
        "agent_hub.execution_backends.probe_skill_broker",
        lambda backend: probe() if backend == "systemd" else None,
    )

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


def test_docker_probe_routes_through_privileged_skill_broker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    probes: list[str] = []

    def probe(backend: str) -> None:
        probes.append(backend)

    monkeypatch.setattr("agent_hub.execution_backends.probe_skill_broker", probe)

    statuses = {item.id: item for item in probe_execution_backends()}

    assert statuses["docker"].available is True
    assert probes == ["systemd", "docker"]
