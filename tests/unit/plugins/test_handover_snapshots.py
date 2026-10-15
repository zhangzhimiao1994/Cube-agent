"""Private snapshot byte persistence only; these tests do not prove a source grant."""

from __future__ import annotations

import errno
import hashlib
import importlib.util
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from typing import TYPE_CHECKING, cast
from uuid import UUID

import pytest

from agent_hub.plugins.handover_sources import FrozenSourceSnapshot, SnapshotFile, SourceLimits
from agent_hub.plugins.handover_storage import ArtifactSource, AuthorizedSource, WorkspaceSource
from tests.unit.plugins import test_handover_sources as freezer_tests

if TYPE_CHECKING:
    from agent_hub.auth.models import AuthenticatedPrincipal
    from agent_hub.files.workspace import ProjectWorkspaceStore
    from agent_hub.plugins.handover_snapshots import FrozenSourceSnapshotStore, SnapshotReference
    from agent_hub.plugins.handover_sources import HandoverSourceService
    from tests.unit.plugins.test_handover_sources import SourceRepository

TENANT = UUID("11111111-1111-4111-8111-111111111111")
CREATOR = UUID("22222222-2222-4222-8222-222222222222")
RUN = UUID("33333333-3333-4333-8333-333333333333")
MANIFEST = b'{ "id": "example", "version": "1.0.0", "permissions": ["network:http"] }\n'
CODE = b"raise RuntimeError('snapshot code must not execute')\n"

source_context = freezer_tests.context
forbid_source_actions = freezer_tests.forbid_execution_and_signing


def frozen() -> FrozenSourceSnapshot:
    files = tuple(
        SnapshotFile(path, content, len(content), hashlib.sha256(content).hexdigest())
        for path, content in (
            ("adapter.py", CODE),
            ("data/raw.bin", b"\x00\xff\x80"),
            ("plugin.json", MANIFEST),
        )
    )
    return FrozenSourceSnapshot(
        authorized=AuthorizedSource(
            tenant_id=TENANT,
            run_id=RUN,
            source=WorkspaceSource(
                project_id="project-a",
                session_id="session-a",
                paths=tuple(file.path for file in files),
            ),
            source_manifest_sha256="a" * 64,
        ),
        files=files,
        plugin_id="example",
        plugin_version="1.0.0",
        total_size_bytes=sum(file.size_bytes for file in files),
    )


def test_snapshot_store_preserves_exact_bytes_and_provenance(tmp_path: Path) -> None:
    assert importlib.util.find_spec("agent_hub.plugins.handover_snapshots") is not None
    from agent_hub.plugins.handover_snapshots import FrozenSourceSnapshotStore

    source = frozen()
    store = FrozenSourceSnapshotStore(package_root=tmp_path / "packages")
    reference = store.publish(source, creator_id=CREATOR)
    restored = store.read(
        tenant_id=TENANT,
        snapshot_id=reference.snapshot_id,
        blob_sha256=reference.blob_sha256,
        source_manifest_sha256=reference.source_manifest_sha256,
    )
    assert restored.snapshot == source
    assert restored.creator_id == CREATOR
    assert restored.reference == reference
    assert (
        next(file.content for file in restored.snapshot.files if file.path == "plugin.json")
        == MANIFEST
    )
    assert not hasattr(restored, "grant")


def read(store: FrozenSourceSnapshotStore, ref: SnapshotReference) -> FrozenSourceSnapshot:
    return store.read(
        tenant_id=TENANT,
        snapshot_id=ref.snapshot_id,
        blob_sha256=ref.blob_sha256,
        source_manifest_sha256=ref.source_manifest_sha256,
    ).snapshot


def object_path(root: Path, ref: SnapshotReference) -> Path:
    return root / ".handover-snapshots" / "v1" / TENANT.hex / ref.blob_sha256


def test_restart_and_dedup_preserve_shared_blob_on_caller_rollback(tmp_path: Path) -> None:
    from agent_hub.plugins.handover_snapshots import FrozenSourceSnapshotStore

    root = tmp_path / "packages"
    store = FrozenSourceSnapshotStore(package_root=root)
    ref = store.publish(frozen(), creator_id=CREATOR)
    before = object_path(root, ref).read_bytes()
    with pytest.raises(RuntimeError, match="caller rollback"):
        assert store.publish(frozen(), creator_id=CREATOR) == ref
        raise RuntimeError("caller rollback")
    restarted = FrozenSourceSnapshotStore(package_root=root)
    assert read(restarted, ref) == frozen()
    assert restarted.publish(frozen(), creator_id=CREATOR) == ref
    assert object_path(root, ref).read_bytes() == before
    assert object_path(root, ref).stat().st_nlink == 1
    assert not any(path.name.startswith("t-") for path in object_path(root, ref).parent.iterdir())
    with pytest.raises(FrozenInstanceError):
        ref.blob_sha256 = "b" * 64  # type: ignore[misc]


