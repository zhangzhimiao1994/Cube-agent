from __future__ import annotations

import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_hub.skills.sandbox.base import SkillInvocation, SkillResult
from agent_hub.skills.sandbox.broker import (
    BrokerPolicy,
    BrokerRequest,
    BrokerRequestError,
    SystemdBroker,
    build_broker_systemd_command,
    load_broker_policy,
    validate_broker_request,
)


def _invocation(root: Path, **changes: object) -> SkillInvocation:
    package = root / "tenant" / "demo.zip"
    workdir = root / "tenant" / "tmp" / "exec_1"
    package.parent.mkdir(parents=True, exist_ok=True)
    workdir.mkdir(parents=True, exist_ok=True)
    package.write_bytes(b"package")
    values: dict[str, object] = {
        "execution_id": "exec_1",
        "package_path": package,
        "package_sha256": "a" * 64,
        "input": {"prompt": "safe"},
        "timeout_seconds": 3,
        "output_limit_bytes": 1024,
        "memory_limit_bytes": 128 * 1024 * 1024,
        "cpu_quota_percent": 50,
        "sandbox_profile": "read_only",
        "network_allowlist": (),
        "read_only_inputs": (),
        "writable_tmp_path": workdir,
        "selected_secret_refs": (),
    }
    values.update(changes)
    return SkillInvocation.model_validate(values, strict=True)


def test_broker_rejects_callers_other_than_configured_service_uid(tmp_path: Path) -> None:
    request = BrokerRequest.run(_invocation(tmp_path / "skills"))
    policy = BrokerPolicy(skill_root=tmp_path / "skills", allowed_uid=10001)

    with pytest.raises(BrokerRequestError, match="caller uid"):
        validate_broker_request(request, peer_uid=10002, policy=policy)


def test_broker_protocol_does_not_accept_commands_or_systemd_properties(tmp_path: Path) -> None:
    invocation = _invocation(tmp_path / "skills")

    with pytest.raises(ValueError):
        BrokerRequest.model_validate(
            {
                "version": 1,
                "action": "run",
                "invocation": invocation.model_dump(mode="json"),
                "command": ["/bin/sh", "-c", "id"],
            },
            strict=True,
        )
    with pytest.raises(ValueError):
        BrokerRequest.model_validate(
            {
                "version": 1,
                "action": "run",
                "invocation": invocation.model_dump(mode="json"),
                "properties": {"User": "root"},
            },
            strict=True,
        )


def test_broker_rejects_paths_outside_fixed_roots_and_symlink_escape(tmp_path: Path) -> None:
    skill_root = tmp_path / "skills"
    policy = BrokerPolicy(skill_root=skill_root, allowed_uid=10001)
    outside = tmp_path / "outside.zip"
    outside.write_bytes(b"outside")
    request = BrokerRequest.run(_invocation(skill_root, package_path=outside))

    with pytest.raises(BrokerRequestError, match="package path"):
        validate_broker_request(request, peer_uid=10001, policy=policy)

    linked = skill_root / "tenant" / "linked.zip"
    try:
        linked.symlink_to(outside)
    except OSError:
        pytest.skip("symlinks are unavailable")
    request = BrokerRequest.run(_invocation(skill_root, package_path=linked))
    with pytest.raises(BrokerRequestError, match="package path"):
        validate_broker_request(request, peer_uid=10001, policy=policy)


def test_broker_rejects_unimplemented_network_and_secret_grants(tmp_path: Path) -> None:
    skill_root = tmp_path / "skills"
    policy = BrokerPolicy(skill_root=skill_root, allowed_uid=10001)

    with pytest.raises(BrokerRequestError, match="network allowlist"):
        validate_broker_request(
            BrokerRequest.run(_invocation(skill_root, network_allowlist=("api.example.com",))),
            peer_uid=10001,
            policy=policy,
        )
    with pytest.raises(BrokerRequestError, match="secret references"):
        validate_broker_request(
            BrokerRequest.run(_invocation(skill_root, selected_secret_refs=("secret://token",))),
            peer_uid=10001,
            policy=policy,
        )

