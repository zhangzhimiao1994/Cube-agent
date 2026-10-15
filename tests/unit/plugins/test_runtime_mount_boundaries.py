"""Synthetic mount-policy contracts; no native namespace or secret-read proof."""

from __future__ import annotations

import asyncio
import inspect
import subprocess
import sys
import sysconfig
from pathlib import Path
from types import SimpleNamespace
from typing import Any, NoReturn

import pytest

from agent_hub.capabilities.runtime import RuntimeCapabilityError
from agent_hub.plugins import runtime


def _forbid_execution(*args: object, **kwargs: object) -> NoReturn:
    pytest.fail("Mount boundary fixture attempted process execution")


@pytest.fixture(autouse=True)
def _no_execution(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(subprocess, "run", _forbid_execution)
    monkeypatch.setattr(subprocess, "Popen", _forbid_execution)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", _forbid_execution)
    monkeypatch.setattr(asyncio, "create_subprocess_shell", _forbid_execution)
    monkeypatch.setattr(runtime, "_probe_bubblewrap_launcher", _forbid_execution)


@pytest.fixture(params=["invoke", "probe"])
def interface(request: pytest.FixtureRequest) -> str:
    return str(request.param)


def _argv(
    launcher: runtime.BubblewrapPluginPackageProcessLauncher,
    directory: Path,
    interface: str,
    *,
    executable: Path | None = None,
) -> tuple[str, ...]:
    package = directory / "package"
    package.mkdir(exist_ok=True)
    entrypoint = package / "main.py"
    entrypoint.write_text("pass", encoding="ascii")
    target = runtime.PluginPackageExecutionTarget(root=package, entrypoint=entrypoint)
    method = launcher.argv if interface == "invoke" else launcher.probe_argv
    return method(python_executable=str(executable or directory / "python"), target=target)


def _protected_launcher(
    directory: Path,
    mounts: tuple[Path, ...] | None,
    protected: tuple[Path, ...],
) -> runtime.BubblewrapPluginPackageProcessLauncher:
    assert (
        "protected_paths"
        in inspect.signature(runtime.BubblewrapPluginPackageProcessLauncher).parameters
    ), "Trusted protected_paths mount boundary is not implemented"
    return runtime.BubblewrapPluginPackageProcessLauncher(
        bubblewrap_executable=directory / "bwrap",
        readonly_bind_paths=mounts,
        protected_paths=protected,
    )


def _mount_pairs(argv: tuple[str, ...]) -> list[tuple[Path, Path]]:
    return [
        (Path(argv[index + 1]), Path(argv[index + 2]))
        for index, argument in enumerate(argv)
        if argument == "--ro-bind"
    ]


@pytest.mark.parametrize("relation", ["same", "ancestor", "descendant"])
def test_explicit_runtime_rejects_protected_overlap(
    tmp_path: Path, interface: str, relation: str
) -> None:
    control = tmp_path / "service" / "control"
    control.mkdir(parents=True)
    descendant = control / "private-runtime"
    descendant.mkdir()
    mount = {"same": control, "ancestor": control.parent, "descendant": descendant}[relation]
    with pytest.raises((ValueError, RuntimeCapabilityError)):
        launcher = _protected_launcher(tmp_path, (mount,), (control,))
        _argv(launcher, tmp_path, interface)


@pytest.mark.parametrize("side", ["source", "destination"])
def test_default_mount_pair_rejects_canonical_protected_alias(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, interface: str, side: str
) -> None:
    control, alias, safe = tmp_path / "control", tmp_path / "alias", tmp_path / "safe-runtime"
    for directory in (control, alias, safe):
        directory.mkdir()
    original_resolve = Path.resolve

    def resolve(path: Path, strict: bool = False) -> Path:
        return original_resolve(control if path == alias else path, strict=strict)

    # Alias metadata only: no native symlink privilege, ACL or platform mutation.
    monkeypatch.setattr(Path, "resolve", resolve)
    pair = (alias, safe) if side == "source" else (safe, alias)
    monkeypatch.setattr(runtime, "_default_python_runtime_readonly_bind_paths", lambda _: (pair,))
    with pytest.raises((ValueError, RuntimeCapabilityError)):
        launcher = _protected_launcher(tmp_path, None, (control,))
        _argv(launcher, tmp_path, interface)


def test_protected_root_is_canonicalized_before_comparison(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, interface: str
) -> None:
    control, alias = tmp_path / "control", tmp_path / "control-alias"
    control.mkdir()
    alias.mkdir()
    original_resolve = Path.resolve

    def resolve(path: Path, strict: bool = False) -> Path:
        return original_resolve(control if path == alias else path, strict=strict)

    monkeypatch.setattr(Path, "resolve", resolve)
    with pytest.raises((ValueError, RuntimeCapabilityError)):
        launcher = _protected_launcher(tmp_path, (control,), (alias,))
        _argv(launcher, tmp_path, interface)


def test_safe_sibling_runtime_is_not_rejected_as_a_string_prefix(
    tmp_path: Path, interface: str
) -> None:
    control, safe = tmp_path / "service", tmp_path / "service-runtime"
    control.mkdir()
    safe.mkdir()
    launcher = _protected_launcher(tmp_path, (safe,), (control,))
    assert (safe.resolve(), safe) in _mount_pairs(_argv(launcher, tmp_path, interface))


@pytest.mark.parametrize("name", ["root", "tmp", "proc", "dev", "relative"])
def test_explicit_mount_rejects_root_reserved_cover_and_relative_path(
    tmp_path: Path, interface: str, name: str
) -> None:
    anchor = Path(tmp_path.anchor)
    mount = (
        Path("relative-runtime")
        if name == "relative"
        else anchor
        if name == "root"
        else anchor / name
    )
    with pytest.raises((ValueError, RuntimeCapabilityError)):
        launcher = runtime.BubblewrapPluginPackageProcessLauncher(
            bubblewrap_executable=tmp_path / "bwrap", readonly_bind_paths=(mount,)
        )
        _argv(launcher, tmp_path, interface)


@pytest.mark.parametrize("side", ["source", "destination"])
@pytest.mark.parametrize("name", ["root", "tmp", "proc", "dev", "relative"])
def test_default_mount_pair_cannot_bypass_path_denials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, interface: str, side: str, name: str
) -> None:
    safe = tmp_path / "safe-runtime"
    safe.mkdir()
    anchor = Path(tmp_path.anchor)
    bad = (
        Path("relative-runtime")
        if name == "relative"
        else anchor
        if name == "root"
        else anchor / name
    )
    pair = (bad, safe) if side == "source" else (safe, bad)
    monkeypatch.setattr(runtime, "_default_python_runtime_readonly_bind_paths", lambda _: (pair,))
    with pytest.raises((ValueError, RuntimeCapabilityError)):
        launcher = runtime.BubblewrapPluginPackageProcessLauncher(
            bubblewrap_executable=tmp_path / "bwrap"
        )
        _argv(launcher, tmp_path, interface)


