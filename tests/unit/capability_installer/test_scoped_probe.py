from __future__ import annotations

import asyncio
import importlib
import importlib.util
import json
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Any, cast

import pytest

from agent_hub.capability_installer.environment import EnvironmentBuildError
from agent_hub.process_namespace import BubblewrapStdioProcessFactory, ProcessNamespaceUnavailable


def test_scoped_probe_is_available() -> None:
    assert importlib.util.find_spec("agent_hub.capability_installer.scoped_probe") is not None


@pytest.fixture
def probe_module() -> ModuleType:
    name = "agent_hub.capability_installer.scoped_probe"
    assert importlib.util.find_spec(name) is not None, "scoped CLI probe is missing"
    return importlib.import_module(name)


@pytest.fixture
def scope(tmp_path: Path) -> tuple[Path, Path, Path, dict[str, Any]]:
    tmp_path = tmp_path.resolve()
    root = tmp_path / "environments"
    staging = root / ".staging" / "fresh"
    staging.mkdir(parents=True)
    executable = staging / "cli"
    executable.write_bytes(b"parent-verified artifact")
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    for name in ("bwrap", "native-probe"):
        (tmp_path / name).write_bytes(b"trusted native fixture")
    document = {
        "schema_version": 1,
        "bubblewrap_executable": str(tmp_path / "bwrap"),
        "runtime_readonly_paths": [str(runtime)],
        "native_probe_program": str(tmp_path / "native-probe"),
        "bindings": [],
    }
    policy = tmp_path / "scope.json"
    policy.write_text(json.dumps(document), encoding="utf-8")
    return policy, staging, executable, document


class NativeProcess:
    def __init__(self, *, code: int = 0, output: bytes = b"version 1\n", alive: bool = False):
        self.stdout = asyncio.StreamReader()
        self.stderr = asyncio.StreamReader()
        self.stdout.feed_data(output)
        self.returncode = None if alive else code
        self.killed = False
        self.reaped = False
        self._done = asyncio.Event()
        if not alive:
            self._finish()

    def _finish(self) -> None:
        self.stdout.feed_eof()
        self.stderr.feed_eof()
        self._done.set()

    async def wait(self) -> int:
        await self._done.wait()
        self.reaped = True
        assert self.returncode is not None
        return self.returncode

    def kill(self) -> None:
        self.killed = True
        if self.returncode is None:
            self.returncode = -9
            self._finish()


