"""Pure boundary tests and explicitly gated Linux Root filesystem tests."""

from __future__ import annotations

import importlib
import importlib.util
import json
import os
import sys
import time
import warnings
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import pytest

from agent_hub.skills.sandbox.toolchain_catalog import ToolchainCatalog

MODULE = "agent_hub.skills.sandbox.toolchain_configuration"


def payload() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "runtime_root": "/var/lib/toolchain-runtime",
        "stage_root": "/var/lib/toolchain-stage",
        "state_root": "/var/lib/toolchain-state",
        "min_free_bytes": 3 * 1024**3,
        "artifacts": [
            {
                "stack": "go",
                "version": "1.27.3",
                "platform": "linux_amd64",
                "url": "https://dl.google.com/go/go1.27.3.linux-amd64.tar.gz",
                "sha256": "a" * 64,
                "archive_bytes": 70,
                "unpacked_bytes_limit": 1000,
                "archive_entries_limit": 100,
                "directory_entries_limit": 100,
                "tar_bytes_limit": 10240,
            }
        ],
    }


def test_configuration_module_and_public_interfaces_exist() -> None:
    assert importlib.util.find_spec(MODULE) is not None
    module = importlib.import_module(MODULE)
    assert callable(module.read_toolchain_configuration)
    assert callable(module.measure_toolchain_spaces)


@pytest.fixture
def api() -> Any:
    if importlib.util.find_spec(MODULE) is None:
        pytest.skip("configuration reader not implemented yet")
    return importlib.import_module(MODULE)


def native() -> None:
    if sys.platform != "linux" or os.geteuid() != 0 or os.getegid() != 0:
        pytest.skip("requires real Linux Root DirFD/statvfs, never mocked as native")


def posix_call(name: str) -> Callable[..., Any]:
    # Resolve Linux-only APIs without pretending they exist in Windows typeshed.
    function = getattr(os, name, None)
    assert callable(function)
    return cast(Callable[..., Any], function)


@pytest.fixture
def layout(tmp_path: Path) -> tuple[Path, ToolchainCatalog, Path]:
    native()
    tmp_path.chmod(0o700)
    data = payload()
    for role in ("stage", "runtime", "state"):
        root = tmp_path / role
        root.mkdir(mode=0o700)
        data[role + "_root"] = str(root)
    execution = tmp_path / "execution"
    execution.mkdir(mode=0o750)
    path = tmp_path / "toolchains.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    path.chmod(0o600)
    return path, ToolchainCatalog.from_payload(data), execution


def test_unsupported_host_cannot_read_or_measure(api: Any, tmp_path: Path) -> None:
    if sys.platform == "linux" and os.geteuid() == 0 and os.getegid() == 0:
        pytest.skip("native host exercised separately")
    with pytest.raises(
        api.ToolchainConfigurationError, match="^toolchain_configuration_unsupported$"
    ):
        api.read_toolchain_configuration(tmp_path / "missing", deadline=time.monotonic() + 5)
    with pytest.raises(
        api.ToolchainConfigurationError, match="^toolchain_configuration_unsupported$"
    ):
        api.measure_toolchain_spaces(
            ToolchainCatalog.from_payload(payload()),
            tmp_path,
            execution_owner_uid=0,
            deadline=time.monotonic() + 5,
        )
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    "deadline",
    [True, None, "10", -1, 0, float("nan"), float("inf"), float("-inf"), 10**1000],
    ids=["bool", "none", "str", "negative", "zero", "nan", "inf", "-inf", "huge"],
)
def test_deadline_validation_is_finite_without_filesystem_access(api: Any, deadline: Any) -> None:
    with pytest.raises(api.ToolchainConfigurationError, match="^toolchain_configuration_unsafe$"):
        api._check(deadline)


def test_expired_deadline_has_distinct_finite_error(api: Any) -> None:
    with pytest.raises(api.ToolchainConfigurationError, match="^toolchain_configuration_timeout$"):
        api._check(time.monotonic() - 1)


