"""Shared writer locks; native platform evidence is separate from fault injection."""

import asyncio
import io
import json
import os
import subprocess
import sys
import zipfile
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Literal
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from cryptography.hazmat.primitives.asymmetric import ed25519
from starlette.requests import Request
from starlette.types import Message

from agent_hub.api.errors import PublicAPIError
from agent_hub.api.routers import admin
from agent_hub.auth.models import AuthenticatedPrincipal, Role
from agent_hub.plugins import handover_confirmation as confirmation
from agent_hub.plugins.handover_storage import Conflict
from agent_hub.settings import Settings
from agent_hub.skills.package import InvalidSkillPackage
from tests.api import test_admin_resources as admin_tests
from tests.unit.plugins.test_handover_admin_writes import AdminContext, plugin_payload
from tests.unit.plugins.test_handover_admin_writes import context as context  # noqa: PLC0414
from tests.unit.plugins.test_handover_stage_journal import private_directory

TENANT = UUID("11111111-1111-4111-8111-111111111111")
type Interface = Literal["admin", "confirmation"]


def request_for(root: Path) -> Request:
    return Request({
        "type": "http",
        "query_string": b"plugin_package_store_dir=/client-controlled-root",
        "app": SimpleNamespace(state=SimpleNamespace(
            settings=SimpleNamespace(plugin_package_store_dir=root),
        )),
    })


def lock_for(interface: Interface, root: Path) -> AbstractContextManager[None]:
    if interface == "admin":
        return admin._plugin_package_uninstall_stage_lock(
            request_for(root), tenant_id=TENANT, plugin_id="example",
        )
    return confirmation._shared_fs_lock(request_for(root), TENANT, "example")


def lock_leaf(root: Path) -> Path:
    return root / ".uninstall-locks" / str(TENANT) / "example.lock"


def file_identity(path: Path) -> tuple[int, ...]:
    info = path.lstat()
    return (
        info.st_dev, info.st_ino, info.st_mode, info.st_size,
        info.st_mtime_ns, info.st_ctime_ns, info.st_nlink,
    )


def test_admin_rejects_real_hardlink_without_touching_owned_victim(tmp_path: Path) -> None:
    root = tmp_path / "packages"
    lock = root / ".uninstall-locks" / str(TENANT) / "example.lock"
    lock.parent.mkdir(parents=True)
    victim = tmp_path / "owned-victim.bin"
    victim.write_bytes(b"\0")
    os.link(victim, lock)
    before = file_identity(victim)
    entered = False
    rejected = False
    try:
        with admin._plugin_package_stage_lock_at_root(
            root, tenant_id=TENANT, plugin_id="example",
        ):
            entered = True
    except OSError:
        rejected = True
    assert victim.read_bytes() == b"\0"
    assert file_identity(victim) == before
    assert rejected and not entered, "admin accepted a hardlinked stage lock"


@pytest.mark.parametrize("interface", ["admin", "confirmation"])
def test_empty_hardlink_never_initializes_victim(tmp_path: Path, interface: Interface) -> None:
    root = tmp_path / "packages"
    leaf = lock_leaf(root)
    leaf.parent.mkdir(parents=True)
    victim = tmp_path / "empty-owned-victim"
    victim.touch()
    os.link(victim, leaf)
    before = file_identity(victim)
    error = OSError if interface == "admin" else Conflict
    try:
        with pytest.raises(error), lock_for(interface, root):
            pytest.fail("hardlink reached the writer")
    finally:
        assert victim.read_bytes() == b""
        assert file_identity(victim) == before


def test_both_interfaces_open_same_native_inode_from_server_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "packages"
    opened: list[tuple[int, int]] = []
    original = confirmation._PinnedDirectory.open

    def observe(directory: confirmation._PinnedDirectory, name: str, flags: int) -> int:
        fd = original(directory, name, flags)
        info = os.fstat(fd)
        opened.append((info.st_dev, info.st_ino))
        return fd

    monkeypatch.setattr(confirmation._PinnedDirectory, "open", observe)
    for interface in ("admin", "confirmation"):
        with lock_for(interface, root):
            pass
        assert lock_leaf(root).read_bytes() == b"\0"
    info = lock_leaf(root).lstat()
    assert info.st_nlink == 1
    assert opened == [(info.st_dev, info.st_ino)] * 2


