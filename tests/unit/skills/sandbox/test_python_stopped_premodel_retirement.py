"""Strict protocol and logical cache boundaries; no fake native/DB authority."""

from __future__ import annotations

import copy
import hashlib
import importlib
import importlib.util
import json
import os
import stat
import time
from contextlib import ExitStack, contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pytest

from agent_hub.skills.sandbox import python_project_cache as cache_api
from agent_hub.skills.sandbox.python_project_protocol import (
    PythonEnvironmentReference,
    PythonFailedMaintenanceInvocation,
    PythonProjectInvocation,
)
from agent_hub.subprocess_cleanup import ProcessCleanupIncomplete

MODULE = "agent_hub.skills.sandbox.python_premodel_retirement"


def dto() -> Any:
    assert importlib.util.find_spec(MODULE) is not None, "stopped pre-model DTO module missing"
    return importlib.import_module(MODULE)


def invocation() -> PythonProjectInvocation:
    return PythonProjectInvocation.model_validate(
        {
            "execution_id": "original",
            "tenant_id": UUID(int=1),
            "run_id": UUID(int=2),
            "project_id": "billing",
            "session_id": "one",
            "sandbox_profile": "workspace_write",
            "timeout_seconds": 30,
            "install_allowed": True,
            "source_kind": "original",
            "original_reference": {
                "schema_version": 1,
                "contract_sha256": "a" * 64,
                "input_source_sha256": "b" * 64,
                "file_count": 1,
            },
            "recipe_sha256": "c" * 64,
            "environment_input_sha256": "d" * 64,
        },
        strict=True,
    )


def maintenance() -> PythonFailedMaintenanceInvocation:
    item = invocation()
    return PythonFailedMaintenanceInvocation(
        execution_id="python_release_expired_" + "1" * 32,
        tenant_id=item.tenant_id,
        run_id=item.run_id,
        project_id=item.project_id,
        session_id=item.session_id,
    )