def test_explicit_empty_mounts_never_expand_to_defaults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, interface: str
) -> None:
    def forbidden_defaults(_: str) -> NoReturn:
        pytest.fail("Explicit empty mounts expanded to implicit host runtime mounts")

    monkeypatch.setattr(runtime, "_default_python_runtime_readonly_bind_paths", forbidden_defaults)
    try:
        launcher = runtime.BubblewrapPluginPackageProcessLauncher(
            bubblewrap_executable=tmp_path / "bwrap", readonly_bind_paths=()
        )
        argv = _argv(launcher, tmp_path, interface)
    except (ValueError, RuntimeCapabilityError):
        return  # Refusing the empty configuration also preserves the boundary.
    assert _mount_pairs(argv) == [(tmp_path / "package", tmp_path / "package")]


def test_none_uses_validated_default_mount_pairs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, interface: str
) -> None:
    source, destination = tmp_path / "runtime-source", tmp_path / "runtime-destination"
    source.mkdir()
    destination.mkdir()
    monkeypatch.setattr(
        runtime, "_default_python_runtime_readonly_bind_paths", lambda _: ((source, destination),)
    )
    launcher = runtime.BubblewrapPluginPackageProcessLauncher(
        bubblewrap_executable=tmp_path / "bwrap", readonly_bind_paths=None
    )
    assert (source, destination) in _mount_pairs(_argv(launcher, tmp_path, interface))


