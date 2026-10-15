"""Installed Python bytes, membership and local receipts must stay bound."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from subprocess import CompletedProcess

import pytest

from agent_hub.capability_installer.environment import (
    CapabilityEnvironmentManager,
    EnvironmentBuildError,
    EnvironmentQuotaExceeded,
    EnvironmentRecord,
    PythonEnvironmentSpec,
)


class TreeRunner:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, argv: list[str], **kwargs: object) -> CompletedProcess[str]:
        self.calls += 1
        if argv[1:3] == ["-m", "venv"]:
            root = Path(argv[3])
            python = root / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
            python.parent.mkdir(parents=True)
            python.write_bytes(b"controlled-interpreter")
            site = root / "Lib/site-packages"
            site.mkdir(parents=True)
            (site / "demo.py").write_bytes(b"VALUE = 1\n")
            (root / "pyvenv.cfg").write_bytes(b"controlled-config\n")
        return CompletedProcess(argv, 0, "", "")


def test_manager_exposes_only_a_readonly_canonical_root(tmp_path: Path) -> None:
    root = tmp_path / "environments"
    manager = CapabilityEnvironmentManager(root)
    assert manager.root == root.resolve()
    with pytest.raises(AttributeError):
        manager.root = tmp_path / "foreign"  # type: ignore[misc]


def _installed(
    tmp_path: Path,
) -> tuple[CapabilityEnvironmentManager, PythonEnvironmentSpec, EnvironmentRecord, TreeRunner]:
    cache = tmp_path / "wheels"
    cache.mkdir()
    lock = tmp_path / "requirements.lock"
    lock.write_text("demo==1.0.0 --hash=sha256:" + "a" * 64 + "\n", encoding="ascii")
    runner = TreeRunner()
    manager = CapabilityEnvironmentManager(tmp_path / "envs", runner=runner)
    spec = PythonEnvironmentSpec("demo", "1.0.0", lock, cache, "demo")
    return manager, spec, manager.build_python(spec), runner


@pytest.mark.parametrize("operation", ["active", "reuse", "retain"])
@pytest.mark.parametrize(
    "mutation", ["modify", "add", "delete", "replace-file", "metadata", "manifest"]
)
def test_python_tree_tampering_refuses_without_state_or_command_changes(
    tmp_path: Path, operation: str, mutation: str
) -> None:
    _, spec, record, runner = _installed(tmp_path)
    source = record.path / "Lib/site-packages/demo.py"
    if mutation == "modify":
        source.write_bytes(b"VALUE = 2\n")
    elif mutation == "add":
        (source.parent / "injected.pth").write_bytes(b"import unknown_package\n")
    elif mutation == "delete":
        source.unlink()
    else:
        target = {
            "replace-file": source,
            "metadata": record.path / "environment.json",
            "manifest": record.path / "python-tree-attestation.json",
        }[mutation]
        replacement = tmp_path / "replacement"
        replacement.write_bytes(target.read_bytes() if target.exists() else b"{}")
        os.replace(replacement, target)
    state = tmp_path / "envs/demo/state.json"
    before, calls = state.read_bytes(), runner.calls
    restarted = CapabilityEnvironmentManager(tmp_path / "envs", runner=runner)
    with pytest.raises(EnvironmentBuildError, match="attestation|identity|tree"):
        if operation == "active":
            restarted.active("demo")
        elif operation == "reuse":
            restarted.build_python(spec)
        else:
            restarted.add_reference("demo", "1.0.0", "run:controlled")
    assert state.read_bytes() == before
    assert runner.calls == calls


@pytest.mark.parametrize("operation", ["active", "reuse", "retain"])
def test_legacy_python_requires_explicit_rebuild(tmp_path: Path, operation: str) -> None:
    manager, spec, record, _ = _installed(tmp_path)
    metadata = record.path / "environment.json"
    payload = json.loads(metadata.read_bytes())
    payload.pop("python_attestation", None)
    metadata.write_text(json.dumps(payload), encoding="ascii")
    state = tmp_path / "envs/demo/state.json"
    payload = json.loads(state.read_bytes())
    payload.pop("python_attestations", None)
    state.write_text(json.dumps(payload), encoding="ascii")
    with pytest.raises(EnvironmentBuildError, match="rebuild"):
        if operation == "active":
            manager.active("demo")
        elif operation == "reuse":
            manager.build_python(spec)
        else:
            manager.add_reference("demo", "1.0.0", "run:controlled")


def test_unchanged_python_reuses_persisted_attestation_without_execution(tmp_path: Path) -> None:
    _, spec, record, runner = _installed(tmp_path)
    calls = runner.calls
    restarted = CapabilityEnvironmentManager(tmp_path / "envs", runner=runner)
    assert restarted.active("demo") == record
    assert restarted.build_python(spec) == record
    restarted.add_reference("demo", "1.0.0", "run:controlled")
    assert restarted.references("demo", "1.0.0") == ("run:controlled",)
    assert runner.calls == calls


@pytest.mark.parametrize("attack", ["hardlink", "kind", "forged-manifest", "metadata-edit"])
def test_python_receipt_cannot_bless_changed_content(tmp_path: Path, attack: str) -> None:
    manager, _, record, _ = _installed(tmp_path)
    if attack == "hardlink":
        os.link(record.path / "Lib/site-packages/demo.py", tmp_path / "alias")
    elif attack == "forged-manifest":
        import hashlib

        manifest = record.path / "python-tree-attestation.json"
        manifest.write_bytes(b'{"schema_version":1,"entries":[]}')
        metadata = record.path / "environment.json"
        payload = json.loads(metadata.read_bytes())
        payload["python_attestation"]["sha256"] = hashlib.sha256(manifest.read_bytes()).hexdigest()
        metadata.write_text(json.dumps(payload), encoding="ascii")
    else:
        metadata = record.path / "environment.json"
        payload = json.loads(metadata.read_bytes())
        if attack == "kind":
            payload["kind"] = "cli"
        else:
            payload["ignored-field"] = "changed"
        metadata.write_text(json.dumps(payload), encoding="ascii")
    with pytest.raises(EnvironmentBuildError, match="attestation"):
        manager.active("demo")


@pytest.mark.parametrize("limit", ["bytes", "members", "depth", "path"])
def test_scanning_has_finite_limits(tmp_path: Path, limit: str) -> None:
    from agent_hub.capability_installer.python_attestation import (
        PythonAttestationLimitError,
        ScanLimits,
        scan_tree,
    )

    root = tmp_path / "tree"
    (root / "nested").mkdir(parents=True)
    (root / "nested/file").write_bytes(b"too-large")
    limits = {
        "bytes": ScanLimits(1),
        "members": ScanLimits(1024, max_entries=1),
        "depth": ScanLimits(1024, max_depth=1),
        "path": ScanLimits(1024, max_path_bytes=3),
    }[limit]
    with pytest.raises(PythonAttestationLimitError):
        scan_tree(root, interpreter=Path(sys.executable).resolve(), limits=limits)


def test_scan_rejects_replacement_between_lstat_and_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agent_hub.capability_installer import python_attestation as attestation

    root = tmp_path / "tree"
    root.mkdir()
    target = root / "module.py"
    target.write_bytes(b"trusted")
    replacement = tmp_path / "replacement"
    replacement.write_bytes(b"trusted")
    original = attestation._open

    def swap(path: Path, flags: int, parent_fd: int | None) -> int:
        if path.name == target.name and replacement.exists():
            os.replace(replacement, target)
        return original(path, flags, parent_fd)

    monkeypatch.setattr(attestation, "_open", swap)
    with pytest.raises(attestation.PythonAttestationError, match="identity|changed"):
        attestation.scan_tree(
            root, interpreter=Path(sys.executable).resolve(), limits=attestation.ScanLimits(1024)
        )


@pytest.mark.skipif(os.name == "nt", reason="requires actual POSIX interpreter symlinks")
def test_actual_posix_venv_links_survive_publication_without_package_execution(
    tmp_path: Path,
) -> None:
    cache = tmp_path / "wheels"
    cache.mkdir()
    lock = tmp_path / "requirements.lock"
    lock.write_text("demo==1.0.0 --hash=sha256:" + "a" * 64 + "\n", encoding="ascii")

    def real_venv_only(argv: list[str], **kwargs: object) -> CompletedProcess[str]:
        if argv[1:3] == ["-m", "venv"]:
            return subprocess.run(
                [*argv, "--without-pip"], check=True, capture_output=True, text=True, timeout=30
            )
        return CompletedProcess(argv, 0, "", "")

    manager = CapabilityEnvironmentManager(tmp_path / "envs", runner=real_venv_only)
    spec = PythonEnvironmentSpec("demo", "1.0.0", lock, cache, "sys")
    record = manager.build_python(spec)
    assert (record.path / "bin/python").is_symlink()
    result = subprocess.run(
        [str(record.path / record.executable), "-I", "-B", "-c", "import sys; print(sys.prefix)"],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.stdout.strip() == str(record.path)
    assert manager.active("demo") == record
    assert manager.build_python(spec) == record
    manager.add_reference("demo", "1.0.0", "run:controlled")


@pytest.mark.skipif(os.name == "nt", reason="POSIX no-follow directory descriptors")
def test_unapproved_symlink_is_rejected_without_following(tmp_path: Path) -> None:
    from agent_hub.capability_installer.python_attestation import (
        PythonAttestationError,
        ScanLimits,
        scan_tree,
    )

    root = tmp_path / "tree"
    root.mkdir()
    (root / "package").symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(PythonAttestationError, match="symlink"):
        scan_tree(root, interpreter=Path(sys.executable).resolve(), limits=ScanLimits(1024))


@pytest.mark.parametrize("changed", ["directory", "configuration", "startup"])
def test_every_tree_member_is_attested(tmp_path: Path, changed: str) -> None:
    manager, _, record, _ = _installed(tmp_path)
    if changed == "directory":
        (record.path / "empty-injection").mkdir()
    elif changed == "configuration":
        (record.path / "pyvenv.cfg").write_bytes(b"include-system-site-packages = true\n")
    else:
        (record.path / "Lib/site-packages/sitecustomize.py").write_bytes(b"raise RuntimeError()\n")
    with pytest.raises(EnvironmentBuildError, match="attestation"):
        manager.active("demo")


def test_python_quota_refuses_publication_and_cleans_only_owned_staging(tmp_path: Path) -> None:
    _, spec, _, runner = _installed(tmp_path)
    manager = CapabilityEnvironmentManager(tmp_path / "limited", runner=runner, quota_bytes=10)
    with pytest.raises(EnvironmentQuotaExceeded):
        manager.build_python(spec)
    assert list((tmp_path / "limited/.staging").iterdir()) == []
    assert not (tmp_path / "limited/demo/state.json").exists()


def test_storage_scanning_is_bounded(tmp_path: Path) -> None:
    from agent_hub.capability_installer.python_attestation import (
        PythonAttestationLimitError,
        ScanLimits,
        bounded_tree_size,
    )

    (tmp_path / "one").write_bytes(b"1234")
    (tmp_path / "two").write_bytes(b"5678")
    with pytest.raises(PythonAttestationLimitError):
        bounded_tree_size(tmp_path, limits=ScanLimits(7))


def test_scan_rechecks_earlier_files_before_returning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from typing import Any

    from agent_hub.capability_installer import python_attestation as attestation

    root = tmp_path / "tree"
    root.mkdir()
    first = root / "first.py"
    first.write_bytes(b"VALUE = 1\n")
    (root / "later.py").write_bytes(b"VALUE = 1\n")
    original = attestation._regular

    def mutate_after_read(
        path: Path,
        parent_fd: int | None,
        budget: attestation._Budget,
        *,
        capture: bool = False,
    ) -> tuple[dict[str, Any], bytes]:
        result = original(path, parent_fd, budget, capture=capture)
        if path.name == "later.py":
            first.write_bytes(b"VALUE = 2\n")
        return result

    monkeypatch.setattr(attestation, "_regular", mutate_after_read)
    with pytest.raises(attestation.PythonAttestationError, match="changed"):
        attestation.scan_tree(
            root, interpreter=Path(sys.executable).resolve(), limits=attestation.ScanLimits(1024)
        )


@pytest.mark.parametrize("operation", ["rollback", "restore"])
def test_previous_version_must_be_attested_before_restoration(
    tmp_path: Path, operation: str
) -> None:
    manager, spec, first, _ = _installed(tmp_path)
    manager.build_python(
        PythonEnvironmentSpec("demo", "2.0.0", spec.lock_file, spec.wheel_cache, "demo")
    )
    (first.path / "Lib/site-packages/demo.py").write_bytes(b"changed")
    state = tmp_path / "envs/demo/state.json"
    before = state.read_bytes()
    with pytest.raises(EnvironmentBuildError, match="attestation"):
        if operation == "rollback":
            manager.rollback("demo")
        else:
            manager.restore_active("demo", "1.0.0")
    assert state.read_bytes() == before


def test_manifest_excludes_its_controls_and_package_bodies(tmp_path: Path) -> None:
    _, _, record, _ = _installed(tmp_path)
    manifest = (record.path / "python-tree-attestation.json").read_bytes()
    entries = json.loads(manifest)["entries"]
    names = {entry["path"] for entry in entries}
    assert "environment.json" not in names
    assert "python-tree-attestation.json" not in names
    assert "Lib/site-packages/demo.py" in names
    assert b"VALUE = 1" not in manifest
    assert b"controlled-interpreter" not in manifest
    assert record.attestation_sha256 is not None


def test_unicode_member_names_have_the_same_persisted_manifest_digest(tmp_path: Path) -> None:
    _, spec, _, _ = _installed(tmp_path)

    class UnicodeRunner(TreeRunner):
        def __call__(self, argv: list[str], **kwargs: object) -> CompletedProcess[str]:
            result = super().__call__(argv, **kwargs)
            if argv[1:3] == ["-m", "venv"]:
                (Path(argv[3]) / "Lib/site-packages/module_\u4e2d.py").write_bytes(b"VALUE = 1\n")
            return result

    manager = CapabilityEnvironmentManager(tmp_path / "unicode", runner=UnicodeRunner())
    record = manager.build_python(spec)
    assert manager.active("demo") == record


def test_publication_cannot_reanchor_metadata_replaced_after_rename(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, spec, _, runner = _installed(tmp_path)
    manager = CapabilityEnvironmentManager(tmp_path / "race", runner=runner)
    original = Path.replace

    def replace(path: Path, target: Path | str) -> Path:
        result = original(path, target)
        if path.parent.name == ".staging":
            metadata = Path(target) / "environment.json"
            payload = json.loads(metadata.read_bytes())
            payload["injected-field"] = "replaced"
            metadata.write_text(json.dumps(payload), encoding="ascii")
        return result

    monkeypatch.setattr(Path, "replace", replace)
    with pytest.raises(EnvironmentBuildError, match="attestation"):
        manager.build_python(spec)
    assert not (tmp_path / "race/demo/state.json").exists()


def test_publication_reserves_bytes_for_persisted_state_references(tmp_path: Path) -> None:
    _, spec, record, runner = _installed(tmp_path)
    installed_bytes = sum(path.stat().st_size for path in record.path.rglob("*") if path.is_file())
    manager = CapabilityEnvironmentManager(
        tmp_path / "limited",
        runner=runner,
        quota_bytes=installed_bytes + 128,
    )
    with pytest.raises(EnvironmentQuotaExceeded):
        manager.build_python(spec)
    assert not (tmp_path / "limited/demo/state.json").exists()
    assert list((tmp_path / "limited/.staging").iterdir()) == []


def test_state_reference_limit_refuses_before_overwriting_existing_state(tmp_path: Path) -> None:
    manager, spec, _, _ = _installed(tmp_path)
    state = tmp_path / "envs/demo/state.json"
    payload = json.loads(state.read_bytes())
    payload["padding"] = "x" * (1024 * 1024 - len(state.read_bytes()) - 200)
    state.write_text(json.dumps(payload), encoding="ascii")
    before = state.read_bytes()
    with pytest.raises(EnvironmentBuildError):
        manager.build_python(
            PythonEnvironmentSpec(
                "demo",
                "2.0.0",
                spec.lock_file,
                spec.wheel_cache,
                "demo",
            )
        )
    assert state.read_bytes() == before
    assert manager.active("demo").version == "1.0.0"


@pytest.mark.parametrize("mode", ["default", "flag", "environment", "isolated-environment"])
def test_real_venv_runtime_bytecode_policy_keeps_attestation_usable(
    tmp_path: Path,
    mode: str,
) -> None:
    _, spec, _, _ = _installed(tmp_path)
    site_relative = (
        Path("Lib/site-packages")
        if os.name == "nt"
        else Path(f"lib/python{sys.version_info.major}.{sys.version_info.minor}/site-packages")
    )

    def fixture_runner(argv: list[str], **kwargs: object) -> CompletedProcess[str]:
        if argv[1:3] == ["-m", "venv"]:
            result = subprocess.run(
                [*argv, "--without-pip"], check=True, capture_output=True, text=True, timeout=30
            )
            site = Path(argv[3]) / site_relative
            site.mkdir(parents=True, exist_ok=True)
            (site / "attestation_runtime_fixture.py").write_bytes(b"VALUE = 1\n")
            return result
        return CompletedProcess(argv, 0, "", "")

    manager = CapabilityEnvironmentManager(tmp_path / "runtime", runner=fixture_runner)
    record = manager.build_python(spec)
    site = record.path / site_relative
    flags = ["-S"] if mode == "environment" else ["-I", "-S"]
    if mode == "flag":
        flags.append("-B")
    environment = {key: os.environ[key] for key in ("SystemRoot", "WINDIR") if key in os.environ}
    if mode in {"environment", "isolated-environment"}:
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
    script = (
        f"import sys; sys.path.insert(0, {str(site)!r}); "
        "import attestation_runtime_fixture; assert attestation_runtime_fixture.VALUE == 1"
    )
    subprocess.run(
        [str(record.path / record.executable), *flags, "-c", script],
        env=environment,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    bytecode = list(site.rglob("attestation_runtime_fixture*.pyc"))
    if mode in {"default", "isolated-environment"}:
        assert bytecode
        with pytest.raises(EnvironmentBuildError, match="attestation"):
            manager.active("demo")
        with pytest.raises(EnvironmentBuildError, match="attestation"):
            manager.add_reference("demo", "1.0.0", "run:controlled")
    else:
        assert not bytecode
        assert manager.active("demo") == record
        assert manager.build_python(spec) == record
        manager.add_reference("demo", "1.0.0", "run:controlled")