def test_broker_builds_only_fixed_hardened_transient_unit(tmp_path: Path) -> None:
    skill_root = tmp_path / "skills"
    invocation = _invocation(skill_root)
    request = BrokerRequest.run(invocation)
    policy = BrokerPolicy(
        skill_root=skill_root,
        allowed_uid=10001,
        python="/opt/agent-hub/current/.venv/bin/python",
        source_path=Path("/opt/agent-hub/current/src"),
    )

    validated = validate_broker_request(request, peer_uid=10001, policy=policy)
    command = build_broker_systemd_command(validated.invocation, policy)
    properties = [command[index + 1] for index, item in enumerate(command) if item == "-p"]

    assert command[:5] == ("/usr/bin/systemd-run", "--wait", "--collect", "--pipe", "--quiet")
    assert "DynamicUser=yes" in properties
    assert "NoNewPrivileges=yes" in properties
    assert "ProtectSystem=strict" in properties
    assert "PrivateNetwork=yes" in properties
    assert "IPAddressDeny=any" in properties
    assert "CapabilityBoundingSet=" in properties
    assert "InaccessiblePaths=/var/lib/agent-hub" in properties
    assert any(item.startswith("BindReadOnlyPaths=") and item.endswith(":/run/agent-hub-skill/package.zip") for item in properties)
    assert any(item.startswith("BindPaths=") and item.endswith(":/run/agent-hub-skill/workspace") for item in properties)
    assert all("User=root" not in item for item in properties)
    assert command[-3:] == (
        "/opt/agent-hub/current/.venv/bin/python",
        "-m",
        "agent_hub.skills.runner",
    )


def test_broker_terminate_targets_only_valid_skill_unit(tmp_path: Path) -> None:
    policy = BrokerPolicy(skill_root=tmp_path / "skills", allowed_uid=10001)
    request = BrokerRequest.terminate("exec_1")

    validated = validate_broker_request(request, peer_uid=10001, policy=policy)

    assert validated.execution_id == "exec_1"
    with pytest.raises(ValueError):
        BrokerRequest.terminate("../root")