@pytest.mark.parametrize("interface", ["admin", "confirmation"])
def test_hardlink_added_during_native_acquire_never_initializes_victim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, interface: Interface,
) -> None:
    root = tmp_path / "packages"
    leaf = lock_leaf(root)
    victim = tmp_path / "race-victim"
    identities: list[tuple[int, ...]] = []

    def add_link() -> None:
        os.link(leaf, victim)
        identities.append(file_identity(victim))

    if sys.platform == "win32":
        import msvcrt

        locking = msvcrt.locking

        def link_after_lock(fd: int, mode: int, size: int) -> None:
            locking(fd, mode, size)
            if mode == msvcrt.LK_NBLCK:
                add_link()

        monkeypatch.setattr(msvcrt, "locking", link_after_lock)
    else:
        import fcntl

        flock = fcntl.flock

        def link_after_flock(fd: int, operation: int) -> None:
            flock(fd, operation)
            if operation == fcntl.LOCK_EX | fcntl.LOCK_NB:
                add_link()

        monkeypatch.setattr(fcntl, "flock", link_after_flock)
    entered = False
    try:
        with pytest.raises(OSError if interface == "admin" else Conflict), lock_for(interface, root):
            entered = True
    finally:
        assert identities and victim.read_bytes() == b""
        assert file_identity(victim) == identities[0]
    assert not entered


@pytest.mark.parametrize("interface", ["admin", "confirmation"])
def test_hardlink_added_by_writer_is_rejected_on_exit(
    tmp_path: Path, interface: Interface,
) -> None:
    root = tmp_path / "packages"
    victim = tmp_path / "writer-victim"
    before: tuple[int, ...] | None = None
    try:
        with pytest.raises(OSError if interface == "admin" else Conflict), lock_for(interface, root):
            os.link(lock_leaf(root), victim)
            before = file_identity(victim)
    finally:
        assert before is not None and victim.read_bytes() == b"\0"
        assert file_identity(victim) == before


CHILD_LOCK = """
import json, os, sys, time
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID
from starlette.requests import Request
from agent_hub.api.routers import admin
from agent_hub.plugins.handover_confirmation import _shared_fs_lock
from agent_hub.plugins.handover_storage import Conflict
root, interface, tenant = Path(sys.argv[1]), sys.argv[2], UUID(sys.argv[3])
request = Request({'type': 'http', 'app': SimpleNamespace(state=SimpleNamespace(
    settings=SimpleNamespace(plugin_package_store_dir=root)))})
lock = root / '.uninstall-locks' / str(tenant) / 'example.lock'
info = lock.lstat()
started = time.monotonic()
try:
    context = (admin._plugin_package_uninstall_stage_lock(
        request, tenant_id=tenant, plugin_id='example') if interface == 'admin'
        else _shared_fs_lock(request, tenant, 'example'))
    with context:
        outcome = 'entered'
except (OSError, Conflict) as error:
    outcome = type(error).__name__
print(json.dumps({'outcome': outcome, 'elapsed': time.monotonic() - started,
                  'node': [info.st_dev, info.st_ino]}), flush=True)
"""


@pytest.mark.parametrize("holder,waiter", [("admin", "confirmation"), ("confirmation", "admin")])
async def test_native_cross_process_lock_has_bounded_await_and_same_inode(
    tmp_path: Path, holder: Interface, waiter: Interface,
) -> None:
    root = tmp_path / "packages"
    with lock_for(holder, root):
        info = lock_leaf(root).lstat()
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-c", CHILD_LOCK, str(root), waiter, str(TENANT),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=15)
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()
        assert process.returncode == 0, stderr.decode()
        result = json.loads(stdout)
        assert result["outcome"] == ("OSError" if waiter == "admin" else "Conflict")
        assert result["node"] == [info.st_dev, info.st_ino]
        assert 4.8 <= result["elapsed"] < 7.5
    with lock_for(waiter, root):
        assert (lock_leaf(root).stat().st_dev, lock_leaf(root).stat().st_ino) == (
            info.st_dev, info.st_ino,
        )