@pytest.mark.parametrize("changed", ["creator", "run", "tenant", "source_hash"])
def test_provenance_changes_blob_identity(tmp_path: Path, changed: str) -> None:
    from agent_hub.plugins.handover_snapshots import FrozenSourceSnapshotStore

    store = FrozenSourceSnapshotStore(package_root=tmp_path / "packages")
    original = frozen()
    first = store.publish(original, creator_id=CREATOR)
    creator = CREATOR
    payload = original.authorized.model_dump(mode="python")
    if changed == "creator":
        creator = RUN
    elif changed == "run":
        payload["run_id"] = CREATOR
    elif changed == "tenant":
        payload["tenant_id"] = RUN
    else:
        payload["source_manifest_sha256"] = "b" * 64
    next_snapshot = replace(original, authorized=AuthorizedSource.model_validate(payload))
    second = store.publish(next_snapshot, creator_id=creator)
    assert second.blob_sha256 != first.blob_sha256
    restored = store.read(
        tenant_id=next_snapshot.authorized.tenant_id,
        snapshot_id=second.snapshot_id,
        blob_sha256=second.blob_sha256,
        source_manifest_sha256=second.source_manifest_sha256,
    )
    assert restored.creator_id == creator and restored.snapshot == next_snapshot
    assert read(store, first) == original


def test_artifact_source_provenance_survives_restart(tmp_path: Path) -> None:
    from agent_hub.plugins.handover_snapshots import FrozenSourceSnapshotStore

    original = frozen()
    payload = original.authorized.model_dump(mode="python")
    payload["source"] = ArtifactSource(artifact_id=str(CREATOR))
    source = replace(original, authorized=AuthorizedSource.model_validate(payload))
    root = tmp_path / "packages"
    ref = FrozenSourceSnapshotStore(package_root=root).publish(source, creator_id=CREATOR)
    assert read(FrozenSourceSnapshotStore(package_root=root), ref) == source


@pytest.mark.parametrize(
    "bad_id",
    [
        "../x",
        "/private/snapshot",
        "C:/private",
        "hs-" + "a" * 64 + "/x",
        "hs-" + "A" * 64,
        "hs-" + "a" * 64 + "\n",
        "a" * 64,
    ],
)
def test_reads_reject_paths_and_nonopaque_identifiers(tmp_path: Path, bad_id: str) -> None:
    from agent_hub.plugins.handover_snapshots import FrozenSourceSnapshotStore, InvalidSnapshot

    store = FrozenSourceSnapshotStore(package_root=tmp_path / "packages")
    with pytest.raises(InvalidSnapshot):
        store.read(
            tenant_id=TENANT,
            snapshot_id=bad_id,
            blob_sha256="a" * 64,
            source_manifest_sha256="a" * 64,
        )
    assert not (tmp_path / "packages" / ".handover-snapshots").exists()


@pytest.mark.parametrize("changed", ["tenant", "blob_hash", "source_hash", "id"])
def test_read_rejects_changed_reference(tmp_path: Path, changed: str) -> None:
    from agent_hub.plugins.handover_snapshots import FrozenSourceSnapshotStore, InvalidSnapshot

    store = FrozenSourceSnapshotStore(package_root=tmp_path / "packages")
    ref = store.publish(frozen(), creator_id=CREATOR)
    kwargs: dict[str, object] = {
        "tenant_id": TENANT,
        "snapshot_id": ref.snapshot_id,
        "blob_sha256": ref.blob_sha256,
        "source_manifest_sha256": ref.source_manifest_sha256,
    }
    kwargs[
        {
            "tenant": "tenant_id",
            "blob_hash": "blob_sha256",
            "source_hash": "source_manifest_sha256",
            "id": "snapshot_id",
        }[changed]
    ] = RUN if changed == "tenant" else ("hs-" + "b" * 64 if changed == "id" else "b" * 64)
    with pytest.raises(InvalidSnapshot):
        store.read(
            tenant_id=cast(UUID, kwargs["tenant_id"]),
            snapshot_id=cast(str, kwargs["snapshot_id"]),
            blob_sha256=cast(str, kwargs["blob_sha256"]),
            source_manifest_sha256=cast(str, kwargs["source_manifest_sha256"]),
        )
    assert read(store, ref) == frozen()


@pytest.mark.parametrize(
    "bad_path",
    ["../adapter.py", "/adapter.py", "a//b", "a\\b", "a:b", "a%2fb", "CON", "a.", "a ", "x\x00y"],
)
def test_publish_rejects_unsafe_members(tmp_path: Path, bad_path: str) -> None:
    from agent_hub.plugins.handover_snapshots import FrozenSourceSnapshotStore, InvalidSnapshot

    original = frozen()
    files = (replace(original.files[0], path=bad_path), *original.files[1:])
    snapshot = replace(original, files=files)
    store = FrozenSourceSnapshotStore(package_root=tmp_path / "packages")
    with pytest.raises(InvalidSnapshot):
        store.publish(snapshot, creator_id=CREATOR)
    assert not (tmp_path / "packages" / ".handover-snapshots").exists()


