"""Portable Root workflow simulation; protected FD/native publication need Linux probes."""

from __future__ import annotations

import importlib
import importlib.util
import json
import stat
import time
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from agent_hub.skills.sandbox.python_dependency_metadata import (
    PythonDependencyMetadataIndex,
    extract_python_wheel_metadata,
)
from agent_hub.skills.sandbox.python_metadata_plan import build_python_metadata_plan
from agent_hub.skills.sandbox.python_metadata_plan_protocol import PythonMetadataPlanningResponse
from agent_hub.skills.sandbox.python_metadata_preparation_protocol import (
    PythonMetadataPreparationRequest,
)
from agent_hub.skills.sandbox.python_project_profile import PythonProjectProfileScope
from agent_hub.subprocess_cleanup import ProcessCleanupIncomplete
from tests.unit.skills.sandbox.test_python_metadata_preparation_broker import (
    INSPECT,
    INSPECT_KEY,
    KEY,
    PREPARE,
)
from tests.unit.skills.test_python_dependency_index_store import FileSystem
from tests.unit.skills.test_python_dependency_metadata import wheel
from tests.unit.skills.test_python_metadata_plan import inputs
from tests.unit.skills.test_python_metadata_planning import (
    configured as configured_observations,
)
from tests.unit.skills.test_python_metadata_planning import (
    install_wheels,
    request_for,
)

MODULE = "agent_hub.skills.sandbox.python_metadata_preparation_worker"
observed_inputs = configured_observations


def test_root_worker_exists() -> None:
    assert importlib.util.find_spec(MODULE) is not None, "v10 Root preparation worker missing"


@pytest.fixture
def api() -> Any:
    assert importlib.util.find_spec(MODULE) is not None, "v10 Root preparation worker missing"
    return importlib.import_module(MODULE)