@pytest.mark.parametrize("interface", ["admin", "confirmation"])
def test_native_unlock_exception_closes_descriptor_and_other_interface_reopens(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, interface: Interface,
) -> None:
    root = tmp_path / "packages"
    descriptors: list[int] = []
    original = confirmation._PinnedDirectory.open

    def observe(directory: confirmation._PinnedDirectory, name: str, flags: int) -> int:
        fd = original(directory, name, flags)
        descriptors.append(fd)
        return fd

    with monkeypatch.context() as patch:
        patch.setattr(confirmation._PinnedDirectory, "open", observe)
        if sys.platform == "win32":
            import msvcrt

            locking = msvcrt.locking

            def fail_unlock(fd: int, mode: int, size: int) -> None:
                if mode == msvcrt.LK_UNLCK:
                    raise OSError("injected unlock failure")
                locking(fd, mode, size)

            patch.setattr(msvcrt, "locking", fail_unlock)
        else:
            import fcntl

            flock = fcntl.flock

            def fail_flock_unlock(fd: int, operation: int) -> None:
                if operation == fcntl.LOCK_UN:
                    raise OSError("injected unlock failure")
                flock(fd, operation)

            patch.setattr(fcntl, "flock", fail_flock_unlock)
        with pytest.raises(OSError if interface == "admin" else Conflict), lock_for(interface, root):
            pass
    assert len(descriptors) == 1
    with pytest.raises(OSError):
        os.fstat(descriptors[0])
    with lock_for("confirmation" if interface == "admin" else "admin", root):
        pass


@pytest.mark.parametrize("plugin_id", ["", "../escape", "a/b", "a\\b", ".", "..", "Upper", "a" * 129])
def test_admin_invalid_plugin_id_is_422_before_filesystem_mutation(
    tmp_path: Path, plugin_id: str,
) -> None:
    root = tmp_path / "packages"
    with (
        pytest.raises(PublicAPIError) as caught,
        admin._plugin_package_stage_lock_at_root(root, tenant_id=TENANT, plugin_id=plugin_id),
    ):
        pytest.fail("invalid plugin ID entered")
    assert caught.value.status_code == 422
    assert not root.exists()


def symlink_or_skip(link: Path, target: Path, *, directory: bool = False) -> None:
    try:
        link.symlink_to(target, target_is_directory=directory)
    except OSError:
        if directory and sys.platform == "win32":
            result = subprocess.run(
                ["cmd.exe", "/c", "mklink", "/J", str(link), str(target)],
                capture_output=True, timeout=10, check=False,
            )
            if result.returncode == 0:
                return
        pytest.skip("native symlinks unavailable for this account")


def test_admin_preserves_path_containment_check(tmp_path: Path) -> None:
    root = tmp_path / "packages"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    symlink_or_skip(root / ".uninstall-locks", outside, directory=True)
    with (
        pytest.raises(InvalidSkillPackage),
        admin._plugin_package_stage_lock_at_root(root, tenant_id=TENANT, plugin_id="example"),
    ):
        pytest.fail("escaped root entered")
    assert list(outside.iterdir()) == []


def test_admin_rejects_directory_alias_even_within_trusted_root(tmp_path: Path) -> None:
    root = tmp_path / "packages"
    target = root / "alias-target"
    target.mkdir(parents=True)
    symlink_or_skip(root / ".uninstall-locks", target, directory=True)
    with pytest.raises(OSError), lock_for("admin", root):
        pytest.fail("directory alias entered")
    assert list(target.iterdir()) == []


def test_admin_rejects_lock_leaf_symlink_without_touching_victim(tmp_path: Path) -> None:
    root = tmp_path / "packages"
    leaf = lock_leaf(root)
    leaf.parent.mkdir(parents=True)
    victim = leaf.parent / "owned-victim"
    victim.write_bytes(b"\0")
    symlink_or_skip(leaf, victim)
    before = file_identity(victim)
    with pytest.raises(OSError), lock_for("admin", root):
        pytest.fail("symlink lock entered")
    assert victim.read_bytes() == b"\0" and file_identity(victim) == before