@pytest.mark.parametrize(
    "changed",
    [
        "sha",
        "file_size",
        "total",
        "duplicate",
        "case_alias",
        "file_parent",
        "missing_manifest",
        "plugin_id",
        "version",
        "nonbytes",
        "source_selection",
        "forged_authorized",
        "bool_size",
    ],
)
def test_publish_revalidates_frozen_data(tmp_path: Path, changed: str) -> None:
    from agent_hub.plugins.handover_snapshots import FrozenSourceSnapshotStore, InvalidSnapshot

    snapshot = frozen()
    if changed in {"sha", "file_size", "nonbytes", "bool_size"}:
        file = snapshot.files[0]
        if changed == "sha":
            file = replace(file, sha256="b" * 64)
        elif changed == "file_size":
            file = replace(file, size_bytes=1)
        elif changed == "nonbytes":
            file = replace(file, content=cast(bytes, "unsafe"))
        else:
            file = replace(file, size_bytes=True)
        snapshot = replace(snapshot, files=(file, *snapshot.files[1:]))
    elif changed == "total":
        snapshot = replace(snapshot, total_size_bytes=1)
    elif changed in {"duplicate", "case_alias", "file_parent"}:
        file = replace(
            snapshot.files[0],
            path={"duplicate": "adapter.py", "case_alias": "ADAPTER.py", "file_parent": "data"}[
                changed
            ],
        )
        snapshot = replace(
            snapshot,
            files=(*snapshot.files, file),
            total_size_bytes=snapshot.total_size_bytes + file.size_bytes,
        )
    elif changed == "missing_manifest":
        snapshot = replace(snapshot, files=snapshot.files[:-1])
    elif changed == "plugin_id":
        snapshot = replace(snapshot, plugin_id="other")
    elif changed == "version":
        snapshot = replace(snapshot, plugin_version="2.0.0")
    elif changed == "source_selection":
        snapshot = replace(
            snapshot,
            authorized=snapshot.authorized.model_copy(
                update={
                    "source": WorkspaceSource(
                        project_id="project-a", session_id="session-a", paths=("plugin.json",)
                    )
                }
            ),
        )
    else:
        snapshot = replace(
            snapshot,
            authorized=snapshot.authorized.model_copy(
                update={"source_manifest_sha256": "not-a-hash"}
            ),
        )
    store = FrozenSourceSnapshotStore(package_root=tmp_path / "packages")
    with pytest.raises(InvalidSnapshot):
        store.publish(snapshot, creator_id=CREATOR)
    assert not (tmp_path / "packages" / ".handover-snapshots").exists()


@pytest.mark.parametrize("limit", ["files", "file_bytes", "total", "blob"])
def test_limits_precede_any_blob_publication(tmp_path: Path, limit: str) -> None:
    from agent_hub.plugins.handover_snapshots import (
        FrozenSourceSnapshotStore,
        SnapshotStoreLimits,
        SnapshotTooLarge,
    )

    source_limits = SourceLimits(
        **(
            {
                "files": {"max_files": 1},
                "file_bytes": {"max_file_bytes": 2},
                "total": {"max_total_bytes": 2},
                "blob": {},
            }[limit]
        )
    )
    store = FrozenSourceSnapshotStore(
        package_root=tmp_path / "packages",
        limits=SnapshotStoreLimits(
            source=source_limits, max_blob_bytes=2 if limit == "blob" else 3_000_000
        ),
    )
    with pytest.raises(SnapshotTooLarge):
        store.publish(frozen(), creator_id=CREATOR)
    assert not (tmp_path / "packages" / ".handover-snapshots").exists()


def test_object_quota_keeps_existing_dedup_and_shared_objects(tmp_path: Path) -> None:
    from agent_hub.plugins.handover_snapshots import (
        FrozenSourceSnapshotStore,
        SnapshotStoreLimits,
        SnapshotTooLarge,
    )

    store = FrozenSourceSnapshotStore(
        package_root=tmp_path / "packages", limits=SnapshotStoreLimits(max_snapshots_per_tenant=1)
    )
    ref = store.publish(frozen(), creator_id=CREATOR)
    assert store.publish(frozen(), creator_id=CREATOR) == ref
    with pytest.raises(SnapshotTooLarge):
        store.publish(frozen(), creator_id=RUN)
    assert read(store, ref) == frozen()


@pytest.mark.parametrize("operation", ["read", "dedup"])
def test_tamper_with_restored_mtime_is_not_cached(tmp_path: Path, operation: str) -> None:
    from agent_hub.plugins.handover_snapshots import FrozenSourceSnapshotStore, InvalidSnapshot

    root = tmp_path / "packages"
    store = FrozenSourceSnapshotStore(package_root=root)
    ref = store.publish(frozen(), creator_id=CREATOR)
    path = object_path(root, ref)
    before = path.stat()
    original = path.read_bytes()
    tampered = original.replace(b"example", b"changed")
    assert tampered != original and len(tampered) == len(original)
    path.write_bytes(tampered)
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    with pytest.raises(InvalidSnapshot):
        read(store, ref) if operation == "read" else store.publish(frozen(), creator_id=CREATOR)
    assert path.read_bytes() == tampered