@pytest.mark.parametrize("side", ["source", "destination"])
def test_runtime_mount_cannot_replace_approved_package(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, interface: str, side: str
) -> None:
    package, safe = tmp_path / "package", tmp_path / "runtime"
    package.mkdir()
    safe.mkdir()
    pair = (package, safe) if side == "source" else (safe, package)
    monkeypatch.setattr(runtime, "_default_python_runtime_readonly_bind_paths", lambda _: (pair,))
    launcher = runtime.BubblewrapPluginPackageProcessLauncher(
        bubblewrap_executable=tmp_path / "bwrap"
    )
    with pytest.raises(RuntimeCapabilityError):
        _argv(launcher, tmp_path, interface)


def test_reserved_destination_alias_cannot_cover_private_tmp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, interface: str
) -> None:
    safe = tmp_path / "runtime"
    safe.mkdir()
    reserved = Path(tmp_path.anchor) / "tmp"
    original_resolve = Path.resolve

    def resolve(path: Path, strict: bool = False) -> Path:
        return original_resolve(safe if path == reserved else path, strict=strict)

    monkeypatch.setattr(Path, "resolve", resolve)
    monkeypatch.setattr(
        runtime, "_default_python_runtime_readonly_bind_paths", lambda _: ((safe, reserved),)
    )
    launcher = runtime.BubblewrapPluginPackageProcessLauncher(
        bubblewrap_executable=tmp_path / "bwrap"
    )
    with pytest.raises(RuntimeCapabilityError):
        _argv(launcher, tmp_path, interface)


def test_protected_alias_retarget_after_construction_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, interface: str
) -> None:
    first, second, alias = tmp_path / "first", tmp_path / "second", tmp_path / "control-alias"
    for path in (first, second, alias):
        path.mkdir()
    current = [first]
    original_resolve = Path.resolve

    def resolve(path: Path, strict: bool = False) -> Path:
        return original_resolve(current[0] if path == alias else path, strict=strict)

    monkeypatch.setattr(Path, "resolve", resolve)
    launcher = _protected_launcher(tmp_path, (second,), (alias,))
    current[0] = second
    with pytest.raises(RuntimeCapabilityError):
        _argv(launcher, tmp_path, interface)


@pytest.mark.parametrize("role", ["package", "dependency"])
def test_package_and_dependency_cannot_bind_entire_host_root(
    tmp_path: Path, interface: str, role: str
) -> None:
    anchor = Path(tmp_path.anchor)
    root = anchor if role == "package" else tmp_path / "package"
    dependency = anchor if role == "dependency" else None
    target = runtime.PluginPackageExecutionTarget(
        root=root, entrypoint=root / "main.py", dependency_root=dependency
    )
    launcher = runtime.BubblewrapPluginPackageProcessLauncher(
        bubblewrap_executable=tmp_path / "bwrap", readonly_bind_paths=()
    )
    method = launcher.argv if interface == "invoke" else launcher.probe_argv
    with pytest.raises(RuntimeCapabilityError):
        method(python_executable=str(tmp_path / "python"), target=target)