def test_missing_control_has_a_preparable_finite_error(
    api: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Error mapping only; real Linux traversal is tested separately.
    monkeypatch.setattr(api, "_native", lambda deadline: None)
    monkeypatch.setattr(api, "_path", lambda path: path)
    monkeypatch.setattr(api, "_control_parent", lambda *args: 99)

    def missing(*args: Any, **kwargs: Any) -> int:
        raise FileNotFoundError

    monkeypatch.setattr(api.os, "open", missing)
    with pytest.raises(api.ToolchainConfigurationError, match="^toolchain_configuration_missing$"):
        api.read_toolchain_configuration(
            Path("/unused/toolchains.json"), deadline=time.monotonic() + 5
        )


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"[]",
        b"null",
        b"\xff",
        b"{}",
        b"{" * 2000,
        b" " * (512 * 1024 + 1),
        b'{"schema_version":1,"schema_version":1}',
        b'{"artifacts":[{"stack":"go","stack":"go"}]}',
    ],
    ids=[
        "empty",
        "list",
        "null",
        "encoding",
        "missing",
        "deep",
        "size",
        "duplicate",
        "nested-duplicate",
    ],
)
def test_strict_bounded_json_rejects_untrusted_controls(api: Any, raw: bytes) -> None:
    with pytest.raises(api.ToolchainConfigurationError, match="^toolchain_configuration_unsafe$"):
        api._decode(raw)


@pytest.mark.parametrize("token", ["NaN", "Infinity", "-Infinity", "1e999"])
def test_nonfinite_numbers_are_rejected(api: Any, token: str) -> None:
    raw = json.dumps(payload()).replace(str(3 * 1024**3), token).encode()
    with pytest.raises(api.ToolchainConfigurationError):
        api._decode(raw)


@pytest.mark.parametrize(
    "change",
    [
        {"extra": "secret-host-path"},
        {"schema_version": True},
        {"stage_root": "/var/lib/toolchain-runtime/child"},
        {"state_root": "/var//state"},
        {"runtime_root": "/var/lib/../runtime"},
        {"runtime_root": "/var/link\\runtime"},
    ],
)
def test_revalidate_catalog_rejects_model_copy_bypasses(api: Any, change: dict[str, Any]) -> None:
    forged = ToolchainCatalog.from_payload(payload()).model_copy(update=change)
    with pytest.raises(api.ToolchainConfigurationError, match="^toolchain_configuration_unsafe$"):
        api._catalog(forged)


def test_nested_model_copy_and_wrong_catalog_types_are_rejected(api: Any) -> None:
    good = ToolchainCatalog.from_payload(payload())
    artifact = good.artifacts[0].model_copy(update={"url": "https://private.invalid/secret"})
    for forged in (good.model_copy(update={"artifacts": (artifact,)}), payload(), None):
        with pytest.raises(api.ToolchainConfigurationError) as error:
            api._catalog(forged)
        assert str(error.value) == "toolchain_configuration_unsafe"


def test_invalid_model_copy_never_emits_serialization_warnings_with_private_values(
    api: Any,
) -> None:
    good = ToolchainCatalog.from_payload(payload())
    forged = good.model_copy(update={"stage_root": {"private": "/private/operator/path"}})
    with warnings.catch_warnings(record=True) as captured:
        warnings.simplefilter("always")
        with pytest.raises(
            api.ToolchainConfigurationError, match="^toolchain_configuration_unsafe$"
        ):
            api._catalog(forged)
    assert captured == []


def test_catalog_roundtrip_preserves_identity_digest_not_model_identity(api: Any) -> None:
    good = ToolchainCatalog.from_payload(payload())
    assert api._decode(good.to_json()).sha256 == good.sha256
    rebuilt = api._catalog(good)
    assert rebuilt == good and rebuilt is not good


@pytest.mark.parametrize("case", ["duplicate", "nested-duplicate", "extra", "nested-extra"])
def test_valid_catalog_with_a_single_json_violation_is_rejected(api: Any, case: str) -> None:
    raw = json.dumps(payload())
    if case == "duplicate":
        raw = raw.replace('"schema_version": 1', '"schema_version": 1, "schema_version": 1')
    elif case == "nested-duplicate":
        raw = raw.replace('"stack": "go"', '"stack": "go", "stack": "go"')
    elif case == "extra":
        raw = raw.replace('"schema_version": 1', '"schema_version": 1, "private": "secret"')
    else:
        raw = raw.replace('"stack": "go"', '"stack": "go", "private": "secret"')
    with pytest.raises(api.ToolchainConfigurationError, match="^toolchain_configuration_unsafe$"):
        api._decode(raw.encode())


def test_inode_aliases_and_physical_nesting_are_rejected(api: Any) -> None:
    # Pure identities, not fabricated native measurement evidence.
    for chains in (
        (((1, 1), (1, 2)), ((1, 1), (1, 2))),
        (((1, 1), (1, 2)), ((1, 1), (1, 2), (1, 3))),
    ):
        with pytest.raises(api.ToolchainConfigurationError):
            api._unaliased(chains)
    api._unaliased((((1, 1), (1, 2)), ((1, 1), (1, 3))))