def test_hardlink_alias_is_rejected_without_removing_shared_bytes(tmp_path: Path) -> None:
    from agent_hub.plugins.handover_snapshots import FrozenSourceSnapshotStore, InvalidSnapshot

    root = tmp_path / "packages"
    store = FrozenSourceSnapshotStore(package_root=root)
    ref = store.publish(frozen(), creator_id=CREATOR)
    path = object_path(root, ref)
    alias = tmp_path / "alias"
    os.link(path, alias)
    with pytest.raises(InvalidSnapshot):
        read(store, ref)
    assert alias.read_bytes() == path.read_bytes()


@pytest.mark.parametrize("component", ["root", "namespace", "tenant", "blob"])
def test_links_never_redirect_snapshot_storage(tmp_path: Path, component: str) -> None:
    from agent_hub.plugins.handover_snapshots import FrozenSourceSnapshotStore, InvalidSnapshot

    root = tmp_path / "packages"
    store = FrozenSourceSnapshotStore(package_root=root)
    ref = store.publish(frozen(), creator_id=CREATOR)
    blob = object_path(root, ref)
    path = {
        "root": root,
        "namespace": root / ".handover-snapshots",
        "tenant": blob.parent,
        "blob": blob,
    }[component]
    moved = tmp_path / "original"
    path.rename(moved)
    try:
        path.symlink_to(moved, target_is_directory=component != "blob")
    except OSError:
        moved.rename(path)
        pytest.skip("host does not grant symlink creation")
    with pytest.raises(InvalidSnapshot):
        FrozenSourceSnapshotStore(package_root=root).read(
            tenant_id=TENANT,
            snapshot_id=ref.snapshot_id,
            blob_sha256=ref.blob_sha256,
            source_manifest_sha256=ref.source_manifest_sha256,
        )
    assert moved.exists()


def test_replacing_configured_root_does_not_rebind_live_store(tmp_path: Path) -> None:
    from agent_hub.plugins.handover_snapshots import FrozenSourceSnapshotStore, InvalidSnapshot

    root = tmp_path / "packages"
    store = FrozenSourceSnapshotStore(package_root=root)
    ref = store.publish(frozen(), creator_id=CREATOR)
    root.rename(tmp_path / "original")
    root.mkdir(mode=0o700)
    with pytest.raises(InvalidSnapshot):
        store.publish(frozen(), creator_id=CREATOR)
    assert not list(root.iterdir())
    assert object_path(tmp_path / "original", ref).exists()


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX ownership/mode gate; Windows uses owner SID and private DACL",
)
@pytest.mark.parametrize("component", ["root", "namespace", "tenant", "blob"])
def test_insecure_posix_modes_are_not_repaired_silently(tmp_path: Path, component: str) -> None:
    from agent_hub.plugins.handover_snapshots import FrozenSourceSnapshotStore, InvalidSnapshot

    root = tmp_path / "packages"
    store = FrozenSourceSnapshotStore(package_root=root)
    ref = store.publish(frozen(), creator_id=CREATOR)
    path = {
        "root": root,
        "namespace": root / ".handover-snapshots",
        "tenant": object_path(root, ref).parent,
        "blob": object_path(root, ref),
    }[component]
    mode = path.stat().st_mode & 0o777
    path.chmod(0o777 if path.is_dir() else 0o644)
    try:
        with pytest.raises(InvalidSnapshot):
            read(store, ref)
    finally:
        path.chmod(mode)


def test_concurrent_creators_dedup_atomic_objects(tmp_path: Path) -> None:
    from agent_hub.plugins.handover_snapshots import FrozenSourceSnapshotStore

    root = tmp_path / "packages"

    def publish(_index: int) -> SnapshotReference:
        return FrozenSourceSnapshotStore(package_root=root).publish(frozen(), creator_id=CREATOR)

    with ThreadPoolExecutor(max_workers=8) as pool:
        refs = tuple(pool.map(publish, range(16)))
    assert len(set(refs)) == 1
    assert read(FrozenSourceSnapshotStore(package_root=root), refs[0]) == frozen()
    assert object_path(root, refs[0]).stat().st_nlink == 1


def test_empty_lock_inode_never_needs_a_write_before_acquiring_exclusive_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agent_hub.plugins.handover_snapshots import FrozenSourceSnapshotStore

    root = tmp_path / "packages"
    store = FrozenSourceSnapshotStore(package_root=root)
    write = os.write

    def forbid_lock_initialization(descriptor: int, data: bytes | memoryview) -> int:
        if bytes(data) == b"\0":
            raise PermissionError(13, "another backend already locked the initialization byte")
        return write(descriptor, data)

    monkeypatch.setattr(os, "write", forbid_lock_initialization)
    ref = store.publish(frozen(), creator_id=CREATOR)
    assert read(store, ref) == frozen()
    assert (object_path(root, ref).parent / "lock").stat().st_size == 0