@pytest.fixture
def configured(api: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    planning, root, supply, _ = inputs()
    planning = planning.model_copy(update={"invocation": planning.invocation.model_copy(
        update={"execution_id": KEY})})
    class Claims(FileSystem):
        def mkdir(self, name: str, mode: int, *, dir_fd: int) -> None:
            path = self.path(name, dir_fd)
            if path in self.nodes:
                raise FileExistsError
            self.add(path, stat.S_IFDIR | mode)
            self.events.append(("mkdir", path))

    fs = Claims()
    for path in (supply.state_root.as_posix(), root.stage_root.as_posix(), root.runtime_root.as_posix()):
        fs.add(path, stat.S_IFDIR | 0o700)
    stage = root.stage_root / (KEY + "-abcdefgh")
    fs.add(stage.as_posix(), stat.S_IFDIR | 0o700)
    value = SimpleNamespace(api=api, root=root, supply=supply, planning=planning, fs=fs,
        stage=stage, events=[], locked=False, cached=(), published=[], index=None,
        wheels={w[0].filename: w[1] for w in (wheel(name=e.name) for e in supply.catalog)})

    @contextmanager
    def roots(*args: Any) -> Any:
        yield SimpleNamespace(stage_root_fd=90, pins=[])

    @contextmanager
    def admission(parent: int, deadline: float) -> Any:
        assert not value.locked and deadline == value.deadline
        value.locked = True
        value.events.append("lock")
        try:
            yield
        finally:
            value.locked = False
            value.events.append("unlock")

    def open_root(path: Path, owner: int, stack: Any, pins: Any, **kwargs: Any) -> int:
        fd = fs.open(path.as_posix(), fs.O_DIRECTORY | fs.O_NOFOLLOW | fs.O_CLOEXEC)
        stack.callback(fs.close, fd)
        return fd

    def open_directory(parent: int, name: str, owner: int, stack: Any, pins: Any, **kwargs: Any) -> int:
        fd = fs.open(name, fs.O_DIRECTORY | fs.O_NOFOLLOW | fs.O_CLOEXEC, dir_fd=parent)
        info = fs.fstat(fd)
        if info.st_uid or info.st_gid or stat.S_IMODE(info.st_mode) != 0o700:
            fs.close(fd)
            raise ValueError("untrusted claim")
        stack.callback(fs.close, fd)
        return fd

    def once(parent: int, name: str, content: object) -> None:
        path = fs.path(name, parent)
        if path in fs.nodes:
            raise FileExistsError
        fs.add(path, stat.S_IFREG | 0o600, json.dumps(content).encode())
        value.events.append("write:" + name)
        fs.fsync(parent)

    def read(parent: int, name: str, owner: int, pins: Any) -> bytes:
        path = fs.path(name, parent)
        if path not in fs.nodes:
            raise FileNotFoundError
        node = fs.nodes[path]
        if not stat.S_ISREG(node.mode) or node.uid or node.nlink != 1 or stat.S_IMODE(node.mode) != 0o600:
            raise ValueError("untrusted control")
        return node.data

    async def source(*args: Any) -> None:
        value.events.append("source")

    def controls(*args: Any) -> None:
        value.events.append("controls")

    async def observe(request: Any, policy: Any, deadline: float) -> Any:
        assert deadline == value.deadline and not value.locked
        value.events.append("observe")
        plan = build_python_metadata_plan(request, root=value.root, supply=value.supply,
            cached=value.cached, index=value.index)
        return PythonMetadataPlanningResponse(scope=PythonProjectProfileScope.for_invocation(request.invocation),
            ok=True, plan=plan)

    def cached(*args: Any) -> Any:
        return value.cached, lambda: None

    async def fetch(entries: Any, invocation: Any, policy: Any, root: Any, supply: Any,
        deadline: float) -> dict[str, bytes]:
        assert not value.locked and not fs.locks and deadline == value.deadline
        assert not hasattr(invocation, "original_reference") and not invocation.install_allowed
        value.events.append("fetch")
        value.fetched = entries
        return {entry.filename: value.wheels[entry.filename] for entry in entries}

    def publish(cache: Any, claim: Any, entry: Any, raw: bytes, *, deadline: float) -> str:
        assert value.locked and deadline == value.deadline
        extract_python_wheel_metadata(entry, raw)
        value.events.append("wheel:" + entry.name)
        value.published.append(entry)
        return "published"

    def publish_index(root: Any, supply: Any, *, deadline: float) -> Any:
        assert not value.locked and deadline == value.deadline
        assert set(value.cached + tuple(value.published)) == set(supply.catalog)
        value.events.append("index")
        value.index = PythonDependencyMetadataIndex(supply.digest, tuple(
            extract_python_wheel_metadata(entry, value.wheels[entry.filename]) for entry in supply.catalog))
        return value.index

    monkeypatch.setattr(api, "os", fs)
    monkeypatch.setattr(api, "_native", lambda: None)
    monkeypatch.setattr(api, "_roots", roots)
    monkeypatch.setattr(api, "_admission", admission)
    monkeypatch.setattr(api, "_root", open_root)
    monkeypatch.setattr(api, "_open_directory", open_directory)
    monkeypatch.setattr(api, "_read_control", read)
    monkeypatch.setattr(api, "_once", once)
    monkeypatch.setattr(api, "read_root_configuration", lambda: value.root)
    monkeypatch.setattr(api, "read_supply_configuration", lambda: value.supply)
    monkeypatch.setattr(api, "validate_configuration", lambda *args: None)
    monkeypatch.setattr(api, "_observe", observe)
    monkeypatch.setattr(api, "_source", source)
    monkeypatch.setattr(api, "_controls", controls)
    monkeypatch.setattr(api, "_cached", cached)
    monkeypatch.setattr(api, "fetch_catalog_wheels", fetch)
    monkeypatch.setattr(api, "publish_catalog_wheel", publish)
    monkeypatch.setattr(api, "publish_dependency_metadata_index", publish_index)
    monkeypatch.setattr(api, "read_dependency_metadata_index", lambda *a, **kw: value.index)
    monkeypatch.setattr(api, "_free_bytes", lambda path: 2**40)
    value.policy = SimpleNamespace()
    value.deadline = time.monotonic() + 60
    yield value
    assert not fs.fds


def request(c: Any, inspect: bool = False) -> Any:
    plan = build_python_metadata_plan(c.planning, root=c.root, supply=c.supply, cached=c.cached)
    planning = c.planning if not inspect else c.planning.model_copy(update={
        "invocation": c.planning.invocation.model_copy(update={"execution_id": INSPECT_KEY})})
    return PythonMetadataPreparationRequest(action=INSPECT if inspect else PREPARE, planning=planning,
        expected_plan_sha256=plan.sha256, preparation_execution_id=KEY if inspect else None)


async def execute(c: Any, value: Any = None) -> Any:
    value = value or request(c)
    stage = c.root.stage_root / (value.invocation.execution_id + "-abcdefgh")
    return await c.api.execute(value, c.policy, c.root, c.supply, stage,
        deadline=c.deadline)


@pytest.mark.parametrize("cached_count", [0, 1, 3])
async def test_complete_catalog_missing_subset_and_receipt_after_full_index_reread(
    configured: Any, cached_count: int,
) -> None:
    c = configured
    c.cached = c.supply.catalog[:cached_count]
    req = request(c)
    result = await execute(c, req)
    result.require_for(req)
    assert result.receipt.plan.catalog_count == result.receipt.index_record_count == 3
    assert result.receipt.index_sha256 == c.index.sha256
    assert c.fetched == c.supply.catalog[cached_count:]
    assert c.events.index("observe") < c.events.index("write:intent.json") < c.events.index("fetch")
    assert c.events.index("unlock") < c.events.index("index") < c.events.index("write:receipt.json")
    assert "https://" not in result.model_dump_json()
    assert not c.locked and not c.fs.locks


@pytest.mark.parametrize("failure", ["hash", "ready", "total", "disk", "deadline"])
async def test_preflight_rejects_before_any_claim_or_network(configured: Any,
    monkeypatch: pytest.MonkeyPatch, failure: str) -> None:
    c = configured
    req = request(c)
    if failure == "hash": req = req.model_copy(update={"expected_plan_sha256": "0" * 64})
    if failure == "ready":
        c.index = PythonDependencyMetadataIndex(c.supply.digest, tuple(
            extract_python_wheel_metadata(e, c.wheels[e.filename]) for e in c.supply.catalog))
    if failure == "total": c.root = replace(c.root, wheel_max_bytes=1)
    if failure == "disk": monkeypatch.setattr(c.api, "_free_bytes", lambda path: c.supply.min_free_bytes + 2 * sum(e.size_bytes for e in c.supply.catalog))
    if failure == "deadline": c.deadline = time.monotonic() - 1
    with pytest.raises(ValueError):
        await execute(c, req)
    assert not any(event in {"fetch", "index", "write:intent.json"} for event in c.events)
    assert not any(event == "mkdir" for event, _ in c.fs.events)


async def test_claim_is_exclusive_and_never_replayed(configured: Any) -> None:
    c = configured
    req = request(c)
    claim = (c.supply.state_root / ("metadata-" + req.expected_plan_sha256)).as_posix()
    c.fs.add(claim, stat.S_IFDIR | 0o700)
    c.fs.add(claim + "/intent.json", stat.S_IFREG | 0o600, b"foreign evidence")
    with pytest.raises(ValueError, match="metadata_prepare_consumed"):
        await execute(c, req)
    assert c.fs.nodes[claim + "/intent.json"].data == b"foreign evidence"
    assert "fetch" not in c.events and "index" not in c.events


@pytest.mark.parametrize("phase", ["post_fetch", "post_wheel", "index_reread", "receipt"])
async def test_failure_retains_intent_and_never_infers_receipt(configured: Any,
    monkeypatch: pytest.MonkeyPatch, phase: str) -> None:
    c = configured
    req = request(c)
    if phase == "post_fetch":
        original = c.api.fetch_catalog_wheels
        async def fetch(*a: Any, **kw: Any) -> Any:
            result = await original(*a, **kw)
            c.root = replace(c.root, wheel_max_bytes=c.root.wheel_max_bytes + 1)
            return result
        monkeypatch.setattr(c.api, "fetch_catalog_wheels", fetch)
    if phase == "post_wheel":
        original_publish = c.api.publish_catalog_wheel
        def publish(*a: Any, **kw: Any) -> Any:
            original_publish(*a, **kw)
            raise OSError("private")
        monkeypatch.setattr(c.api, "publish_catalog_wheel", publish)
    if phase == "index_reread":
        monkeypatch.setattr(c.api, "read_dependency_metadata_index", lambda *a, **kw: None)
    if phase == "receipt":
        original_once = c.api._once
        def once(parent: int, name: str, content: Any) -> None:
            if name == "receipt.json": raise OSError("private")
            original_once(parent, name, content)
        monkeypatch.setattr(c.api, "_once", once)
    with pytest.raises((ValueError, ProcessCleanupIncomplete)):
        await execute(c, req)
    claim = (c.supply.state_root / ("metadata-" + req.expected_plan_sha256)).as_posix()
    assert claim + "/intent.json" in c.fs.nodes and claim + "/receipt.json" not in c.fs.nodes
    c.root = replace(c.root, wheel_max_bytes=inputs()[1].wheel_max_bytes)
    result = await execute(c, request(c, True))
    assert result.status.state == "unknown" and result.status.receipt is None


async def test_inspect_absent_has_no_write_fetch_publish_or_new_claim(configured: Any) -> None:
    c = configured
    result = await execute(c, request(c, True))
    assert result.status.state == "absent"
    assert not any(event in {"fetch", "index", "write:intent.json"} for event in c.events)
    assert not any(event == "mkdir" for event, _ in c.fs.events)


async def test_inspect_prepared_requires_matching_receipt_and_current_complete_index(configured: Any) -> None:
    c = configured
    prepared = await execute(c)
    before = list(c.events)
    req = request(c, True)
    result = await execute(c, req)
    result.require_for(req)
    assert result.status.state == "prepared" and result.status.receipt == prepared.receipt
    assert not any(event in {"fetch", "index", "write:intent.json", "write:receipt.json"}
        for event in c.events[len(before):])


@pytest.mark.parametrize("field", ["preparation_execution_id", "scope", "draft", "schema_version"])
async def test_inspect_rejects_misbound_or_ambiguous_intent(configured: Any, field: str) -> None:
    c = configured
    req = request(c)
    await execute(c, req)
    path = (c.supply.state_root / ("metadata-" + req.expected_plan_sha256) / "intent.json").as_posix()
    payload = json.loads(c.fs.nodes[path].data)
    if field == "preparation_execution_id": payload[field] = "metadata_prepare_" + "c" * 32
    elif field == "schema_version": payload[field] = True
    elif field == "scope": payload["plan"]["scope"]["user_id"] = "00000000-0000-0000-0000-000000000009"
    else: payload["plan"]["draft"]["source_sha256"] = "0" * 64
    c.fs.nodes[path].data = json.dumps(payload).encode()
    with pytest.raises(ValueError):
        await execute(c, request(c, True))


async def test_draft_observation_never_holds_stage_admission(configured: Any,
    monkeypatch: pytest.MonkeyPatch) -> None:
    c = configured
    original = c.api._source

    async def source(*args: Any) -> None:
        assert not c.locked, "draft observation holds stage admission"
        await original(*args)

    monkeypatch.setattr(c.api, "_source", source)
    assert (await execute(c)).ok


async def test_inspect_rejects_duplicate_receipt_keys(configured: Any) -> None:
    c = configured
    req = request(c)
    await execute(c, req)
    path = (c.supply.state_root / ("metadata-" + req.expected_plan_sha256) / "receipt.json").as_posix()
    c.fs.nodes[path].data = c.fs.nodes[path].data.replace(b'"schema_version": 1,',
        b'"schema_version": 1, "schema_version": 1,', 1)
    with pytest.raises(ValueError):
        await execute(c, request(c, True))


@pytest.mark.parametrize("bad_stage", ["wrong_execution", "outside", "bad_suffix"])
async def test_stage_identity_is_checked_before_observation(configured: Any, bad_stage: str) -> None:
    c = configured
    stage = c.root.stage_root / (KEY + "-abcdefgh")
    if bad_stage == "wrong_execution": stage = stage.with_name(INSPECT_KEY + "-abcdefgh")
    if bad_stage == "outside": stage = c.root.wheel_root / stage.name
    if bad_stage == "bad_suffix": stage = stage.with_name(KEY + "-invalid")
    with pytest.raises(ValueError, match="metadata_prepare_request_invalid"):
        await c.api.execute(request(c), c.policy, c.root, c.supply, stage, deadline=c.deadline)
    assert not c.events


@pytest.mark.parametrize("state", ["symlink", "wrong_owner", "writable"])
async def test_inspect_refuses_untrusted_claim_directory(configured: Any, state: str) -> None:
    c = configured
    req = request(c, True)
    path = (c.supply.state_root / ("metadata-" + req.expected_plan_sha256)).as_posix()
    node = c.fs.add(path, stat.S_IFDIR | 0o700)
    if state == "symlink": node.mode = stat.S_IFLNK | 0o777
    if state == "wrong_owner": node.uid = 10001
    if state == "writable": node.mode = stat.S_IFDIR | 0o777
    with pytest.raises((ValueError, OSError)):
        await execute(c, req)
    assert "fetch" not in c.events and "index" not in c.events


def test_cli_rejects_non_linux_before_reading_any_request(api: Any,
    monkeypatch: pytest.MonkeyPatch, capsys: Any) -> None:
    monkeypatch.setattr(api, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(api, "_root", lambda *a, **k: pytest.fail("request opened on unsupported host"))
    assert api.main() == 2
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("cached_count,drift,bad_cache", [
    (0, False, False), (1, False, False), (3, False, False), (0, True, False), (1, False, True),
])
async def test_actual_v9_draft_cache_observer_and_complete_index_store(
    configured: Any, observed_inputs: Any, monkeypatch: pytest.MonkeyPatch,
    cached_count: int, drift: bool, bad_cache: bool,
) -> None:
    c, observed = configured, observed_inputs
    c.root, c.supply, c.policy = observed.root, observed.supply, observed.policy
    planning = await request_for(observed)
    c.planning = planning.model_copy(update={"invocation": planning.invocation.model_copy(
        update={"execution_id": KEY})})
    c.cached = c.supply.catalog[:cached_count]
    install_wheels(observed, cached_count)
    for name in ("_observe", "_source", "_controls", "_cached"):
        monkeypatch.setattr(c.api, name, getattr(observed.api, name))
    monkeypatch.setattr(c.api, "publish_dependency_metadata_index", observed.store.publish_dependency_metadata_index)
    monkeypatch.setattr(c.api, "read_dependency_metadata_index", observed.store.read_dependency_metadata_index)
    original_fetch, original_publish = c.api.fetch_catalog_wheels, c.api.publish_catalog_wheel

    async def fetch(*args: Any, **kwargs: Any) -> Any:
        assert not observed.fs.locks and not c.locked
        result = await original_fetch(*args, **kwargs)
        if drift: (observed.project / "app.py").write_bytes(b"changed after fetch")
        return result

    def publish(cache: Path, claim: Path, entry: Any, raw: bytes, *, deadline: float) -> str:
        result = original_publish(cache, claim, entry, raw, deadline=deadline)
        path = (cache / entry.filename).as_posix()
        assert path not in observed.fs.nodes, "existing wheel would be overwritten"
        observed.fs.add(path, stat.S_IFREG | 0o444, raw)
        assert isinstance(result, str)
        return result

    monkeypatch.setattr(c.api, "fetch_catalog_wheels", fetch)
    monkeypatch.setattr(c.api, "publish_catalog_wheel", publish)
    if bad_cache:
        path = (c.root.wheel_root / c.supply.catalog[0].filename).as_posix()
        node = observed.fs.nodes[path]
        node.data = b"x" * len(node.data)
        with pytest.raises(ValueError, match="metadata_plan_cache_untrusted"):
            await execute(c)
        assert not c.published and "fetch" not in c.events
        assert not any(event == "mkdir" for event, _ in c.fs.events)
    elif drift:
        with pytest.raises(ValueError, match="metadata_plan_draft_changed"):
            await execute(c)
        assert not c.published
    else:
        result = await execute(c)
        index = observed.store.read_dependency_metadata_index(c.supply, deadline=c.deadline)
        assert result.receipt.index_record_count == 3 and result.receipt.index_sha256 == index.sha256
        assert {r.wheel.name for r in index.records} == {"one", "two", "unused"}
