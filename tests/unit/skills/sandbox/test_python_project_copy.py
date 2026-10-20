"""Copy dummy bytes only; native FD/UID guarantees require actual Linux."""

from __future__ import annotations

import copy
import hashlib
import importlib
import importlib.util
import inspect
import os
import stat
import sys
import time
import traceback
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from agent_hub.capability_installer.python_attestation import ScanLimits
from agent_hub.skills.sandbox.python_project_environment import observe_python_project_environment

INPUT = "a" * 64
EXECUTABLE = b"dummy interpreter bytes; never execute"
SHA = hashlib.sha256(EXECUTABLE).hexdigest()
LIMITS = ScanLimits(1024 * 1024)
native = pytest.mark.skipif(sys.platform != "linux", reason="requires real Linux rooted FDs/UIDs")


def test_copy_api_exists() -> None:
    assert importlib.util.find_spec("agent_hub.skills.sandbox.python_project_copy") is not None


@pytest.fixture
def api() -> Any:
    name = "agent_hub.skills.sandbox.python_project_copy"
    assert importlib.util.find_spec(name) is not None, "bounded environment copy helper is missing"
    return importlib.import_module(name)


def receipt() -> dict[str, Any]:
    entries: list[dict[str, Any]] = []
    for path in (".", "bin", "lib"):
        entries.append(
            {"path": path, "kind": "directory", "identity": [1, len(entries), stat.S_IFDIR | 0o555]}
        )
    for name in ("python", "python3", "python3.12"):
        entries.append(
            {
                "path": f"bin/{name}",
                "kind": "file",
                "identity": [1, len(entries), stat.S_IFREG | 0o555, 1, len(EXECUTABLE), 7, 8],
                "sha256": SHA,
            }
        )
    return {
        "schema_version": 1,
        "entries": entries,
        "total_bytes": 3 * len(EXECUTABLE),
        "interpreter": "/trusted/runtime/python",
        "interpreter_receipt": None,
    }


def test_api_has_only_the_trusted_copy_arguments(api: Any) -> None:
    signature = inspect.signature(api.copy_python_project_environment)
    assert tuple(signature.parameters) == (
        "source",
        "destination",
        "source_owner",
        "interpreter",
        "interpreter_sha256",
        "environment_input_sha256",
        "expected_environment_sha256",
        "limits",
        "deadline",
    )
    assert all(
        parameter.kind is inspect.Parameter.KEYWORD_ONLY
        for parameter in list(signature.parameters.values())[2:]
    )


def test_observation_cannot_select_an_external_interpreter(api: Any) -> None:
    assert "interpreter" not in inspect.signature(api._observe).parameters


@pytest.mark.parametrize("reason", ["copy_limit", "copy_timeout", "copy_unsafe"])
def test_error_has_a_finite_path_free_reason(api: Any, reason: str) -> None:
    error = api.PythonProjectCopyError(reason)
    assert error.reason == reason and str(error) == reason
    assert isinstance(error, ValueError)
    assert str(api.PythonProjectCopyError("/private/source secret")) == "copy_invalid"


