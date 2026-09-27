from __future__ import annotations

import hashlib
import json
import threading
import time
from pathlib import Path
from subprocess import CompletedProcess

import pytest

from agent_hub.capability_installer.environment import (
    CapabilityEnvironmentManager,
    CliArtifactSpec,
    EnvironmentBuildError,
    EnvironmentInUseError,
    EnvironmentQuotaExceeded,
    PythonEnvironmentSpec,
)


class RecordingRunner:
    def __init__(self, *, fail_smoke: bool = False) -> None:
        self.calls: list[tuple[tuple[str, ...], dict[str, object]]] = []
        self.fail_smoke = fail_smoke

    def __call__(self, argv: list[str], **kwargs: object) -> CompletedProcess[str]:
        args = tuple(argv)
        self.calls.append((args, dict(kwargs)))
        if args[1:3] == ("-m", "venv"):
            environment = Path(args[3])
            executable = environment / ("Scripts/python.exe" if _is_windows_layout() else "bin/python")
            executable.parent.mkdir(parents=True, exist_ok=True)
            executable.write_bytes(b"python")
        if self.fail_smoke and args[-1] in {"--version", "check"}:
            raise RuntimeError("smoke failed")
        return CompletedProcess(argv, 0, "ok", "")


def _is_windows_layout() -> bool:
    import os

    return os.name == "nt"


def _hashed_lock(path: Path) -> Path:
    path.write_text(
        "demo-package==1.2.3 \\\n    --hash=sha256:" + "a" * 64 + "\n",
        encoding="utf-8",
    )
    return path


def test_build_python_environment_uses_only_offline_hash_locked_wheels(tmp_path: Path) -> None:
    cache = tmp_path / "wheels"
    cache.mkdir()
    lock = _hashed_lock(tmp_path / "requirements.lock")
    runner = RecordingRunner()
    manager = CapabilityEnvironmentManager(tmp_path / "envs", runner=runner)

    record = manager.build_python(
        PythonEnvironmentSpec(
            capability_id="document-search",
            version="1.2.3",
            lock_file=lock,
            wheel_cache=cache,
            smoke_module="demo_package",
        )
    )

    assert record.version == "1.2.3"
    assert record.path.name == "1.2.3"
    assert manager.active("document-search") == record
    install_argv, install_options = runner.calls[1]
    assert install_argv[1:] == (
        "-m",
        "pip",
        "install",
        "--no-index",
        "--require-hashes",
        "--no-deps",
        "--find-links",
        str(cache.resolve()),
        "-r",
        str(lock.resolve()),
    )
    assert install_options["shell"] is False
    smoke_argv, smoke_options = runner.calls[2]
    assert smoke_argv[1:3] == ("-I", "-c")
    assert "demo_package" in smoke_argv[3]
    assert smoke_options["shell"] is False
    smoke_cwd = Path(str(smoke_options["cwd"]))
    assert smoke_cwd.parent == tmp_path / "envs" / ".staging"
    smoke_env = smoke_options["env"]
    assert isinstance(smoke_env, dict)
    assert smoke_env["PYTHONDONTWRITEBYTECODE"] == "1"
    assert Path(smoke_env["HOME"]).is_relative_to(smoke_cwd)


def test_python_environment_rejects_unhashed_or_remote_requirements(tmp_path: Path) -> None:
    cache = tmp_path / "wheels"
    cache.mkdir()
    runner = RecordingRunner()
    manager = CapabilityEnvironmentManager(tmp_path / "envs", runner=runner)
    lock = tmp_path / "requirements.lock"
    lock.write_text("demo-package>=1\nhttps://example.invalid/demo.whl\n", encoding="utf-8")

    with pytest.raises(EnvironmentBuildError, match="hash|fixed|remote"):
        manager.build_python(
            PythonEnvironmentSpec(
                capability_id="unsafe",
                version="1.0.0",
                lock_file=lock,
                wheel_cache=cache,
                smoke_module="demo_package",
            )
        )

    assert runner.calls == []


