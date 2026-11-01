"""Portable integration: real controls/metadata; Root identity and IO are simulated.

These tests do not prove native Linux Root ownership or index path confinement.
"""

from __future__ import annotations

import asyncio
import builtins
import hashlib
import importlib
import importlib.util
import io
import json
import os
import re
import socket
import subprocess
import threading
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Literal
from uuid import UUID

import pytest

from agent_hub.capabilities.python_project_inputs import PythonInterpreterIdentity
from agent_hub.skills.sandbox.broker import BrokerPolicy
from agent_hub.skills.sandbox.project import ProjectInvocation, project_source_fingerprint
from agent_hub.skills.sandbox.python_dependency_index_store import PythonDependencyIndexError
from agent_hub.skills.sandbox.python_dependency_metadata import (
    PythonDependencyMetadataIndex,
    extract_python_wheel_metadata,
)
from agent_hub.skills.sandbox.python_dependency_protocol import PythonDependencyClosureRequest
from agent_hub.skills.sandbox.python_input_supply_catalog import RootSupplyConfig
from tests.unit.capabilities.test_python_project_recipe import MANIFEST
from tests.unit.skills.test_python_dependency_metadata import wheel


def test_root_inspector_module_exists() -> None:
    assert importlib.util.find_spec(
        "agent_hub.skills.sandbox.python_dependency_inspection"
    ) is not None, "Root dependency inspector is missing"


@pytest.fixture
def api() -> Any:
    name = "agent_hub.skills.sandbox.python_dependency_inspection"
    assert importlib.util.find_spec(name) is not None, "Root dependency inspector is missing"
    return importlib.import_module(name)


