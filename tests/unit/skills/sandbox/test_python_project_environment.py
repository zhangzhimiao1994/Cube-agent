"""Dummy bytes only: never run an environment interpreter or package hook."""

from __future__ import annotations

import copy
import hashlib
import importlib
import importlib.util
import json
import os
import stat
import sys
import traceback
from collections.abc import Mapping
from dataclasses import FrozenInstanceError
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from agent_hub.capability_installer.python_attestation import ScanLimits

INPUT = "a" * 64
LIMITS = ScanLimits(1024 * 1024)
native = pytest.mark.skipif(sys.platform != "linux", reason="requires real Linux rooted FDs")


def test_observation_api_is_available() -> None:
    name = "agent_hub.skills.sandbox.python_project_environment"
    assert importlib.util.find_spec(name) is not None, "environment observation is missing"


@pytest.fixture
def api() -> Any:
    name = "agent_hub.skills.sandbox.python_project_environment"
    assert importlib.util.find_spec(name) is not None, "environment observation is missing"
    return importlib.import_module(name)


def manifest() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "entries": [
            {"path": ".", "kind": "directory", "identity": [1, 2, stat.S_IFDIR | 0o700]},
            {
                "path": "pkg+name with space.py",
                "kind": "file",
                "identity": [1, 3, stat.S_IFREG | 0o600, 1, 3, 4, 5],
                "sha256": "b" * 64,
            },
        ],
        "total_bytes": 3,
        "interpreter": "/trusted/runtime/python",
        "interpreter_receipt": None,
    }


def tree(root: Path) -> Path:
    root.mkdir(mode=0o700)
    (root / "bin").mkdir(mode=0o700)
    (root / "bin" / "python").write_bytes(b"dummy interpreter; do not execute")
    (root / "pkg").mkdir(mode=0o700)
    (root / "pkg" / "__init__.py").write_bytes(b"dummy source; do not import")
    return root


def observe(api: Any, root: Path, **kwargs: Any) -> Any:
    return api.observe_python_project_environment(
        root,
        interpreter=root / "bin" / "python",
        environment_input_sha256=INPUT,
        limits=LIMITS,
        **kwargs,
    )


def bytecode(root: Path, name: str = "__init__.cpython-312.pyc") -> Path:
    cache = root / "pkg" / "__pycache__"
    cache.mkdir(exist_ok=True, mode=0o700)
    result = cache / name
    result.write_bytes(b"non-executable dummy bytecode")
    return result


def test_projection_ignores_location_and_identity_clocks_but_binds_bytes(api: Any) -> None:
    first, second = manifest(), manifest()
    second["interpreter"] = "/different/location/python"
    second["entries"][0]["identity"] = [9, 10, stat.S_IFDIR | 0o755]
    second["entries"][1]["identity"] = [9, 11, stat.S_IFREG | 0o644, 1, 3, 77, 88]
    sha = api.python_environment_content_sha256(first, environment_input_sha256=INPUT)
    assert sha == api.python_environment_content_sha256(second, environment_input_sha256=INPUT)
    second["entries"][1]["sha256"] = "c" * 64
    assert sha != api.python_environment_content_sha256(second, environment_input_sha256=INPUT)
    assert sha != api.python_environment_content_sha256(first, environment_input_sha256="d" * 64)


@pytest.mark.parametrize("change", ["execute", "path", "directory", "kind", "link"])
def test_projection_binds_each_semantic_member(api: Any, change: str) -> None:
    first, second = manifest(), manifest()
    entry = second["entries"][1]
    if change == "execute":
        entry["identity"][2] |= 0o100
    elif change == "path":
        entry["path"] = "other.py"
    elif change == "directory":
        second["entries"].append(
            {"path": "empty", "kind": "directory", "identity": [1, 4, stat.S_IFDIR | 0o700]}
        )
    elif change == "kind":
        second["entries"][1] = {
            "path": entry["path"],
            "kind": "directory",
            "identity": [1, 3, stat.S_IFDIR | 0o700],
        }
    else:
        second["entries"][1] = {
            "path": "lib64",
            "kind": "symlink",
            "target": "lib",
            "identity": [1, 3, stat.S_IFLNK | 0o777, 1, 3, 4, 5],
        }
    assert api.python_environment_content_sha256(first, environment_input_sha256=INPUT) != (
        api.python_environment_content_sha256(second, environment_input_sha256=INPUT)
    )


