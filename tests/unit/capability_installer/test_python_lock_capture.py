from __future__ import annotations

import hashlib
import json
import os
import sys
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from subprocess import CompletedProcess
from uuid import UUID

import pytest

from agent_hub.capability_installer import environment
from agent_hub.capability_installer.environment import (
    CapabilityEnvironmentManager,
    EnvironmentBuildError,
    PythonEnvironmentSpec,
)
from agent_hub.capability_installer.python_attestation import canonical_json, read_control
from tests.unit.capability_installer.test_environment import RecordingRunner


def _raw_lock(*, crlf: bool = False, version: str = "1.2.3") -> bytes:
    raw = ("demo-package==" + version + " \\\n    --hash=sha256:" + "a" * 64 + "\n").encode()
    return raw.replace(b"\n", b"\r\n") if crlf else raw


def _spec(tmp_path: Path) -> PythonEnvironmentSpec:
    cache = tmp_path / "wheels"
    cache.mkdir()
    lock = tmp_path / "requirements.lock"
    lock.write_bytes(_raw_lock())
    return PythonEnvironmentSpec("demo", "1.0.0", lock, cache, "demo_package")


def test_pip_uses_captured_bytes_when_original_lock_changes_during_venv(
    tmp_path: Path,
) -> None:
    spec = _spec(tmp_path)
    original = spec.lock_file.read_bytes()
    installed: list[bytes] = []
    pip_paths: list[Path] = []

    class MutatingRunner(RecordingRunner):
        def __call__(self, argv: list[str], **kwargs: object) -> CompletedProcess[str]:
            result = super().__call__(argv, **kwargs)
            if argv[1:3] == ["-m", "venv"]:
                spec.lock_file.write_bytes(_raw_lock(version="9.9.9"))
            elif argv[1:4] == ["-m", "pip", "install"]:
                path = Path(argv[argv.index("-r") + 1])
                pip_paths.append(path)
                installed.append(path.read_bytes())
            return result

    runner = MutatingRunner()
    manager = CapabilityEnvironmentManager(tmp_path / "envs", runner=runner)
    record = manager.build_python(spec)

    assert installed == [original]
    assert pip_paths[0] != spec.lock_file
    assert pip_paths[0].parent == Path(str(runner.calls[1][1]["cwd"]))
    assert record.content_sha256 == hashlib.sha256(original).hexdigest()
    assert spec.lock_file.read_bytes() != original


def test_capture_preserves_raw_crlf_digest_and_is_immutable(tmp_path: Path) -> None:
    lock = tmp_path / "requirements.lock"
    raw = _raw_lock(crlf=True)
    lock.write_bytes(raw)

    captured = environment.capture_python_lock(lock)

    assert captured.raw_bytes == raw
    assert captured.sha256 == hashlib.sha256(raw).hexdigest()
    assert captured.sha256 != hashlib.sha256(raw.replace(b"\r\n", b"\n")).hexdigest()
    assert environment._validate_hashed_lock(lock) == captured.sha256
    with pytest.raises(FrozenInstanceError):
        captured.raw_bytes = b"changed"  # type: ignore[misc]


@pytest.mark.parametrize("expected", ["b" * 64, "g" * 64, "a" * 63, ""])
def test_expected_lock_digest_refuses_before_staging_or_commands(
    tmp_path: Path,
    expected: str,
) -> None:
    spec = _spec(tmp_path)
    runner = RecordingRunner()
    manager = CapabilityEnvironmentManager(tmp_path / "envs", runner=runner)
    before = sorted(path.relative_to(manager.root) for path in manager.root.rglob("*"))

    with pytest.raises(EnvironmentBuildError, match="SHA256|digest"):
        manager.build_python(replace(spec, lock_sha256=expected))

    assert runner.calls == []
    assert sorted(path.relative_to(manager.root) for path in manager.root.rglob("*")) == before


def test_crlf_lock_build_and_reuse_bind_raw_bytes_without_new_commands(tmp_path: Path) -> None:
    spec = _spec(tmp_path)
    raw = _raw_lock(crlf=True)
    spec.lock_file.write_bytes(raw)
    spec = replace(spec, lock_sha256=hashlib.sha256(raw).hexdigest())
    runner = RecordingRunner()
    manager = CapabilityEnvironmentManager(tmp_path / "envs", runner=runner)

    first = manager.build_python(spec)
    calls = len(runner.calls)
    reused = CapabilityEnvironmentManager(tmp_path / "envs", runner=runner).build_python(spec)

    assert first == reused == manager.active("demo")
    assert first.content_sha256 == spec.lock_sha256
    assert len(runner.calls) == calls
    assert list((manager.root / ".staging").iterdir()) == []


@pytest.mark.parametrize("kind", ["directory", "hardlink"])
def test_capture_rejects_nonregular_or_aliased_lock(tmp_path: Path, kind: str) -> None:
    lock = tmp_path / "requirements.lock"
    if kind == "directory":
        lock.mkdir()
    else:
        lock.write_bytes(_raw_lock())
        os.link(lock, tmp_path / "alias")

    with pytest.raises(EnvironmentBuildError, match="regular|lock|identity"):
        environment.capture_python_lock(lock)