@pytest.mark.parametrize(
    "change",
    [
        "missing-python",
        "missing-python3",
        "missing-python3.12",
        "hash",
        "not-executable",
        "hardlink",
        "interpreter-link",
        "other-link",
        "wrong-lib64",
        "dangling-lib64",
        "pyc",
        "control-manifest",
        "control-metadata",
        "external-receipt",
    ],
)
def test_first_copies_recipe_rejects_unsafe_metadata(api: Any, change: str) -> None:
    value = receipt()
    if change.startswith("missing-"):
        value["entries"] = [e for e in value["entries"] if e["path"] != "bin/" + change[8:]]
    elif change in {"hash", "not-executable", "hardlink"}:
        entry = value["entries"][-1]
        if change == "hash":
            entry["sha256"] = "b" * 64
        elif change == "not-executable":
            entry["identity"][2] = stat.S_IFREG | 0o444
        else:
            entry["identity"][3] = 2
    elif change == "external-receipt":
        value["interpreter_receipt"] = {
            "identity": [1, 9, stat.S_IFREG | 0o555, 1, 1, 2, 3],
            "sha256": SHA,
        }
    elif change in {"pyc", "control-manifest", "control-metadata"}:
        entry = copy.deepcopy(value["entries"][-1])
        entry["path"] = {
            "pyc": "payload.pyc",
            "control-manifest": "python-tree-attestation.json",
            "control-metadata": "environment.json",
        }[change]
        value["entries"].append(entry)
    else:
        path = "bin/python" if change == "interpreter-link" else "lib64"
        if change == "other-link":
            path = "arbitrary"
        value["entries"] = [e for e in value["entries"] if e["path"] != path]
        value["entries"].append(
            {
                "path": path,
                "kind": "symlink",
                "identity": [1, 99, stat.S_IFLNK | 0o777, 1, 3, 7, 8],
                "target": "elsewhere" if change == "wrong-lib64" else "lib",
            }
        )
        if change == "dangling-lib64":
            value["entries"] = [e for e in value["entries"] if e["path"] != "lib"]
    with pytest.raises(api.PythonProjectCopyError):
        api._recipe_entries(value, SHA)


def test_first_recipe_allows_only_literal_lib64_and_preserves_metadata(api: Any) -> None:
    value = receipt()
    value["entries"].append(
        {
            "path": "lib64",
            "kind": "symlink",
            "identity": [1, 99, stat.S_IFLNK | 0o777, 1, 3, 7, 8],
            "target": "lib",
        }
    )
    before = copy.deepcopy(value)
    entries = api._recipe_entries(value, SHA)
    assert set(entries) == {e["path"] for e in value["entries"]}
    assert value == before


def arguments(tmp_path: Path) -> dict[str, Any]:
    return {
        "source": tmp_path / "source",
        "destination": tmp_path / "private" / "pending",
        "source_owner": 1000,
        "interpreter": tmp_path / "runtime" / "python",
        "interpreter_sha256": SHA,
        "environment_input_sha256": INPUT,
        "expected_environment_sha256": "b" * 64,
        "limits": LIMITS,
        "deadline": time.monotonic() + 60,
    }


@pytest.mark.parametrize(
    "field,value",
    [
        ("source_owner", True),
        ("source_owner", -1),
        ("source_owner", 2**32 - 1),
        ("deadline", True),
        ("deadline", float("inf")),
        ("deadline", float("nan")),
        ("deadline", 0),
        ("interpreter_sha256", "A" * 64),
        ("environment_input_sha256", "a" * 63),
        ("expected_environment_sha256", True),
        ("limits", {"max_bytes": 1024}),
        ("limits", ScanLimits(True)),
        ("limits", ScanLimits(1, max_entries=100_001)),
        ("limits", ScanLimits(1, max_depth=65)),
        ("limits", ScanLimits(1, max_path_bytes=4097)),
    ],
)
def test_exact_arguments_fail_before_any_filesystem_access(
    api: Any,
    tmp_path: Path,
    field: str,
    value: Any,
) -> None:
    params = arguments(tmp_path)
    params[field] = value
    with pytest.raises(api.PythonProjectCopyError, match="^copy_invalid$"):
        api.copy_python_project_environment(**params)
    assert not params["destination"].exists()


@pytest.mark.parametrize("case", ["relative", "parent", "equal", "inside-source", "inside-dest"])
def test_paths_are_absolute_canonical_and_disjoint(api: Any, tmp_path: Path, case: str) -> None:
    params = arguments(tmp_path)
    if case == "relative":
        params["source"] = Path("relative")
    elif case == "parent":
        params["source"] = tmp_path / "x" / ".." / "source"
    elif case == "equal":
        params["destination"] = params["source"]
    elif case == "inside-source":
        params["destination"] = params["source"] / "child"
    else:
        params["source"] = params["destination"] / "child"
    with pytest.raises(api.PythonProjectCopyError, match="^copy_invalid$"):
        api.copy_python_project_environment(**params)


