from __future__ import annotations

import asyncio
import importlib
import importlib.util
import os
import sys
from itertools import pairwise
from pathlib import Path
from typing import Any, cast

import pytest


def _factory(tmp_path: Path, **overrides: Any) -> Any:
    name = "agent_hub.process_namespace"
    assert importlib.util.find_spec(name) is not None, "trusted namespace factory is missing"
    module = importlib.import_module(name)
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    program = tmp_path / "python"
    if not program.exists():
        program.write_bytes(b"trusted executable")
    launcher = tmp_path / "bwrap"
    launcher.write_bytes(b"trusted launcher")
    values = {
        "bubblewrap_executable": launcher,
        "working_directory": workspace,
        "approved_programs": (program,),
        "runtime_readonly_paths": (),
        "protected_paths": (tmp_path / "private",),
    }
    values.update(overrides)
    return module.BubblewrapStdioProcessFactory(**values)


@pytest.fixture
def linux_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(os, "geteuid", lambda: 10001, raising=False)


async def test_launch_enforces_namespace_without_host_environment_or_handles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, linux_identity: None
) -> None:
    factory = _factory(tmp_path)
    observed: list[tuple[tuple[str, ...], dict[str, Any]]] = []
    result = cast(asyncio.subprocess.Process, object())

    async def spawn(*args: str, **kwargs: Any) -> asyncio.subprocess.Process:
        observed.append((args, kwargs))
        return result

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setenv("NATIVE_SYNTHETIC_SECRET", "must-not-inherit")
    assert (
        await factory(
            str(tmp_path / "python"),
            "-c",
            "pass",
            env={"HOME": "/private/home"},
            stdin=-1,
            stdout=-1,
            stderr=-3,
        )
        is result
    )
    args, kwargs = observed[0]
    assert args[:9] == (
        str(tmp_path / "bwrap"),
        "--die-with-parent",
        "--new-session",
        "--unshare-net",
        "--unshare-pid",
        "--unshare-ipc",
        "--unshare-uts",
        "--clearenv",
        "--tmpfs",
    )
    assert args[-4:] == ("--", str(tmp_path / "python"), "-c", "pass")
    assert "--bind" not in args
    assert ("--remount-ro", "/") in tuple(pairwise(args))
    assert "must-not-inherit" not in args and "/private/home" not in args
    assert ("--setenv", "HOME", "/tmp") in tuple(zip(args, args[1:], args[2:]))
    assert kwargs == {
        "stdin": -1,
        "stdout": -1,
        "stderr": -3,
        "close_fds": True,
        "env": {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"},
    }


@pytest.mark.parametrize("key", ["LD_PRELOAD", "PYTHONPATH", "NATIVE_SYNTHETIC_SECRET"])
async def test_unapproved_environment_is_rejected_before_spawn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, linux_identity: None, key: str
) -> None:
    factory = _factory(tmp_path)

    async def forbidden(*args: str, **kwargs: Any) -> asyncio.subprocess.Process:
        pytest.fail("unapproved environment reached native spawn")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", forbidden)
    with pytest.raises(OSError, match="environment"):
        await factory(str(tmp_path / "python"), env={key: "unsafe"}, stdin=-1, stdout=-1, stderr=-3)


@pytest.mark.parametrize("slot", ["stdin", "stdout", "stderr"])
async def test_host_descriptor_cannot_be_smuggled_into_namespace(
    tmp_path: Path, linux_identity: None, slot: str
) -> None:
    factory = _factory(tmp_path)
    descriptors = {"stdin": -1, "stdout": -1, "stderr": -3}
    descriptors[slot] = 7
    with pytest.raises(OSError, match="descriptor"):
        await factory(str(tmp_path / "python"), **descriptors)


async def test_unapproved_program_is_not_resolved_on_host_path(
    tmp_path: Path, linux_identity: None
) -> None:
    factory = _factory(tmp_path)
    with pytest.raises(OSError, match="program"):
        await factory("python", stdin=-1, stdout=-1, stderr=-3)


async def test_root_identity_replacement_is_refused(tmp_path: Path, linux_identity: None) -> None:
    factory = _factory(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.rename(tmp_path / "original")
    workspace.mkdir()
    with pytest.raises(OSError, match="identity"):
        await factory(str(tmp_path / "python"), stdin=-1, stdout=-1, stderr=-3)


async def test_workspace_hardlink_is_refused(tmp_path: Path, linux_identity: None) -> None:
    factory = _factory(tmp_path)
    host = tmp_path / "outside"
    host.write_bytes(b"private")
    os.link(host, tmp_path / "workspace" / "alias")
    with pytest.raises(OSError, match="unsafe filesystem"):
        await factory(str(tmp_path / "python"), stdin=-1, stdout=-1, stderr=-3)


@pytest.mark.parametrize("directory", [True, False])
async def test_runtime_hardlink_is_refused_before_spawn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, linux_identity: None, directory: bool
) -> None:
    private = tmp_path / "private"
    private.write_bytes(b"private")
    runtime = tmp_path / "runtime"
    if directory:
        runtime.mkdir()
        os.link(private, runtime / "alias")
    else:
        os.link(private, runtime)
    factory = _factory(tmp_path, runtime_readonly_paths=(runtime,))

    async def forbidden(*args: str, **kwargs: Any) -> asyncio.subprocess.Process:
        pytest.fail("unsafe runtime mount reached native spawn")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", forbidden)
    with pytest.raises(OSError, match="unsafe filesystem"):
        await factory(str(tmp_path / "python"), stdin=-1, stdout=-1, stderr=-3)


async def test_runtime_mount_cannot_cover_protected_control_store(tmp_path: Path) -> None:
    with pytest.raises(OSError, match="protected storage"):
        _factory(tmp_path, runtime_readonly_paths=(tmp_path,))


async def test_root_execution_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, linux_identity: None
) -> None:
    factory = _factory(tmp_path)
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    with pytest.raises(OSError, match="nonroot Linux"):
        await factory(str(tmp_path / "python"), stdin=-1, stdout=-1, stderr=-3)


async def test_launcher_failure_never_falls_back_to_program(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, linux_identity: None
) -> None:
    factory = _factory(tmp_path)
    calls: list[str] = []

    async def fail(program: str, *args: str, **kwargs: Any) -> asyncio.subprocess.Process:
        calls.append(program)
        raise OSError("controlled namespace setup failure")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fail)
    with pytest.raises(OSError, match="controlled namespace setup failure"):
        await factory(str(tmp_path / "python"), stdin=-1, stdout=-1, stderr=-3)
    assert calls == [str(tmp_path / "bwrap")]