@pytest.fixture
def interpreter_metadata(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    executable = tmp_path / "interpreter" / "bin" / "python"
    executable.parent.mkdir(parents=True)
    executable.write_text("not executed", encoding="ascii")
    stdlib = tmp_path / "interpreter" / "lib" / "python3.12"
    dynamic = stdlib / "lib-dynload"
    libraries = tmp_path / "interpreter" / "native-libraries"
    prefix, base = tmp_path / "service-prefix", tmp_path / "service-base-prefix"
    for directory in (dynamic, libraries, prefix, base):
        directory.mkdir(parents=True)
    metadata = SimpleNamespace(**vars(sys))
    metadata.executable = str(executable)
    metadata._base_executable = str(executable)
    metadata.prefix, metadata.base_prefix = str(prefix), str(base)
    monkeypatch.setattr(runtime, "sys", metadata)
    paths = {
        "stdlib": str(stdlib),
        "platstdlib": str(stdlib),
        "purelib": str(prefix / "site-packages"),
        "platlib": str(prefix / "site-packages"),
        "scripts": str(executable.parent),
        "data": str(prefix),
        "include": str(base / "include"),
        "platinclude": str(base / "include"),
    }
    variables: dict[str, Any] = {"DESTSHARED": str(dynamic), "LIBDIR": str(libraries)}
    monkeypatch.setattr(sysconfig, "get_path", lambda name, **kwargs: paths[name])
    monkeypatch.setattr(sysconfig, "get_paths", lambda **kwargs: dict(paths))
    monkeypatch.setattr(sysconfig, "get_config_var", lambda name: variables.get(name))

    def config_vars(*names: str) -> Any:
        return [variables.get(name) for name in names] if names else dict(variables)

    monkeypatch.setattr(sysconfig, "get_config_vars", config_vars)
    return {
        "executable": executable,
        "stdlib": stdlib,
        "dynamic": dynamic,
        "libraries": libraries,
        "prefix": prefix,
        "base": base,
    }


def test_default_mounts_bind_selected_interpreter_file_not_its_parent(
    interpreter_metadata: dict[str, Path],
) -> None:
    executable = interpreter_metadata["executable"]
    mounts = runtime._default_python_runtime_readonly_bind_paths(str(executable))
    assert (executable.resolve(), executable) in mounts, (
        "Default mounts omitted the interpreter file"
    )
    assert all(source != executable.parent for source, _ in mounts), (
        "Interpreter directory was overbound"
    )


def test_default_mounts_do_not_bind_parent_service_prefix_trees(
    interpreter_metadata: dict[str, Path],
) -> None:
    mounts = runtime._default_python_runtime_readonly_bind_paths(
        str(interpreter_metadata["executable"])
    )
    forbidden = {interpreter_metadata["prefix"], interpreter_metadata["base"]}
    assert all(
        source not in forbidden and destination not in forbidden for source, destination in mounts
    ), "Default mounts exposed parent service prefix trees"


def test_default_mounts_cover_current_sysconfig_library_sources(
    interpreter_metadata: dict[str, Path],
) -> None:
    mounts = runtime._default_python_runtime_readonly_bind_paths(
        str(interpreter_metadata["executable"])
    )
    for name in ("stdlib", "dynamic", "libraries"):
        required = interpreter_metadata[name]
        assert any(required == source or required.is_relative_to(source) for source, _ in mounts), (
            f"Default mounts omitted required sysconfig {name} source"
        )


def test_foreign_interpreter_requires_explicit_runtime_mounts(
    tmp_path: Path,
    interpreter_metadata: dict[str, Path],
) -> None:
    foreign = tmp_path / "foreign-interpreter" / "python"
    foreign.parent.mkdir()
    foreign.write_text("not executed", encoding="ascii")
    with pytest.raises(RuntimeCapabilityError):
        runtime._default_python_runtime_readonly_bind_paths(str(foreign))


def test_explicit_mounts_do_not_infer_a_foreign_interpreter_prefix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    interface: str,
) -> None:
    foreign = tmp_path / "foreign-runtime" / "python"
    foreign.parent.mkdir()
    foreign.write_text("not executed", encoding="ascii")

    def forbidden_defaults(_: str) -> NoReturn:
        pytest.fail("Explicit foreign runtime unexpectedly used default discovery")

    monkeypatch.setattr(runtime, "_default_python_runtime_readonly_bind_paths", forbidden_defaults)
    launcher = runtime.BubblewrapPluginPackageProcessLauncher(
        bubblewrap_executable=tmp_path / "bwrap", readonly_bind_paths=(foreign.parent,)
    )
    argv = _argv(launcher, tmp_path, interface, executable=foreign)
    assert (foreign.parent, foreign.parent) in _mount_pairs(argv)
    assert str(foreign) in argv
