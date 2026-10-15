"""Offline registration denials; simulated platform metadata is not native isolation."""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
from typing import Literal, NoReturn

import pytest

from agent_hub.plugins import runtime


def _no_processes(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    attempted: list[Path] = []

    def forbidden(path: Path) -> bool:
        attempted.append(path)
        raise AssertionError("denial path attempted an unmanaged launcher probe")

    monkeypatch.setattr(runtime, "_probe_bubblewrap_launcher", forbidden)

    def forbidden_runner(*args: object, **kwargs: object) -> NoReturn:
        raise AssertionError("denial path constructed an executable package runner")

    monkeypatch.setattr(runtime, "PythonSubprocessPluginPackageRunner", forbidden_runner)
    return attempted


class _FalseyRejectingProbe:
    def __init__(self) -> None:
        self.calls: list[Path] = []

    def __bool__(self) -> bool:
        return False

    def __call__(self, executable: Path) -> bool:
        self.calls.append(executable)
        return False


@pytest.mark.parametrize("interface", ["status", "build"])
def test_explicit_falsey_rejecting_probe_cannot_fall_back_to_host_launcher(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, interface: str
) -> None:
    executable = tmp_path / "bwrap-fixture"
    executable.write_text("not executable", encoding="ascii")
    monkeypatch.setattr(
        runtime, "os", SimpleNamespace(name="posix", access=lambda path, mode: True, X_OK=os.X_OK)
    )
    attempted = _no_processes(monkeypatch)
    probe = _FalseyRejectingProbe()
    if interface == "status":
        result = runtime._plugin_package_subprocess_registration_status(
            enabled=True,
            adapter_ids=("calendar_python",),
            isolation_backend="bubblewrap",
            bubblewrap_executable=executable,
            launcher_probe=probe,
        )
        assert result == "launcher_probe_failed"
    else:
        # Use explicit arguments so strict typing keeps the same public contract.
        result_adapters = runtime.build_plugin_package_subprocess_adapters(
            enabled=True,
            adapter_ids=("calendar_python",),
            isolation_backend="bubblewrap",
            bubblewrap_executable=executable,
            launcher_probe=probe,
            package_store_dir=tmp_path,
        )
        assert result_adapters == {}
    assert probe.calls == [executable]
    assert not attempted


@pytest.mark.parametrize(
    ("case", "expected"),
    [
        ("disabled", "disabled"),
        ("no_ids", "no_adapter_ids"),
        ("backend_disabled", "unsupported_isolation_backend"),
        ("no_launcher", "missing_isolation_launcher"),
        ("relative", "launcher_path_not_absolute"),
        ("missing", "launcher_not_found"),
        ("directory", "launcher_not_found"),
        ("non_posix", "unsupported_isolation_backend"),
        ("not_executable", "launcher_not_executable"),
    ],
)
def test_prerequisite_denial_never_probes_or_constructs_an_adapter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str, expected: str
) -> None:
    launcher = tmp_path / "launcher"
    launcher.write_text("not executable", encoding="ascii")
    executable: Path | None = launcher
    enabled = case != "disabled"
    ids = () if case == "no_ids" else ("calendar_python",)
    backend: Literal["disabled", "bubblewrap"] = (
        "disabled" if case == "backend_disabled" else "bubblewrap"
    )
    if case == "no_launcher":
        executable = None
    elif case == "relative":
        executable = Path("relative-launcher")
    elif case == "missing":
        executable = tmp_path / "missing"
    elif case == "directory":
        executable = tmp_path
    monkeypatch.setattr(
        runtime,
        "os",
        SimpleNamespace(
            name="nt" if case == "non_posix" else "posix",
            access=lambda path, mode: case != "not_executable",
            X_OK=os.X_OK,
        ),
    )
    attempted = _no_processes(monkeypatch)
    probe_calls: list[Path] = []

    def rejected_probe(path: Path) -> bool:
        probe_calls.append(path)
        return False

    status = runtime._plugin_package_subprocess_registration_status(
        enabled=enabled,
        adapter_ids=ids,
        isolation_backend=backend,
        bubblewrap_executable=executable,
        launcher_probe=rejected_probe,
    )
    adapters = runtime.build_plugin_package_subprocess_adapters(
        enabled=enabled,
        adapter_ids=ids,
        isolation_backend=backend,
        bubblewrap_executable=executable,
        launcher_probe=rejected_probe,
        package_store_dir=tmp_path,
    )
    assert status == expected
    assert adapters == {}
    assert not probe_calls and not attempted