def test_capture_rejects_file_replacement_between_stat_and_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agent_hub.capability_installer import python_attestation

    lock = tmp_path / "requirements.lock"
    lock.write_bytes(_raw_lock())
    replacement = tmp_path / "replacement"
    replacement.write_bytes(_raw_lock(version="9.9.9"))
    original_open = python_attestation._open

    def swap(path: Path, flags: int, parent_fd: int | None) -> int:
        if path.name == lock.name and replacement.exists():
            os.replace(replacement, lock)
        return original_open(path, flags, parent_fd)

    monkeypatch.setattr(python_attestation, "_open", swap)
    with pytest.raises(EnvironmentBuildError, match="regular|lock|identity"):
        environment.capture_python_lock(lock)


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink and FIFO identity checks")
@pytest.mark.parametrize("kind", ["symlink", "fifo"])
def test_capture_does_not_follow_or_block_on_special_locks(tmp_path: Path, kind: str) -> None:
    lock = tmp_path / "requirements.lock"
    if kind == "symlink":
        target = tmp_path / "target"
        target.write_bytes(_raw_lock())
        lock.symlink_to(target)
    elif sys.platform != "win32":
        os.mkfifo(lock)

    with pytest.raises(EnvironmentBuildError, match="regular|lock|identity"):
        environment.capture_python_lock(lock)


@pytest.mark.parametrize("marker", [None, "path-only-v0"])
def test_legacy_capture_metadata_refuses_reuse_even_with_valid_attestation(
    tmp_path: Path,
    marker: str | None,
) -> None:
    spec = _spec(tmp_path)
    runner = RecordingRunner()
    manager = CapabilityEnvironmentManager(tmp_path / "envs", runner=runner)
    record = manager.build_python(spec)
    metadata = record.path / "environment.json"
    payload = json.loads(metadata.read_bytes())
    if marker is None:
        payload.pop("lock_capture_format", None)
    else:
        payload["lock_capture_format"] = marker
    metadata.write_bytes(canonical_json(payload))
    _, receipt = read_control(metadata)
    state_path = manager.root / "demo/state.json"
    state = json.loads(state_path.read_bytes())
    state["python_attestations"]["1.0.0"]["metadata"] = receipt
    state_path.write_bytes(canonical_json(state))
    before, calls = state_path.read_bytes(), len(runner.calls)

    with pytest.raises(EnvironmentBuildError, match="explicit new version"):
        manager.build_python(spec)

    assert len(runner.calls) == calls
    assert state_path.read_bytes() == before
    assert list((manager.root / ".staging").iterdir()) == []


@pytest.mark.parametrize("phase", ["pip", "smoke"])
def test_failed_captured_lock_build_preserves_active_and_cleans_staging(
    tmp_path: Path,
    phase: str,
) -> None:
    spec = _spec(tmp_path)

    class FailingRunner(RecordingRunner):
        failing = False

        def __call__(self, argv: list[str], **kwargs: object) -> CompletedProcess[str]:
            if self.failing and (
                (phase == "pip" and argv[1:4] == ["-m", "pip", "install"])
                or (phase == "smoke" and argv[1:4] == ["-I", "-S", "-c"])
            ):
                raise RuntimeError("controlled build failure")
            return super().__call__(argv, **kwargs)

    runner = FailingRunner()
    manager = CapabilityEnvironmentManager(tmp_path / "envs", runner=runner)
    first = manager.build_python(spec)
    state_path = manager.root / "demo/state.json"
    before = state_path.read_bytes()
    runner.failing = True

    with pytest.raises(EnvironmentBuildError):
        manager.build_python(replace(spec, version="2.0.0"))

    assert manager.active("demo") == first
    assert state_path.read_bytes() == before
    assert list((manager.root / ".staging").iterdir()) == []
    assert not (manager.root / "demo/versions/2.0.0").exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX staging parent permissions")
def test_writable_staging_parent_refuses_before_build_commands(tmp_path: Path) -> None:
    spec = _spec(tmp_path)
    runner = RecordingRunner()
    manager = CapabilityEnvironmentManager(tmp_path / "envs", runner=runner)
    (manager.root / ".staging").chmod(0o777)
    try:
        with pytest.raises(EnvironmentBuildError, match="staging|ownership"):
            manager.build_python(spec)
        assert runner.calls == []
    finally:
        (manager.root / ".staging").chmod(0o700)


def test_existing_staging_collision_is_never_reused_or_removed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = _spec(tmp_path)
    runner = RecordingRunner()
    manager = CapabilityEnvironmentManager(tmp_path / "envs", runner=runner)
    identifier = UUID("00000000-0000-4000-8000-000000000001")
    staging = manager.root / ".staging" / identifier.hex
    if sys.platform == "win32":
        from agent_hub.plugins.handover_snapshots import _windows_security

        _windows_security(staging, private=True, create=True)
    else:
        staging.mkdir(mode=0o700)
    sentinel = staging / "previous-owner"
    sentinel.write_bytes(b"must remain")
    monkeypatch.setattr(environment, "uuid4", lambda: identifier)

    with pytest.raises(EnvironmentBuildError, match="staging"):
        manager.build_python(spec)

    assert sentinel.read_bytes() == b"must remain"
    assert sorted(path.name for path in staging.iterdir()) == ["previous-owner"]
    assert runner.calls == []
