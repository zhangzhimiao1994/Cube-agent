"""Approved package bytes only; native Linux tests never emulate seals."""

from __future__ import annotations

import asyncio
import hashlib
import importlib
import math
import os
import stat
import sys
from contextlib import ExitStack
from pathlib import Path, PurePosixPath
from types import ModuleType, SimpleNamespace
from typing import cast

import pytest

from agent_hub.capabilities.runtime import RuntimeCapabilityError
from agent_hub.plugins.execution_authority import PackageExecutionMember
from agent_hub.plugins.execution_view import _CapturedFile

FAILURE = "Plugin package immutable capture failed"
NATIVE = pytest.mark.skipif(
    sys.platform != "linux", reason="requires native Linux descriptors/memfd"
)
SOURCE = b"VALUE = 1\n"


@pytest.fixture
def view() -> ModuleType:
    return importlib.import_module("agent_hub.plugins.execution_view")


@pytest.fixture
def validation_view(view: ModuleType, monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    def reached_platform_gate() -> None:
        pytest.fail("invalid authority or budget reached the platform gate")

    monkeypatch.setattr(view, "_require_linux", reached_platform_gate)
    return view


def member(path: str = "main.py", data: bytes = SOURCE) -> PackageExecutionMember:
    return PackageExecutionMember(path, len(data), hashlib.sha256(data).hexdigest())


def package(tmp_path: Path) -> Path:
    root = tmp_path / "package"
    root.mkdir()
    (root / "main.py").write_bytes(SOURCE)
    return root


@pytest.mark.parametrize("manifest_position", ["first", "last", "absent"])
def test_trusted_report_projection_ignores_one_root_manifest(
    manifest_position: str, view: ModuleType
) -> None:
    manifest = member("plugin.json", b"manifest content exceeds the payload byte budget")
    reports = {
        "first": (manifest, member()),
        "last": (member(), manifest),
        "absent": (member(),),
    }
    approved, directories = view._approved_members(
        reports[manifest_position], 1, view._Budget(len(SOURCE), math.inf)
    )
    assert approved == {"main.py": member()}
    assert directories == set()


def test_api_and_unsupported_platform_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    view = importlib.import_module("agent_hub.plugins.execution_view")
    monkeypatch.setattr(view.sys, "platform", "win32")
    with (
        pytest.raises(RuntimeCapabilityError, match=f"^{FAILURE}$") as caught,
        view.capture_sealed_package(tmp_path, (member(),)),
    ):
        pytest.fail("unsupported platform yielded an execution view")
    assert caught.value.__cause__ is None
    assert caught.value.__suppress_context__


@pytest.mark.parametrize(
    "path",
    [
        "",
        ".",
        "..",
        "../main.py",
        "x/../main.py",
        "/main.py",
        "x//y",
        "x/./y",
        "x/",
        "C:/main.py",
        "x\\main.py",
        "x\x00.py",
        "x/plugin.json",
        "x/PLUGIN.JSON",
        "x/" * 65 + "main.py",
        "x" * 4097,
    ],
)
def test_authority_rejects_noncanonical_or_manifest_paths(
    tmp_path: Path, path: str, validation_view: ModuleType
) -> None:
    with (
        pytest.raises(RuntimeCapabilityError, match=f"^{FAILURE}$"),
        validation_view.capture_sealed_package(tmp_path, (member(path),)),
    ):
        pytest.fail("unsafe authority yielded an execution view")


@pytest.mark.parametrize(
    "size,digest", [(-1, "a" * 64), (True, "a" * 64), (1, "A" * 64), (1, "z" * 64), (1, "a" * 63)]
)
def test_authority_rejects_invalid_size_or_digest(
    tmp_path: Path, size: int, digest: str, validation_view: ModuleType
) -> None:
    with (
        pytest.raises(RuntimeCapabilityError, match=f"^{FAILURE}$"),
        validation_view.capture_sealed_package(
            tmp_path, (PackageExecutionMember("main.py", size, digest),)
        ),
    ):
        pytest.fail("invalid authority yielded an execution view")


@pytest.mark.parametrize(
    "members",
    [
        (),
        (member(), member()),
        (member("x"), member("x/y")),
        (member("plugin.json"),),
        (member(), member("plugin.json"), member("plugin.json")),
    ],
)
def test_authority_rejects_empty_duplicate_or_file_directory_collision(
    tmp_path: Path, members: tuple[PackageExecutionMember, ...], validation_view: ModuleType
) -> None:
    with (
        pytest.raises(RuntimeCapabilityError, match=f"^{FAILURE}$"),
        validation_view.capture_sealed_package(tmp_path, members),
    ):
        pytest.fail("ambiguous authority yielded an execution view")


@pytest.mark.parametrize(
    "options",
    [
        {"max_files": 0},
        {"max_files": True},
        {"max_total_bytes": -1},
        {"max_total_bytes": True},
        {"timeout_seconds": 0},
        {"timeout_seconds": -1},
        {"timeout_seconds": math.nan},
        {"timeout_seconds": math.inf},
    ],
)
def test_invalid_budgets_fail_closed(
    tmp_path: Path, options: dict[str, object], validation_view: ModuleType
) -> None:
    with (
        pytest.raises(RuntimeCapabilityError, match=f"^{FAILURE}$"),
        validation_view.capture_sealed_package(tmp_path, (member(),), **options),
    ):
        pytest.fail("invalid budget yielded an execution view")


@pytest.mark.parametrize(
    "change",
    ["unrelated", "dev", "inode", "binding", "mode", "uid", "gid", "source", "root", "nested"],
)
def test_capture_distinguishes_shared_ancestor_changes_from_package_changes(
    monkeypatch: pytest.MonkeyPatch, view: ModuleType, change: str,
) -> None:
    # Descriptor-boundary doubles exercise revalidation on Windows without emulating seals.
    def info(inode: int, *, directory: bool = True, field: str = "") -> os.stat_result:
        values = [stat.S_IFDIR | 0o755 if directory else stat.S_IFREG | 0o644,
                  inode, 1, 2 if directory else 1, 1000, 1000, len(SOURCE), 0, 0, 0]
        for name, index in (("mode", 0), ("inode", 1), ("dev", 2), ("uid", 4), ("gid", 5)):
            if field == name:
                values[index] += 1
        if field == "metadata":
            values[3] += 1
            values[6] += 1
        return os.stat_result(values, {"st_mtime_ns": 2 if field == "metadata" else 1,
                                      "st_ctime_ns": 2 if field == "metadata" else 1})

    states = {fd: info(fd, directory=fd < 4) for fd in range(6)}
    bindings = {(None, "/"): 0, (0, "shared"): 1, (1, "package"): 2,
                (2, "nested"): 3, (2, "main.py"): 4, (3, "last.py"): 5}
    rebound: os.stat_result | None = None

    def entry_stat(name: str, parent_fd: int | None) -> os.stat_result:
        if name == "shared" and rebound is not None:
            return rebound
        return states[bindings[parent_fd, name]]

    def directory(name: str, parent_fd: int | None, *args: object, **kwargs: object) -> SimpleNamespace:
        fd = bindings[parent_fd, name]
        return SimpleNamespace(fd=fd, parent_fd=parent_fd, name=name, info=states[fd],
                               ancestor=kwargs.get("ancestor", False))

    def regular(name: str, *args: object) -> bytes:
        nonlocal rebound
        if name == "last.py":
            if change == "binding":
                rebound = info(99)
            else:
                fd = {"source": 4, "root": 2, "nested": 3}.get(change, 1)
                states[fd] = info(fd, directory=fd < 4,
                                  field="metadata" if change in {"unrelated", "source", "root", "nested"}
                                  else change)
        return SOURCE

    monkeypatch.setattr(view, "_directory", directory)
    monkeypatch.setattr(view, "_entry_stat", entry_stat)
    monkeypatch.setattr(view, "_regular", regular)
    monkeypatch.setattr(view, "_names", lambda fd, *args: {2: ("main.py", "nested"),
                                                         3: ("last.py",)}[fd])
    monkeypatch.setattr(view.os, "fstat", lambda fd: states[fd])

    def capture() -> tuple[_CapturedFile, ...]:
        return cast(tuple[_CapturedFile, ...], view._capture_tree(
            cast(Path, PurePosixPath("/shared/package")),
            {"main.py": member(), "nested/last.py": member("nested/last.py")},
            {"nested"}, view._Budget(100, math.inf),
        ))

    if change == "unrelated":
        captured = capture()
        assert [(item.path, item.data) for item in captured] == [
            ("main.py", SOURCE), ("nested/last.py", SOURCE),
        ]
    else:
        with pytest.raises(ValueError, match=f"^{FAILURE}$"):
            capture()


@pytest.mark.parametrize("ancestor", [True, False])
def test_directory_open_allows_metadata_changes_only_for_shared_ancestors(
    monkeypatch: pytest.MonkeyPatch, view: ModuleType, ancestor: bool,
) -> None:
    values = (stat.S_IFDIR | 0o755, 1, 1, 2, 1000, 1000, 512, 0, 0, 0)
    before = os.stat_result(values, {
        "st_mtime_ns": 1, "st_ctime_ns": 1, "st_file_attributes": 0,
    })
    after = os.stat_result(values, {
        "st_mtime_ns": 2, "st_ctime_ns": 2, "st_file_attributes": 0,
    })
    monkeypatch.setattr(view, "sys", SimpleNamespace(platform="linux"))
    monkeypatch.setattr(view, "os", SimpleNamespace(
        O_RDONLY=0, O_DIRECTORY=1, O_NOFOLLOW=2, O_CLOEXEC=4,
        stat=lambda *args, **kwargs: before, open=lambda *args, **kwargs: 97,
        fstat=lambda fd: after, close=lambda fd: None,
    ))
    with ExitStack() as stack:
        if ancestor:
            directory = view._directory(
                "shared", 96, stack, view._Budget(100, math.inf), ancestor=True,
            )
            assert directory.fd == 97 and directory.info.st_ino == 1
        else:
            with pytest.raises(ValueError, match=f"^{FAILURE}$"):
                view._directory("package", 96, stack, view._Budget(100, math.inf))


@NATIVE
@pytest.mark.parametrize("phase", ["open", "read"])
def test_unrelated_shared_ancestor_changes_do_not_invalidate_native_capture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, view: ModuleType, phase: str,
) -> None:
    if sys.platform != "linux":
        pytest.skip("requires native Linux descriptors/memfd")
    root = package(tmp_path)
    original_open, original_read = os.open, os.read
    changed = False

    def mutate_shared_ancestor() -> None:
        nonlocal changed
        before = tmp_path.stat()
        (tmp_path / "unrelated").write_bytes(b"unrelated sibling")
        os.utime(tmp_path, ns=(before.st_atime_ns, before.st_mtime_ns + 1_000_000_000))
        changed = True

    def opening(
        path: str | Path, flags: int, mode: int = 0o777, *, dir_fd: int | None = None,
    ) -> int:
        if not changed and phase == "open" and str(path) == tmp_path.name:
            mutate_shared_ancestor()
        return original_open(path, flags, mode, dir_fd=dir_fd)

    def reading(fd: int, size: int) -> bytes:
        data = original_read(fd, size)
        if not changed and phase == "read" and data == SOURCE:
            mutate_shared_ancestor()
        return data

    monkeypatch.setattr(view.os, "open", opening)
    monkeypatch.setattr(view.os, "read", reading)
    with view.capture_sealed_package(root, (member(),)) as sealed:
        assert os.read(sealed.files[0].fd, 100) == SOURCE
    assert changed