def test_python_environment_rejects_extra_pip_directives_in_lock(tmp_path: Path) -> None:
    cache = tmp_path / "wheels"
    cache.mkdir()
    runner = RecordingRunner()
    manager = CapabilityEnvironmentManager(tmp_path / "envs", runner=runner)
    lock = tmp_path / "requirements.lock"
    lock.write_text(
        "demo-package==1.2.3 --find-links ../untrusted "
        f"--hash=sha256:{'a' * 64}\n",
        encoding="utf-8",
    )

    with pytest.raises(EnvironmentBuildError, match="directive|lock"):
        manager.build_python(
            PythonEnvironmentSpec("unsafe", "1.0.0", lock, cache, "demo_package")
        )

    assert runner.calls == []


def test_cli_artifact_is_hash_verified_and_atomically_activated(tmp_path: Path) -> None:
    artifact = tmp_path / "trusted-cli"
    artifact.write_bytes(b"trusted executable")
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    runner = RecordingRunner()
    manager = CapabilityEnvironmentManager(tmp_path / "envs", runner=runner)

    record = manager.deploy_cli(
        CliArtifactSpec(
            capability_id="trusted-cli",
            version="2.0.0",
            artifact=artifact,
            sha256=digest,
            executable_name="trusted-cli.exe" if _is_windows_layout() else "trusted-cli",
        )
    )

    assert record.path.is_dir()
    assert (record.path / record.executable).read_bytes() == b"trusted executable"
    assert not list((tmp_path / "envs" / ".staging").iterdir())
    state = json.loads((tmp_path / "envs" / "trusted-cli" / "state.json").read_text("utf-8"))
    assert state["active_version"] == "2.0.0"
    argv, options = runner.calls[0]
    assert Path(argv[0]).parent.parent == tmp_path / "envs" / ".staging"
    assert Path(argv[0]).name == record.executable
    assert argv[1:] == ("--version",)
    assert options["shell"] is False
    assert options["cwd"] == Path(argv[0]).parent


def test_failed_build_cleans_staging_and_preserves_previous_active_version(tmp_path: Path) -> None:
    first = tmp_path / "cli-v1"
    first.write_bytes(b"v1")
    manager = CapabilityEnvironmentManager(tmp_path / "envs", runner=RecordingRunner())
    manager.deploy_cli(
        CliArtifactSpec("tool", "1.0.0", first, hashlib.sha256(b"v1").hexdigest(), "tool")
    )
    second = tmp_path / "cli-v2"
    second.write_bytes(b"v2")
    manager = CapabilityEnvironmentManager(
        tmp_path / "envs", runner=RecordingRunner(fail_smoke=True)
    )

    with pytest.raises(EnvironmentBuildError, match="smoke"):
        manager.deploy_cli(
            CliArtifactSpec("tool", "2.0.0", second, hashlib.sha256(b"v2").hexdigest(), "tool")
        )

    assert manager.active("tool").version == "1.0.0"
    assert not (tmp_path / "envs" / "tool" / "versions" / "2.0.0").exists()
    assert not list((tmp_path / "envs" / ".staging").iterdir())


def test_existing_cli_version_is_rejected_after_content_tampering(tmp_path: Path) -> None:
    artifact = tmp_path / "tool"
    artifact.write_bytes(b"trusted")
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    spec = CliArtifactSpec("tool", "1.0.0", artifact, digest, "tool")
    manager = CapabilityEnvironmentManager(tmp_path / "envs", runner=RecordingRunner())
    record = manager.deploy_cli(spec)
    (record.path / record.executable).write_bytes(b"tampered")

    with pytest.raises(EnvironmentBuildError, match="immutable|content|SHA256"):
        manager.deploy_cli(spec)


def test_rollback_restores_previous_version(tmp_path: Path) -> None:
    manager = CapabilityEnvironmentManager(tmp_path / "envs", runner=RecordingRunner())
    for version in ("1.0.0", "2.0.0"):
        artifact = tmp_path / f"cli-{version}"
        artifact.write_text(version, encoding="utf-8")
        manager.deploy_cli(
            CliArtifactSpec(
                "tool", version, artifact, hashlib.sha256(artifact.read_bytes()).hexdigest(), "tool"
            )
        )

    restored = manager.rollback("tool")

    assert restored.version == "1.0.0"
    assert manager.active("tool").version == "1.0.0"


