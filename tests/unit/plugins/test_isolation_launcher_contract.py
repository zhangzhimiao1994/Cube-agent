"""Offline argv/registration contracts, not native Linux isolation evidence."""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Literal, NoReturn

import pytest

from agent_hub.plugins import runtime
from agent_hub.plugins.runtime import (
    BubblewrapPluginPackageProcessLauncher,
    PluginPackageExecutionTarget,
    _plugin_package_subprocess_registration_status,
    build_plugin_package_subprocess_adapters,
)

_BOOTSTRAP = (
    "import runpy, sys; "
    "sys.path.insert(0, sys.argv[1]); "
    "runpy.run_path(sys.argv[2], run_name='__main__')"
)
_PREFLIGHT = (
    "import pathlib, sys; "
    "root = pathlib.Path(sys.argv[1]).resolve(); "
    "entrypoint = pathlib.Path(sys.argv[2]).resolve(); "
    "entrypoint.relative_to(root); "
    "compile(entrypoint.read_bytes(), str(entrypoint), 'exec'); "
    "dependency_root = sys.argv[3]; "
    "assert not dependency_root or pathlib.Path(dependency_root).resolve().is_dir()"
)


def _forbid_execution(*args: object, **kwargs: object) -> NoReturn:
    pytest.fail("Offline isolation contract attempted to execute a process or launcher")


@pytest.fixture(autouse=True)
def _no_process_execution(monkeypatch: pytest.MonkeyPatch) -> None:
    assert vars(runtime)["subprocess"] is subprocess
    assert vars(runtime)["asyncio"] is asyncio
    monkeypatch.setattr(subprocess, "run", _forbid_execution)
    monkeypatch.setattr(subprocess, "Popen", _forbid_execution)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", _forbid_execution)
    monkeypatch.setattr(asyncio, "create_subprocess_shell", _forbid_execution)
    monkeypatch.setattr(runtime, "_probe_bubblewrap_launcher", _forbid_execution)


@pytest.fixture
def contract_root() -> Path:
    # These paths are argv values only; no fixture directory or executable is created.
    return Path(__file__).resolve().parent / "virtual-isolation-contract"


@pytest.mark.parametrize("with_dependencies", [False, True])
@pytest.mark.parametrize(
    "component",
    ["ordinary", "literal spaces 'quote' $(echo ignored);$CONTRACT_VALUE;%CONTRACT_VALUE%;~"],
    ids=["ordinary-path", "literal-shell-metacharacters"],
)
def test_exact_argv_and_mount_order_match_preflight_and_invoke(
    contract_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    with_dependencies: bool,
    component: str,
) -> None:
    monkeypatch.setenv("CONTRACT_VALUE", "must-not-expand")
    package = contract_root / component / "package"
    dependency = contract_root / component / "dependencies" if with_dependencies else None
    runtimes = (contract_root / component / "runtime", contract_root / component / "libraries")
    entrypoint = package / "adapter" / "main.py"
    executable = str(runtimes[0] / "bin" / "python")
    bubblewrap = contract_root / component / "bwrap"
    target = PluginPackageExecutionTarget(
        root=package, entrypoint=entrypoint, dependency_root=dependency
    )
    launcher = BubblewrapPluginPackageProcessLauncher(
        bubblewrap_executable=bubblewrap, readonly_bind_paths=runtimes
    )
    dependency_mount = () if dependency is None else ("--ro-bind", str(dependency), str(dependency))
    expected_boundary = (
        str(bubblewrap),
        "--die-with-parent",
        "--new-session",
        "--unshare-net",
        "--unshare-pid",
        "--unshare-ipc",
        "--unshare-uts",
        "--tmpfs",
        "/tmp",
        "--ro-bind",
        str(package),
        str(package),
        *dependency_mount,
        "--ro-bind",
        str(runtimes[0]),
        str(runtimes[0]),
        "--ro-bind",
        str(runtimes[1]),
        str(runtimes[1]),
        "--dev",
        "/dev",
        "--proc",
        "/proc",
        "--chdir",
        str(package),
        "--",
        executable,
    )
    invoke_args = (
        ("-I", "-X", "utf8", str(entrypoint))
        if dependency is None
        else ("-I", "-X", "utf8", "-c", _BOOTSTRAP, str(dependency), str(entrypoint))
    )
    probe_args = (
        "-I",
        "-X",
        "utf8",
        "-c",
        _PREFLIGHT,
        str(package),
        str(entrypoint),
        "" if dependency is None else str(dependency),
    )

    invoke = launcher.argv(python_executable=executable, target=target)
    preflight = launcher.probe_argv(python_executable=executable, target=target)

    assert invoke == (*expected_boundary, *invoke_args)
    assert preflight == (*expected_boundary, *probe_args)
    assert invoke[: len(expected_boundary)] == preflight[: len(expected_boundary)]
    for argv in (invoke, preflight):
        assert argv.count("--") == 1
        assert argv.count("--ro-bind") == 3 + int(with_dependencies)
        assert not {"--bind", "--bind-try", "--dev-bind", "--dev-bind-try"}.intersection(argv)
        assert "/bin/sh" not in argv and "/bin/true" not in argv
        assert "must-not-expand" not in "".join(argv)


