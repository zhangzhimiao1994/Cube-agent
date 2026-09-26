from __future__ import annotations

from subprocess import CompletedProcess

from agent_hub.execution_backends import (
    clear_execution_backend_probe_cache,
    execution_backend_unavailable_reason,
    probe_execution_backends,
)


def setup_function() -> None:
    clear_execution_backend_probe_cache()


def teardown_function() -> None:
    clear_execution_backend_probe_cache()


def test_probe_reports_systemd_and_docker_as_real_runtime_capabilities(monkeypatch) -> None:
    executables = {
        "systemd-run": "/usr/bin/systemd-run",
        "systemctl": "/usr/bin/systemctl",
        "docker": "/usr/bin/docker",
    }
    monkeypatch.setattr(
        "agent_hub.execution_backends.shutil.which",
        lambda name: executables.get(name),
    )
    monkeypatch.setattr(
        "agent_hub.execution_backends.subprocess.run",
        lambda *args, **kwargs: CompletedProcess(args=args[0], returncode=0, stdout="image-id\n", stderr=""),
    )

    statuses = {item.id: item for item in probe_execution_backends()}

    assert statuses["systemd"].available is True
    assert statuses["systemd"].adapter == "SystemdSkillSandbox"
    assert statuses["docker"].available is True
    assert statuses["docker"].adapter == "DockerSkillSandbox"


def test_probe_explains_missing_docker_instead_of_advertising_placeholder(monkeypatch) -> None:
    monkeypatch.setattr(
        "agent_hub.execution_backends.shutil.which",
        lambda name: "/usr/bin/systemd-run" if name in {"systemd-run", "systemctl"} else None,
    )

    statuses = {item.id: item for item in probe_execution_backends()}

    assert statuses["docker"].available is False
    assert statuses["docker"].reason == "docker_cli_not_found"


def test_probe_rejects_systemd_when_transient_units_cannot_start(monkeypatch) -> None:
    executables = {
        "systemd-run": "/usr/bin/systemd-run",
        "systemctl": "/usr/bin/systemctl",
        "docker": "/usr/bin/docker",
    }
    monkeypatch.setattr(
        "agent_hub.execution_backends.shutil.which",
        lambda name: executables.get(name),
    )

    def run(args, **kwargs):
        del kwargs
        if args[0] == "/usr/bin/systemd-run":
            return CompletedProcess(args=args, returncode=1, stdout="", stderr="Failed to connect to bus")
        return CompletedProcess(args=args, returncode=0, stdout="image-id\n", stderr="")

    monkeypatch.setattr("agent_hub.execution_backends.subprocess.run", run)

    statuses = {item.id: item for item in probe_execution_backends()}

    assert statuses["systemd"].available is False
    assert statuses["systemd"].reason == "systemd_transient_unit_unavailable"


def test_systemd_probe_exercises_the_runtime_isolation_contract(monkeypatch) -> None:
    executables = {
        "systemd-run": "/usr/bin/systemd-run",
        "systemctl": "/usr/bin/systemctl",
    }
    monkeypatch.setattr(
        "agent_hub.execution_backends.shutil.which",
        lambda name: executables.get(name),
    )
    commands: list[tuple[str, ...]] = []

    def run(args, **kwargs):
        del kwargs
        commands.append(tuple(args))
        return CompletedProcess(args=args, returncode=0, stdout="", stderr="")

    monkeypatch.setattr("agent_hub.execution_backends.subprocess.run", run)

    probe_execution_backends()

    command = commands[0]
    properties = {
        command[index + 1].split("=", 1)[0]: command[index + 1].split("=", 1)[1]
        for index, value in enumerate(command)
        if value == "-p" and "=" in command[index + 1]
    }
    assert properties["DynamicUser"] == "yes"
    assert properties["NoNewPrivileges"] == "yes"
    assert properties["ProtectSystem"] == "strict"
    assert properties["PrivateTmp"] == "yes"
    assert properties["PrivateDevices"] == "yes"
    assert properties["RestrictSUIDSGID"] == "yes"
    assert properties["PrivateNetwork"] == "yes"
    assert properties["IPAddressDeny"] == "any"
    assert properties["MemoryMax"]
    assert properties["CPUQuota"]
    assert properties["RuntimeMaxSec"]
    assert properties["ReadOnlyPaths"]
    assert properties["ReadWritePaths"]
    assert properties["WorkingDirectory"]
    assert "import agent_hub.skills.runner" in command


def test_selected_backend_probe_is_cached_without_probing_other_backends(monkeypatch) -> None:
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


def test_docker_probe_starts_the_hardened_runner_image(monkeypatch) -> None:
    monkeypatch.setattr(
        "agent_hub.execution_backends.shutil.which",
        lambda name: "/usr/bin/docker" if name == "docker" else None,
    )
    commands: list[tuple[str, ...]] = []

    def run(args, **kwargs):
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