def test_projection_is_order_independent_and_binds_literal_symlink(api: Any) -> None:
    first = manifest()
    first["entries"].append(
        {
            "path": "lib64",
            "kind": "symlink",
            "target": "lib",
            "identity": [1, 4, stat.S_IFLNK | 0o777, 1, 3, 4, 5],
        }
    )
    second = copy.deepcopy(first)
    second["entries"].reverse()
    digest = api.python_environment_content_sha256(first, environment_input_sha256=INPUT)
    assert digest == api.python_environment_content_sha256(second, environment_input_sha256=INPUT)
    second["entries"][0]["target"] = "./lib"
    assert digest != api.python_environment_content_sha256(second, environment_input_sha256=INPUT)


@pytest.mark.parametrize(
    "path",
    [
        "",
        "/absolute",
        "../escape",
        "a/../b",
        "a//b",
        "a\\b",
        "a\0b",
        "a/",
        "a/./b",
        "C:/host",
        "x" * 4097,
    ],
)
def test_projection_rejects_unsafe_paths_without_echo(api: Any, path: str) -> None:
    value = manifest()
    value["entries"][1]["path"] = path
    with pytest.raises(ValueError, match="^python_project_environment_rejected$"):
        api.python_environment_content_sha256(value, environment_input_sha256=INPUT)


@pytest.mark.parametrize("sha", ["", "A" * 64, "a" * 63, "g" * 64, True, None])
def test_strict_input_digest(api: Any, sha: object) -> None:
    with pytest.raises(ValueError, match="^python_project_environment_rejected$"):
        api.python_environment_content_sha256(manifest(), environment_input_sha256=sha)


@pytest.mark.parametrize(
    "case",
    [
        "not-mapping",
        "entries",
        "duplicate",
        "no-root",
        "mode",
        "identity",
        "sha",
        "hardlink",
        "kind",
        "pyc",
        "metadata",
    ],
)
def test_projection_rejects_malformed_or_incomplete_receipts(api: Any, case: str) -> None:
    value: Any = manifest()
    if case == "not-mapping":
        value = []
    elif case == "entries":
        value["entries"] = "not entries"
    elif case == "duplicate":
        value["entries"].append(copy.deepcopy(value["entries"][1]))
    elif case == "no-root":
        value["entries"].pop(0)
    elif case == "mode":
        value["entries"][1]["identity"][2] = True
    elif case == "identity":
        value["entries"][1]["identity"] = [1, 2, 3]
    elif case == "sha":
        value["entries"][1]["sha256"] = "invalid"
    elif case == "hardlink":
        value["entries"][1]["identity"][3] = 2
    elif case == "kind":
        value["entries"][1]["kind"] = "fifo"
    elif case == "pyc":
        value["entries"][1]["path"] = "payload.pyc"
    else:
        value["entries"][1]["path"] = "environment.json"
    with pytest.raises(ValueError, match="^python_project_environment_rejected$"):
        api.python_environment_content_sha256(value, environment_input_sha256=INPUT)


def test_real_dummy_trees_have_equal_semantics_and_distinct_security_receipts(
    api: Any,
    tmp_path: Path,
) -> None:
    first, second = tree(tmp_path / "one"), tree(tmp_path / "two")
    os.utime(second / "pkg" / "__init__.py", ns=(1000000000, 1000000000))
    a, b = observe(api, first), observe(api, second)
    assert a.environment_sha256 == b.environment_sha256
    assert a.security_manifest != b.security_manifest
    assert a.total_bytes == sum(p.stat().st_size for p in first.rglob("*") if p.is_file())
    assert a.removed_bytecode_files == 0
    assert isinstance(a.security_manifest, Mapping)
    with pytest.raises(TypeError):
        a.security_manifest["entries"][0]["path"] = "forged"
    with pytest.raises(FrozenInstanceError):
        a.environment_sha256 = "f" * 64
    (second / "pkg" / "__init__.py").write_bytes(b"different dummy source")
    assert a.environment_sha256 != observe(api, second).environment_sha256