@pytest.fixture
def native(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    state: dict[str, Any] = {"calls": [], "processes": [], "options": {}, "error": None}

    async def spawn(
        factory: BubblewrapStdioProcessFactory, *argv: str, **kwargs: Any
    ) -> asyncio.subprocess.Process:
        state["calls"].append((factory, argv, kwargs))
        if state["error"] is not None:
            raise state["error"]
        process = NativeProcess(**state["options"])
        state["processes"].append(process)
        return cast(asyncio.subprocess.Process, process)

    async def forbidden(*args: Any, **kwargs: Any) -> asyncio.subprocess.Process:
        raise AssertionError("host fallback must never execute")

    monkeypatch.setattr(BubblewrapStdioProcessFactory, "__call__", spawn)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", forbidden)
    return state


def _build(
    module: ModuleType, policy: Path, protected: tuple[Path, ...] = ()
) -> Callable[[Path, Path], None]:
    return cast(Callable[[Path, Path], None], module.build_scoped_cli_probe(policy, protected))


def test_probe_can_validate_policy_with_a_not_yet_installed_managed_binding(
    probe_module: ModuleType,
    scope: tuple[Path, Path, Path, dict[str, Any]],
    native: dict[str, Any],
) -> None:
    policy, staging, executable, document = scope
    workspace = policy.parent / "workspaces"
    project = workspace / "project"
    project.mkdir(parents=True)
    document["bindings"] = [
        {
            "kind": "plugin",
            "tenant_id": "11111111-1111-4111-8111-111111111111",
            "source_id": "installed-plugin",
            "working_directory": str(project),
            "approved_programs": [],
            "managed_environment_id": "installed-tool",
        }
    ]
    policy.write_text(json.dumps(document), encoding="utf-8")
    _build(probe_module, policy, (staging.parent.parent, workspace))(executable, staging)
    assert len(native["calls"]) == 1
    assert native["calls"][0][1] == (str(executable), "--version")


def test_missing_policy_builder_returns_callable_and_probe_fails_closed(
    probe_module: ModuleType,
    scope: tuple[Path, Path, Path, dict[str, Any]],
    native: dict[str, Any],
) -> None:
    policy, staging, executable, _ = scope
    policy.unlink()
    probe = _build(probe_module, policy)
    assert callable(probe)
    with pytest.raises(EnvironmentBuildError):
        probe(executable, staging)
    assert not policy.exists()
    assert native["calls"] == []


def test_each_invocation_rereads_changed_policy_file(
    probe_module: ModuleType,
    scope: tuple[Path, Path, Path, dict[str, Any]],
    native: dict[str, Any],
) -> None:
    policy, staging, executable, document = scope
    probe = _build(probe_module, policy)
    probe(executable, staging)
    changed_runtime = policy.parent / "new-runtime"
    changed_runtime.mkdir()
    document["runtime_readonly_paths"] = [str(changed_runtime)]
    policy.write_text(json.dumps(document), encoding="utf-8")
    probe(executable, staging)
    assert len(native["calls"]) == 2
    assert native["calls"][0][0]._mounts == (staging, executable, policy.parent / "runtime")
    assert native["calls"][1][0]._mounts == (staging, executable, changed_runtime)


def test_changed_policy_fails_closed_after_a_previous_success(
    probe_module: ModuleType,
    scope: tuple[Path, Path, Path, dict[str, Any]],
    native: dict[str, Any],
) -> None:
    policy, staging, executable, document = scope
    root = staging.parent.parent
    probe = _build(probe_module, policy, (root,))
    probe(executable, staging)
    document["runtime_readonly_paths"] = [str(root)]
    policy.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(EnvironmentBuildError):
        probe(executable, staging)
    assert len(native["calls"]) == 1


def test_runs_only_exact_staged_program_without_credentials_or_success_cache(
    probe_module: ModuleType,
    scope: tuple[Path, Path, Path, dict[str, Any]],
    native: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    policy, staging, executable, _ = scope
    root = staging.parent.parent
    sibling = root / "other-version"
    monkeypatch.setenv("API_KEY", "secret-must-not-pass")
    monkeypatch.setenv("HOME", "host-home-must-not-pass")
    probe = _build(probe_module, policy, (root, sibling))
    assert probe(executable, staging) is None
    assert probe(executable, staging) is None
    assert len(native["calls"]) == 2
    for factory, argv, kwargs in native["calls"]:
        assert argv == (str(executable), "--version")
        assert kwargs["env"] == {}
        assert kwargs["stdin"] == asyncio.subprocess.DEVNULL
        assert kwargs["stdout"] == kwargs["stderr"] == asyncio.subprocess.PIPE
        assert factory._working_directory == staging
        assert factory._programs == frozenset((executable,))
        assert root not in factory._protected_paths
        assert sibling in factory._protected_paths
        assert policy in factory._protected_paths
        assert factory._mounts == (staging, executable, policy.parent / "runtime")
    assert all(process.reaped for process in native["processes"])


@pytest.mark.parametrize("mount_kind", ["root", "stage", "sibling", "ancestor", "policy"])
def test_runtime_mounts_cannot_use_owned_stage_ancestor_exception(
    probe_module: ModuleType,
    scope: tuple[Path, Path, Path, dict[str, Any]],
    native: dict[str, Any],
    mount_kind: str,
) -> None:
    policy, staging, executable, document = scope
    root = staging.parent.parent
    sibling = root / "other-version"
    sibling.mkdir()
    mounts = {
        "root": root,
        "stage": staging,
        "sibling": sibling,
        "ancestor": root.parent,
        "policy": policy,
    }
    document["runtime_readonly_paths"] = [str(mounts[mount_kind])]
    policy.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(EnvironmentBuildError):
        _build(probe_module, policy, (root,))(executable, staging)
    assert native["calls"] == []


@pytest.mark.parametrize("location", ["stage", "child", "executable"])
def test_keeps_protections_that_are_not_strict_stage_ancestors(
    probe_module: ModuleType,
    scope: tuple[Path, Path, Path, dict[str, Any]],
    native: dict[str, Any],
    location: str,
) -> None:
    policy, staging, executable, _ = scope
    protected = {"stage": staging, "child": staging / "secret", "executable": executable}
    with pytest.raises(EnvironmentBuildError):
        _build(probe_module, policy, (staging.parent.parent, protected[location]))(
            executable, staging
        )
    assert native["calls"] == []


@pytest.mark.parametrize(
    "case",
    [
        "relative_program",
        "relative_stage",
        "missing_program",
        "missing_stage",
        "directory_program",
        "file_stage",
        "outside",
        "nested",
        "dotdot_program",
        "dotdot_stage",
        "hardlink_program",
    ],
)
def test_rejects_invalid_stage_or_program_before_native_execution(
    probe_module: ModuleType,
    scope: tuple[Path, Path, Path, dict[str, Any]],
    native: dict[str, Any],
    case: str,
) -> None:
    policy, staging, executable, _ = scope
    probe = _build(probe_module, policy)
    if case == "relative_program":
        executable = Path("cli")
    elif case == "relative_stage":
        staging = Path("fresh")
    elif case == "missing_program":
        executable.unlink()
    elif case == "missing_stage":
        executable.unlink()
        staging.rmdir()
    elif case == "directory_program":
        executable.unlink()
        executable.mkdir()
    elif case == "file_stage":
        staging = executable
    elif case == "outside":
        executable = policy.parent / "other-cli"
        executable.write_bytes(b"other artifact")
    elif case == "nested":
        nested = staging / "nested"
        nested.mkdir()
        executable = executable.rename(nested / "cli")
    elif case == "dotdot_program":
        (staging / "nested").mkdir()
        executable = staging / "nested" / ".." / "cli"
    elif case == "dotdot_stage":
        staging = staging / ".." / "fresh"
    elif case == "hardlink_program":
        (staging / "alias").hardlink_to(executable)
    with pytest.raises(EnvironmentBuildError):
        probe(executable, staging)
    assert native["calls"] == []


@pytest.mark.parametrize("alias_kind", ["program", "stage", "stage_parent", "policy"])
def test_rejects_filesystem_aliases(
    probe_module: ModuleType,
    scope: tuple[Path, Path, Path, dict[str, Any]],
    native: dict[str, Any],
    alias_kind: str,
) -> None:
    policy, staging, executable, _ = scope
    alias = policy.parent / "alias"
    target = {
        "program": executable,
        "stage": staging,
        "stage_parent": staging.parent,
        "policy": policy,
    }[alias_kind]
    try:
        alias.symlink_to(target, target_is_directory=target.is_dir())
    except OSError:
        pytest.skip("filesystem symlink creation unavailable")
    if alias_kind == "program":
        executable = alias
    elif alias_kind == "stage":
        staging, executable = alias, alias / "cli"
    elif alias_kind == "stage_parent":
        staging, executable = alias / "fresh", alias / "fresh" / "cli"
    else:
        policy = alias
    with pytest.raises(EnvironmentBuildError):
        _build(probe_module, policy)(executable, staging)
    assert native["calls"] == []


@pytest.mark.parametrize(
    "case",
    [
        "missing",
        "directory",
        "relative",
        "bad_json",
        "bad_schema",
        "missing_runtime",
        "missing_binding",
        "missing_launcher",
    ],
)
def test_bad_policy_fails_closed_without_provisioning_candidates(
    probe_module: ModuleType,
    scope: tuple[Path, Path, Path, dict[str, Any]],
    native: dict[str, Any],
    case: str,
) -> None:
    policy, staging, executable, document = scope
    candidate = policy.parent / "nonexistent-candidate"
    if case == "missing":
        policy = candidate
    elif case == "directory":
        policy = staging
    elif case == "relative":
        policy = Path("scope.json")
    elif case == "bad_json":
        policy.write_text("not json with secret-value", encoding="utf-8")
    else:
        if case == "bad_schema":
            document["schema_version"] = 2
        elif case == "missing_runtime":
            document["runtime_readonly_paths"] = [str(candidate)]
        elif case == "missing_launcher":
            document["bubblewrap_executable"] = str(candidate)
        elif case == "missing_binding":
            document["bindings"] = [
                {
                    "kind": "plugin",
                    "tenant_id": "11111111-1111-4111-8111-111111111111",
                    "source_id": "candidate",
                    "working_directory": str(candidate),
                    "approved_programs": [str(candidate / "cli")],
                }
            ]
        policy.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(EnvironmentBuildError) as failure:
        _build(probe_module, policy)(executable, staging)
    assert "secret-value" not in str(failure.value)
    assert str(candidate) not in str(failure.value)
    assert not candidate.exists()
    assert native["calls"] == []


def test_nonzero_result_is_sanitized_build_error(
    probe_module: ModuleType,
    scope: tuple[Path, Path, Path, dict[str, Any]],
    native: dict[str, Any],
) -> None:
    policy, staging, executable, _ = scope
    native["options"] = {"code": 7, "output": b"private diagnostic credential"}
    with pytest.raises(EnvironmentBuildError) as failure:
        _build(probe_module, policy)(executable, staging)
    assert "credential" not in str(failure.value)
    assert native["processes"][0].reaped


def test_unavailable_namespace_never_falls_back_or_exposes_native_error(
    probe_module: ModuleType,
    scope: tuple[Path, Path, Path, dict[str, Any]],
    native: dict[str, Any],
) -> None:
    policy, staging, executable, _ = scope
    native["error"] = ProcessNamespaceUnavailable("secret native configuration")
    with pytest.raises(EnvironmentBuildError) as failure:
        _build(probe_module, policy)(executable, staging)
    assert "secret" not in str(failure.value)
    assert len(native["calls"]) == 1


def test_output_limit_reaps_process_via_existing_helper(
    probe_module: ModuleType,
    scope: tuple[Path, Path, Path, dict[str, Any]],
    native: dict[str, Any],
) -> None:
    policy, staging, executable, _ = scope
    native["options"] = {"alive": True, "output": b"x" * 262145}
    with pytest.raises(EnvironmentBuildError):
        _build(probe_module, policy)(executable, staging)
    process = native["processes"][0]
    assert process.killed and process.reaped


def test_timeout_reaps_process_via_existing_helper(
    probe_module: ModuleType,
    scope: tuple[Path, Path, Path, dict[str, Any]],
    native: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    policy, staging, executable, _ = scope
    native["options"] = {"alive": True}
    original = probe_module._run_local_command

    async def short_deadline(*args: Any, **kwargs: Any) -> tuple[int, str, str]:
        assert 0 < kwargs["timeout_seconds"] <= 30
        kwargs["timeout_seconds"] = 0.01
        return cast(tuple[int, str, str], await original(*args, **kwargs))

    monkeypatch.setattr(probe_module, "_run_local_command", short_deadline)
    with pytest.raises(EnvironmentBuildError):
        _build(probe_module, policy)(executable, staging)
    process = native["processes"][0]
    assert process.killed and process.reaped


def test_revalidates_runtime_mounts_before_each_new_stage_factory(
    probe_module: ModuleType,
    scope: tuple[Path, Path, Path, dict[str, Any]],
    native: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    policy, staging, executable, _ = scope
    probe = _build(probe_module, policy, (staging.parent.parent,))
    runtime = policy.parent / "runtime"
    runtime.rmdir()
    try:
        runtime.symlink_to(staging.parent.parent, target_is_directory=True)
    except OSError:
        pytest.skip("filesystem symlink creation unavailable")

    def forbidden_factory(**kwargs: Any) -> None:
        raise AssertionError("unsafe runtime must be rejected before factory construction")

    monkeypatch.setattr(probe_module, "BubblewrapStdioProcessFactory", forbidden_factory)
    with pytest.raises(EnvironmentBuildError):
        probe(executable, staging)
    assert native["calls"] == []
