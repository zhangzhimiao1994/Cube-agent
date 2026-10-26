"""Real disk controls/source and wheel bytes, simulated Linux Root/FD boundary.

These portable tests are not evidence of native Linux ownership/confinement.
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib
import importlib.util
import json
import stat
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any
from uuid import UUID

import pytest

from agent_hub.skills.sandbox.broker import BrokerPolicy
from agent_hub.skills.sandbox.project import ProjectInvocation, project_source_fingerprint
from agent_hub.skills.sandbox.python_dependency_metadata import (
    PythonDependencyMetadataIndex,
    extract_python_wheel_metadata,
)
from agent_hub.skills.sandbox.python_input_supply_catalog import RootSupplyConfig
from agent_hub.skills.sandbox.python_metadata_plan_protocol import (
    PythonMetadataDraft,
    PythonMetadataPlanningRequest,
    PythonMetadataScope,
)
from agent_hub.skills.sandbox.python_project_backend import RootConfig
from tests.unit.skills.test_python_dependency_index_store import (
    STATE,
    WHEELS,
    FileSystem,
    root_configuration,
)
from tests.unit.skills.test_python_dependency_metadata import wheel
from tests.unit.skills.test_python_metadata_plan import digest

MODULE = "agent_hub.skills.sandbox.python_metadata_planning"


def test_root_metadata_planner_exists() -> None:
    assert importlib.util.find_spec(MODULE) is not None, "Root metadata planner is missing"


@pytest.fixture
def configured(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    assert importlib.util.find_spec(MODULE) is not None, "Root metadata planner is missing"
    api = importlib.import_module(MODULE)
    store = importlib.import_module("agent_hub.skills.sandbox.python_dependency_index_store")
    catalog = importlib.import_module("agent_hub.skills.sandbox.python_input_supply_catalog")
    fs = FileSystem()
    monkeypatch.setattr(api, "os", fs)
    monkeypatch.setattr(store, "os", fs)
    monkeypatch.setattr(catalog, "os", fs)
    monkeypatch.setattr(store, "sys", SimpleNamespace(platform="linux"))
    lock = ModuleType("fcntl")
    for name, value in (("LOCK_SH", 1), ("LOCK_EX", 2), ("LOCK_NB", 4), ("flock", fs.flock)):
        monkeypatch.setattr(lock, name, value, raising=False)
    monkeypatch.setitem(sys.modules, "fcntl", lock)
    monkeypatch.setattr(api, "_require_root", lambda: None)
    invocation = ProjectInvocation(execution_id="metadata_1", tenant_id=UUID(int=1),
        run_id=UUID(int=2), project_id="billing", session_id="draft",
        sandbox_profile="restricted", timeout_seconds=60)
    workspace = tmp_path / "workspace"
    project = workspace / str(invocation.tenant_id) / "projects/billing/sessions/draft"
    project.mkdir(parents=True)
    for filename, raw in (("ACCEPTANCE.json", b'{"goal":"test"}'),
                          ("pyproject.toml", b'[project]\nname="billing"\n'),
                          ("requirements.lock", b"one==1.0\n"),
                          ("app.py", b"raise AssertionError('never execute')\n")):
        (project / filename).write_bytes(raw)
    wheels = tuple(wheel(name=name) for name in ("one", "two", "unused"))
    root = root_configuration()
    supply = RootSupplyConfig(Path(STATE), tuple(w[0] for w in wheels), 64 * 1024**2, 0, 60)
    root_file, supply_file = tmp_path / "root.json", tmp_path / "supply.json"
    root_file.write_text(json.dumps(root.to_payload()), encoding="ascii")
    supply_file.write_text(json.dumps(supply.to_payload()), encoding="ascii")
    reads: list[tuple[str, int]] = []

    def read_root() -> RootConfig:
        reads.append(("root", threading.get_ident()))
        return RootConfig.from_payload(json.loads(root_file.read_bytes()))

    def read_supply() -> RootSupplyConfig:
        reads.append(("supply", threading.get_ident()))
        return RootSupplyConfig.from_payload(json.loads(supply_file.read_bytes()))

    monkeypatch.setattr(api, "read_root_configuration", read_root)
    monkeypatch.setattr(api, "read_supply_configuration", read_supply)
    value = SimpleNamespace(api=api, store=store, fs=fs, root=root, supply=supply,
        root_file=root_file, supply_file=supply_file, wheels=wheels, invocation=invocation,
        policy=BrokerPolicy(project_workspace_root=workspace), project=project, reads=reads)
    yield value
    assert not fs.fds, "all simulated FDs must close"


async def request_for(c: Any) -> PythonMetadataPlanningRequest:
    scope = PythonMetadataScope(tenant_id=c.invocation.tenant_id, run_id=c.invocation.run_id,
        user_id=UUID(int=3), project_id="billing", session_id="draft", actor="main_agent",
        actor_role="operator", request_sha256="a" * 64, permissions_sha256="b" * 64)
    return PythonMetadataPlanningRequest(invocation=c.invocation, scope=scope,
        draft=PythonMetadataDraft(
            source_sha256=await project_source_fingerprint(c.policy.project_workspace_root,
                                                           c.invocation),
            acceptance_sha256=hashlib.sha256((c.project / "ACCEPTANCE.json").read_bytes()).hexdigest(),
            manifest_sha256=hashlib.sha256((c.project / "pyproject.toml").read_bytes()).hexdigest(),
            lock_sha256=hashlib.sha256((c.project / "requirements.lock").read_bytes()).hexdigest()),
        expected_root_config_sha256=digest(c.root.to_payload()),
        expected_catalog_sha256=c.supply.digest)


async def plan(c: Any, request: Any = None) -> Any:
    request = await request_for(c) if request is None else request
    response = await c.api.plan_python_dependency_metadata(request, policy=c.policy)
    response.require_for(request)
    return response


def install_index(c: Any) -> PythonDependencyMetadataIndex:
    index = PythonDependencyMetadataIndex(c.supply.digest,
        tuple(extract_python_wheel_metadata(entry, raw) for entry, raw in c.wheels))
    c.fs.add(STATE + "/dependency-metadata-" + c.supply.digest + ".json",
             stat.S_IFREG | 0o600, index.to_bytes())
    return index


def install_wheels(c: Any, count: int = 3) -> None:
    for entry, raw in c.wheels[:count]:
        c.fs.add(WHEELS + "/" + entry.filename, stat.S_IFREG | 0o444, raw)


@pytest.mark.asyncio
@pytest.mark.parametrize("count,mode,missing", [(0, "download_required", 3),
    (2, "download_required", 1), (3, "publish_cached", 0)])
async def test_actual_catalog_and_wheel_metadata(configured: Any, count: int,
                                                mode: str, missing: int) -> None:
    install_wheels(configured, count)
    response = await plan(configured)
    assert response.ok and response.plan.mode == mode
    assert response.plan.catalog_count == 3 and response.plan.missing_count == missing
    assert response.plan.missing_bytes == sum(e.size_bytes for e, _ in configured.wheels[count:])
    assert all(thread != threading.get_ident() for _, thread in configured.reads)
    assert [name for name, _ in configured.reads] == ["root", "supply", "root", "supply"]
    assert not any(event in {"create", "link", "unlink", "fsync"}
                   for event, _ in configured.fs.events)


@pytest.mark.asyncio
async def test_ready_reads_no_wheel_even_over_bootstrap_quota(configured: Any) -> None:
    c = configured
    c.supply = replace(c.supply, max_total_bytes=1)
    c.supply_file.write_text(json.dumps(c.supply.to_payload()), encoding="ascii")
    index = install_index(c)
    result = await plan(c)
    assert result.ok and result.plan.mode == "ready"
    assert result.plan.metadata_index_sha256 == index.sha256
    assert not any(path.startswith(WHEELS) for _, path in c.fs.events)


@pytest.mark.asyncio
@pytest.mark.parametrize("location", ["request", "invocation", "scope", "draft"])
async def test_model_copy_extras_rejected_before_root(configured: Any,
    monkeypatch: pytest.MonkeyPatch, location: str) -> None:
    c = configured
    request = await request_for(c)
    if location == "request":
        request = request.model_copy(update={"private_path": "/secret"})
    else:
        request = request.model_copy(update={location: getattr(request, location).model_copy(
            update={"private_path": "/secret"})})
    monkeypatch.setattr(c.api, "_require_root", lambda: pytest.fail("invalid request reached Root"))
    with pytest.raises(ValueError, match="^metadata_plan_request_invalid$"):
        await c.api.plan_python_dependency_metadata(request, policy=c.policy)
    assert not c.reads


@pytest.mark.asyncio
@pytest.mark.parametrize("code,expected", [("metadata_untrusted", "metadata_plan_metadata_untrusted"),
    ("catalog_changed", "metadata_plan_catalog_changed"), ("timeout", "metadata_plan_timeout"),
    ("unsupported", "metadata_plan_unavailable")])
async def test_index_failure_never_means_missing(configured: Any,
    monkeypatch: pytest.MonkeyPatch, code: str, expected: str) -> None:
    def fail(*args: Any, **kwargs: Any) -> Any:
        raise configured.store.PythonDependencyIndexError(code)
    monkeypatch.setattr(configured.api, "read_dependency_metadata_index", fail)
    result = await plan(configured)
    assert result.error == expected and result.plan is None
    assert not configured.fs.events


@pytest.mark.asyncio
@pytest.mark.parametrize("control,limit", [("ACCEPTANCE.json", 128 * 1024),
    ("pyproject.toml", 256 * 1024), ("requirements.lock", 256 * 1024)])
async def test_actual_control_limits(configured: Any, control: str, limit: int) -> None:
    (configured.project / control).write_bytes(b"x" * (limit + 1))
    result = await plan(configured)
    assert result.error == "metadata_plan_draft_changed"


@pytest.mark.asyncio
@pytest.mark.parametrize("filename", ["app.py", "ACCEPTANCE.json", "pyproject.toml", "requirements.lock"])
@pytest.mark.parametrize("when", ["before", "during"])
async def test_full_draft_fingerprint_before_and_after(configured: Any,
    monkeypatch: pytest.MonkeyPatch, filename: str, when: str) -> None:
    c = configured
    request = await request_for(c)
    def drift(*args: Any, **kwargs: Any) -> Any:
        (c.project / filename).write_bytes(b"changed")
        return install_index(c)
    if when == "before":
        drift()
    else:
        monkeypatch.setattr(c.api, "read_dependency_metadata_index", drift)
    assert (await plan(c, request)).error == "metadata_plan_draft_changed"


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["acceptance_sha256", "manifest_sha256", "lock_sha256"])
async def test_control_sha_must_match_draft(configured: Any, field: str) -> None:
    request = await request_for(configured)
    request = request.model_copy(update={"draft": request.draft.model_copy(update={field: "f" * 64})})
    assert (await plan(configured, request)).error == "metadata_plan_draft_changed"


@pytest.mark.asyncio
@pytest.mark.parametrize("control", ["root", "supply"])
@pytest.mark.parametrize("when", ["before", "during"])
async def test_configuration_digests_rechecked(configured: Any,
    monkeypatch: pytest.MonkeyPatch, control: str, when: str) -> None:
    c = configured
    request = await request_for(c)
    def drift(*args: Any, **kwargs: Any) -> Any:
        file = c.root_file if control == "root" else c.supply_file
        payload = json.loads(file.read_bytes())
        if control == "root":
            payload["wheel_max_bytes"] += 1
        else:
            payload["min_free_bytes"] += 1
        file.write_text(json.dumps(payload), encoding="ascii")
        return install_index(c)
    if when == "before":
        drift()
    else:
        monkeypatch.setattr(c.api, "read_dependency_metadata_index", drift)
    assert (await plan(c, request)).error == "metadata_plan_catalog_changed"


@pytest.mark.asyncio
@pytest.mark.parametrize("quota", ["root", "supply"])
async def test_full_catalog_quota_precedes_any_cache_io(configured: Any, quota: str) -> None:
    c = configured
    if quota == "root":
        c.root = replace(c.root, wheel_max_bytes=sum(e.size_bytes for e, _ in c.wheels[:2]))
        c.root_file.write_text(json.dumps(c.root.to_payload()), encoding="ascii")
    else:
        c.supply = replace(c.supply, max_total_bytes=sum(e.size_bytes for e, _ in c.wheels[:2]))
        c.supply_file.write_text(json.dumps(c.supply.to_payload()), encoding="ascii")
    assert (await plan(c)).error == "metadata_plan_quota"
    assert not any(path.startswith(WHEELS) for _, path in c.fs.events)


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["hash", "metadata", "symlink", "hardlink", "mode", "owner",
    "device", "open_disappear", "after_read", "end_change", "missing_appears"])
async def test_cache_fails_closed(configured: Any, monkeypatch: pytest.MonkeyPatch, fault: str) -> None:
    c = configured
    install_wheels(c)
    entry, raw = c.wheels[0]
    path = WHEELS + "/" + entry.filename
    node = c.fs.nodes[path]
    if fault == "hash": node.data = b"x" * len(raw)
    if fault == "metadata":
        bad_entry, bad_raw = wheel(name="one", metadata=b"Metadata-Version: 2.4\nName: foreign\nVersion: 1.0\n\n")
        c.wheels = ((bad_entry, bad_raw), *c.wheels[1:])
        c.supply = replace(c.supply, catalog=tuple(e for e, _ in c.wheels))
        c.supply_file.write_text(json.dumps(c.supply.to_payload()), encoding="ascii")
        node.data = bad_raw
    if fault == "symlink": node.mode = stat.S_IFLNK | 0o777
    if fault == "hardlink": node.nlink = 2
    if fault == "mode": node.mode |= 0o022
    if fault == "owner": node.uid = 1000
    if fault == "device": node.dev = 2
    if fault == "open_disappear":
        original = c.fs.open
        def disappear(name: str, flags: int, *args: Any, **kwargs: Any) -> int:
            if name == entry.filename:
                del c.fs.nodes[path]
            return original(name, flags, *args, **kwargs)
        monkeypatch.setattr(c.fs, "open", disappear)
        c.fs.supports_dir_fd = {c.fs.open, c.fs.stat, c.fs.link, c.fs.unlink}
    if fault == "after_read":
        c.fs.after_read = lambda changed, name: setattr(changed, "ctime", 2) if name == path else None
    if fault in {"end_change", "missing_appears"}:
        if fault == "missing_appears": del c.fs.nodes[path]
        original_source = c.api.project_source_fingerprint
        calls = 0
        async def source(*args: Any, **kwargs: Any) -> str:
            nonlocal calls
            calls += 1
            if calls == 2:
                if fault == "end_change": node.ctime += 1
                else: c.fs.nodes[path] = node
            return await original_source(*args, **kwargs)
        monkeypatch.setattr(c.api, "project_source_fingerprint", source)
    result = await plan(c)
    assert result.error == "metadata_plan_cache_untrusted" and result.plan is None
    assert "/private" not in result.model_dump_json()


@pytest.mark.asyncio
async def test_cancelled_worker_holds_inspection_slot(configured: Any,
    monkeypatch: pytest.MonkeyPatch) -> None:
    c = configured
    request = await request_for(c)
    inspection = importlib.import_module("agent_hub.skills.sandbox.python_dependency_inspection")
    started, release = threading.Event(), threading.Event()
    original = c.api.read_root_configuration
    def slow() -> RootConfig:
        started.set()
        assert release.wait(5)
        return original()
    monkeypatch.setattr(c.api, "read_root_configuration", slow)
    task = asyncio.create_task(plan(c, request))
    try:
        async with asyncio.timeout(2):
            while not started.is_set(): await asyncio.sleep(0.005)
        task.cancel()
        with pytest.raises(asyncio.CancelledError): await task
        assert not inspection._WORKER_SLOT.acquire(blocking=False)
        waiting = asyncio.create_task(plan(c, request))
        await asyncio.sleep(0.05)
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError): await waiting
        assert not inspection._WORKER_SLOT.acquire(blocking=False)
    finally:
        release.set()
        async with asyncio.timeout(3):
            while not inspection._WORKER_SLOT.acquire(blocking=False): await asyncio.sleep(0.005)
        inspection._WORKER_SLOT.release()


@pytest.mark.asyncio
async def test_queued_timeout_counts_against_original_deadline(configured: Any) -> None:
    c = configured
    request = await request_for(c)
    request = request.model_copy(update={"invocation": c.invocation.model_copy(update={"timeout_seconds": 1})})
    inspection = importlib.import_module("agent_hub.skills.sandbox.python_dependency_inspection")
    assert inspection._WORKER_SLOT.acquire(blocking=False)
    before = time.monotonic()
    try:
        result = await plan(c, request)
        assert result.error == "metadata_plan_timeout"
        assert 0.9 <= time.monotonic() - before < 2
        assert not c.reads
    finally:
        inspection._WORKER_SLOT.release()