@pytest.mark.parametrize("name", ["environment.json", "python-tree-attestation.json"])
def test_ignored_root_controls_are_rejected_even_as_directories(
    api: Any,
    tmp_path: Path,
    name: str,
) -> None:
    root = tree(tmp_path / "env")
    control = root / name
    control.write_bytes(b"must not be ignored")
    with pytest.raises(ValueError):
        observe(api, root)
    control.unlink()
    control.mkdir()
    with pytest.raises(ValueError):
        observe(api, root)


def test_published_bytecode_is_rejected_without_mutation(api: Any, tmp_path: Path) -> None:
    root = tree(tmp_path / "env")
    pyc = bytecode(root)
    with pytest.raises(ValueError):
        observe(api, root)
    assert pyc.read_bytes() == b"non-executable dummy bytecode"


@pytest.mark.parametrize(
    "field,value",
    [
        ("root", "host/private"),
        ("root", Path("relative")),
        ("interpreter", "host/private"),
        ("interpreter", Path("relative")),
        ("limits", None),
        ("limits", ScanLimits(True)),
        ("limits", ScanLimits(-1)),
        ("limits", ScanLimits(10, max_depth=0)),
        ("limits", ScanLimits(10, max_entries=True)),
        ("limits", ScanLimits(10, max_path_bytes=0)),
        ("normalize_bytecode", 1),
        ("environment_input_sha256", "Z" * 64),
    ],
)
def test_invalid_inputs_are_finite_and_path_free(
    api: Any,
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    args: dict[str, object] = {
        "root": tmp_path / "private",
        "interpreter": tmp_path / "private-python",
        "limits": LIMITS,
        "environment_input_sha256": INPUT,
        "normalize_bytecode": False,
    }
    args[field] = value
    with pytest.raises(ValueError, match="^python_project_environment_rejected$"):
        api.observe_python_project_environment(**args)


def test_missing_tree_exception_trace_does_not_expose_hostpath(api: Any, tmp_path: Path) -> None:
    with pytest.raises(ValueError) as caught:
        observe(api, tmp_path / "private-host-secret")
    assert str(tmp_path) not in "".join(traceback.format_exception(caught.value))
    assert caught.value.__cause__ is None


def test_scan_limits_are_applied_to_actual_bytes(api: Any, tmp_path: Path) -> None:
    root = tree(tmp_path / "env")
    with pytest.raises(ValueError, match="^python_project_environment_rejected$"):
        api.observe_python_project_environment(
            root,
            interpreter=root / "bin" / "python",
            limits=ScanLimits(1),
            environment_input_sha256=INPUT,
        )


@pytest.mark.parametrize(
    "limits",
    [
        ScanLimits(1024, max_entries=1),
        ScanLimits(1024, max_depth=1),
        ScanLimits(1024, max_path_bytes=3),
    ],
)
def test_traversal_quotas_apply_to_actual_tree(
    api: Any,
    tmp_path: Path,
    limits: ScanLimits,
) -> None:
    root = tree(tmp_path / "env")
    with pytest.raises(ValueError, match="^python_project_environment_rejected$"):
        api.observe_python_project_environment(
            root, interpreter=root / "bin" / "python", limits=limits, environment_input_sha256=INPUT
        )


@pytest.mark.parametrize("name", ["environment.json", "python-tree-attestation.json"])
def test_controls_added_during_scan_cannot_hide_in_ignored_names(
    api: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
) -> None:
    root = tree(tmp_path / "env")
    scan = api.scan_tree

    def insert(*args: Any, **kwargs: Any) -> Any:
        receipt = scan(*args, **kwargs)
        (root / name).write_bytes(b"inserted after scan")
        return receipt

    monkeypatch.setattr(api, "scan_tree", insert)
    with pytest.raises(ValueError):
        observe(api, root)


def test_external_interpreter_content_is_bound_without_its_location(api: Any) -> None:
    first, second = manifest(), manifest()
    first["interpreter_receipt"] = {
        "sha256": "c" * 64,
        "identity": [1, 2, stat.S_IFREG | 0o700, 1, 10, 11, 12],
    }
    second["interpreter_receipt"] = copy.deepcopy(first["interpreter_receipt"])
    second["interpreter_receipt"]["identity"][1] = 99
    second["interpreter"] = "/another/trusted/interpreter"
    digest = api.python_environment_content_sha256(first, environment_input_sha256=INPUT)
    assert digest == api.python_environment_content_sha256(second, environment_input_sha256=INPUT)
    second["interpreter_receipt"]["sha256"] = "d" * 64
    assert digest != api.python_environment_content_sha256(second, environment_input_sha256=INPUT)


def test_measurement_only_reads_dummy_tree_bytes(api: Any, tmp_path: Path) -> None:
    root = tree(tmp_path / "env")
    (root / "pkg+name with space").mkdir(mode=0o700)
    (root / "pkg+name with space" / "payload.py").write_bytes(b"not valid Python")
    result = observe(api, root)
    assert any(
        e["path"] == "pkg+name with space/payload.py" for e in result.security_manifest["entries"]
    )


def test_native_normalization_rejects_windows_without_deleting(
    api: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tree(tmp_path / "env")
    pyc = bytecode(root)
    monkeypatch.setattr(api.sys, "platform", "win32")
    with pytest.raises(ValueError):
        observe(api, root, normalize_bytecode=True)
    assert pyc.exists()


@native
def test_linux_removes_only_cpython312_generated_pyc_and_rescans(
    api: Any,
    tmp_path: Path,
) -> None:
    root = tree(tmp_path / "env")
    pycs = [bytecode(root, f"__init__.cpython-312{opt}.pyc") for opt in ("", ".opt-1", ".opt-2")]
    before_size = sum(p.stat().st_size for p in root.rglob("*") if p.is_file())
    removed_size = sum(p.stat().st_size for p in pycs)
    result = observe(api, root, normalize_bytecode=True)
    assert result.removed_bytecode_files == 3
    assert result.total_bytes == before_size - removed_size
    assert all(not p.exists() for p in pycs)
    assert (root / "pkg" / "__pycache__").is_dir()
    assert result.environment_sha256 == observe(api, root).environment_sha256


@native
@pytest.mark.parametrize(
    "name",
    [
        "__init__.cpython-311.pyc",
        "__init__.pyc",
        "orphan.cpython-312.pyc",
        "__init__.cpython-312.opt-x.pyc",
        "__init__.cpython-312.opt-0.pyc",
        "__init__.cpython-312.opt-3.pyc",
    ],
)
def test_linux_preflights_all_unknown_or_sourceless_bytecode_before_deleting(
    api: Any,
    tmp_path: Path,
    name: str,
) -> None:
    root = tree(tmp_path / "env")
    good, bad = bytecode(root), bytecode(root, name)
    with pytest.raises(ValueError):
        observe(api, root, normalize_bytecode=True)
    assert good.exists() and bad.exists()


@native
@pytest.mark.parametrize("kind", ["symlink", "hardlink"])
def test_linux_never_deletes_linked_bytecode(api: Any, tmp_path: Path, kind: str) -> None:
    root = tree(tmp_path / "env")
    pyc = bytecode(root)
    payload = tmp_path / "user-data"
    payload.write_bytes(b"user payload")
    pyc.unlink()
    if kind == "symlink":
        pyc.symlink_to(payload)
    else:
        os.link(payload, pyc)
    with pytest.raises(ValueError):
        observe(api, root, normalize_bytecode=True)
    assert payload.read_bytes() == b"user payload"
    assert pyc.is_symlink() or pyc.exists()


@native
@pytest.mark.parametrize("change", ["pyc", "source", "parent", "mode", "metadata"])
def test_linux_detects_replacement_after_initial_scan_without_unlinking_user_data(
    api: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    change: str,
) -> None:
    root = tree(tmp_path / "env")
    pyc = bytecode(root)
    scan = api.scan_tree
    fired = False

    def replace(*args: Any, **kwargs: Any) -> Any:
        nonlocal fired
        receipt = scan(*args, **kwargs)
        if not fired:
            fired = True
            if change == "pyc":
                pyc.unlink()
                pyc.write_bytes(b"user replacement")
            elif change == "source":
                (root / "pkg" / "__init__.py").write_bytes(b"new source")
            elif change == "parent":
                pyc.parent.rename(root / "pkg" / "saved-cache")
                pyc.parent.mkdir(mode=0o700)
                pyc.write_bytes(b"user replacement")
            elif change == "mode":
                pyc.parent.chmod(0o755)
            else:
                (root / "environment.json").write_bytes(b"hidden control")
        return receipt

    monkeypatch.setattr(api, "scan_tree", replace)
    with pytest.raises(ValueError):
        observe(api, root, normalize_bytecode=True)
    assert pyc.exists()


@native
def test_linux_lib64_alias_is_measured_without_traversal(api: Any, tmp_path: Path) -> None:
    root = tree(tmp_path / "env")
    (root / "lib").mkdir(mode=0o700)
    (root / "lib64").symlink_to("lib", target_is_directory=True)
    result = observe(api, root, normalize_bytecode=True)
    assert any(
        e["path"] == "lib64" and e["target"] == "lib" for e in result.security_manifest["entries"]
    )


@native
def test_linux_retry_identity_ignores_generated_bytecode_timestamps(
    api: Any,
    tmp_path: Path,
) -> None:
    first, second = tree(tmp_path / "one"), tree(tmp_path / "two")
    bytecode(first).write_bytes(b"timestamp-one")
    bytecode(second).write_bytes(b"different-timestamp-two")
    assert observe(api, first, normalize_bytecode=True).environment_sha256 == (
        observe(api, second, normalize_bytecode=True).environment_sha256
    )


@native
def test_linux_refuses_pyc_outside_cache_directory(api: Any, tmp_path: Path) -> None:
    root = tree(tmp_path / "env")
    good = bytecode(root)
    bad = root / "pkg" / "__init__.cpython-312.pyc"
    bad.write_bytes(b"wheel payload")
    with pytest.raises(ValueError):
        observe(api, root, normalize_bytecode=True)
    assert good.exists() and bad.read_bytes() == b"wheel payload"


@native
@pytest.mark.parametrize("change", ["residual-pyc", "source", "root-mode", "root-replaced"])
def test_linux_final_scan_refuses_changes_after_authorized_unlink(
    api: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    change: str,
) -> None:
    root = tree(tmp_path / "env")
    bytecode(root)
    unlink = api.os.unlink

    def inject(path: Any, *, dir_fd: int | None = None) -> None:
        unlink(path, dir_fd=dir_fd)
        if change == "residual-pyc":
            (root / "payload.pyc").write_bytes(b"new payload")
        elif change == "source":
            (root / "pkg" / "__init__.py").write_bytes(b"changed source")
        elif change == "root-mode":
            root.chmod(0o755)
        else:
            root.rename(tmp_path / "old-env")
            tree(root)

    monkeypatch.setattr(api.os, "unlink", inject)
    with pytest.raises(ValueError):
        observe(api, root, normalize_bytecode=True)


@native
def test_linux_does_not_follow_replaced_ancestor_to_user_data(
    api: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tree(tmp_path / "env")
    pyc = bytecode(root)
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o700)
    payload = outside / pyc.name
    payload.write_bytes(b"user data")
    scan = api.scan_tree

    def replace(*args: Any, **kwargs: Any) -> Any:
        receipt = scan(*args, **kwargs)
        pyc.parent.rename(root / "pkg" / "old-cache")
        pyc.parent.symlink_to(outside, target_is_directory=True)
        return receipt

    monkeypatch.setattr(api, "scan_tree", replace)
    with pytest.raises(ValueError):
        observe(api, root, normalize_bytecode=True)
    assert payload.read_bytes() == b"user data"


@pytest.mark.parametrize("suffix", [".opt-0", ".opt-3", ".opt-12", ".opt-01"])
def test_only_cpython_optimization_levels_one_and_two_are_disposable(
    api: Any,
    suffix: str,
) -> None:
    # A pure filename rule test, not native deletion coverage.
    assert api._CACHE.fullmatch(f"foo.cpython-312{suffix}.pyc") is None


def large_receipt(interpreter: Path) -> dict[str, Any]:
    value = manifest()
    value["interpreter"] = str(interpreter)
    value["entries"] = value["entries"][:1]
    for number in range(7000):
        value["entries"].append(
            {
                "path": f"{number:05d}-" + "x" * 190 + ".py",
                "kind": "file",
                "identity": [1, number + 3, stat.S_IFREG | 0o600, 1, 0, 4, 5],
                "sha256": hashlib.sha256(b"").hexdigest(),
            }
        )
    value["total_bytes"] = 0
    return value


def test_injected_scan_receipt_above_2mb_is_frozen_with_internal_scan_budgets(
    api: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Scanner injection isolates evidence storage; this is NOT an actual FS observation.
    root = tree(tmp_path / "env")
    value = large_receipt(root / "bin" / "python")
    assert len(json.dumps(value).encode("utf-8")) > 2_000_000
    from agent_hub.runtime.contracts import _freeze_object

    with pytest.raises(ValueError, match="serialized size limit"):
        _freeze_object(value, name="public artifact", max_nodes=7001 * 32 + 64)

    def scan(path: Path, *, interpreter: Path, limits: ScanLimits) -> Any:
        assert path == root and interpreter == root / "bin" / "python"
        assert limits == LIMITS
        return value

    monkeypatch.setattr(api, "scan_tree", scan)
    result = observe(api, root)
    frozen = result.security_manifest
    assert len(frozen["entries"]) == 7001
    with pytest.raises(TypeError):
        frozen["entries"][1]["identity"][0] = 99
    with pytest.raises(TypeError):
        frozen["entries"][1]["path"] = "changed"
    value["entries"][1]["identity"][0] = 99
    value["entries"].clear()
    assert len(frozen["entries"]) == 7001
    assert frozen["entries"][1]["identity"][0] == 1


@pytest.mark.parametrize(
    "case",
    [
        "members",
        "path",
        "depth",
        "bytes",
        "total-type",
        "size-mismatch",
        "extra-root",
        "extra-entry",
        "extra-interpreter",
        "bad-interpreter",
        "large-integer",
    ],
)
def test_internal_evidence_rejects_excess_budgets_and_non_scanner_shapes(
    api: Any,
    tmp_path: Path,
    case: str,
) -> None:
    value = manifest()
    value["interpreter"] = str(tmp_path / "python")
    limits = LIMITS
    if case == "members":
        limits = ScanLimits(1024, max_entries=1)
        value["entries"].append(
            {"path": "empty", "kind": "directory", "identity": [1, 4, stat.S_IFDIR | 0o700]}
        )
    elif case == "path":
        limits = ScanLimits(1024, max_path_bytes=2)
    elif case == "depth":
        limits = ScanLimits(1024, max_depth=1)
        value["entries"].append(
            {"path": "empty", "kind": "directory", "identity": [1, 4, stat.S_IFDIR | 0o700]}
        )
        value["entries"][1]["path"] = "empty/file.py"
    elif case == "bytes":
        limits = ScanLimits(2)
    elif case == "total-type":
        value["total_bytes"] = True
    elif case == "size-mismatch":
        value["total_bytes"] = 0
    elif case == "extra-root":
        value["unbounded"] = {"nested": value}
    elif case == "extra-entry":
        value["entries"][1]["unbounded"] = {"nested": value}
    elif case == "extra-interpreter":
        value["interpreter_receipt"] = {
            "identity": [1, 2, stat.S_IFREG | 0o600, 1, 0, 4, 5],
            "sha256": "a" * 64,
            "unexpected": value,
        }
    elif case == "bad-interpreter":
        value["interpreter"] = {"nested": value}
    else:
        value["entries"][1]["identity"][1] = 1 << 1024
    with pytest.raises(ValueError, match="^python_project_environment_rejected$"):
        api._freeze_security_manifest(value, limits=limits)


class DescriptorTree:
    """Virtual OS for FD lifetime checks only; no native security claim."""

    O_RDONLY = 0
    O_DIRECTORY = 1
    O_NOFOLLOW = 2
    O_CLOEXEC = 4
    O_NONBLOCK = 8

    def __init__(self, count: int) -> None:
        self.entries: dict[str, dict[str, Any]] = {}
        self.contents: dict[str, bytes] = {}
        self.descriptors: dict[int, tuple[str, int]] = {3: (".", 0)}
        self.next_fd = 4
        self.peak = 1
        self.unlinked: list[str] = []
        self.owners: dict[str, int] = {}
        self.add(".", "directory")
        for number in range(count):
            directory = f"pkg{number:04d}"
            self.add(directory, "directory")
            self.add(f"{directory}/__pycache__", "directory")
            self.add(f"{directory}/foo.py", "file", b"dummy source")
            self.add(f"{directory}/__pycache__/foo.cpython-312.pyc", "file", b"dummy pyc")

    def add(self, path: str, kind: str, data: bytes = b"") -> None:
        mode = stat.S_IFDIR | 0o700 if kind == "directory" else stat.S_IFREG | 0o600
        identity = [1, len(self.entries) + 2, mode]
        entry: dict[str, Any] = {"path": path, "kind": kind, "identity": identity}
        if kind == "file":
            identity.extend([1, len(data), 4, 5])
            entry["sha256"] = hashlib.sha256(data).hexdigest()
            self.contents[path] = data
        self.entries[path] = entry

    @staticmethod
    def getuid() -> int:
        return 1000

    def member(self, name: str, parent: int) -> str:
        path = self.descriptors[parent][0]
        return name if path == "." else f"{path}/{name}"

    def info(self, path: str) -> Any:
        identity = self.entries[path]["identity"]
        extended = identity if len(identity) == 7 else [*identity, 2, 0, 4, 5]
        return SimpleNamespace(
            **dict(
                zip(
                    (
                        "st_dev",
                        "st_ino",
                        "st_mode",
                        "st_nlink",
                        "st_size",
                        "st_mtime_ns",
                        "st_ctime_ns",
                    ),
                    extended,
                    strict=True,
                )
            ),
            st_uid=self.owners.get(path, 1000),
            st_gid=1000,
        )

    def fstat(self, descriptor: int) -> Any:
        return self.info(self.descriptors[descriptor][0])

    def stat(self, name: str, *, dir_fd: int, follow_symlinks: bool) -> Any:
        assert follow_symlinks is False
        return self.info(self.member(name, dir_fd))

    def open(self, name: str, flags: int, *, dir_fd: int) -> int:
        assert flags & self.O_NOFOLLOW
        if len(self.descriptors) >= 64:
            raise OSError(24, "virtual descriptor ceiling")
        path = self.member(name, dir_fd)
        assert path in self.entries
        descriptor = self.next_fd
        self.next_fd += 1
        self.descriptors[descriptor] = path, 0
        self.peak = max(self.peak, len(self.descriptors))
        return descriptor

    def close(self, descriptor: int) -> None:
        del self.descriptors[descriptor]

    def read(self, descriptor: int, size: int) -> bytes:
        path, offset = self.descriptors[descriptor]
        data = self.contents[path][offset : offset + size]
        self.descriptors[descriptor] = path, offset + len(data)
        return data

    def unlink(self, name: str, *, dir_fd: int) -> None:
        path = self.member(name, dir_fd)
        assert path.endswith("foo.cpython-312.pyc")
        self.unlinked.append(path)
        del self.entries[path]
        del self.contents[path]

    def scan(self, root: Path, *, interpreter: Path, limits: ScanLimits) -> Any:
        return {
            "schema_version": 1,
            "entries": copy.deepcopy(list(self.entries.values())),
            "total_bytes": sum(len(data) for data in self.contents.values()),
            "interpreter": str(interpreter),
            "interpreter_receipt": None,
        }


@pytest.mark.parametrize(
    "count,fault",
    [
        (5, None),
        (1000, None),
        (1000, "preflight-sha"),
        (1000, "delete-sha"),
        (1000, "unlink-error"),
        (1000, "directory-owner"),
        (1000, "preflight-target"),
    ],
)
def test_normalization_fd_window_is_bounded_independently_of_file_count(
    api: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    count: int,
    fault: str | None,
) -> None:
    virtual = DescriptorTree(count)

    class Pin:
        def __init__(self, parent: int, name: str, descriptor: int, identity: Any) -> None:
            self.parent, self.name, self.descriptor, self.identity = (
                parent,
                name,
                descriptor,
                identity,
            )

        def check(self) -> None:
            for info in (
                virtual.fstat(self.descriptor),
                virtual.stat(
                    self.name,
                    dir_fd=self.parent,
                    follow_symlinks=False,
                ),
            ):
                assert (
                    info.st_dev,
                    info.st_ino,
                    info.st_mode,
                    info.st_uid,
                    info.st_gid,
                ) == self.identity

    def identity(info: Any) -> list[int]:
        return [
            info.st_dev,
            info.st_ino,
            info.st_mode,
            info.st_nlink,
            info.st_size,
            info.st_mtime_ns,
            info.st_ctime_ns,
        ]

    def scan(*args: Any, **kwargs: Any) -> Any:
        snapshot = virtual.scan(*args, **kwargs)
        if fault == "preflight-sha":
            virtual.contents[f"pkg{count - 1:04d}/foo.py"] = b"changed data"
        elif fault == "preflight-target":
            target = virtual.entries[f"pkg{count - 1:04d}/__pycache__/foo.cpython-312.pyc"]
            target["identity"][3] = 2
        return snapshot

    visit = api._visit_bytecode

    def window(*args: Any, **kwargs: Any) -> None:
        visit(*args, **kwargs)
        if not kwargs["remove"] and args[3].startswith(f"pkg{count - 1:04d}/"):
            if fault == "delete-sha":
                virtual.contents["pkg0000/foo.py"] = b"changed data"
            elif fault == "directory-owner":
                virtual.owners["pkg0000"] = 0

    def failed_unlink(*args: Any, **kwargs: Any) -> None:
        raise OSError("simulated unlink failure")

    monkeypatch.setattr(api, "os", virtual)
    monkeypatch.setattr(api, "sys", SimpleNamespace(platform="linux"))
    monkeypatch.setattr(api, "_Pinned", Pin)
    monkeypatch.setattr(api, "_identity", identity)
    monkeypatch.setattr(api, "scan_tree", scan)
    monkeypatch.setattr(api, "_pin_directory", lambda *args: 3)
    monkeypatch.setattr(api, "_visit_bytecode", window)
    if fault == "unlink-error":
        monkeypatch.setattr(virtual, "unlink", failed_unlink)
    if fault is None:
        result = observe(api, tmp_path / "env", normalize_bytecode=True)
        assert result.removed_bytecode_files == count
        assert len(virtual.unlinked) == count
        assert result.total_bytes == count * len(b"dummy source")
    else:
        with pytest.raises(ValueError, match="^python_project_environment_rejected$"):
            observe(api, tmp_path / "env", normalize_bytecode=True)
        assert not virtual.unlinked
        assert all(
            f"pkg{number:04d}/__pycache__/foo.cpython-312.pyc" in virtual.entries
            for number in range(count)
        )
    assert virtual.peak <= 12
    assert set(virtual.descriptors) == {3}


@native
def test_linux_thousand_bytecode_files_fit_lowered_nofile_limit(api: Any, tmp_path: Path) -> None:
    if sys.platform != "linux":
        pytest.skip("requires real Linux resource limits and rooted FDs")
    import resource

    root = tree(tmp_path / "env")
    cache = root / "pkg" / "__pycache__"
    cache.mkdir(mode=0o700)
    for number in range(1000):
        (root / "pkg" / f"foo{number}.py").write_bytes(b"dummy source")
        (cache / f"foo{number}.cpython-312.pyc").write_bytes(b"dummy pyc")
    old_soft, old_hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    new_soft = min(old_soft, 128)
    if new_soft < 64:
        pytest.skip("test process has insufficient descriptor headroom")
    try:
        resource.setrlimit(resource.RLIMIT_NOFILE, (new_soft, old_hard))
        result = observe(api, root, normalize_bytecode=True)
    finally:
        resource.setrlimit(resource.RLIMIT_NOFILE, (old_soft, old_hard))
    assert resource.getrlimit(resource.RLIMIT_NOFILE) == (old_soft, old_hard)
    assert result.removed_bytecode_files == 1000
    assert not list(cache.glob("*.pyc"))