@pytest.mark.parametrize("fault", ["unlock", "seek"])
@pytest.mark.parametrize("verification", ["descriptor", "second_opener"])
def test_unlock_failure_always_closes_descriptor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fault: str,
    verification: str,
) -> None:
    from agent_hub.plugins.handover_snapshots import (
        FrozenSourceSnapshotStore,
        SnapshotUnavailable,
        _native_path,
    )

    if fault == "seek" and sys.platform != "win32":
        pytest.skip("only Windows seeks before unlocking")
    store = FrozenSourceSnapshotStore(package_root=tmp_path / "packages")
    descriptors: list[int] = []
    releasing = False
    seek = os.lseek
    if sys.platform == "win32":
        import msvcrt

        lock, acquire, release = msvcrt.locking, msvcrt.LK_NBLCK, msvcrt.LK_UNLCK
        backend, attribute = msvcrt, "locking"
    else:
        import fcntl

        lock, acquire, release = fcntl.flock, fcntl.LOCK_EX | fcntl.LOCK_NB, fcntl.LOCK_UN
        backend, attribute = fcntl, "flock"

    def fail_unlock(descriptor: int, operation: int, *args: int) -> None:
        if operation == release:
            descriptors.append(descriptor)
            raise OSError(errno.EIO, "injected unlock failure")
        lock(descriptor, operation, *args)

    def fail_seek(descriptor: int, offset: int, whence: int) -> int:
        if releasing:
            descriptors.append(descriptor)
            raise OSError(errno.EIO, "injected unlock seek failure")
        return seek(descriptor, offset, whence)

    with monkeypatch.context() as interruption:
        if fault == "unlock":
            interruption.setattr(backend, attribute, fail_unlock)
        else:
            interruption.setattr(os, "lseek", fail_seek)
        with pytest.raises(SnapshotUnavailable), store._tenant(TENANT) as directory:
            lock_path = directory.path / "lock"
            releasing = True

    assert len(descriptors) == 1
    descriptor = descriptors[0]
    try:
        os.fstat(descriptor)
    except OSError as error:
        assert error.errno == errno.EBADF
        closed = True
    else:
        closed = False
    try:
        if verification == "descriptor":
            assert closed, "unlock failure leaked the lock descriptor"
        else:
            second = os.open(_native_path(lock_path), os.O_RDWR)
            try:
                seek(second, 0, os.SEEK_SET)
                if sys.platform == "win32":
                    lock(second, acquire, 1)
                else:
                    lock(second, acquire)
            finally:
                os.close(second)
    finally:
        if not closed:
            os.close(descriptor)


@pytest.mark.parametrize("fault", ["partial_write", "before_link", "after_link"])
def test_restart_recovers_only_owned_staging_and_keeps_shared_objects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fault: str,
) -> None:
    from agent_hub.plugins.handover_snapshots import FrozenSourceSnapshotStore, SnapshotUnavailable

    root = tmp_path / "packages"
    store = FrozenSourceSnapshotStore(package_root=root)
    shared = store.publish(frozen(), creator_id=RUN)
    expected = FrozenSourceSnapshotStore(package_root=tmp_path / "reference").publish(
        frozen(), creator_id=CREATOR
    )
    write, link, unlink = os.write, os.link, os.unlink
    writes = 0

    def fail_write(fd: int, data: bytes | memoryview) -> int:
        nonlocal writes
        writes += 1
        if writes == 1:
            return write(fd, data[:16])
        raise OSError("bounded partial write fault")

    def fail_link(
        src: str | Path,
        dst: str | Path,
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
        follow_symlinks: bool = True,
    ) -> None:
        raise OSError("bounded publish fault")

    def fail_unlink(path: str | Path, *, dir_fd: int | None = None) -> None:
        if Path(path).name.startswith("t-"):
            raise OSError("bounded post-publication fault")
        unlink(path, dir_fd=dir_fd)

    with monkeypatch.context() as interruption:
        interruption.setattr(
            os,
            {"partial_write": "write", "before_link": "link", "after_link": "unlink"}[fault],
            {"partial_write": fail_write, "before_link": fail_link, "after_link": fail_unlink}[
                fault
            ],
        )
        with pytest.raises(SnapshotUnavailable):
            store.publish(frozen(), creator_id=CREATOR)
    assert link is os.link and write is os.write
    restarted = FrozenSourceSnapshotStore(package_root=root)
    assert restarted.recover(tenant_id=TENANT) == 1
    assert read(restarted, shared) == frozen()
    if fault == "partial_write":
        assert not object_path(root, expected).exists()
        assert restarted.publish(frozen(), creator_id=CREATOR) == expected
    assert read(restarted, expected) == frozen()
    assert object_path(root, expected).stat().st_nlink == 1
    assert not any(
        path.name.startswith("t-") for path in object_path(root, expected).parent.iterdir()
    )