def test_non_linux_is_an_explicit_rejection(api: Any, tmp_path: Path) -> None:
    if sys.platform == "linux":
        pytest.skip("tests unsupported host behavior only")
    with pytest.raises(api.PythonProjectCopyError, match="^copy_unsupported$"):
        api.copy_python_project_environment(**arguments(tmp_path))


@pytest.mark.parametrize("change", ["atime", "ctime", "mtime", "uid", "mode", "membership"])
def test_snapshot_comparison_excludes_only_access_time(api: Any, change: str) -> None:
    info = SimpleNamespace(
        st_dev=1,
        st_ino=2,
        st_mode=stat.S_IFREG | 0o444,
        st_uid=1000,
        st_gid=1000,
        st_nlink=1,
        st_size=7,
        st_mtime_ns=8,
        st_ctime_ns=9,
        st_atime_ns=10,
    )
    changed = copy.copy(info)
    left, right = {"file": info}, {"file": changed}
    if change == "membership":
        right.clear()
    else:
        field = {
            "atime": "st_atime_ns",
            "ctime": "st_ctime_ns",
            "mtime": "st_mtime_ns",
            "uid": "st_uid",
            "mode": "st_mode",
        }[change]
        setattr(changed, field, getattr(changed, field) + 1)
    assert api._same_snapshots(left, right) is (change == "atime")


def tree(root: Path) -> Path:
    root.mkdir(mode=0o700)
    (root / "bin").mkdir(mode=0o755)
    for name in ("python", "python3", "python3.12"):
        path = root / "bin" / name
        path.write_bytes(EXECUTABLE)
        path.chmod(0o755)
    (root / "lib").mkdir(mode=0o755)
    (root / "lib" / "module.py").write_bytes(b"dummy module; never import")
    (root / "lib64").symlink_to("lib", target_is_directory=True)
    root.chmod(0o555)
    return root


def native_arguments(tmp_path: Path) -> dict[str, Any]:
    params = arguments(tmp_path)
    root = tree(params["source"])
    params["source_owner"] = linux_uid()
    params["destination"].parent.mkdir(mode=0o700)
    params["interpreter"].parent.mkdir(mode=0o700)
    params["interpreter"].write_bytes(EXECUTABLE)
    params["interpreter"].chmod(0o555)
    params["expected_environment_sha256"] = observe_python_project_environment(
        root,
        interpreter=params["interpreter"],
        environment_input_sha256=INPUT,
        limits=LIMITS,
    ).environment_sha256
    return params


def linux_uid() -> int:
    if sys.platform != "linux":
        pytest.skip("requires native Linux euid")
    return os.geteuid()


@native
def test_linux_copies_new_identities_and_seals_every_member(api: Any, tmp_path: Path) -> None:
    params = native_arguments(tmp_path)
    result = api.copy_python_project_environment(**params)
    actual = observe_python_project_environment(
        params["destination"],
        interpreter=params["destination"] / "bin" / "python",
        environment_input_sha256=INPUT,
        limits=LIMITS,
    )
    assert result == actual
    assert result.security_manifest["interpreter"] == str(params["destination"] / "bin" / "python")
    assert result.security_manifest["interpreter_receipt"] is None
    assert result.environment_sha256 == params["expected_environment_sha256"]
    assert result.removed_bytecode_files == 0
    assert (params["destination"] / "lib64").readlink() == Path("lib")
    assert stat.S_IMODE(params["destination"].parent.stat().st_mode) == 0o700
    for entry in result.security_manifest["entries"]:
        target = params["destination"] / entry["path"]
        info = target.lstat()
        assert info.st_uid == linux_uid()
        assert info.st_ino != (params["source"] / entry["path"]).lstat().st_ino
        if entry["kind"] != "symlink":
            assert stat.S_IMODE(info.st_mode) == (0o555 if info.st_mode & 0o111 else 0o444)
    with pytest.raises(TypeError):
        result.security_manifest["entries"][0]["path"] = "changed"