@pytest.fixture
def configured(api: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    invocation = ProjectInvocation(execution_id="dependency_1", tenant_id=UUID(int=1),
        run_id=UUID(int=2), project_id="billing", session_id="draft",
        sandbox_profile="restricted", install_allowed=False, timeout_seconds=60)
    workspace = tmp_path / "workspace"
    project = workspace / str(invocation.tenant_id) / "projects" / "billing" / "sessions" / "draft"
    project.mkdir(parents=True)
    manifest = MANIFEST.replace(b'dependencies = ["packaging==25.0"]', b'dependencies = []')
    (project / "pyproject.toml").write_bytes(manifest)
    (project / "src").mkdir()
    (project / "src" / "billing.py").write_text(
        "raise AssertionError('project code must never execute')\n", encoding="ascii")
    (project / "setup.py").write_text(
        "raise AssertionError('build code must never execute')\n", encoding="ascii")
    entries, records = [], []
    for name, version, headers in (
        ("packaging", "25.0", b"Requires-Python: >=3.8\n"),
        ("setuptools", "80.9.0", b""),
        ("wheel", "0.45.1", b"Requires-Dist: packaging>=24\n"),
    ):
        entry, raw = wheel(name=name, version=version, headers=headers)
        entries.append(entry)
        records.append(extract_python_wheel_metadata(entry, raw))
    supply = RootSupplyConfig(Path("/var/lib/private-dependency-state"), tuple(entries),
                              2**20, 0, 60)
    index = PythonDependencyMetadataIndex(supply.digest, tuple(records))
    # Round trip actual canonical bytes, without simulating extraction or the closure checker.
    index = PythonDependencyMetadataIndex.from_bytes(index.to_bytes())
    cold = tmp_path / "cold-wheels"
    cold.mkdir()
    interpreter = PythonInterpreterIdentity("cpython", "3.12.321", "cp312", "linux_x86_64",
                                            "a" * 64, "b" * 64)
    # Native config reading/ownership is explicitly simulated on Windows.
    root = SimpleNamespace(interpreter=interpreter, wheel_root=cold)
    root.to_payload = lambda: {"interpreter": root.interpreter.to_payload(),
                               "wheel_root": str(root.wheel_root)}
    reads: list[tuple[str, int]] = []
    resolution_reads: list[str] = []
    resolution_records: dict[str, object] = {}
    state = SimpleNamespace(api=api, invocation=invocation, project=project, manifest=manifest,
        policy=BrokerPolicy(project_workspace_root=workspace), root=root, supply=supply,
        index=index, entries=entries, cold=cold, reads=reads,
        resolution_reads=resolution_reads, resolution_records=resolution_records)

    def read_root() -> Any:
        reads.append(("root", threading.get_ident()))
        return state.root

    def read_supply() -> Any:
        reads.append(("supply", threading.get_ident()))
        return state.supply

    def read_index(supply: Any, *, deadline: float) -> Any:
        reads.append(("index", threading.get_ident()))
        assert supply == state.supply and supply.digest == state.supply.digest
        assert deadline > time.monotonic()
        return state.index

    # Keep the actual effective_supply selection/validation. Only its Root OS
    # identity and private-file read boundary are simulated, just like root/index
    # configuration above. An absent file alone raises FileNotFoundError; a known
    # pending record is returned intact and must be rejected by the real helper.
    overlay = importlib.import_module("agent_hub.skills.sandbox.python_resolution_overlay")
    def read_resolution(store: Any, name: str, deadline: float) -> object:
        assert store.supply == state.supply
        assert deadline > time.monotonic()
        assert re.fullmatch(r"resolution-(?:binding|attempt)-[a-f0-9]{64}\.json", name)
        resolution_reads.append(name)
        if name not in resolution_records:
            raise FileNotFoundError(name)
        return resolution_records[name]

    monkeypatch.setattr(overlay.indexes, "_native", lambda: None)
    monkeypatch.setattr(overlay.ResolutionStore, "_read", read_resolution)
    monkeypatch.setattr(api, "_require_root", lambda: None)
    monkeypatch.setattr(api, "read_root_configuration", read_root)
    monkeypatch.setattr(api, "read_supply_configuration", read_supply)
    monkeypatch.setattr(api, "read_dependency_metadata_index", read_index)
    monkeypatch.setattr(api.os, "uname", lambda: SimpleNamespace(sysname="Linux",
        release="6.12.9-test", version="#1 Root test kernel", machine="x86_64"), raising=False)
    write_lock(state, complete=False)
    return state


def write_lock(state: Any, *, complete: bool) -> None:
    entries = state.entries if complete else [entry for entry in state.entries if entry.name != "packaging"]
    (state.project / "requirements.lock").write_bytes(b"".join(
        f"{entry.name}=={entry.version} --hash=sha256:{entry.sha256}\r\n".encode("ascii")
        for entry in entries))


async def request_for(state: Any) -> PythonDependencyClosureRequest:
    source = await project_source_fingerprint(state.policy.project_workspace_root, state.invocation)
    return PythonDependencyClosureRequest(invocation=state.invocation,
        expected_source_sha256=source, expected_catalog_sha256=state.supply.digest)


async def inspect(state: Any, request: Any = None) -> Any:
    request = request if request is not None else await request_for(state)
    response = await state.api.inspect_python_dependency_closure(request, policy=state.policy)
    response.require_for(request)
    return response


@pytest.mark.asyncio
async def test_actual_metadata_missing_packaging_then_corrected_lock_completes(configured: Any) -> None:
    first = await inspect(configured)
    assert first.ok and first.version == 8
    assert first.observation.closure["status"] == "incomplete"
    issue, = first.observation.closure["issues"]
    assert (issue["code"], issue["source"], issue["dependency"]) == (
        "missing_dependency", "wheel", "packaging")
    assert issue["constraint"] == ">=24" and issue["locked_version"] is None
    assert issue["candidates"] == [configured.index.records[0].wheel.to_payload()]
    write_lock(configured, complete=True)
    second = await inspect(configured)
    assert second.ok and second.observation.closure == {"status": "complete", "issues": []}
    assert first.observation.source_sha256 != second.observation.source_sha256
    assert first.observation.lock_sha256 != second.observation.lock_sha256


@pytest.mark.asyncio
async def test_observation_binds_raw_controls_full_source_and_explicit_root_environment(configured: Any) -> None:
    write_lock(configured, complete=True)
    request = await request_for(configured)
    value = await inspect(configured, request)
    observed = value.observation
    assert observed.source_sha256 == request.expected_source_sha256
    assert observed.manifest_sha256 == hashlib.sha256(configured.manifest).hexdigest()
    assert observed.lock_sha256 == hashlib.sha256((configured.project / "requirements.lock").read_bytes()).hexdigest()
    assert observed.catalog_sha256 == configured.supply.digest
    assert observed.metadata_index_sha256 == hashlib.sha256(configured.index.to_bytes()).hexdigest()
    environment = {"implementation_name": "cpython", "implementation_version": "3.12.321",
        "os_name": "posix", "platform_machine": "x86_64", "platform_release": "6.12.9-test",
        "platform_system": "Linux", "platform_version": "#1 Root test kernel",
        "platform_python_implementation": "CPython", "python_full_version": "3.12.321",
        "python_version": "3.12", "sys_platform": "linux"}
    assert observed.target_environment == environment
    assert observed.target_environment_sha256 == hashlib.sha256(json.dumps(environment,
        sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")).hexdigest()
    assert all(thread != threading.get_ident() for _, thread in configured.reads)
    assert [name for name, _ in configured.reads].count("root") == 2
    assert [name for name, _ in configured.reads].count("supply") == 2


@pytest.mark.asyncio
async def test_cold_wheel_cache_does_not_need_install_fetch_project_execution_or_writes(
    configured: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_lock(configured, complete=True)
    request = await request_for(configured)
    before = {str(path.relative_to(configured.project)): path.read_bytes()
              for path in configured.project.rglob("*") if path.is_file()}
    real_open = builtins.open
    real_io_open = io.open
    real_os_open = os.open

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("read-only inspection attempted mutation, network or execution")

    def read_only_open(file: Any, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
        if any(char in mode for char in "wax+"):
            forbidden()
        return real_open(file, mode, *args, **kwargs)

    def read_only_os_open(path: Any, flags: int, *args: Any, **kwargs: Any) -> Any:
        if flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND):
            forbidden()
        return real_os_open(path, flags, *args, **kwargs)

    def read_only_io_open(file: Any, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
        if any(char in mode for char in "wax+"):
            forbidden()
        return real_io_open(file, mode, *args, **kwargs)

    with monkeypatch.context() as guard:
        guard.setattr(builtins, "open", read_only_open)
        guard.setattr(io, "open", read_only_io_open)
        guard.setattr(os, "open", read_only_os_open)
        for name in ("mkdir", "rename", "replace", "unlink", "remove", "system"):
            guard.setattr(os, name, forbidden)
        guard.setattr(subprocess, "Popen", forbidden)
        guard.setattr(socket, "create_connection", forbidden)
        fetch = importlib.import_module("agent_hub.skills.sandbox.python_input_fetch")
        guard.setattr(fetch, "fetch_wheel", forbidden)
        result = await inspect(configured, request)
    assert result.ok and result.observation.closure["status"] == "complete"
    assert list(configured.cold.iterdir()) == []
    assert before == {str(path.relative_to(configured.project)): path.read_bytes()
                      for path in configured.project.rglob("*") if path.is_file()}


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["initial", "during_index"])
async def test_full_source_drift_never_reports_complete(
    configured: Any, monkeypatch: pytest.MonkeyPatch, phase: str,
) -> None:
    write_lock(configured, complete=True)
    request = await request_for(configured)

    def drift(*args: Any, **kwargs: Any) -> Any:
        (configured.project / "src" / "billing.py").write_bytes(b"# changed non-control source\n")
        return configured.index

    if phase == "initial":
        drift()
    else:
        monkeypatch.setattr(configured.api, "read_dependency_metadata_index", drift)
    value = await inspect(configured, request)
    assert not value.ok and value.error == "python_dependency_draft_changed"
    assert value.observation is None


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["expected", "supply_end", "root_end", "index_catalog", "partial_index"])
async def test_catalog_and_root_configuration_are_bound_before_and_after(
    configured: Any, monkeypatch: pytest.MonkeyPatch, change: str,
) -> None:
    request = await request_for(configured)
    if change == "expected":
        request = request.model_copy(update={"expected_catalog_sha256": "f" * 64})
    elif change in {"supply_end", "root_end"}:
        def drift(*args: Any, **kwargs: Any) -> Any:
            if change == "supply_end":
                configured.supply = replace(configured.supply, min_free_bytes=1)
            else:
                configured.root.interpreter = replace(configured.root.interpreter, version="3.12.322")
            return configured.index
        monkeypatch.setattr(configured.api, "read_dependency_metadata_index", drift)
    elif change == "index_catalog":
        configured.index = replace(configured.index, catalog_sha256="c" * 64)
    else:
        configured.index = replace(configured.index, records=configured.index.records[:-1])
    value = await inspect(configured, request)
    assert not value.ok and value.error == "python_dependency_catalog_changed"


@pytest.mark.asyncio
@pytest.mark.parametrize("code,expected", [
    ("metadata_unavailable", "python_dependency_metadata_unavailable"),
    ("metadata_untrusted", "python_dependency_metadata_untrusted"),
    ("catalog_changed", "python_dependency_catalog_changed"),
    ("unsupported", "python_dependency_unavailable"),
    ("timeout", "python_dependency_unavailable"),
])
async def test_index_errors_map_to_finite_path_free_codes(
    configured: Any, monkeypatch: pytest.MonkeyPatch,
    code: Literal["metadata_unavailable", "metadata_untrusted", "catalog_changed", "unsupported", "timeout"],
    expected: str,
) -> None:
    def fail(*args: Any, **kwargs: Any) -> Any:
        raise PythonDependencyIndexError(code)
    monkeypatch.setattr(configured.api, "read_dependency_metadata_index", fail)
    value = await inspect(configured)
    assert value.error == expected and value.observation is None
    assert "/var/" not in value.model_dump_json() and "https:" not in value.model_dump_json()


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["tenant_id", "run_id", "project_id", "session_id", "execution_id"])
async def test_response_rejects_every_foreign_scope_field(configured: Any, field: str) -> None:
    request = await request_for(configured)
    value = await inspect(configured, request)
    changed = UUID(int=999) if field.endswith("_id") and field in {"tenant_id", "run_id"} else "foreign"
    foreign = request.model_copy(update={"invocation": request.invocation.model_copy(update={field: changed})})
    with pytest.raises(ValueError):
        value.require_for(foreign)


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["install", "profile", "timeout_bool", "extra", "version"])
async def test_forged_requests_fail_before_root_or_io(
    configured: Any, monkeypatch: pytest.MonkeyPatch, mutation: str,
) -> None:
    request = await request_for(configured)
    changes: dict[str, dict[str, object]] = {"install": {"install_allowed": True}, "profile": {"sandbox_profile": "workspace_write"},
               "timeout_bool": {"timeout_seconds": True}, "extra": {"original_reference": {}}}
    if mutation == "version":
        request = request.model_copy(update={"version": True})
    else:
        request = request.model_copy(update={"invocation": request.invocation.model_copy(update=changes[mutation])})
    def forbidden() -> None:
        pytest.fail("invalid request reached Root boundary")
    monkeypatch.setattr(configured.api, "_require_root", forbidden)
    with pytest.raises(ValueError, match="python_dependency_request_invalid"):
        await configured.api.inspect_python_dependency_closure(request, policy=configured.policy)
    assert not configured.reads


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["broken_lock", "oversize", "too_many_requirements", "hardlink"])
async def test_control_limits_and_no_links_fail_closed(configured: Any, case: str) -> None:
    lock = configured.project / "requirements.lock"
    if case == "broken_lock":
        lock.write_bytes(b"--index-url https://private.invalid/secret\n")
    elif case == "oversize":
        lock.write_bytes(b"#" * (256 * 1024 + 1))
    elif case == "too_many_requirements":
        with lock.open("ab") as stream:
            for number in range(127):
                stream.write(f"lib{number}==1.0 --hash=sha256:{'d' * 64}\n".encode())
    else:
        os.link(lock, configured.project / "duplicate.lock")
        request = PythonDependencyClosureRequest(invocation=configured.invocation,
            expected_source_sha256="a" * 64, expected_catalog_sha256=configured.supply.digest)
        value = await inspect(configured, request)
        assert value.error == "python_dependency_draft_changed"
        return
    value = await inspect(configured)
    assert value.error == "python_dependency_declaration_invalid" and value.observation is None