def test_default_runtime_mounts_preserve_source_and_destination_in_both_argv(
    contract_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    package = contract_root / "package"
    executable = str(contract_root / "python-link" / "bin" / "python")
    mounts = (
        (contract_root / "resolved-runtime", contract_root / "python-link"),
        (contract_root / "resolved-libraries", contract_root / "library-link"),
    )
    calls: list[str] = []

    def runtime_mounts(python_executable: str) -> tuple[tuple[Path, Path], ...]:
        calls.append(python_executable)
        return mounts

    monkeypatch.setattr(runtime, "_default_python_runtime_readonly_bind_paths", runtime_mounts)
    launcher = BubblewrapPluginPackageProcessLauncher(
        bubblewrap_executable=contract_root / "bwrap", readonly_bind_paths=None
    )
    target = PluginPackageExecutionTarget(root=package, entrypoint=package / "main.py")
    expected_mounts = tuple(
        argument
        for source, destination in mounts
        for argument in ("--ro-bind", str(source), str(destination))
    )

    invoke = launcher.argv(python_executable=executable, target=target)
    preflight = launcher.probe_argv(python_executable=executable, target=target)

    assert calls == [executable, executable]
    for argv in (invoke, preflight):
        assert argv[12:18] == expected_mounts
        assert argv[18:26] == (
            "--dev",
            "/dev",
            "--proc",
            "/proc",
            "--chdir",
            str(package),
            "--",
            executable,
        )
        assert argv.count("--ro-bind") == 3
        assert "--bind" not in argv


@pytest.mark.parametrize(
    "reason",
    [
        "disabled",
        "no_adapter_ids",
        "unsupported_backend",
        "missing_isolation_launcher",
        "launcher_path_not_absolute",
        "launcher_not_found",
        "non_posix",
        "launcher_not_executable",
        "launcher_probe_failed",
    ],
)
def test_offline_registration_negative_never_constructs_runner_or_falls_back(
    contract_root: Path, monkeypatch: pytest.MonkeyPatch, reason: str
) -> None:
    launcher_path: Path | None = contract_root / "virtual-bwrap"
    backend: Literal["disabled", "bubblewrap"] = (
        "disabled" if reason == "unsupported_backend" else "bubblewrap"
    )
    if reason == "missing_isolation_launcher":
        launcher_path = None
    elif reason == "launcher_path_not_absolute":
        launcher_path = Path("relative-bwrap")
    probe_calls: list[Path] = []
    access_calls: list[tuple[Path, int]] = []

    def probe(path: Path) -> bool:
        probe_calls.append(path)
        return False

    def access(path: Path, mode: int) -> bool:
        access_calls.append((path, mode))
        return reason != "launcher_not_executable"

    def is_file(path: Path) -> bool:
        return path == launcher_path and reason != "launcher_not_found"

    expected_status = (
        "unsupported_isolation_backend"
        if reason in {"unsupported_backend", "non_posix"}
        else reason
    )
    enabled = reason != "disabled"
    adapter_ids = () if reason == "no_adapter_ids" else ("calendar_python",)
    # Replace only runtime's os binding: no global platform mutation, ACL or executable-bit change.
    with monkeypatch.context() as patch:
        patch.setattr(
            runtime,
            "os",
            SimpleNamespace(name="nt" if reason == "non_posix" else "posix", access=access, X_OK=1),
        )
        patch.setattr(Path, "is_file", is_file)
        patch.setattr(runtime, "PythonSubprocessPluginPackageRunner", _forbid_execution)
        patch.setattr(runtime, "BubblewrapPluginPackageProcessLauncher", _forbid_execution)
        status = _plugin_package_subprocess_registration_status(
            enabled=enabled,
            adapter_ids=adapter_ids,
            isolation_backend=backend,
            bubblewrap_executable=launcher_path,
            launcher_probe=probe,
        )
        adapters = build_plugin_package_subprocess_adapters(
            enabled=enabled,
            adapter_ids=adapter_ids,
            isolation_backend=backend,
            bubblewrap_executable=launcher_path,
            launcher_probe=probe,
            package_store_dir=contract_root / "virtual-store",
        )

    assert status == expected_status
    assert adapters == {}
    assert probe_calls == (
        [launcher_path, launcher_path] if reason == "launcher_probe_failed" else []
    )
    assert access_calls == (
        [(launcher_path, 1), (launcher_path, 1)]
        if reason in {"launcher_probe_failed", "launcher_not_executable"}
        else []
    )