@pytest.mark.skipif(sys.platform != "win32", reason="Windows handle validation boundary")
def test_handle_escape_is_a_snapshot_error_without_private_detail(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agent_hub.plugins import handover_snapshots as snapshots
    from agent_hub.plugins.handover_sources import InvalidSource, _check_windows_handle

    root = tmp_path / "packages"
    store = snapshots.FrozenSourceSnapshotStore(package_root=root)
    ref = store.publish(frozen(), creator_id=CREATOR)
    check = _check_windows_handle

    def reject_handle(descriptor: int, path: Path) -> None:
        if path == object_path(root, ref):
            raise InvalidSource("PRIVATE_HANDLE_DETAIL")
        check(descriptor, path)

    monkeypatch.setattr(snapshots, "_check_windows_handle", reject_handle)
    with pytest.raises(snapshots.InvalidSnapshot) as error:
        read(store, ref)
    assert "PRIVATE_HANDLE_DETAIL" not in str(error.value)


def test_recovery_preserves_complete_staging_when_limits_shrink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent_hub.plugins.handover_snapshots import (
        FrozenSourceSnapshotStore,
        SnapshotStoreLimits,
        SnapshotTooLarge,
        SnapshotUnavailable,
    )

    root = tmp_path / "packages"
    store = FrozenSourceSnapshotStore(package_root=root)

    def interrupted_link(
        src: str | Path,
        dst: str | Path,
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
        follow_symlinks: bool = True,
    ) -> None:
        raise OSError("publication interrupted")

    with monkeypatch.context() as interruption:
        interruption.setattr(os, "link", interrupted_link)
        with pytest.raises(SnapshotUnavailable):
            store.publish(frozen(), creator_id=CREATOR)
    stages = tuple((root / ".handover-snapshots" / "v1" / TENANT.hex).glob("t-*"))
    assert len(stages) == 1
    original = stages[0].read_bytes()
    limited = FrozenSourceSnapshotStore(
        package_root=root, limits=SnapshotStoreLimits(source=SourceLimits(max_file_bytes=2))
    )
    with pytest.raises(SnapshotTooLarge):
        limited.recover(tenant_id=TENANT)
    assert stages[0].read_bytes() == original
    assert store.recover(tenant_id=TENANT) == 1


@pytest.mark.parametrize("tamper", ["tenant", "member", "schema"])
def test_recovery_does_not_delete_complete_untrusted_staging(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tamper: str,
) -> None:
    import json

    from agent_hub.plugins.handover_snapshots import (
        FrozenSourceSnapshotStore,
        InvalidSnapshot,
        SnapshotUnavailable,
    )

    root = tmp_path / "packages"
    store = FrozenSourceSnapshotStore(package_root=root)

    def interrupted_link(
        src: str | Path,
        dst: str | Path,
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
        follow_symlinks: bool = True,
    ) -> None:
        raise OSError("publication interrupted")

    with monkeypatch.context() as interruption:
        interruption.setattr(os, "link", interrupted_link)
        with pytest.raises(SnapshotUnavailable):
            store.publish(frozen(), creator_id=CREATOR)
    stages = tuple((root / ".handover-snapshots" / "v1" / TENANT.hex).glob("t-*"))
    assert len(stages) == 1
    data = stages[0].read_bytes()
    if tamper == "tenant":
        data = data.replace(str(TENANT).encode(), str(RUN).encode())
    elif tamper == "member":
        data = data.replace(b"example", b"changed")
    else:
        data = data.replace(b'"schema_version":1', b'"schema_version":2')
    json.loads(data)
    stages[0].write_bytes(data)
    with pytest.raises(InvalidSnapshot):
        store.recover(tenant_id=TENANT)
    assert stages[0].read_bytes() == data


def create_windows_junction(path: Path, target: Path) -> None:
    if sys.platform != "win32":
        raise RuntimeError("Windows junction fixture required")
    import ctypes
    import struct
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel.DeviceIoControl.argtypes = (
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        wintypes.LPVOID,
    )
    path.mkdir()
    handle = kernel.CreateFileW(str(path), 0x40000000, 7, None, 3, 0x02200000, None)
    if handle == wintypes.HANDLE(-1).value:
        raise OSError(ctypes.get_last_error(), "junction handle unavailable")
    substitute = ("\\??\\" + str(target)).encode("utf-16-le")
    printable = str(target).encode("utf-16-le")
    paths = substitute + b"\0\0" + printable + b"\0\0"
    data = (
        struct.pack(
            "<IHHHHHH",
            0xA0000003,
            len(paths) + 8,
            0,
            0,
            len(substitute),
            len(substitute) + 2,
            len(printable),
        )
        + paths
    )
    buffer = ctypes.create_string_buffer(data)
    returned = wintypes.DWORD()
    try:
        if not kernel.DeviceIoControl(
            handle, 0x900A4, buffer, len(data), None, 0, ctypes.byref(returned), None
        ):
            raise OSError(ctypes.get_last_error(), "junction fixture unavailable")
    finally:
        kernel.CloseHandle(handle)


@pytest.mark.skipif(sys.platform != "win32", reason="actual Windows reparse-point fixture")
@pytest.mark.parametrize("component", ["root", "namespace", "tenant"])
def test_actual_windows_junction_cannot_redirect_store(tmp_path: Path, component: str) -> None:
    from agent_hub.plugins.handover_snapshots import FrozenSourceSnapshotStore, InvalidSnapshot

    root = tmp_path / "packages"
    store = FrozenSourceSnapshotStore(package_root=root)
    ref = store.publish(frozen(), creator_id=CREATOR)
    path = {
        "root": root,
        "namespace": root / ".handover-snapshots",
        "tenant": object_path(root, ref).parent,
    }[component]
    moved = tmp_path / "original"
    path.rename(moved)
    create_windows_junction(path, moved)
    with pytest.raises(InvalidSnapshot):
        read(store, ref)
    assert moved.exists()
    path.rmdir()
    moved.rename(path)
    assert read(store, ref) == frozen()


@pytest.mark.skipif(sys.platform != "win32", reason="actual Windows ACL fixture")
@pytest.mark.parametrize("component", ["root", "namespace", "blob"])
def test_windows_null_dacl_is_rejected_without_repair(tmp_path: Path, component: str) -> None:
    if sys.platform != "win32":
        raise RuntimeError("Windows ACL fixture required")
    import ctypes
    from ctypes import wintypes

    from agent_hub.plugins.handover_snapshots import FrozenSourceSnapshotStore, InvalidSnapshot

    root = tmp_path / "packages"
    store = FrozenSourceSnapshotStore(package_root=root)
    ref = store.publish(frozen(), creator_id=CREATOR)
    path = {
        "root": root,
        "namespace": root / ".handover-snapshots",
        "blob": object_path(root, ref),
    }[component]
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.LocalFree.argtypes = (wintypes.LPVOID,)
    advapi.GetNamedSecurityInfoW.argtypes = (
        wintypes.LPWSTR,
        ctypes.c_int,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.LPVOID,
        ctypes.POINTER(wintypes.LPVOID),
        wintypes.LPVOID,
        ctypes.POINTER(wintypes.LPVOID),
    )
    advapi.SetNamedSecurityInfoW.argtypes = (
        wintypes.LPWSTR,
        ctypes.c_int,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.LPVOID,
        wintypes.LPVOID,
        wintypes.LPVOID,
    )
    dacl, descriptor = wintypes.LPVOID(), wintypes.LPVOID()
    assert (
        advapi.GetNamedSecurityInfoW(
            str(path), 1, 4, None, None, ctypes.byref(dacl), None, ctypes.byref(descriptor)
        )
        == 0
    )
    try:
        assert advapi.SetNamedSecurityInfoW(str(path), 1, 0x80000004, None, None, None, None) == 0
        with pytest.raises(InvalidSnapshot):
            read(store, ref)
    finally:
        assert advapi.SetNamedSecurityInfoW(str(path), 1, 0x80000004, None, None, dacl, None) == 0
        kernel.LocalFree(descriptor)
    assert read(store, ref) == frozen()


@pytest.mark.parametrize("point", ["after_fsync", "after_link"])
def test_publish_checks_actual_bytes_before_returning_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    point: str,
) -> None:
    from agent_hub.plugins.handover_snapshots import FrozenSourceSnapshotStore, InvalidSnapshot

    root = tmp_path / "packages"
    store = FrozenSourceSnapshotStore(package_root=root)
    shared = store.publish(frozen(), creator_id=RUN)
    sync, link = os.fsync, os.link
    tenant = object_path(root, shared).parent
    changed: list[Path] = []

    def corrupt_stage() -> None:
        stages = tuple(tenant.glob("t-*"))
        assert len(stages) == 1
        data = stages[0].read_bytes()
        corrupt = data.replace(b"example", b"changed")
        assert len(data) == len(corrupt) and data != corrupt
        stages[0].write_bytes(corrupt)
        changed.append(stages[0])

    def corrupt_after_sync(descriptor: int) -> None:
        sync(descriptor)
        if tuple(tenant.glob("t-*")) and not changed:
            corrupt_stage()

    def corrupt_after_link(
        src: str | Path,
        dst: str | Path,
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
        follow_symlinks: bool = True,
    ) -> None:
        link(
            src, dst, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd, follow_symlinks=follow_symlinks
        )
        corrupt_stage()

    with monkeypatch.context() as corruption:
        if point == "after_fsync":
            corruption.setattr(os, "fsync", corrupt_after_sync)
        else:
            corruption.setattr(os, "link", corrupt_after_link)
        with pytest.raises(InvalidSnapshot):
            store.publish(frozen(), creator_id=CREATOR)
    assert len(changed) == 1 and changed[0].exists()
    assert b"changed" in changed[0].read_bytes()
    # Uncertain/corrupt publication is retained, never rolled back over shared bytes.
    assert object_path(root, shared).exists()
    assert object_path(root, shared).read_bytes().find(b"changed") == -1