def encoded(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def identity(inode: int, mode: int, size: int = 0) -> tuple[int, ...]:
    return (1, inode, mode, 0, 0, 1, size, 100, 100)


def stage_payload(
    execution: str = "original", root: str = "stage", **change: Any
) -> dict[str, Any]:
    item = invocation()
    marker = {
        "execution_id": execution,
        "tenant_id": str(item.tenant_id),
        "run_id": str(item.run_id),
        "project_id": item.project_id,
        "session_id": item.session_id,
        "source_sha256": None,
        "unit": f"agent-hub-project-{execution}.service",
        "state": "UNKNOWN",
    }
    marker.update(change)
    raw = json.dumps(marker, sort_keys=True) + "\n"  # Original noncanonical bytes must survive.
    return {
        "root": root,
        "name": execution + "-abcdefgh",
        "record": raw,
        "record_sha256": digest(raw.encode()),
        "identities": (
            identity(41, stat.S_IFDIR | 0o700),
            identity(42, stat.S_IFREG | 0o644, len(raw)),
        ),
    }


def snapshot_payload() -> dict[str, Any]:
    item = invocation()
    scope = cache_api._scope(item).model_dump(mode="json")
    reservation = {
        "schema_version": 1,
        "scope": scope,
        "recipe_sha256": item.recipe_sha256,
        "environment_input_sha256": item.environment_input_sha256,
        "origin_execution_id": "original",
        "expires_at": 1,
        "environment_limit_bytes": 1024,
    }
    reference = PythonEnvironmentReference(
        scope=cache_api._scope(item),
        recipe_sha256=item.recipe_sha256,
        environment_input_sha256=item.environment_input_sha256,
        environment_sha256="e" * 64,
        security_manifest_sha256="f" * 64,
        origin_execution_id="original",
    )
    raw, ref = encoded(reservation), encoded(reference.model_dump(mode="json"))
    return {
        "schema_version": 1,
        "cache_key": cache_api.python_project_cache_key(item),
        "scope": scope,
        "reservation": raw.decode(),
        "reference": ref.decode(),
        "reservation_sha256": digest(raw),
        "reference_sha256": digest(ref),
        "environment_tree_sha256": "3" * 64,
        "identities": (
            identity(20, stat.S_IFDIR | 0o700),
            identity(21, stat.S_IFREG | 0o600, len(raw)),
            identity(22, stat.S_IFREG | 0o600, len(ref)),
            identity(30, stat.S_IFDIR | 0o555),
        ),
        "stages": (stage_payload(),),
    }


def snapshot() -> Any:
    return dto().PythonStoppedPremodelSnapshot.model_validate_json(
        encoded(snapshot_payload()), strict=True
    )


def test_old_empty_failure_refuses_ready_and_new_operation_is_required(monkeypatch: Any) -> None:
    cache = cache_api.PythonProjectEnvironmentCache(
        Path.cwd() / "unused", cache_api.PythonProjectCachePolicy(2**30, 8, 2**30, 7200)
    )

    @contextmanager
    def locked(*args: Any, **kw: Any) -> Any:
        with ExitStack() as stack:
            yield 1, stack, []

    monkeypatch.setattr(cache, "_locked", locked)
    monkeypatch.setattr(cache, "_catalog", lambda *a: {"a" * 64})
    monkeypatch.setattr(cache_api, "_open_directory", lambda *a, **kw: 2)
    monkeypatch.setattr(
        cache_api, "_reservation", lambda *a: json.loads(snapshot_payload()["reservation"])
    )
    monkeypatch.setattr(cache_api, "_names", lambda *a: set(cache_api._READY))
    assert (
        cache.maintain_failed(
            maintenance(),
            deadline=time.monotonic() + 3,
            quiescence_check=lambda origin: pytest.fail(
                "old empty protocol reached READY quiescence"
            ),
        )
        is None
    )
    assert callable(getattr(cache, "maintain_stopped_premodel", None)), (
        "strict READY retirement missing"
    )


def test_snapshot_archives_exact_stage_bytes_with_stable_identity() -> None:
    s = snapshot()
    archive = s.archive()
    assert s.origin_execution_id == "original"
    assert archive == snapshot().archive()
    assert archive.type == "python_stopped_premodel_cache_archive"
    assert archive.content["snapshot"]["stages"][0]["record"] == stage_payload()["record"]
    assert archive.content["snapshot"]["reservation"] == s.reservation


def test_snapshot_revalidates_copied_stage_instances() -> None:
    s = snapshot()
    bad = s.stages[0].model_copy(update={"record_sha256": "0" * 64})
    with pytest.raises(ValueError):
        dto().PythonStoppedPremodelSnapshot.model_validate(
            s.model_dump() | {"stages": (bad,)}, strict=True
        )


def test_stage_deep_or_duplicate_json_is_a_bounded_validation_rejection() -> None:
    for record in (
        '{"x":' + '[' * 1100 + '0' + ']' * 1100 + '}',
        '{"execution_id":"original","execution_id":"foreign"}',
    ):
        p = stage_payload()
        p["record"] = record
        p["record_sha256"] = digest(record.encode())
        with pytest.raises(ValueError):
            dto().PythonStoppedPremodelStage.model_validate_json(encoded(p), strict=True)


@pytest.mark.parametrize(
    "defect",
    [
        "hash",
        "scope",
        "unit",
        "name",
        "coercion",
        "extra",
        "identity",
        "duplicate",
        "missing-origin",
        "root",
        "schema",
        "reference",
    ],
)
def test_snapshot_rejects_unbound_control_records(defect: str) -> None:
    p = snapshot_payload()
    if defect == "hash":
        p["stages"][0]["record_sha256"] = "0" * 64
    elif defect == "scope":
        p["stages"] = (stage_payload(run_id=str(UUID(int=7))),)
    elif defect == "unit":
        p["stages"] = (stage_payload(unit="agent-hub-project-foreign.service"),)
    elif defect == "name":
        p["stages"][0]["name"] = "../foreign"
    elif defect == "coercion":
        p["identities"] = ((*p["identities"][0][:-1], True), *p["identities"][1:])
    elif defect == "extra":
        p["stage_delete_authorized"] = True
    elif defect == "identity":
        p["stages"][0]["identities"] = (
            identity(41, stat.S_IFDIR | 0o777),
            identity(42, stat.S_IFREG | 0o644),
        )
    elif defect == "duplicate":
        p["stages"] = (p["stages"][0], p["stages"][0])
    elif defect == "missing-origin":
        p["stages"] = (stage_payload("other"),)
    elif defect == "root":
        p["stages"][0]["root"] = "/tmp"
    elif defect == "schema":
        p["schema_version"] = True
    elif defect == "reference":
        p["reference"] = "{}"
        p["reference_sha256"] = digest(b"{}")
    with pytest.raises(ValueError):
        dto().PythonStoppedPremodelSnapshot.model_validate_json(encoded(p), strict=True)


def test_request_and_response_exact_archive_phase_and_scope_binding() -> None:
    m = dto()
    s = snapshot()
    invocation = maintenance()
    archive = s.archive()
    request = m.PythonStoppedPremodelMaintenanceRequest(
        invocation=invocation,
        phase="commit",
        snapshot=s,
        archive_id=archive.id,
        archive_sha256=archive.content_sha256,
    )
    response = m.PythonStoppedPremodelMaintenanceResponse(
        execution_id=invocation.execution_id,
        ok=True,
        retirement_phase="commit",
        retirement_removed=True,
    )
    response.require_for(request)
    failure = m.PythonStoppedPremodelMaintenanceResponse(
        execution_id=invocation.execution_id, ok=False, error="python_project_cleanup_incomplete"
    )
    failure.require_for(request)
    for change in (
        {"archive_sha256": "0" * 64},
        {"archive_id": UUID(int=8)},
        {"phase": "inspect"},
        {"snapshot": None},
    ):
        with pytest.raises(ValueError):
            m.PythonStoppedPremodelMaintenanceRequest.model_validate(
                request.model_dump() | change, strict=True
            )
    with pytest.raises(ValueError):
        m.PythonStoppedPremodelMaintenanceResponse(execution_id=invocation.execution_id, ok=False)
    with pytest.raises(ValueError):
        m.PythonStoppedPremodelMaintenanceResponse(
            execution_id=invocation.execution_id, ok=True, retirement_phase="commit"
        )
    wrong = m.PythonStoppedPremodelMaintenanceResponse(
        execution_id="python_release_expired_" + "2" * 32,
        ok=True,
        retirement_phase="commit",
        retirement_removed=True,
    )
    with pytest.raises(ValueError):
        wrong.require_for(request)


def test_maintenance_request_uses_strict_version_four_frame() -> None:
    m = dto()
    request = m.PythonStoppedPremodelMaintenanceRequest(invocation=maintenance(), phase="inspect")
    assert request.model_dump(mode="json")["version"] == 4
    for version in (True, 4.0, "4", 3, 5):
        with pytest.raises(ValueError):
            m.PythonStoppedPremodelMaintenanceRequest.model_validate_json(
                encoded(request.model_dump(mode="json") | {"version": version}), strict=True
            )


@pytest.fixture
def logical_cache(monkeypatch: Any) -> Any:
    """Simulate Linux IO only; real DTO/hash/selection/commit logic remains in use."""
    m = dto()
    initial = snapshot_payload()
    events = []
    reservation = json.loads(initial["reservation"])
    reference = PythonEnvironmentReference.model_validate_json(initial["reference"], strict=True)
    key = initial["cache_key"]
    files: dict[str, object] = {}
    entries = {key: reservation}
    def info(stamp: tuple[int, ...]) -> os.stat_result:
        dev, ino, mode, uid, gid, nlink, size, mtime, ctime = stamp
        return os.stat_result(
            (mode, ino, dev, nlink, uid, gid, size, 0, mtime / 10**9, ctime / 10**9),
            {"st_mtime_ns": mtime, "st_ctime_ns": ctime},
        )

    tree = {".": info(identity(30, stat.S_IFDIR | 0o555))}

    cache = cache_api.PythonProjectEnvironmentCache(
        Path.cwd() / "unused", cache_api.PythonProjectCachePolicy(2**30, 8, 0, 7200)
    )

    @contextmanager
    def locked(*a: Any, **kw: Any) -> Any:
        with ExitStack() as stack:
            yield 1, stack, []

    monkeypatch.setattr(cache, "_locked", locked)
    monkeypatch.setattr(cache, "_catalog", lambda *a: set(entries))
    monkeypatch.setattr(cache, "_capacity", lambda *a, **kw: (0, 2**30))
    monkeypatch.setattr(cache_api, "_open_directory", lambda *a, **kw: 20)
    monkeypatch.setattr(cache_api, "_reservation", lambda fd, k, *a: entries[k])
    monkeypatch.setattr(cache_api, "_entry_members", lambda *a: set(cache_api._READY))
    monkeypatch.setattr(cache_api, "_ready", lambda *a: cache_api._Ready(reference, 30, tree))
    monkeypatch.setattr(cache_api, "_snapshot", lambda *a: dict(tree))
    monkeypatch.setattr(
        cache_api,
        "_names",
        lambda fd, *a: (
            set(entries) | set(files) | {"cache.lock"} if fd == 1 else set(cache_api._READY)
        ),
    )
    monkeypatch.setattr(
        cache_api,
        "_read",
        lambda fd, name, *a: (
            files[name]
            if fd == 1
            else json.loads(
                initial["reservation"] if name == "reservation.json" else initial["reference"]
            )
        ),
    )

    def write(fd: int, name: str, value: Any) -> None:
        assert name not in files
        events.append("tombstone")
        files[name] = value

    monkeypatch.setattr(cache_api, "_write", write)

    def erase(*a: Any) -> None:
        events.append("erase")
        entries.clear()

    monkeypatch.setattr(cache_api, "_erase_ready", erase)
    fakeos = SimpleNamespace(
        fstat=lambda fd: info(initial["identities"][3] if fd == 30 else initial["identities"][0]),
        stat=lambda name, **kw: info(initial["identities"][1 if name == "reservation.json" else 2]),
    )
    monkeypatch.setattr(cache_api, "os", fakeos)
    stages = (
        m.PythonStoppedPremodelStage.model_validate_json(encoded(stage_payload()), strict=True),
    )

    def callback(origin: str) -> Any:
        assert origin == "original"
        events.append("quiescence")
        return stages

    return SimpleNamespace(
        cache=cache,
        events=events,
        files=files,
        entries=entries,
        initial=initial,
        callback=callback,
        stages=stages,
        tree=tree,
    )


def test_inspect_then_commit_writes_tombstone_before_erasing_only_ready(logical_cache: Any) -> None:
    h = logical_cache
    deadline = time.monotonic() + 5
    s = h.cache.maintain_stopped_premodel(
        maintenance(), deadline=deadline, quiescence_check=h.callback
    )
    assert s is not None and h.entries and not h.files
    assert (
        h.cache.maintain_stopped_premodel(
            maintenance(), deadline=deadline, quiescence_check=h.callback, expected=s
        )
        is True
    )
    assert h.events.index("tombstone") < h.events.index("erase")
    assert not h.entries and len(h.files) == 1
    marker = next(iter(h.files.values()))
    assert marker["kind"] == "python_stopped_premodel_owner_retired"
    assert marker["archive_id"] == str(s.archive().id)
    assert marker["archive_sha256"] == s.archive().content_sha256
    assert h.cache._retired_scope(1, cache_api._owner(maintenance()), deadline)
    before = list(h.events)
    with pytest.raises(ValueError):
        h.cache.maintain_stopped_premodel(
            maintenance(), deadline=deadline, quiescence_check=h.callback, expected=s
        )
    assert h.events == before


@pytest.mark.parametrize(
    "defect",
    [
        "active",
        "stage-drift",
        "tree-drift",
        "generation",
        "partial",
        "multiple",
        "unexpired",
        "cancel",
    ],
)
def test_changed_or_unproven_ready_never_writes_tombstone(
    logical_cache: Any, monkeypatch: Any, defect: str
) -> None:
    h = logical_cache
    deadline = time.monotonic() + 5
    s = h.cache.maintain_stopped_premodel(
        maintenance(), deadline=deadline, quiescence_check=h.callback
    )
    callback = h.callback
    if defect == "active":

        def callback(origin: str) -> Any:
            raise ProcessCleanupIncomplete("python_project_cleanup_incomplete")
    elif defect == "cancel":

        def callback(origin: str) -> Any:
            raise KeyboardInterrupt
    elif defect == "stage-drift":
        callback = lambda origin: (
            dto().PythonStoppedPremodelStage.model_validate_json(
                encoded(stage_payload(state="RUNNING")), strict=True
            ),
        )
    elif defect == "tree-drift":
        observed = h.tree["."]
        fields = list(observed)
        fields[1] = 90
        h.tree["."] = os.stat_result(fields, {
            "st_mtime_ns": observed.st_mtime_ns, "st_ctime_ns": observed.st_ctime_ns,
        })
    elif defect == "generation":
        h.initial["identities"] = (*h.initial["identities"][:3], identity(99, stat.S_IFDIR | 0o555))
    elif defect == "partial":
        monkeypatch.setattr(cache_api, "_entry_members", lambda *a: {"reservation.json"})
    elif defect == "multiple":
        h.entries["2" * 64] = copy.deepcopy(next(iter(h.entries.values())))
    elif defect == "unexpired":
        next(iter(h.entries.values()))["expires_at"] = int(time.time()) + 60
    expected_error = (
        KeyboardInterrupt if defect == "cancel" else (ValueError, ProcessCleanupIncomplete)
    )
    with pytest.raises(expected_error):
        h.cache.maintain_stopped_premodel(
            maintenance(), deadline=deadline, quiescence_check=callback, expected=s
        )
    assert "tombstone" not in h.events and "erase" not in h.events and not h.files


def test_partial_mutation_retains_tombstone_and_never_auto_replays(
    logical_cache: Any, monkeypatch: Any
) -> None:
    h = logical_cache
    deadline = time.monotonic() + 5
    s = h.cache.maintain_stopped_premodel(
        maintenance(), deadline=deadline, quiescence_check=h.callback
    )

    def fail(*a: Any) -> None:
        raise OSError("simulated unlink interruption")

    monkeypatch.setattr(cache_api, "_erase_ready", fail)
    with pytest.raises(ProcessCleanupIncomplete):
        h.cache.maintain_stopped_premodel(
            maintenance(), deadline=deadline, quiescence_check=h.callback, expected=s
        )
    assert h.files and h.entries
    with pytest.raises(ValueError):
        h.cache.maintain_stopped_premodel(
            maintenance(), deadline=deadline, quiescence_check=h.callback, expected=s
        )


def test_new_tombstone_is_strict_and_old_owner_cannot_reserve(logical_cache: Any) -> None:
    h = logical_cache
    deadline = time.monotonic() + 5
    s = h.cache.maintain_stopped_premodel(
        maintenance(), deadline=deadline, quiescence_check=h.callback
    )
    h.cache.maintain_stopped_premodel(
        maintenance(), deadline=deadline, quiescence_check=h.callback, expected=s
    )
    name, marker = next(iter(h.files.items()))
    cache_api._validate_retirement(name, marker)
    for change in (
        {"owner": marker["owner"] | {"run_id": str(UUID(int=9))}},
        {"archive_sha256": "bad"},
        {"schema_version": True},
        {"erase_stage": True},
    ):
        with pytest.raises(ValueError):
            cache_api._validate_retirement(name, marker | change)
    with (
        pytest.raises(cache_api.PythonProjectCacheError, match="cache_incomplete"),
        h.cache.reserve(invocation(), next_bytes=1, deadline=deadline),
    ):
        pytest.fail("retired owner revived")


def test_empty_callback_is_non_authorizing_only_for_inspect(logical_cache: Any) -> None:
    h = logical_cache
    deadline = time.monotonic() + 5
    assert (
        h.cache.maintain_stopped_premodel(
            maintenance(), deadline=deadline, quiescence_check=lambda origin: ()
        )
        is None
    )
    s = h.cache.maintain_stopped_premodel(
        maintenance(), deadline=deadline, quiescence_check=h.callback
    )
    with pytest.raises(ValueError):
        h.cache.maintain_stopped_premodel(
            maintenance(), deadline=deadline, quiescence_check=lambda origin: (), expected=s
        )
    assert not h.files and "erase" not in h.events


def test_inspect_archives_raw_reference_not_default_reconstructed_bytes(logical_cache: Any) -> None:
    h = logical_cache
    value = json.loads(h.initial["reference"])
    del value["schema_version"]  # Existing DTO permits this default on read.
    h.initial["reference"] = encoded(value).decode()
    identities = list(h.initial["identities"])
    reference_identity = list(identities[2])
    reference_identity[6] = len(h.initial["reference"])
    identities[2] = tuple(reference_identity)
    h.initial["identities"] = tuple(identities)
    s = h.cache.maintain_stopped_premodel(
        maintenance(), deadline=time.monotonic() + 5, quiescence_check=h.callback
    )
    assert s.reference == h.initial["reference"]
    assert s.reference_sha256 == digest(h.initial["reference"].encode())


def test_callback_rechecked_before_commit_and_stage_bytes_preserved(logical_cache: Any) -> None:
    h = logical_cache
    deadline = time.monotonic() + 5
    s = h.cache.maintain_stopped_premodel(
        maintenance(), deadline=deadline, quiescence_check=h.callback
    )
    before = tuple(stage.model_dump_json() for stage in h.stages)
    calls = []

    def callback(origin: str) -> Any:
        calls.append(origin)
        if len(calls) == 2:
            raise ProcessCleanupIncomplete("python_project_cleanup_incomplete")
        return h.stages

    with pytest.raises(ProcessCleanupIncomplete):
        h.cache.maintain_stopped_premodel(
            maintenance(), deadline=deadline, quiescence_check=callback, expected=s
        )
    assert len(calls) == 2 and not h.files
    assert tuple(stage.model_dump_json() for stage in h.stages) == before


def test_request_inspect_rejects_install_and_unbound_success_fields() -> None:
    m = dto()
    item = maintenance()
    req = m.PythonStoppedPremodelMaintenanceRequest(invocation=item, phase="inspect")
    for change in ({"install_allowed": True}, {"timeout_seconds": 31}):
        with pytest.raises(ValueError):
            m.PythonStoppedPremodelMaintenanceRequest.model_validate_json(
                encoded(
                    req.model_dump(mode="json")
                    | {"invocation": item.model_dump(mode="json") | change}
                ),
                strict=True,
            )
    for response in (
        {"ok": True, "retirement_phase": "inspect", "retirement_removed": False},
        {"ok": False, "error": "python_project_cleanup_incomplete", "retirement_phase": "inspect"},
        {
            "ok": True,
            "retirement_phase": "commit",
            "retirement_removed": True,
            "retirement_snapshot": snapshot(),
        },
        {"ok": 1, "retirement_phase": "inspect"},
    ):
        with pytest.raises(ValueError):
            m.PythonStoppedPremodelMaintenanceResponse(execution_id=item.execution_id, **response)


def test_stage_pretty_bytes_both_roots_and_bounded_inventory() -> None:
    m = dto()
    p = snapshot_payload()
    p["stages"] = (stage_payload(root="runtime"), stage_payload(root="stage"))
    s = m.PythonStoppedPremodelSnapshot.model_validate_json(encoded(p), strict=True)
    assert s.stages[0].record.endswith("\n")
    assert len(s.stages) == 2
    p["stages"] = tuple(stage_payload("extra" + str(i), root="runtime") for i in range(65))
    with pytest.raises(ValueError):
        m.PythonStoppedPremodelSnapshot.model_validate_json(encoded(p), strict=True)


@pytest.mark.skipif(
    __import__("sys").platform != "linux",
    reason="real held-FD/ownership cache proof requires Linux Root",
)
def test_native_ready_inspect_commit_retains_stages_and_disables_owner(tmp_path: Path) -> None:
    """Actual native cache IO; no project execution, model, service or remote RPC."""
    import os
    import sys

    if sys.platform != "linux":
        pytest.skip("actual Linux Root required")
    if os.geteuid() != 0:
        pytest.skip("requires actual root ownership checks")
    spec = importlib.util.spec_from_file_location(
        "_native_ready_test_fixture", Path(__file__).with_name("test_python_project_cache.py")
    )
    assert spec is not None and spec.loader is not None
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    cache, reference = helper.ready(cache_api, tmp_path)
    original = helper.invocation()
    owner = PythonFailedMaintenanceInvocation(
        execution_id="python_release_expired_" + "1" * 32,
        tenant_id=original.tenant_id,
        run_id=original.run_id,
        project_id=original.project_id,
        session_id=original.session_id,
    )
    # The native fixture's default policy retains 7200s. Advancing only the
    # cache wall clock does not manufacture a Root unit/quiescence proof.
    before = time.time()
    actual_clock = time.time
    time.time = lambda: before + cache.policy.retention_seconds + 1
    try:
        marker = stage_payload()
        raw = json.loads(marker["record"])
        for key in ("tenant_id", "run_id", "project_id", "session_id"):
            raw[key] = str(getattr(original, key))
        marker["record"] = json.dumps(raw, sort_keys=True) + "\n"
        marker["record_sha256"] = digest(marker["record"].encode())
        marker["identities"] = (
            identity(41, stat.S_IFDIR | 0o700),
            identity(42, stat.S_IFREG | 0o644, len(marker["record"])),
        )
        stages = (
            dto().PythonStoppedPremodelStage.model_validate_json(encoded(marker), strict=True),
        )
        s = cache.maintain_stopped_premodel(
            owner, deadline=time.monotonic() + 10, quiescence_check=lambda origin: stages
        )
        assert s is not None and s.reference == encoded(reference.model_dump(mode="json")).decode()
        archive = s.archive()
        assert archive.content["snapshot"]["stages"][0]["record"] == marker["record"]
        assert cache.maintain_stopped_premodel(
            owner,
            deadline=time.monotonic() + 10,
            quiescence_check=lambda origin: stages,
            expected=s,
        )
        assert not (cache.root / s.cache_key).exists()
        with (
            pytest.raises(cache_api.PythonProjectCacheError, match="cache_incomplete"),
            cache.reserve(original, next_bytes=1024, deadline=time.monotonic() + 5),
        ):
            pytest.fail("retired owner admitted")
    finally:
        time.time = actual_clock