@native
@pytest.mark.parametrize(
    "change",
    [
        "owner",
        "root-mode",
        "bin-symlink",
        "other-symlink",
        "hardlink",
        "pyc",
        "control",
        "hash",
        "private-parent",
        "ancestor-link",
        "destination-exists",
    ],
)
def test_linux_rejects_unsafe_source_or_destination(
    api: Any,
    tmp_path: Path,
    change: str,
) -> None:
    params = native_arguments(tmp_path)
    root = params["source"]
    if change == "owner":
        params["source_owner"] += 1
    elif change == "root-mode":
        root.chmod(0o755)
    elif change == "bin-symlink":
        (root / "bin" / "python").unlink()
        (root / "bin" / "python").symlink_to(params["interpreter"])
    elif change == "other-symlink":
        (root / "lib" / "link").symlink_to("module.py")
    elif change == "hardlink":
        os.link(root / "bin" / "python", root / "bin" / "hardlink")
    elif change == "pyc":
        (root / "lib" / "payload.pyc").write_bytes(b"not normalized")
    elif change == "control":
        root.chmod(0o755)
        (root / "environment.json").write_bytes(b"untrusted control")
        root.chmod(0o555)
    elif change == "hash":
        params["expected_environment_sha256"] = "c" * 64
    elif change == "private-parent":
        params["destination"].parent.chmod(0o755)
    elif change == "ancestor-link":
        real = params["destination"].parent
        real.rename(tmp_path / "real-private")
        real.symlink_to(tmp_path / "real-private", target_is_directory=True)
    else:
        params["destination"].mkdir(mode=0o700)
        (params["destination"] / "keep").write_bytes(b"caller partial")
    with pytest.raises(api.PythonProjectCopyError):
        api.copy_python_project_environment(**params)
    assert params["interpreter"].read_bytes() == EXECUTABLE
    assert stat.S_IMODE(params["interpreter"].stat().st_mode) == 0o555
    if change == "destination-exists":
        assert (params["destination"] / "keep").read_bytes() == b"caller partial"


@native
@pytest.mark.parametrize(
    "limits",
    [
        ScanLimits(1),
        ScanLimits(1024, max_entries=1),
        ScanLimits(1024, max_depth=1),
        ScanLimits(1024, max_path_bytes=1),
    ],
)
def test_linux_quota_and_deadline_have_distinct_reasons(
    api: Any,
    tmp_path: Path,
    limits: ScanLimits,
) -> None:
    params = native_arguments(tmp_path)
    params["limits"] = limits
    with pytest.raises(api.PythonProjectCopyError, match="^copy_limit$"):
        api.copy_python_project_environment(**params)
    params["limits"] = LIMITS
    params["deadline"] = time.monotonic() - 1
    with pytest.raises(api.PythonProjectCopyError, match="^copy_timeout$"):
        api.copy_python_project_environment(**params)
    assert not params["destination"].exists()


@native
@pytest.mark.parametrize("when", ["copy", "target-observed"])
def test_linux_detects_source_changes_and_keeps_partial(
    api: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    when: str,
) -> None:
    params = native_arguments(tmp_path)
    source = params["source"] / "lib" / "module.py"
    if when == "copy":
        seal = api._seal_readonly

        def changed_seal(*args: Any, **kwargs: Any) -> None:
            seal(*args, **kwargs)
            source.write_bytes(b"changed bytes after initial observation")

        monkeypatch.setattr(api, "_seal_readonly", changed_seal)
    else:
        observe = api._observe

        def changed_observation(root: Path, *args: Any, **kwargs: Any) -> Any:
            result = observe(root, *args, **kwargs)
            if root == params["destination"]:
                source.chmod(0o600)
            return result

        monkeypatch.setattr(api, "_observe", changed_observation)
    with pytest.raises(api.PythonProjectCopyError):
        api.copy_python_project_environment(**params)
    assert params["destination"].is_dir()