def test_recovery_checks_linked_bytes_and_preserves_uncertain_objects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agent_hub.plugins.handover_snapshots import (
        FrozenSourceSnapshotStore,
        InvalidSnapshot,
        SnapshotUnavailable,
    )

    root = tmp_path / "packages"
    store = FrozenSourceSnapshotStore(package_root=root)
    link = os.link

    def interrupt(
        src: str | Path,
        dst: str | Path,
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
        follow_symlinks: bool = True,
    ) -> None:
        raise OSError("bounded interruption")

    with monkeypatch.context() as fault:
        fault.setattr(os, "link", interrupt)
        with pytest.raises(SnapshotUnavailable):
            store.publish(frozen(), creator_id=CREATOR)
    tenant = root / ".handover-snapshots" / "v1" / TENANT.hex
    stages = tuple(tenant.glob("t-*"))
    assert len(stages) == 1
    original = stages[0].read_bytes()
    published = tenant / hashlib.sha256(original).hexdigest()

    def corrupt_link(
        src: str | Path,
        dst: str | Path,
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
        follow_symlinks: bool = True,
    ) -> None:
        link(
            src, dst, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd, follow_symlinks=follow_symlinks
        )
        stages[0].write_bytes(original.replace(b"example", b"changed"))

    with monkeypatch.context() as fault:
        fault.setattr(os, "link", corrupt_link)
        with pytest.raises(InvalidSnapshot):
            store.recover(tenant_id=TENANT)
    assert stages[0].exists() and published.exists()
    assert stages[0].stat().st_nlink == published.stat().st_nlink == 2
    assert stages[0].read_bytes() == published.read_bytes() != original