def test_admin_rejects_leaf_rebind_before_open(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "packages"
    leaf = lock_leaf(root)
    leaf.parent.mkdir(parents=True)
    leaf.write_bytes(b"\0")
    original = confirmation._PinnedDirectory._native_open
    reached = False

    def rebind(directory: confirmation._PinnedDirectory, name: str, flags: int) -> int:
        nonlocal reached
        reached = True
        leaf.rename(leaf.with_suffix(".original"))
        leaf.write_bytes(b"\0")
        return original(directory, name, flags)

    monkeypatch.setattr(confirmation._PinnedDirectory, "_native_open", rebind)
    with pytest.raises(OSError), lock_for("admin", root):
        pytest.fail("rebound lock entered")
    assert reached
    assert leaf.with_suffix(".original").read_bytes() == b"\0"


def test_admin_rejects_injected_root_identity_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "packages"
    original = confirmation._PinnedDirectory.check

    def changed(directory: confirmation._PinnedDirectory) -> None:
        path, node = directory.nodes[-1]
        directory.nodes[-1] = (path, (node[0], node[1] + 1, node[2]))
        original(directory)

    monkeypatch.setattr(confirmation._PinnedDirectory, "check", changed)
    with pytest.raises(OSError), lock_for("admin", root):
        pytest.fail("changed directory identity entered")
    assert not lock_leaf(root).exists()
    monkeypatch.setattr(confirmation._PinnedDirectory, "check", original)
    with lock_for("confirmation", root):
        pass


@pytest.mark.skipif(sys.platform != "win32", reason="requires real Windows ancestor handles")
def test_windows_native_ancestor_handles_pin_root_until_lock_closes(tmp_path: Path) -> None:
    root = tmp_path / "packages"
    moved = tmp_path / "moved"
    with lock_for("admin", root):
        before = root.stat()
        with pytest.raises(OSError):
            root.rename(moved)
        assert root.stat().st_ino == before.st_ino and not moved.exists()
    root.rename(moved)
    assert moved.stat().st_ino == before.st_ino


@pytest.mark.skipif(sys.platform == "win32", reason="requires native POSIX root rebind")
def test_posix_native_root_rebind_is_rejected_on_exit(tmp_path: Path) -> None:
    root = tmp_path / "packages"
    moved = tmp_path / "moved"
    with pytest.raises(OSError), lock_for("admin", root):
        root.rename(moved)
        replacement = lock_leaf(root)
        replacement.parent.mkdir(parents=True)
        replacement.write_bytes(b"\0")
    assert lock_leaf(moved).read_bytes() == b"\0"
    with lock_for("confirmation", root):
        pass


def test_linux_fake_native_open_uses_nofollow_nonblock_and_dirfd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, int, int, int]] = []

    def fake_open(name: str, flags: int, mode: int, *, dir_fd: int) -> int:
        calls.append((name, flags, mode, dir_fd))
        return 777

    fake_os = SimpleNamespace(**{name: getattr(os, name) for name in dir(os)})
    fake_os.open = fake_open
    fake_os.O_NOFOLLOW, fake_os.O_NONBLOCK, fake_os.O_CLOEXEC = 0x10000, 0x20000, 0x40000
    monkeypatch.setattr(confirmation, "os", fake_os)
    monkeypatch.setattr(confirmation, "sys", SimpleNamespace(platform="linux"))
    directory = object.__new__(confirmation._PinnedDirectory)
    directory.path, directory.fd = tmp_path, 17
    flags = os.O_RDWR | os.O_CREAT
    assert directory._native_open("example.lock", flags) == 777
    assert calls == [("example.lock", flags | 0x70000, 0o600, 17)]


def test_uninstall_audit_hardlink_failure_remains_sanitized_503(tmp_path: Path) -> None:
    root = tmp_path / "packages"
    leaf = lock_leaf(root)
    leaf.parent.mkdir(parents=True)
    victim = tmp_path / "owned-victim"
    victim.write_bytes(b"\0")
    os.link(victim, leaf)
    before = file_identity(victim)
    pending = root / ".uninstalling" / str(TENANT) / "example"
    state = admin._PluginPackageUninstallState(
        pending_root=pending, package_root=root / str(TENANT) / "example",
        staged_artifact_root=None, audit_confirmed_path=pending / "audit-confirmed",
        operation_id=TENANT, plugin_snapshot_sha256="a" * 64,
        created_at=datetime.now(UTC), retry=False,
    )
    with pytest.raises(PublicAPIError) as caught:
        admin._mark_plugin_uninstall_audit_confirmed(state)
    assert caught.value.status_code == 503
    assert str(root) not in str(caught.value)
    assert not state.audit_confirmed_path.exists()
    assert victim.read_bytes() == b"\0" and file_identity(victim) == before