@NATIVE
def test_exact_approved_bytes_are_sealed_cloexec_and_owned(
    tmp_path: Path, view: ModuleType
) -> None:
    if sys.platform != "linux":
        pytest.skip("requires native Linux descriptors/memfd")
    import fcntl

    root = package(tmp_path)
    (root / "pkg").mkdir()
    (root / "pkg/helper.py").write_bytes(b"helper")
    approved = (member("pkg/helper.py", b"helper"), member("plugin.json", b"{}"), member())
    with view.capture_sealed_package(root, approved) as sealed:
        assert isinstance(sealed.files, tuple)
        assert tuple(item.path for item in sealed.files) == ("main.py", "pkg/helper.py")
        assert sealed.pass_fds == tuple(item.fd for item in sealed.files)
        for item in sealed.files:
            assert os.get_inheritable(item.fd) is False
            mask = fcntl.F_SEAL_WRITE | fcntl.F_SEAL_SHRINK | fcntl.F_SEAL_GROW | fcntl.F_SEAL_SEAL
            assert fcntl.fcntl(item.fd, fcntl.F_GET_SEALS) & mask == mask
            with pytest.raises(OSError):
                os.write(item.fd, b"X")
            with pytest.raises(OSError):
                os.ftruncate(item.fd, 0)
        (root / "main.py").write_bytes(b"VALUE = 2\n")
        assert os.read(sealed.files[0].fd, 100) == SOURCE
        assert os.read(sealed.files[1].fd, 100) == b"helper"
        fds = sealed.pass_fds
    for fd in fds:
        with pytest.raises(OSError):
            os.fstat(fd)