def test_restore_active_can_restore_a_specific_version_or_clear_activation(tmp_path: Path) -> None:
    manager = CapabilityEnvironmentManager(tmp_path / "envs", runner=RecordingRunner())
    records = []
    for version in ("1.0.0", "2.0.0"):
        artifact = tmp_path / f"cli-{version}"
        artifact.write_text(version, encoding="utf-8")
        records.append(
            manager.deploy_cli(
                CliArtifactSpec(
                    "tool",
                    version,
                    artifact,
                    hashlib.sha256(artifact.read_bytes()).hexdigest(),
                    "tool",
                )
            )
        )

    assert manager.restore_active("tool", records[0].version) == records[0]
    manager.restore_active("tool", None)

    with pytest.raises(KeyError):
        manager.active("tool")


def test_concurrent_build_for_same_capability_is_serialized(tmp_path: Path) -> None:
    entered = threading.Event()
    release = threading.Event()

    class BlockingRunner(RecordingRunner):
        def __call__(self, argv: list[str], **kwargs: object) -> CompletedProcess[str]:
            if argv[-1] == "--version":
                entered.set()
                assert release.wait(3)
            return super().__call__(argv, **kwargs)

    artifact = tmp_path / "tool"
    artifact.write_bytes(b"tool")
    spec = CliArtifactSpec(
        "tool", "1.0.0", artifact, hashlib.sha256(b"tool").hexdigest(), "tool"
    )
    manager = CapabilityEnvironmentManager(tmp_path / "envs", runner=BlockingRunner())
    errors: list[BaseException] = []

    first = threading.Thread(target=lambda: _capture(errors, manager.deploy_cli, spec))
    first.start()
    assert entered.wait(2)
    second = threading.Thread(target=lambda: _capture(errors, manager.deploy_cli, spec))
    second.start()
    time.sleep(0.1)
    assert second.is_alive()
    release.set()
    first.join(3)
    second.join(3)

    assert not errors
    assert manager.active("tool").version == "1.0.0"


def _capture(errors: list[BaseException], function: object, *args: object) -> None:
    try:
        assert callable(function)
        function(*args)
    except Exception as exc:  # noqa: BLE001  # pragma: no cover - thread diagnostics
        errors.append(exc)


def test_quota_rejects_build_and_cleans_staging(tmp_path: Path) -> None:
    artifact = tmp_path / "large-cli"
    artifact.write_bytes(b"x" * 128)
    manager = CapabilityEnvironmentManager(
        tmp_path / "envs", quota_bytes=64, runner=RecordingRunner()
    )

    with pytest.raises(EnvironmentQuotaExceeded):
        manager.deploy_cli(
            CliArtifactSpec(
                "tool", "1.0.0", artifact, hashlib.sha256(artifact.read_bytes()).hexdigest(), "tool"
            )
        )

    assert not (tmp_path / "envs" / "tool" / "versions" / "1.0.0").exists()
    assert not list((tmp_path / "envs" / ".staging").iterdir())


def test_references_protect_versions_until_released(tmp_path: Path) -> None:
    manager = CapabilityEnvironmentManager(tmp_path / "envs", runner=RecordingRunner())
    for version in ("1.0.0", "2.0.0", "3.0.0"):
        artifact = tmp_path / f"cli-{version}"
        artifact.write_text(version, encoding="utf-8")
        manager.deploy_cli(
            CliArtifactSpec(
                "tool", version, artifact, hashlib.sha256(artifact.read_bytes()).hexdigest(), "tool"
            )
        )
    manager.add_reference("tool", "1.0.0", "run:123")

    removed = manager.cleanup("tool", keep_versions=1)

    assert removed == ("2.0.0",)
    assert manager.references("tool", "1.0.0") == ("run:123",)
    with pytest.raises(EnvironmentInUseError):
        manager.remove_version("tool", "1.0.0")
    manager.remove_reference("tool", "1.0.0", "run:123")
    manager.remove_version("tool", "1.0.0")
    assert manager.list_versions("tool") == ("3.0.0",)