def test_install_hardlink_lock_failure_is_sanitized_503(tmp_path: Path) -> None:
    root = tmp_path / "packages"
    leaf = root / ".uninstall-locks" / str(admin_tests.TENANT_ID) / "calendar.lock"
    leaf.parent.mkdir(parents=True)
    victim = tmp_path / "owned-victim"
    victim.write_bytes(b"\0")
    os.link(victim, leaf)
    before = file_identity(victim)
    key = ed25519.Ed25519PrivateKey.generate()
    api = admin_tests.client_with_settings(
        Settings.model_construct(plugin_package_store_dir=root),
    )
    try:
        trusted = api.post(
            "/api/v1/admin/plugins/signing-keys", headers=admin_tests.headers(),
            json={
                "key_id": "calendar-prod", "algorithm": "ed25519",
                "public_key": admin_tests.plugin_public_key_value(key),
            },
        )
        assert trusted.status_code == 200
        response = api.post(
            "/api/v1/admin/plugins/install",
            headers={
                **admin_tests.headers(), "Content-Type": "application/zip",
                "X-Agent-Hub-Plugin-Filename": "calendar-plugin.zip",
            },
            content=admin_tests.signed_plugin_archive(key),
        )
    finally:
        api.close()
    assert response.status_code == 503, response.text
    assert response.json()["error"]["code"] == "plugin_package_store_unavailable"
    assert str(root) not in response.text and "Conflict" not in response.text
    assert not (root / str(admin_tests.TENANT_ID)).exists()
    assert victim.read_bytes() == b"\0" and file_identity(victim) == before


@pytest.mark.parametrize("invalid_package", [False, True])
async def test_second_install_lock_failure_cannot_commit_state(
    context: AdminContext, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    invalid_package: bool,
) -> None:
    service, db, tenant_id = context
    db.put(plugin_payload(generated=False))
    before, before_ids = deepcopy(db.rows), deepcopy(db.row_ids)
    store_root = tmp_path / "packages"
    private_directory(store_root)
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as package:
        package.writestr("plugin.json", json.dumps({"id": "generated-tool", "name": "Installed"}))
        package.writestr("adapter.py", b"owned artifact")
    body = archive.getvalue()

    async def receive() -> Message:
        return {"type": "http.request", "body": body, "more_body": False}

    request = Request({
        "type": "http",
        "headers": [(b"x-agent-hub-plugin-filename", b"plugin.zip")],
        "app": request_for(store_root).app,
    }, receive)
    principal = AuthenticatedPrincipal(user_id=uuid4(), tenant_id=tenant_id, role=Role.ADMIN)
    verified = admin.PluginPackageMetadata(
        kind="adapter_package", verified_public_key_sha256="a" * 64,
    )
    monkeypatch.setattr(admin, "_verified_plugin_package_metadata", lambda *args: verified)
    monkeypatch.setattr(service, "list_plugin_signing_keys", AsyncMock(return_value=()))
    original = admin._plugin_package_uninstall_stage_lock
    acquisitions = 0

    @contextmanager
    def fail_second_lock(request: Request, *, tenant_id: UUID, plugin_id: str) -> Iterator[None]:
        nonlocal acquisitions
        acquisitions += 1
        if acquisitions == 2:
            if invalid_package:
                raise InvalidSkillPackage("unsafe package")
            raise OSError(str(tmp_path / "private-stage-lock"))
        with original(request, tenant_id=tenant_id, plugin_id=plugin_id):
            yield

    monkeypatch.setattr(admin, "_plugin_package_uninstall_stage_lock", fail_second_lock)
    try:
        if invalid_package:
            with pytest.raises(InvalidSkillPackage):
                await admin.install_plugin_archive(request, principal, service)
        else:
            with pytest.raises(PublicAPIError) as caught:
                await admin.install_plugin_archive(request, principal, service)
            assert caught.value.status_code == 503
            assert caught.value.code == "plugin_package_store_unavailable"
            assert str(tmp_path) not in str(caught.value)
    finally:
        assert acquisitions == 2
        assert db.rows == before and db.row_ids == before_ids
        assert not db.active
        assert not any(event[0] in {"write", "audit"} for event in db.events)