@NATIVE
@pytest.mark.parametrize(
    "mutation",
    [
        "same-size",
        "size",
        "missing",
        "extra",
        "manifest",
        "nested-manifest",
        "empty-directory",
        "hardlink",
        "fifo",
    ],
)
def test_installed_tree_must_exactly_match_external_authority(
    tmp_path: Path, mutation: str, view: ModuleType
) -> None:
    if sys.platform != "linux":
        pytest.skip("requires native Linux descriptors/memfd")
    root = package(tmp_path)
    source = root / "main.py"
    if mutation == "same-size":
        source.write_bytes(b"VALUE = 2\n")
    elif mutation == "size":
        source.write_bytes(SOURCE + b" ")
    elif mutation == "missing":
        source.unlink()
    elif mutation == "extra":
        (root / "injected.py").write_bytes(SOURCE)
    elif mutation == "manifest":
        (root / "plugin.json").write_bytes(b"{}")
    elif mutation == "nested-manifest":
        (root / "nested").mkdir()
        (root / "nested/plugin.json").write_bytes(b"{}")
    elif mutation == "empty-directory":
        (root / "unexpected").mkdir()
    elif mutation == "hardlink":
        os.link(source, tmp_path / "alias")
    else:
        source.unlink()
        os.mkfifo(source)
    with (
        pytest.raises(RuntimeCapabilityError, match=f"^{FAILURE}$"),
        view.capture_sealed_package(root, (member(), member("plugin.json", b"{}"))),
    ):
        pytest.fail("changed package yielded an execution view")