def test_real_configuration_and_four_role_measurements_are_read_only(api: Any, layout: Any) -> None:
    path, catalog, execution = layout
    before = {p: p.stat() for p in path.parent.iterdir()}
    assert api.read_toolchain_configuration(path, deadline=time.monotonic() + 5) == catalog
    result = api.measure_toolchain_spaces(
        catalog, execution, execution_owner_uid=0, deadline=time.monotonic() + 5
    )
    assert tuple(s.role for s in result) == ("stage", "runtime", "state", "execution")
    assert {s.device for s in result} == {execution.stat().st_dev}
    assert len({(s.available_bytes, s.allocation_unit) for s in result}) == 1
    assert all(s.available_bytes >= 0 and s.allocation_unit > 0 for s in result)
    after = {p: p.stat() for p in path.parent.iterdir()}
    assert before.keys() == after.keys()
    for p, observed in before.items():
        assert (
            observed.st_ino,
            observed.st_mode,
            observed.st_size,
            observed.st_mtime_ns,
            observed.st_ctime_ns,
        ) == (
            after[p].st_ino,
            after[p].st_mode,
            after[p].st_size,
            after[p].st_mtime_ns,
            after[p].st_ctime_ns,
        )


def test_statvfs_is_called_once_per_actual_device(
    api: Any, layout: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, catalog, execution = layout
    actual = posix_call("fstatvfs")
    devices: list[int] = []

    def observe(fd: int) -> Any:
        devices.append(os.fstat(fd).st_dev)
        return actual(fd)

    monkeypatch.setattr(api.os, "fstatvfs", observe)
    result = api.measure_toolchain_spaces(
        catalog, execution, execution_owner_uid=0, deadline=time.monotonic() + 5
    )
    assert len(result) == 4 and len(devices) == len(set(devices)) == 1


@pytest.mark.parametrize("mode", [0o644, 0o640, 0o400, 0o666, 0o1600])
def test_control_requires_exact_root600(api: Any, layout: Any, mode: int) -> None:
    path, _, _ = layout
    path.chmod(mode)
    with pytest.raises(api.ToolchainConfigurationError):
        api.read_toolchain_configuration(path, deadline=time.monotonic() + 5)


@pytest.mark.parametrize(
    "case", ["symlink", "hardlink", "nonroot", "fifo", "directory", "oversized", "empty", "missing"]
)
def test_unsafe_control_types_owners_and_sizes_are_rejected(
    api: Any, layout: Any, case: str
) -> None:
    path, _, _ = layout
    raw = path.read_bytes()
    path.unlink()
    if case == "symlink":
        target = path.with_name("target")
        target.write_bytes(raw)
        target.chmod(0o600)
        path.symlink_to(target)
    elif case == "hardlink":
        path.write_bytes(raw)
        path.chmod(0o600)
        os.link(path, path.with_name("alias"))
    elif case == "nonroot":
        path.write_bytes(raw)
        path.chmod(0o600)
        posix_call("chown")(path, 65534, 65534)
    elif case == "fifo":
        posix_call("mkfifo")(path, 0o600)
    elif case == "directory":
        path.mkdir(mode=0o600)
    elif case == "oversized":
        with path.open("wb") as stream:
            stream.truncate(512 * 1024 + 1)
        path.chmod(0o600)
    elif case == "empty":
        path.touch(mode=0o600)
    with pytest.raises(api.ToolchainConfigurationError) as error:
        api.read_toolchain_configuration(path, deadline=time.monotonic() + 5)
    assert str(path) not in str(error.value)


def test_size_is_rejected_before_any_read(
    api: Any, layout: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, _, _ = layout
    with path.open("wb") as stream:
        stream.truncate(512 * 1024 + 1)
    calls: list[int] = []
    actual = os.read

    def observe(fd: int, size: int) -> bytes:
        calls.append(size)
        return actual(fd, size)

    monkeypatch.setattr(api.os, "read", observe)
    with pytest.raises(api.ToolchainConfigurationError):
        api.read_toolchain_configuration(path, deadline=time.monotonic() + 5)
    assert calls == []


@pytest.mark.parametrize("role", ["stage", "runtime", "state"])
@pytest.mark.parametrize("case", ["mode", "owner", "symlink", "missing"])
def test_catalog_roots_are_existing_canonical_root700_dirs(
    api: Any, layout: Any, role: str, case: str
) -> None:
    path, catalog, execution = layout
    root = Path(getattr(catalog, role + "_root"))
    if case == "mode":
        root.chmod(0o750)
    elif case == "owner":
        posix_call("chown")(root, 65534, 65534)
    elif case == "symlink":
        root.rmdir()
        root.symlink_to(execution, target_is_directory=True)
    else:
        root.rmdir()
    with pytest.raises(api.ToolchainConfigurationError):
        api.read_toolchain_configuration(path, deadline=time.monotonic() + 5)
    with pytest.raises(api.ToolchainConfigurationError):
        api.measure_toolchain_spaces(
            catalog, execution, execution_owner_uid=0, deadline=time.monotonic() + 5
        )
    if case == "missing":
        assert not root.exists()


@pytest.mark.parametrize("case", ["writable", "nonroot", "symlink"])
def test_unsafe_ancestor_is_rejected(api: Any, layout: Any, case: str) -> None:
    path, _, _ = layout
    ancestor = path.parent
    if case == "writable":
        ancestor.chmod(0o777)
    elif case == "nonroot":
        posix_call("chown")(ancestor, 65534, 65534)
    else:
        alias = ancestor.with_name(ancestor.name + "-alias")
        alias.symlink_to(ancestor, target_is_directory=True)
        path = alias / path.name
    try:
        with pytest.raises(api.ToolchainConfigurationError):
            api.read_toolchain_configuration(path, deadline=time.monotonic() + 5)
    finally:
        posix_call("chown")(ancestor, 0, 0)
        ancestor.chmod(0o700)


def test_root_sticky_exception_is_only_for_ancestors(api: Any, layout: Any) -> None:
    path, catalog, execution = layout
    path.parent.chmod(0o1777)
    try:
        assert api.read_toolchain_configuration(path, deadline=time.monotonic() + 5) == catalog
        execution.chmod(0o1777)
        with pytest.raises(api.ToolchainConfigurationError):
            api.measure_toolchain_spaces(
                catalog, execution, execution_owner_uid=0, deadline=time.monotonic() + 5
            )
    finally:
        path.parent.chmod(0o700)


def test_operator_execution_root_may_have_explicit_nonroot_final_owner(
    api: Any, layout: Any
) -> None:
    _, catalog, execution = layout
    posix_call("chown")(execution, 65534, 65534)
    result = api.measure_toolchain_spaces(
        catalog, execution, execution_owner_uid=65534, deadline=time.monotonic() + 5
    )
    assert len(result) == 4
    with pytest.raises(api.ToolchainConfigurationError):
        api.measure_toolchain_spaces(
            catalog, execution, execution_owner_uid=0, deadline=time.monotonic() + 5
        )


@pytest.mark.parametrize("uid", [True, -1, 2**32 - 1, "0", None])
def test_execution_owner_is_strict_valid_uid(api: Any, layout: Any, uid: Any) -> None:
    _, catalog, execution = layout
    with pytest.raises(api.ToolchainConfigurationError):
        api.measure_toolchain_spaces(
            catalog, execution, execution_owner_uid=uid, deadline=time.monotonic() + 5
        )


@pytest.mark.parametrize("case", ["same", "nested", "symlink", "missing", "writable"])
def test_execution_cannot_overlap_or_redirect_into_catalog_roots(
    api: Any, layout: Any, case: str
) -> None:
    _, catalog, execution = layout
    if case == "same":
        execution = Path(catalog.runtime_root)
    elif case == "nested":
        execution = Path(catalog.runtime_root) / "project"
        execution.mkdir(mode=0o700)
    elif case == "symlink":
        execution.rmdir()
        execution.symlink_to(catalog.runtime_root, target_is_directory=True)
    elif case == "missing":
        execution.rmdir()
    else:
        execution.chmod(0o777)
    with pytest.raises(api.ToolchainConfigurationError):
        api.measure_toolchain_spaces(
            catalog, execution, execution_owner_uid=0, deadline=time.monotonic() + 5
        )


@pytest.mark.parametrize("change", ["control", "parent", "root-mode"])
def test_read_rechecks_pinned_control_and_directory_snapshots(
    api: Any, layout: Any, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    path, catalog, _ = layout
    actual = os.read
    changed = False

    def mutate(fd: int, size: int) -> bytes:
        nonlocal changed
        raw = actual(fd, size)
        if raw and not changed:
            changed = True
            if change == "control":
                replacement = path.with_name("replacement")
                replacement.write_bytes(raw)
                replacement.chmod(0o600)
                replacement.replace(path)
            elif change == "parent":
                path.parent.rename(path.parent.with_name(path.parent.name + "-moved"))
                path.parent.mkdir(mode=0o700)
            else:
                Path(catalog.state_root).chmod(0o750)
        return raw

    monkeypatch.setattr(api.os, "read", mutate)
    with pytest.raises(api.ToolchainConfigurationError):
        api.read_toolchain_configuration(path, deadline=time.monotonic() + 5)
    assert changed


def test_measure_rechecks_directory_after_statvfs(
    api: Any, layout: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, catalog, execution = layout
    actual = posix_call("fstatvfs")

    def mutate(fd: int) -> Any:
        result = actual(fd)
        execution.rename(execution.with_name("execution-moved"))
        execution.mkdir(mode=0o750)
        return result

    monkeypatch.setattr(api.os, "fstatvfs", mutate)
    with pytest.raises(api.ToolchainConfigurationError):
        api.measure_toolchain_spaces(
            catalog, execution, execution_owner_uid=0, deadline=time.monotonic() + 5
        )


@pytest.mark.parametrize("phase", ["read", "statvfs"])
def test_deadline_is_rechecked_immediately_after_native_io(
    api: Any, layout: Any, monkeypatch: pytest.MonkeyPatch, phase: str
) -> None:
    path, catalog, execution = layout
    clock = [100.0]
    calls: list[str] = []
    actual_read = os.read
    actual_statvfs = posix_call("fstatvfs")

    def read(fd: int, size: int) -> bytes:
        result = actual_read(fd, size)
        calls.append("read")
        clock[0] = 102.0
        return result

    def statvfs(fd: int) -> Any:
        result = actual_statvfs(fd)
        calls.append("statvfs")
        clock[0] = 102.0
        return result

    # Only the clock/fault timing is injected; platform, Root identity and all
    # actual filesystem operations stay native and this test skips on Windows.
    monkeypatch.setattr(api.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(
        api.os, "read" if phase == "read" else "fstatvfs", read if phase == "read" else statvfs
    )
    with pytest.raises(api.ToolchainConfigurationError, match="^toolchain_configuration_timeout$"):
        if phase == "read":
            api.read_toolchain_configuration(path, deadline=101.0)
        else:
            api.measure_toolchain_spaces(catalog, execution, execution_owner_uid=0, deadline=101.0)
    assert calls == [phase]


@pytest.mark.parametrize("size", [512 * 1024, 512 * 1024 + 1])
def test_exact_control_byte_boundary(api: Any, layout: Any, size: int) -> None:
    path, catalog, _ = layout
    raw = catalog.to_json()
    path.write_bytes(raw + b" " * (size - len(raw)))
    if size == 512 * 1024:
        assert api.read_toolchain_configuration(path, deadline=time.monotonic() + 5) == catalog
    else:
        with pytest.raises(api.ToolchainConfigurationError):
            api.read_toolchain_configuration(path, deadline=time.monotonic() + 5)


@pytest.mark.parametrize("mutation", ["append", "rewrite", "link"])
def test_changed_open_control_is_rejected(
    api: Any, layout: Any, monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    path, _, _ = layout
    actual_read = os.read
    changed = False

    def read(fd: int, size: int) -> bytes:
        nonlocal changed
        raw = actual_read(fd, size)
        if raw and not changed:
            changed = True
            if mutation == "append":
                with path.open("ab") as stream:
                    stream.write(b" ")
            elif mutation == "rewrite":
                path.write_bytes(raw.replace(b'"schema_version": 1', b'"schema_version": 2'))
            else:
                os.link(path, path.with_name("alias"))
        return raw

    monkeypatch.setattr(api.os, "read", read)
    with pytest.raises(api.ToolchainConfigurationError):
        api.read_toolchain_configuration(path, deadline=time.monotonic() + 5)
    assert changed


def test_no_fd_leaks_on_repeated_success_and_failure(api: Any, layout: Any) -> None:
    path, catalog, execution = layout
    before = len(os.listdir("/proc/self/fd"))
    for _ in range(8):
        api.read_toolchain_configuration(path, deadline=time.monotonic() + 5)
        api.measure_toolchain_spaces(
            catalog, execution, execution_owner_uid=0, deadline=time.monotonic() + 5
        )
        path.chmod(0o644)
        with pytest.raises(api.ToolchainConfigurationError):
            api.read_toolchain_configuration(path, deadline=time.monotonic() + 5)
        path.chmod(0o600)
    assert len(os.listdir("/proc/self/fd")) == before