@pytest.mark.asyncio
@pytest.mark.parametrize("machine,system,declared,expected", [
    ("aarch64", "Linux", "linux_aarch64", True),
    ("x86_64", "Linux", "linux_aarch64", False),
    ("AMD64", "Linux", "linux_x86_64", False),
    ("x86_64", "Darwin", "linux_x86_64", False),
])
async def test_target_platform_requires_matching_linux_uname(
    configured: Any, monkeypatch: pytest.MonkeyPatch,
    machine: str, system: str, declared: str, expected: bool,
) -> None:
    configured.root.interpreter = replace(configured.root.interpreter, platform=declared)
    monkeypatch.setattr(configured.api.os, "uname", lambda: SimpleNamespace(
        sysname=system, machine=machine, release="6.12", version="#1"))
    value = await inspect(configured)
    assert value.ok is expected
    if expected:
        assert value.observation.target_environment["platform_machine"] == machine
    else:
        assert value.error == "python_dependency_unavailable"


@pytest.mark.parametrize("platform,name,uid,euid", [
    ("win32", "nt", 0, 0), ("linux", "nt", 0, 0),
    ("linux", "posix", 1000, 0), ("linux", "posix", 0, 1000),
])
def test_production_root_guard_rejects_non_linux_or_non_root(
    api: Any, monkeypatch: pytest.MonkeyPatch, platform: str, name: str, uid: int, euid: int,
) -> None:
    monkeypatch.setattr(api, "sys", SimpleNamespace(platform=platform))
    monkeypatch.setattr(api, "os", SimpleNamespace(name=name, getuid=lambda: uid,
        geteuid=lambda: euid, getgid=lambda: 0, getegid=lambda: 0))
    with pytest.raises((ValueError, OSError)):
        api._require_root()