@pytest.mark.usefixtures("forbid_source_actions")
async def test_real_freezer_snapshot_is_persisted_without_mutating_workspace(
    tmp_path: Path,
    source_context: tuple[
        HandoverSourceService,
        SourceRepository,
        ProjectWorkspaceStore,
        AuthenticatedPrincipal,
    ],
) -> None:
    from agent_hub.plugins.handover_snapshots import FrozenSourceSnapshotStore

    service, repository, workspace, principal = source_context
    snapshot = await service.freeze(
        principal, repository.record.id, freezer_tests.workspace_source()
    )
    manifest = workspace.resolve_file(principal.tenant_id, "project-a", "session-a", "plugin.json")
    original = manifest.read_bytes()
    root = tmp_path / "packages"
    ref = FrozenSourceSnapshotStore(package_root=root).publish(
        snapshot, creator_id=principal.user_id
    )
    assert manifest.read_bytes() == original
    manifest.write_bytes(b"workspace changed after freezing")
    restored = FrozenSourceSnapshotStore(package_root=root).read(
        tenant_id=principal.tenant_id,
        snapshot_id=ref.snapshot_id,
        blob_sha256=ref.blob_sha256,
        source_manifest_sha256=ref.source_manifest_sha256,
    )
    assert restored.snapshot == snapshot and restored.creator_id == principal.user_id
    assert (
        next(file.content for file in restored.snapshot.files if file.path == "plugin.json")
        == original
    )
    assert manifest.read_bytes() == b"workspace changed after freezing"


def test_limits_are_revalidated_and_detached_from_configuration(tmp_path: Path) -> None:
    from agent_hub.plugins.handover_snapshots import (
        FrozenSourceSnapshotStore,
        SnapshotStoreLimits,
        SnapshotTooLarge,
    )

    source = SourceLimits(max_file_bytes=2)
    limits = SnapshotStoreLimits(source=source)
    store = FrozenSourceSnapshotStore(package_root=tmp_path / "packages", limits=limits)
    object.__setattr__(source, "max_file_bytes", 512_000)
    object.__setattr__(limits, "max_blob_bytes", 5_000_000)
    with pytest.raises(SnapshotTooLarge):
        store.publish(frozen(), creator_id=CREATOR)
    with pytest.raises(ValueError):
        FrozenSourceSnapshotStore(package_root=tmp_path / "unsafe", limits=limits)
    assert not (tmp_path / "unsafe").exists()


@pytest.mark.skipif(sys.platform != "win32", reason="actual Windows long-path API")
def test_default_temp_long_parent_does_not_shorten_or_escape_namespace(tmp_path: Path) -> None:
    from agent_hub.plugins.handover_snapshots import FrozenSourceSnapshotStore

    parent = tmp_path
    index = 0
    while len(str(parent / "packages")) <= 260:
        parent /= f"bounded-parent-{index:02d}"
        Path("\\\\?\\" + str(parent)).mkdir()
        index += 1
    root = parent / "packages"
    assert len(str(root)) > 260
    store = FrozenSourceSnapshotStore(package_root=root)
    ref = store.publish(frozen(), creator_id=CREATOR)
    assert read(FrozenSourceSnapshotStore(package_root=root), ref) == frozen()
    assert Path("\\\\?\\" + str(object_path(root, ref))).is_file()


def test_recovery_preserves_unknown_deeply_nested_staging_with_safe_error(tmp_path: Path) -> None:
    from agent_hub.plugins.handover_snapshots import FrozenSourceSnapshotStore, InvalidSnapshot

    root = tmp_path / "packages"
    store = FrozenSourceSnapshotStore(package_root=root)
    ref = store.publish(frozen(), creator_id=CREATOR)
    stage = object_path(root, ref).parent / ("t-" + "f" * 32)
    depth = max(sys.getrecursionlimit() + 500, 10_000)
    data = b"[" * depth + b"0" + b"]" * depth
    descriptor = os.open(stage, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        assert os.write(descriptor, data) == len(data)
    finally:
        os.close(descriptor)
    with pytest.raises(InvalidSnapshot):
        store.recover(tenant_id=TENANT)
    assert stage.read_bytes() == data
    assert object_path(root, ref).is_file()