async def test_broker_probe_returns_stable_reason_without_systemd_stderr(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class FailedProcess:
        returncode = 1

        async def communicate(self) -> tuple[bytes, bytes]:
            return b"", b"Failed to connect to bus: sensitive host detail"

    async def create_subprocess_exec(*args: object, **kwargs: object) -> FailedProcess:
        del args, kwargs
        return FailedProcess()

    monkeypatch.setattr("agent_hub.skills.sandbox.broker.asyncio.create_subprocess_exec", create_subprocess_exec)
    broker = SystemdBroker(BrokerPolicy(skill_root=tmp_path, allowed_uid=10001))

    response = await broker.handle(BrokerRequest.probe(), peer_uid=10001)

    assert response.ok is False
    assert response.error == "systemd_transient_unit_unavailable"


@pytest.mark.parametrize("allowed_uid,allowed_gid", [(10001, 10001), (4242, 4343)])
async def test_broker_routes_docker_run_through_fixed_hardened_command(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    allowed_uid: int,
    allowed_gid: int,
) -> None:
    skill_root = tmp_path / "skills"
    invocation = _invocation(skill_root)
    commands: list[tuple[str, ...]] = []

    async def run_limited(
        command: tuple[str, ...],
        received: SkillInvocation,
        **kwargs: object,
    ) -> SkillResult:
        del kwargs
        commands.append(command)
        assert received == invocation
        return SkillResult(exit_code=0, stdout="docker-ok\n", stderr="", timed_out=False)

    monkeypatch.setattr(
        "agent_hub.skills.sandbox.broker.run_subprocess_with_limits",
        run_limited,
    )
    broker = SystemdBroker(
        BrokerPolicy(
            skill_root=skill_root,
            allowed_uid=allowed_uid,
            allowed_gid=allowed_gid,
            docker="/snap/bin/docker",
        )
    )

    response = await broker.handle(
        BrokerRequest.run(invocation, backend="docker"),
        peer_uid=allowed_uid,
    )

    assert response.ok is True
    assert response.result is not None
    assert response.result.stdout == "docker-ok\n"
    command = commands[0]
    assert command[:3] == ("/snap/bin/docker", "run", "--rm")
    assert "--interactive" in command
    assert "--tty" not in command and "-t" not in command
    assert command[command.index("--user") + 1] == f"{allowed_uid}:{allowed_gid}"
    assert command[command.index("--network") + 1] == "none"
    assert all("docker.sock" not in item for item in command)


async def test_docker_broker_probe_executes_real_mounted_skill_contract(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    received: list[tuple[SkillInvocation, str]] = []
    monkeypatch.setattr(
        "agent_hub.skills.sandbox.broker.os.chown",
        lambda *_args: None,
        raising=False,
    )
    broker = SystemdBroker(
        BrokerPolicy(
            skill_root=tmp_path / "skills",
            allowed_uid=10001,
            docker="/snap/bin/docker",
            runtime_root=tmp_path / "run",
        )
    )

    async def run_probe(invocation: SkillInvocation, backend: str) -> SkillResult:
        received.append((invocation, backend))
        assert invocation.package_path.is_file()
        assert invocation.writable_tmp_path.is_dir()
        with zipfile.ZipFile(invocation.package_path) as archive:
            assert {"skill.yaml", "main.py"}.issubset(archive.namelist())
        return SkillResult(exit_code=0, stdout='{"docker_probe":"ok"}\n', stderr="", timed_out=False)

    monkeypatch.setattr(broker, "_run", run_probe)

    response = await broker.handle(BrokerRequest.probe(backend="docker"), peer_uid=10001)

    assert response.ok is True
    assert len(received) == 1
    assert received[0][1] == "docker"
    assert received[0][0].input == {"docker_probe": "ok"}


@pytest.mark.parametrize("allowed_uid,allowed_gid", [(10001, 10001), (4242, 4343)])
async def test_docker_probe_hands_off_private_workspace_to_installed_runner_user(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    allowed_uid: int,
    allowed_gid: int,
) -> None:
    permissions: dict[Path, int] = {}
    ownership: dict[Path, tuple[int, int]] = {}
    original_chmod = Path.chmod

    def chmod(path: Path, mode: int, **kwargs: object) -> None:
        if path in ownership:
            raise PermissionError("CAP_FOWNER is intentionally unavailable")
        permissions[path] = mode
        original_chmod(path, mode)

    def chown(path: Path, uid: int, gid: int) -> None:
        ownership[path] = (uid, gid)

    monkeypatch.setattr(Path, "chmod", chmod)
    monkeypatch.setattr("agent_hub.skills.sandbox.broker.os.chown", chown, raising=False)
    runtime_root = tmp_path / "run"
    broker = SystemdBroker(
        BrokerPolicy(
            allowed_uid=allowed_uid,
            allowed_gid=allowed_gid,
            runtime_root=runtime_root,
            docker="/snap/bin/docker",
        )
    )

    async def run_limited(
        command: tuple[str, ...],
        invocation: SkillInvocation,
        **kwargs: object,
    ) -> SkillResult:
        assert command[command.index("--user") + 1] == f"{allowed_uid}:{allowed_gid}"
        assert ownership[invocation.writable_tmp_path] == (allowed_uid, allowed_gid)
        assert permissions.get(invocation.writable_tmp_path) == 0o700
        assert permissions.get(invocation.package_path) == 0o644
        with zipfile.ZipFile(invocation.package_path) as archive:
            assert "probe-write.txt" in archive.read("main.py").decode("utf-8")
        return SkillResult(exit_code=0, stdout='{"docker_probe":"ok"}', stderr="", timed_out=False)

    monkeypatch.setattr(
        "agent_hub.skills.sandbox.broker.run_subprocess_with_limits", run_limited
    )

    response = await broker.handle(BrokerRequest.probe(backend="docker"), peer_uid=allowed_uid)

    assert response.ok is True
    assert list(runtime_root.iterdir()) == []


def test_broker_policy_resolves_installed_agent_hub_uid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    modules = {
        "grp": SimpleNamespace(getgrnam=lambda name: SimpleNamespace(gr_name=name)),
        "pwd": SimpleNamespace(getpwnam=lambda name: SimpleNamespace(pw_uid=4242, pw_name=name)),
    }
    monkeypatch.setattr(
        "agent_hub.skills.sandbox.broker.importlib.import_module",
        lambda name: modules[name],
    )

    policy = load_broker_policy()

    assert policy.allowed_uid == 4242