@native
def test_linux_deadline_terminates_and_reaps_a_slow_actual_observer(
    api: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if sys.platform != "linux":
        pytest.skip("requires real Linux fork/waitpid")
    fork = os.fork
    workers: list[int] = []

    def tracked_fork() -> int:
        pid = fork()
        if pid > 0:
            workers.append(pid)
        return pid

    def slow_observer(*args: Any, **kwargs: Any) -> Any:
        time.sleep(10)
        raise AssertionError("worker must be killed before this point")

    monkeypatch.setattr(api.os, "fork", tracked_fork)
    monkeypatch.setattr(api, "observe_python_project_environment", slow_observer)
    started = time.monotonic()
    with pytest.raises(api.PythonProjectCopyError, match="^copy_timeout$"):
        api._observe(tmp_path, INPUT, LIMITS, started + 0.2)
    assert time.monotonic() - started < 3
    assert len(workers) == 1
    with pytest.raises(ChildProcessError):
        os.waitpid(workers[0], os.WNOHANG)


@native
def test_linux_failure_keeps_partial_without_host_path_traceback(
    api: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    params = native_arguments(tmp_path)

    def failed_write(*args: Any, **kwargs: Any) -> int:
        raise OSError("/private/host/source: secret source body")

    copy_tree = api._copy_tree

    def copy_with_failure(*args: Any, **kwargs: Any) -> None:
        monkeypatch.setattr(api.os, "write", failed_write)
        copy_tree(*args, **kwargs)

    monkeypatch.setattr(api, "_copy_tree", copy_with_failure)
    with pytest.raises(api.PythonProjectCopyError) as caught:
        api.copy_python_project_environment(**params)
    assert params["destination"].is_dir()
    rendered = "".join(traceback.format_exception(caught.value))
    assert "secret source body" not in rendered and "/private/host/source" not in rendered


@native
def test_linux_thousand_files_fit_a_small_descriptor_limit(api: Any, tmp_path: Path) -> None:
    if sys.platform != "linux":
        pytest.skip("requires Linux resource limits")
    import resource

    params = native_arguments(tmp_path)
    for number in range(1000):
        (params["source"] / "lib" / f"module{number}.py").write_bytes(b"dummy")
    params["expected_environment_sha256"] = observe_python_project_environment(
        params["source"],
        interpreter=params["interpreter"],
        environment_input_sha256=INPUT,
        limits=LIMITS,
    ).environment_sha256
    old = resource.getrlimit(resource.RLIMIT_NOFILE)
    soft = min(old[0], 128)
    if soft < 64:
        pytest.skip("insufficient descriptor headroom")
    try:
        resource.setrlimit(resource.RLIMIT_NOFILE, (soft, old[1]))
        result = api.copy_python_project_environment(**params)
    finally:
        resource.setrlimit(resource.RLIMIT_NOFILE, old)
    assert result.environment_sha256 == params["expected_environment_sha256"]


@native
def test_linux_root_can_copy_dynamic_owner_through_root_sticky_export(
    api: Any, tmp_path: Path
) -> None:
    if sys.platform != "linux":
        pytest.skip("requires native Linux chown")
    if linux_uid() != 0:
        pytest.skip("requires actual root chown, not emulated UID checks")
    params = native_arguments(tmp_path)
    export = tmp_path / "export"
    export.mkdir(mode=0o1700)
    export.chmod(0o1700)
    params["source"].rename(export / "env")
    params["source"] = export / "env"
    dynamic_uid = 61234
    for directory, dirs, files in os.walk(params["source"]):
        os.chown(directory, dynamic_uid, dynamic_uid)
        for name in dirs + files:
            os.chown(Path(directory) / name, dynamic_uid, dynamic_uid, follow_symlinks=False)
    params["source_owner"] = dynamic_uid
    result = api.copy_python_project_environment(**params)
    assert result.environment_sha256 == params["expected_environment_sha256"]
    assert params["destination"].stat().st_uid == 0