@NATIVE
@pytest.mark.parametrize("link", ["file", "directory", "root", "ancestor"])
def test_symlinks_are_rejected_in_tree_and_root_ancestry(
    tmp_path: Path, link: str, view: ModuleType
) -> None:
    if sys.platform != "linux":
        pytest.skip("requires native Linux descriptors/memfd")
    root = package(tmp_path)
    if link == "file":
        (root / "main.py").unlink()
        (tmp_path / "external.py").write_bytes(SOURCE)
        (root / "main.py").symlink_to(tmp_path / "external.py")
    elif link == "directory":
        (root / "nested").symlink_to(tmp_path, target_is_directory=True)
    elif link == "root":
        (tmp_path / "alias").symlink_to(root, target_is_directory=True)
        root = tmp_path / "alias"
    else:
        (tmp_path / "alias").symlink_to(tmp_path, target_is_directory=True)
        root = tmp_path / "alias/package"
    with (
        pytest.raises(RuntimeCapabilityError, match=f"^{FAILURE}$"),
        view.capture_sealed_package(root, (member(),)),
    ):
        pytest.fail("symlink yielded an execution view")


@NATIVE
@pytest.mark.parametrize("limit", ["files", "bytes", "time"])
def test_native_capture_enforces_budgets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, limit: str, view: ModuleType
) -> None:
    if sys.platform != "linux":
        pytest.skip("requires native Linux descriptors/memfd")
    root = package(tmp_path)
    approved: tuple[PackageExecutionMember, ...] = (member(),)
    options: dict[str, int | float] = {}
    if limit == "files":
        approved += (member("other.py"),)
        (root / "other.py").write_bytes(SOURCE)
        options["max_files"] = 1
    elif limit == "bytes":
        options["max_total_bytes"] = len(SOURCE) - 1
    else:
        ticks = iter((1.0, 20.0))
        monkeypatch.setattr(view, "monotonic", lambda: next(ticks, 20.0))
    with (
        pytest.raises(RuntimeCapabilityError, match=f"^{FAILURE}$"),
        view.capture_sealed_package(root, approved, **options),
    ):
        pytest.fail("budget exceeded but yielded an execution view")