@pytest.mark.asyncio
async def test_native_guard_failure_never_reads_configuration(
    configured: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unsupported() -> None:
        raise OSError("private host path /etc/secret")
    monkeypatch.setattr(configured.api, "_require_root", unsupported)
    value = await inspect(configured)
    assert value.error == "python_dependency_unavailable" and not configured.reads


@pytest.mark.asyncio
async def test_bad_index_values_never_reach_complete(configured: Any) -> None:
    configured.index = object()
    value = await inspect(configured)
    assert value.error == "python_dependency_metadata_untrusted"


@pytest.mark.asyncio
async def test_timeout_during_final_configuration_recheck_is_unavailable(
    configured: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0
    def read_root() -> Any:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise TimeoutError("private /etc/config path")
        return configured.root
    monkeypatch.setattr(configured.api, "read_root_configuration", read_root)
    value = await inspect(configured)
    assert value.error == "python_dependency_unavailable" and value.observation is None


@pytest.mark.asyncio
async def test_unsupported_metadata_semantics_return_negative_observation(configured: Any) -> None:
    records = configured.index.records
    configured.index = replace(configured.index, records=(*records[:-1],
        replace(records[-1], requires_dist=("packaging @ https://private.invalid/file.whl",))))
    value = await inspect(configured)
    assert value.ok and value.error is None
    assert value.observation.closure == {"status": "unsupported", "issues": [{
        "code": "unsupported_dependency", "source": "wheel", "dependency": "",
        "candidates": [], "constraint": "", "locked_version": None}]}
    assert "private.invalid" not in value.model_dump_json()
    assert [name for name, _ in configured.reads].count("root") == 2
    assert [name for name, _ in configured.reads].count("supply") == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("case,status,code,source", [
    ("unknown_package", "metadata_unavailable", "metadata_missing", "unknown-package"),
    ("hash_mismatch", "unsupported", "metadata_mismatch", "packaging"),
])
async def test_complete_index_returns_negative_observation_for_unsupported_lock(
    configured: Any, case: str, status: str, code: str, source: str,
) -> None:
    write_lock(configured, complete=True)
    lock = configured.project / "requirements.lock"
    raw = lock.read_bytes()
    if case == "unknown_package":
        raw += f"unknown-package==1.0 --hash=sha256:{'d' * 64}\n".encode("ascii")
    else:
        raw = raw.replace(configured.entries[0].sha256.encode("ascii"), b"d" * 64)
    lock.write_bytes(raw)
    request = await request_for(configured)
    value = await inspect(configured, request)
    assert value.ok and value.error is None
    assert value.observation.closure == {"status": status, "issues": [{
        "code": code, "source": source, "dependency": "", "candidates": [],
        "constraint": "", "locked_version": None}]}
    assert value.observation.source_sha256 == request.expected_source_sha256
    assert value.observation.metadata_index_sha256 == configured.index.sha256
    assert value.observation.lock_sha256 == hashlib.sha256(raw).hexdigest()
    assert [name for name, _ in configured.reads].count("root") == 2
    assert [name for name, _ in configured.reads].count("supply") == 2


@pytest.mark.asyncio
async def test_waiting_for_slot_uses_original_invocation_timeout(
    configured: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    configured.invocation = configured.invocation.model_copy(update={"timeout_seconds": 1})
    request = await request_for(configured)
    started, release = threading.Event(), threading.Event()
    calls: list[int] = []
    def slow_root() -> Any:
        calls.append(1)
        started.set()
        assert release.wait(5)
        return configured.root
    monkeypatch.setattr(configured.api, "read_root_configuration", slow_root)
    pending = asyncio.create_task(inspect(configured, request))
    try:
        async with asyncio.timeout(2):
            while not started.is_set():
                await asyncio.sleep(0.005)
        waiting_request = request.model_copy(update={"invocation": request.invocation.model_copy(
            update={"execution_id": "waiting_1"})})
        before = asyncio.get_running_loop().time()
        overlap = await inspect(configured, waiting_request)
        elapsed = asyncio.get_running_loop().time() - before
        assert overlap.error == "python_dependency_unavailable"
        assert 0.9 <= elapsed < 2
        value = await asyncio.wait_for(pending, timeout=2)
        assert value.error == "python_dependency_unavailable" and len(calls) == 1
    finally:
        release.set()
        await pending
    # Drain the read-only timed-out worker before the event loop/fixtures are torn down.
    for _ in range(200):
        value = await inspect(configured, request)
        if value.ok:
            break
        await asyncio.sleep(0.005)
    assert value.ok


@pytest.mark.asyncio
async def test_cancellation_retains_worker_slot_until_read_finishes(
    configured: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = await request_for(configured)
    started, release = threading.Event(), threading.Event()
    calls: list[int] = []
    def slow_root() -> Any:
        calls.append(1)
        started.set()
        assert release.wait(5)
        return configured.root
    monkeypatch.setattr(configured.api, "read_root_configuration", slow_root)
    pending = asyncio.create_task(inspect(configured, request))
    try:
        async with asyncio.timeout(2):
            while not started.is_set():
                await asyncio.sleep(0.005)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        waiting = asyncio.create_task(inspect(configured, request))
        await asyncio.sleep(0.05)
        assert not waiting.done() and len(calls) == 1
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
        # Cancelling a waiter must not release the actual worker's slot.
        next_waiting = asyncio.create_task(inspect(configured, request))
        await asyncio.sleep(0.05)
        assert not next_waiting.done() and len(calls) == 1
        next_waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await next_waiting
    finally:
        release.set()
        if not pending.done():
            await pending
    for _ in range(200):
        value = await inspect(configured, request)
        if value.ok:
            break
        await asyncio.sleep(0.005)
    assert value.ok and value.observation.closure["status"] == "incomplete"


@pytest.mark.asyncio
async def test_concurrent_requests_queue_then_both_execute_with_one_worker(
    configured: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = await request_for(configured)
    second_request = request.model_copy(update={"invocation": request.invocation.model_copy(
        update={"execution_id": "dependency_2"})})
    started, release = threading.Event(), threading.Event()
    calls: list[int] = []
    def slow_first_root() -> Any:
        calls.append(threading.get_ident())
        if len(calls) == 1:
            started.set()
            assert release.wait(5)
        return configured.root
    monkeypatch.setattr(configured.api, "read_root_configuration", slow_first_root)
    first = asyncio.create_task(inspect(configured, request))
    second = None
    try:
        async with asyncio.timeout(2):
            while not started.is_set():
                await asyncio.sleep(0.005)
        second = asyncio.create_task(inspect(configured, second_request))
        await asyncio.sleep(0.05)
        assert not first.done() and not second.done() and len(calls) == 1
        release.set()
        values = await asyncio.wait_for(asyncio.gather(first, second), timeout=3)
        assert all(value.ok and value.error is None for value in values)
        assert [value.scope.execution_id for value in values] == ["dependency_1", "dependency_2"]
        assert len(calls) == 4
    finally:
        release.set()
        await asyncio.gather(*(task for task in (first, second) if task is not None),
                             return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("phase,expected", [
    ("initial", "python_dependency_unavailable"),
    ("final", "python_dependency_catalog_changed"),
])
async def test_known_pending_overlay_never_falls_back_to_base(
    configured: Any, monkeypatch: pytest.MonkeyPatch, phase: str, expected: str,
) -> None:
    write_lock(configured, complete=True)
    request = await request_for(configured)
    overlay = importlib.import_module("agent_hub.skills.sandbox.python_resolution_overlay")
    scope = overlay._scope(request.invocation, request.expected_source_sha256)
    attempt_name = overlay._name(scope, "attempt")
    attempt = {"schema_version": 1, "purpose": "python_resolution_attempt", "scope": scope,
               "resolution_key": "a" * 64, "base_catalog_sha256": configured.supply.digest}
    if phase == "initial":
        configured.resolution_records[attempt_name] = attempt
    else:
        def become_pending(supply: Any, *, deadline: float) -> Any:
            assert supply == configured.supply and deadline > time.monotonic()
            configured.resolution_records[attempt_name] = attempt
            return configured.index
        monkeypatch.setattr(configured.api, "read_dependency_metadata_index", become_pending)
    result = await inspect(configured, request)
    assert not result.ok and result.error == expected and result.observation is None
    assert attempt_name in configured.resolution_reads
    assert configured.resolution_records[attempt_name] == attempt