@NATIVE
@pytest.mark.parametrize(
    "phase",
    ["before-open", "during-read", "after-read", "late-add", "directory-replace", "root-replace"],
)
def test_capture_rejects_replacement_and_read_races(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, phase: str, view: ModuleType
) -> None:
    if sys.platform != "linux":
        pytest.skip("requires native Linux descriptors/memfd")
    root = package(tmp_path)
    (root / "nested").mkdir()
    (root / "nested/last.py").write_bytes(b"last")
    approved = (member(), member("nested/last.py", b"last"))
    original_open, original_read = os.open, os.read
    swapped = False

    def mutate() -> None:
        nonlocal swapped
        swapped = True
        if phase == "late-add":
            (root / "injected.py").write_bytes(b"injected")
        elif phase == "directory-replace":
            (root / "nested").rename(tmp_path / "old-nested")
            (root / "nested").mkdir()
            (root / "nested/last.py").write_bytes(b"last")
        elif phase == "root-replace":
            root.rename(tmp_path / "old-root")
            root.mkdir()
            (root / "main.py").write_bytes(SOURCE)
        else:
            replacement = tmp_path / "replacement"
            replacement.write_bytes(SOURCE)
            os.replace(replacement, root / "main.py")

    def opening(
        path: str | Path, flags: int, mode: int = 0o777, *, dir_fd: int | None = None
    ) -> int:
        if not swapped and str(path) == "main.py" and phase == "before-open":
            mutate()
        if (
            not swapped
            and str(path) == "last.py"
            and phase in {"after-read", "late-add", "directory-replace", "root-replace"}
        ):
            mutate()
        return original_open(path, flags, mode, dir_fd=dir_fd)

    def reading(fd: int, size: int) -> bytes:
        data = original_read(fd, size)
        if not swapped and data == SOURCE and phase == "during-read":
            mutate()
        return data

    monkeypatch.setattr(view.os, "open", opening)
    monkeypatch.setattr(view.os, "read", reading)
    with (
        pytest.raises(RuntimeCapabilityError, match=f"^{FAILURE}$"),
        view.capture_sealed_package(root, approved),
    ):
        pytest.fail("racy capture yielded an execution view")
    assert swapped


@NATIVE
@pytest.mark.parametrize(
    "failure", ["consumer", "cancel", "capture-cancel", "read", "memfd", "write", "seal"]
)
def test_all_owned_descriptors_close_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str, view: ModuleType
) -> None:
    if sys.platform != "linux":
        pytest.skip("requires native Linux descriptors/memfd")
    import fcntl

    root = package(tmp_path)
    (root / "second.py").write_bytes(SOURCE)
    opened: list[int] = []
    original_open, original_memfd = os.open, os.memfd_create
    original_read, original_write = os.read, os.write
    original_fcntl = fcntl.fcntl
    memfd_count = 0

    def opening(
        path: str | Path, flags: int, mode: int = 0o777, *, dir_fd: int | None = None
    ) -> int:
        fd = original_open(path, flags, mode, dir_fd=dir_fd)
        opened.append(fd)
        return fd

    def creating(name: str, flags: int = 1) -> int:
        nonlocal memfd_count
        memfd_count += 1
        if failure == "memfd" and memfd_count == 2:
            raise OSError("private-host-path private-source")
        fd = original_memfd(name, flags)
        opened.append(fd)
        return fd

    def reading(fd: int, size: int) -> bytes:
        if failure == "capture-cancel":
            raise asyncio.CancelledError()
        if failure == "read":
            raise OSError("private-host-path private-source")
        return original_read(fd, size)

    def writing(fd: int, data: bytes | memoryview) -> int:
        if failure == "write":
            raise OSError("private-host-path private-source")
        return original_write(fd, data)

    def sealing(fd: int, operation: int, argument: int = 0) -> int:
        if failure == "seal" and operation == fcntl.F_ADD_SEALS:
            raise OSError("private-host-path private-source")
        return original_fcntl(fd, operation, argument)

    monkeypatch.setattr(view.os, "open", opening)
    monkeypatch.setattr(view.os, "memfd_create", creating)
    monkeypatch.setattr(view.os, "read", reading)
    monkeypatch.setattr(view.os, "write", writing)
    monkeypatch.setattr(fcntl, "fcntl", sealing)
    expected = (
        asyncio.CancelledError
        if failure in {"cancel", "capture-cancel"}
        else (LookupError if failure == "consumer" else RuntimeCapabilityError)
    )
    with (
        pytest.raises(expected) as caught,
        view.capture_sealed_package(root, (member(), member("second.py"))),
    ):
        if failure == "cancel":
            raise asyncio.CancelledError()
        if failure == "consumer":
            raise LookupError("consumer failure")
        pytest.fail("injected capture fault was ignored")
    if isinstance(caught.value, RuntimeCapabilityError):
        assert str(caught.value) == FAILURE
        assert caught.value.__cause__ is None
        assert caught.value.__suppress_context__
    assert opened
    for fd in opened:
        with pytest.raises(OSError):
            os.fstat(fd)
