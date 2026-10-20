"""Cache admission and scoped lifetime; Linux IO is never simulated as native proof."""

from __future__ import annotations

import asyncio
import hashlib
import importlib
import importlib.util
import json
import os
import stat
import sys
import time
from contextlib import ExitStack, contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pytest

from agent_hub.skills.sandbox.python_project_backend import PythonProjectBackendError
from agent_hub.skills.sandbox.python_project_copy import PythonProjectCopyError
from agent_hub.skills.sandbox.python_project_protocol import (
    PythonEnvironmentReference,
    PythonProjectInvocation,
)
from agent_hub.subprocess_cleanup import ProcessCleanupIncomplete


@pytest.fixture
def api() -> Any:
    name = "agent_hub.skills.sandbox.python_project_cache"
    assert importlib.util.find_spec(name) is not None, "scoped Python cache missing"
    return importlib.import_module(name)


def invocation(**changes: Any) -> PythonProjectInvocation:
    values = {
        "execution_id": "original",
        "tenant_id": UUID(int=1),
        "run_id": UUID(int=2),
        "project_id": "billing",
        "session_id": "one",
        "sandbox_profile": "workspace_write",
        "timeout_seconds": 60,
        "install_allowed": True,
        "original_reference": {
            "schema_version": 1,
            "contract_sha256": "a" * 64,
            "input_source_sha256": "b" * 64,
            "file_count": 6,
        },
        "source_kind": "original",
        "recipe_sha256": "c" * 64,
        "environment_input_sha256": "d" * 64,
    }
    values.update(changes)
    return PythonProjectInvocation.model_validate(values, strict=True)


def failed_reservation() -> dict[str, Any]:
    item = invocation()
    return {
        "schema_version": 1,
        "scope": {key: value for key, value in reference_scope(item).items()},
        "recipe_sha256": item.recipe_sha256,
        "environment_input_sha256": item.environment_input_sha256,
        "origin_execution_id": item.execution_id,
        "expires_at": int(time.time()) + 60,
        "environment_limit_bytes": 1024,
    }


def reference_scope(item: PythonProjectInvocation) -> dict[str, Any]:
    return {
        "tenant_id": str(item.tenant_id),
        "run_id": str(item.run_id),
        "project_id": item.project_id,
        "session_id": item.session_id,
        "original_reference": item.original_reference.model_dump(mode="json"),
    }


@pytest.mark.parametrize("defect", ["untyped", "exit", "timeout", "quiet", "not-launched", "scope"])
def test_recording_failure_rejects_fake_process_or_unconfirmed_unit_before_filesystem(
    api: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    defect: str,
) -> None:
    from agent_hub.capability_installer.python_attestation import ScanLimits
    from agent_hub.skills.sandbox.project import _Execution
    from agent_hub.skills.sandbox.python_project_process import PythonPhaseProcessResult
    from agent_hub.skills.sandbox.python_project_protocol import base_python_invocation

    item = invocation()
    cache = api.PythonProjectEnvironmentCache(tmp_path, policy(api))
    lease = api.PythonProjectCacheLease(
        tmp_path, item, ScanLimits(1024), failed_reservation(), 1, [], cache, 2, _fresh=True
    )
    monkeypatch.setattr(api.PythonProjectCacheLease, "current", lambda self, **kwargs: None)
    execution = _Execution(
        base_python_invocation(item),
        None,
        stage=tmp_path,
        launched=defect != "not-launched",
        quiet=defect != "quiet",
    )
    if defect == "scope":
        execution.invocation = execution.invocation.model_copy(update={"execution_id": "wrong"})
    phase: Any = PythonPhaseProcessResult(
        0 if defect == "exit" else 1, defect == "timeout", "", "", False, False, 0, 0
    )
    if defect == "untyped":
        phase = {"exit_code": 1, "timed_out": False}

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("invalid failure reached filesystem")

    monkeypatch.setattr(api, "_root", forbidden)
    with pytest.raises(api.PythonProjectCacheError):
        lease.record_install_failure(phase, execution=execution, deadline=time.monotonic() + 2)


def failed_outcome(reservation: dict[str, Any]) -> dict[str, Any]:
    raw = json.dumps(reservation, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    return {
        "schema_version": 1,
        "state": "failed_quiescent",
        "phase": "install",
        "execution_id": reservation["origin_execution_id"],
        "reservation_sha256": hashlib.sha256(raw).hexdigest(),
        "exit_code": 1,
        "timed_out": False,
        "unit_quiet": True,
        "export_empty": True,
    }


@pytest.mark.parametrize(
    "defect", [None, "schema", "execution", "reservation", "exit", "quiet", "export", "extra"]
)
def test_failed_state_requires_exact_bound_quiescent_failure_not_claimed_partial(
    api: Any,
    defect: str | None,
) -> None:
    reservation = failed_reservation()
    reservation["environment_limit_bytes"] = 32 * 1024**2
    value = failed_outcome(reservation)
    if defect is None:
        api._validate_failed_outcome(value, reservation)
        return
    key, changed = {
        "schema": ("schema_version", True),
        "execution": ("execution_id", "other"),
        "reservation": ("reservation_sha256", "0" * 64),
        "exit": ("exit_code", 0),
        "quiet": ("unit_quiet", False),
        "export": ("export_empty", False),
        "extra": ("install_allowed", True),
    }[defect]
    value[key] = changed
    with pytest.raises(api.PythonProjectCacheError):
        api._validate_failed_outcome(value, reservation)


@pytest.mark.parametrize(
    "install,execution,partial",
    [
        (True, "candidate", False),
        (False, "candidate", False),
        (True, "original", False),
        (True, "candidate", True),
        (True, "candidate", "badmarker"),
        (True, "candidate", "environment"),
        (True, "candidate", "orphan"),
        (True, "candidate", "consumed"),
        (True, "candidate", "historylimit"),
        (True, "candidate", "renamecrash"),
    ],
)
def test_recovery_archives_only_explicit_failed_state_with_fresh_grant_and_id(
    api: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    install: bool,
    execution: str,
    partial: bool | str,
) -> None:
    # Logical transition only. Native ownership/no-follow checks are tested on Linux.
    reservation = failed_reservation()
    reservation["environment_limit_bytes"] = 32 * 1024**2
    files = {"reservation.json": reservation}
    if partial is not True:
        files["failed.json"] = failed_outcome(reservation)
    if partial == "badmarker":
        files["failed.json"] = {**files["failed.json"], "unit_quiet": False}
    elif partial == "environment":
        files["environment"] = {}
    elif partial == "orphan":
        files["failed-old.reservation.json"] = {**reservation, "origin_execution_id": "old"}
    elif partial in {"consumed", "historylimit"}:
        for index in range(1 if partial == "consumed" else 16):
            origin = "candidate" if partial == "consumed" else f"old-{index}"
            previous = {**reservation, "origin_execution_id": origin}
            files[f"failed-{origin}.reservation.json"] = previous
            files[f"failed-{origin}.outcome.json"] = failed_outcome(previous)
    before = dict(files)
    cache = api.PythonProjectEnvironmentCache(tmp_path, policy(api, disk_reserve_bytes=0))

    @contextmanager
    def locked(*args: Any, **kwargs: Any) -> Any:
        with ExitStack() as stack:
            yield 1, stack, []

    def write(parent: int, name: str, value: Any) -> None:
        assert name not in files
        files[name] = value

    def rename(source: str, destination: str, **kwargs: Any) -> None:
        assert destination not in files
        if partial == "renamecrash" and source == "failed.json":
            raise OSError(5, "private rename failure")
        files[destination] = files.pop(source)

    monkeypatch.setattr(cache, "_locked", locked)
    monkeypatch.setattr(
        cache, "_catalog", lambda *args: {api.python_project_cache_key(invocation())}
    )
    monkeypatch.setattr(cache, "_capacity", lambda *args, **kwargs: (0, 2**30))
    monkeypatch.setattr(api, "_open_directory", lambda *args, **kwargs: 2)
    monkeypatch.setattr(api, "_read", lambda parent, name, pins: files[name])
    monkeypatch.setattr(api, "_write", write)
    monkeypatch.setattr(api, "_names", lambda *args: set(files))
    monkeypatch.setattr(api.os, "rename", rename)
    monkeypatch.setattr(api.os, "fsync", lambda *args: None)
    actual = invocation(
        execution_id=execution, install_allowed=install, workspace_limit_bytes=192 * 1024**2
    )
    if not install or execution == "original" or partial:
        with (
            pytest.raises(api.PythonProjectCacheError),
            cache.reserve(actual, next_bytes=192 * 1024**2, deadline=time.monotonic() + 2),
        ):
            pytest.fail("unsafe recovery")
        if partial == "renamecrash":
            assert files["failed-original.reservation.json"] == before["reservation.json"]
            after = dict(files)
            with (
                pytest.raises(api.PythonProjectCacheError),
                cache.reserve(
                    invocation(execution_id="another"),
                    next_bytes=1024,
                    deadline=time.monotonic() + 2,
                ),
            ):
                pytest.fail("interrupted archival recovered itself")
            assert files == after
        else:
            assert files == before
    else:
        with cache.reserve(
            actual, next_bytes=192 * 1024**2, deadline=time.monotonic() + 2
        ) as lease:
            assert lease.current(deadline=time.monotonic() + 2) is None
            assert lease.reservation["origin_execution_id"] == "candidate"
            assert lease.limits.max_bytes == 192 * 1024**2
        assert files["failed-original.reservation.json"] == before["reservation.json"]
        assert files["failed-original.outcome.json"] == before["failed.json"]


def policy(api: Any, **changes: Any) -> Any:
    return api.PythonProjectCachePolicy(
        **(
            {
                "max_bytes": 1024**3,
                "max_entries": 8,
                "disk_reserve_bytes": 1024**3,
                "retention_seconds": 7200,
            }
            | changes
        )
    )


def test_cache_key_binds_original_scope_recipe_and_inputs_not_execution(api: Any) -> None:
    key = api.python_project_cache_key(invocation())
    assert len(key) == 64
    assert (
        api.python_project_cache_key(invocation(execution_id="candidate", source_kind="candidate"))
        == key
    )
    for change in (
        {"tenant_id": UUID(int=3)},
        {"run_id": UUID(int=3)},
        {"project_id": "other"},
        {"session_id": "other"},
        {"recipe_sha256": "e" * 64},
        {"environment_input_sha256": "e" * 64},
        {"original_reference": invocation().original_reference.model_dump() | {"file_count": 7}},
    ):
        assert api.python_project_cache_key(invocation(**change)) != key


@pytest.mark.parametrize(
    "key,value",
    [
        ("max_bytes", True),
        ("max_bytes", 0),
        ("max_bytes", 2**63),
        ("max_entries", 0),
        ("max_entries", 513),
        ("disk_reserve_bytes", -1),
        ("retention_seconds", 0),
        ("retention_seconds", 86401),
    ],
)
def test_policy_rejects_unbounded_or_coerced_values(api: Any, key: str, value: object) -> None:
    with pytest.raises(ValueError):
        policy(api, **{key: value})


def test_admission_accounts_for_next_copy_and_retains_disk_reserve(api: Any) -> None:
    overhead = api._CONTROL_ALLOWANCE
    item = policy(api, max_bytes=1000 + overhead, disk_reserve_bytes=200)
    item.require_capacity(used_bytes=600, entries=1, next_bytes=400, free_bytes=600 + overhead)
    for args in ({"used_bytes": 601}, {"entries": 8}, {"free_bytes": 599}, {"next_bytes": 0}):
        with pytest.raises(api.PythonProjectCacheError, match="cache_quota"):
            item.require_capacity(
                **(
                    {
                        "used_bytes": 600,
                        "entries": 1,
                        "next_bytes": 400,
                        "free_bytes": 600 + overhead,
                    }
                    | args
                )
            )


def reference(api: Any) -> PythonEnvironmentReference:
    item = invocation()
    return PythonEnvironmentReference(
        scope=api._scope(item),
        recipe_sha256=item.recipe_sha256,
        environment_input_sha256=item.environment_input_sha256,
        environment_sha256="e" * 64,
        security_manifest_sha256="f" * 64,
        origin_execution_id=item.execution_id,
    )


def test_json_reference_preserves_strict_uuid_and_rejects_coercion(api: Any) -> None:
    expected = reference(api)
    assert api._reference(expected.model_dump(mode="json")) == expected
    bad = expected.model_dump(mode="json") | {"schema_version": True}
    with pytest.raises(api.PythonProjectCacheError):
        api._reference(bad)


@pytest.mark.parametrize(
    "field", ["scope", "recipe_sha256", "environment_input_sha256", "origin_execution_id"]
)
def test_ready_metadata_binding_is_required_before_observation(api: Any, field: str) -> None:
    expected = reference(api)
    metadata = {
        "scope": expected.scope.model_dump(mode="json"),
        "recipe_sha256": expected.recipe_sha256,
        "environment_input_sha256": expected.environment_input_sha256,
        "origin_execution_id": expected.origin_execution_id,
    }
    api._bind_reference(expected, metadata)
    metadata[field] = {} if field == "scope" else "different"
    with pytest.raises(api.PythonProjectCacheError, match="cache_changed"):
        api._bind_reference(expected, metadata)


@pytest.mark.parametrize(
    "protected",
    [
        None,
        set(),
        frozenset({("x", "y")}),
        frozenset({(UUID(int=1),)}),
        frozenset({(UUID(int=1), UUID(int=2), UUID(int=3))}),
    ],
)
def test_protected_runs_require_authoritative_exact_uuid_pairs(api: Any, protected: Any) -> None:
    with pytest.raises(api.PythonProjectCacheError, match="cache_invalid"):
        api._protected_runs(protected)


def test_protected_runs_accept_frozen_uuid_pairs(api: Any) -> None:
    protected = frozenset({(UUID(int=1), UUID(int=2))})
    assert api._protected_runs(protected) == protected


@pytest.mark.parametrize("operation", ["inspect", "release", "prune"])
def test_readonly_or_reclamation_does_not_require_allocation_headroom(
    api: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    cache = api.PythonProjectEnvironmentCache(tmp_path, policy(api))

    @contextmanager
    def locked(*args: Any, **kwargs: Any) -> Any:
        yield (None, None, [])

    def no_allocation(*args: Any, **kwargs: Any) -> Any:
        raise api.PythonProjectCacheError("cache_quota")

    monkeypatch.setattr(cache, "_locked", locked)
    monkeypatch.setattr(cache, "_catalog", lambda *args: set())
    monkeypatch.setattr(cache, "_capacity", no_allocation)
    expected = reference(api)
    if operation == "inspect":
        assert cache.inspect(invocation(), expected, deadline=time.monotonic() + 2) is None
    elif operation == "release":
        assert (
            cache.release_completed(invocation(), expected, deadline=time.monotonic() + 2) is False
        )
    else:
        assert cache.prune_expired(protected_runs=frozenset(), deadline=time.monotonic() + 2) == 0


def test_retention_age_is_not_lease_authority(api: Any, tmp_path: Path) -> None:
    lease = api.PythonProjectCacheLease(
        tmp_path,
        invocation(install_allowed=False),
        None,
        {"expires_at": 1},
        1,
        [],
        None,
        2,
    )
    lease._check(time.monotonic() + 2)
    lease._closed = True
    with pytest.raises(api.PythonProjectCacheError, match="cache_invalid"):
        lease._check(time.monotonic() + 2)


def test_expired_inspection_still_requires_fresh_ready_measurement(
    api: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Isolate retention policy here; real filesystem reuse is covered below on Linux.
    cache = api.PythonProjectEnvironmentCache(tmp_path, policy(api))
    expected = reference(api)
    measured = []

    @contextmanager
    def locked(*args: Any, **kwargs: Any) -> Any:
        with ExitStack() as stack:
            yield 1, stack, []

    def observe(*args: Any) -> Any:
        measured.append(True)
        return SimpleNamespace(reference=expected)

    monkeypatch.setattr(cache, "_locked", locked)
    monkeypatch.setattr(
        cache, "_catalog", lambda *args: {api.python_project_cache_key(invocation())}
    )
    monkeypatch.setattr(api, "_open_directory", lambda *args, **kwargs: 2)
    monkeypatch.setattr(api, "_names", lambda *args: api._READY)
    monkeypatch.setattr(
        api, "_reservation", lambda *args: {"expires_at": 1, "environment_limit_bytes": 1024}
    )
    monkeypatch.setattr(api, "_ready", observe)
    assert (
        cache.inspect(invocation(install_allowed=False), expected, deadline=time.monotonic() + 2)
        == expected
    )
    assert measured == [True]

    def changed(*args: Any) -> Any:
        raise api.PythonProjectCacheError("cache_changed")

    monkeypatch.setattr(api, "_ready", changed)
    with pytest.raises(api.PythonProjectCacheError, match="cache_changed"):
        cache.inspect(invocation(install_allowed=False), expected, deadline=time.monotonic() + 2)


@pytest.mark.parametrize(
    "error",
    [
        RuntimeError("caller runtime"),
        ValueError("caller value"),
        OSError("caller OS"),
        PythonProjectCopyError("copy_timeout"),
        ProcessCleanupIncomplete("caller cleanup"),
        PythonProjectBackendError("python_project_execution_unavailable"),
        asyncio.CancelledError("caller cancelled"),
    ],
)
@pytest.mark.parametrize("teardown_fails", [False, True])
def test_lock_boundary_preserves_caller_exception_identity_and_closes_resources(
    api: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error: BaseException,
    teardown_fails: bool,
) -> None:
    # Injectable context structure only; never a simulated Linux filesystem proof.
    cache = api.PythonProjectEnvironmentCache(tmp_path, policy(api))
    events: list[str] = []

    @contextmanager
    def resources(*args: Any, **kwargs: Any) -> Any:
        events.append("acquired")
        try:
            yield None
        finally:
            events.append("closed")
            if teardown_fails:
                raise api.PythonProjectCacheError("cache_unsafe")

    monkeypatch.setattr(cache, "_lock_resources", resources, raising=False)
    with pytest.raises(type(error)) as caught, cache._locked(time.monotonic() + 2):
        raise error
    assert caught.value is error
    assert events == ["acquired", "closed"]


@pytest.mark.parametrize(
    "error,reason",
    [
        (OSError(28, "/private/disk"), "cache_quota"),
        (ValueError("/private/control"), "cache_unsafe"),
        (PythonProjectCopyError("copy_timeout"), "cache_timeout"),
        (PythonProjectCopyError("copy_limit"), "cache_quota"),
        (PythonProjectCopyError("copy_unsafe"), "cache_changed"),
    ],
)
def test_internal_cache_errors_remain_finite(api: Any, error: Exception, reason: str) -> None:
    with pytest.raises(api.PythonProjectCacheError, match=f"^{reason}$"), api._io_errors():
        raise error


@pytest.mark.parametrize("phase", ["setup", "teardown"])
@pytest.mark.parametrize(
    "error,reason",
    [
        (OSError(28, "/private/control"), "cache_quota"),
        (PythonProjectCopyError("copy_timeout"), "cache_timeout"),
    ],
)
def test_internal_lock_failure_cannot_return_success(
    api: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
    error: Exception,
    reason: str,
) -> None:
    cache = api.PythonProjectEnvironmentCache(tmp_path, policy(api))
    events: list[str] = []

    @contextmanager
    def resources(*args: Any, **kwargs: Any) -> Any:
        with api._io_errors():
            events.append("setup")
            if phase == "setup":
                raise error
            try:
                yield None
            finally:
                events.append("closed")
                raise error

    monkeypatch.setattr(cache, "_lock_resources", resources)
    with (
        pytest.raises(api.PythonProjectCacheError, match=f"^{reason}$"),
        cache._locked(time.monotonic() + 2),
    ):
        events.append("body")
    assert events == (["setup"] if phase == "setup" else ["setup", "body", "closed"])


@pytest.mark.skipif(sys.platform != "linux", reason="requires actual root-owned Linux cache")
@pytest.mark.parametrize(
    "expected",
    [
        RuntimeError("trusted caller body"),
        PythonProjectBackendError("python_project_execution_unavailable"),
        asyncio.CancelledError("cancelled"),
    ],
)
def test_real_linux_lock_releases_after_caller_exception(
    api: Any, tmp_path: Path, expected: BaseException
) -> None:
    cache = cache_root(api, tmp_path)
    with pytest.raises(type(expected)) as caught, cache._locked(time.monotonic() + 2):
        raise expected
    assert caught.value is expected
    with cache._locked(time.monotonic() + 2) as held:
        assert held is not None
    with (
        pytest.raises(type(expected)) as caught,
        cache.reserve(invocation(), next_bytes=100, deadline=time.monotonic() + 2),
    ):
        raise expected
    assert caught.value is expected
    with (
        pytest.raises(api.PythonProjectCacheError, match="cache_incomplete"),
        cache.reserve(invocation(), next_bytes=100, deadline=time.monotonic() + 2),
    ):
        pytest.fail("caller failure replayed a consumed reservation")


@pytest.mark.skipif(sys.platform == "linux", reason="non-Linux refusal only")
def test_cache_never_claims_posix_authority_on_windows(api: Any, tmp_path: Path) -> None:
    cache = api.PythonProjectEnvironmentCache(tmp_path, policy(api))
    with (
        pytest.raises(api.PythonProjectCacheError, match="cache_unsupported"),
        cache.reserve(invocation(), next_bytes=100, deadline=time.monotonic() + 2),
    ):
        pytest.fail("Windows entered a native cache reservation")


native = pytest.mark.skipif(
    sys.platform != "linux", reason="requires actual root-owned Linux cache"
)


def require_root() -> None:
    if sys.platform != "linux":
        pytest.skip("requires Linux UID semantics")
    if os.geteuid() != 0:
        pytest.skip("requires actual root, not emulated UID checks")


def cache_root(api: Any, tmp_path: Path) -> Any:
    require_root()
    root = tmp_path / "cache"
    root.mkdir(mode=0o700)
    return api.PythonProjectEnvironmentCache(root, policy(api))


@native
@pytest.mark.parametrize("defect", [None, "quiet", "exit", "timeout", "export", "scope"])
def test_actual_failed_marker_requires_quiet_typed_process_and_empty_private_export(
    api: Any,
    tmp_path: Path,
    defect: str | None,
) -> None:
    require_root()
    from agent_hub.skills.sandbox.project import _Execution
    from agent_hub.skills.sandbox.python_project_process import PythonPhaseProcessResult
    from agent_hub.skills.sandbox.python_project_protocol import base_python_invocation

    cache = cache_root(api, tmp_path)
    stage = tmp_path / "stage"
    stage.mkdir(mode=0o700)
    export = stage / "export"
    export.mkdir(mode=0o700)
    export.chmod(0o1777)
    if defect == "export":
        (export / "unexpected").write_bytes(b"never delete this partial")
    original = invocation(workspace_limit_bytes=32 * 1024**2)
    execution = _Execution(
        base_python_invocation(original), None, stage=stage, launched=True, quiet=defect != "quiet"
    )
    if defect == "scope":
        execution.invocation = execution.invocation.model_copy(update={"execution_id": "different"})
    phase = PythonPhaseProcessResult(
        0 if defect == "exit" else 1, defect == "timeout", "", "ENOSPC", False, False, 0, 6
    )
    with cache.reserve(original, next_bytes=32 * 1024**2, deadline=time.monotonic() + 5) as lease:
        if defect:
            with pytest.raises(api.PythonProjectCacheError):
                lease.record_install_failure(
                    phase, execution=execution, deadline=time.monotonic() + 5
                )
            assert not (lease.path / "failed.json").exists()
        else:
            lease.record_install_failure(phase, execution=execution, deadline=time.monotonic() + 5)
            marker = lease.path / "failed.json"
            assert marker.stat().st_uid == 0 and marker.stat().st_nlink == 1
            assert stat.S_IMODE(marker.stat().st_mode) == 0o600
            payload = json.loads(marker.read_bytes())
            assert payload["state"] == "failed_quiescent" and "ENOSPC" not in marker.read_text()
            with pytest.raises(api.PythonProjectCacheError):
                lease.record_install_failure(
                    phase, execution=execution, deadline=time.monotonic() + 5
                )
    candidate = invocation(
        execution_id="fresh_repair", source_kind="candidate", workspace_limit_bytes=192 * 1024**2
    )
    if defect:
        with (
            pytest.raises(api.PythonProjectCacheError),
            cache.reserve(candidate, next_bytes=192 * 1024**2, deadline=time.monotonic() + 5),
        ):
            pytest.fail("partial/UNKNOWN replayed")
    else:
        with cache.reserve(
            candidate, next_bytes=192 * 1024**2, deadline=time.monotonic() + 5
        ) as lease:
            assert lease.current(deadline=time.monotonic() + 5) is None
            assert lease.limits.max_bytes == 192 * 1024**2
            assert (lease.path / "failed-original.reservation.json").is_file()
            assert (lease.path / "failed-original.outcome.json").is_file()
        with (
            pytest.raises(api.PythonProjectCacheError),
            cache.reserve(original, next_bytes=32 * 1024**2, deadline=time.monotonic() + 5),
        ):
            pytest.fail("original consumed execution replayed")


def ready(api: Any, tmp_path: Path, *, prior_failure: bool = False) -> tuple[Any, Any]:
    from agent_hub.capability_installer.python_attestation import ScanLimits
    from agent_hub.skills.sandbox.python_project_environment import (
        observe_python_project_environment,
    )

    cache = cache_root(api, tmp_path)
    source = tmp_path / "export"
    source.mkdir(mode=0o700)
    (source / "bin").mkdir(mode=0o755)
    payload = b"dummy executable; never run or import"
    for name in ("python", "python3", "python3.12"):
        path = source / "bin" / name
        path.write_bytes(payload)
        path.chmod(0o755)
    (source / "lib").mkdir(mode=0o755)
    (source / "lib" / "module.py").write_bytes(b"dummy module; never import")
    (source / "lib64").symlink_to("lib", target_is_directory=True)
    source.chmod(0o555)
    item = invocation()
    if prior_failure:
        from agent_hub.skills.sandbox.project import _Execution
        from agent_hub.skills.sandbox.python_project_process import PythonPhaseProcessResult
        from agent_hub.skills.sandbox.python_project_protocol import base_python_invocation

        stage = tmp_path / "failed-stage"
        stage.mkdir(mode=0o700)
        (stage / "export").mkdir(mode=0o700)
        (stage / "export").chmod(0o1777)
        job = _Execution(base_python_invocation(item), None, stage=stage, launched=True, quiet=True)
        with cache.reserve(item, next_bytes=128, deadline=time.monotonic() + 5) as lease:
            lease.record_install_failure(
                PythonPhaseProcessResult(1, False, "", "", False, False, 0, 0),
                execution=job,
                deadline=time.monotonic() + 5,
            )
        item = invocation(execution_id="fresh_repair", source_kind="candidate")
    observed = observe_python_project_environment(
        source,
        interpreter=source / "bin/python",
        environment_input_sha256=item.environment_input_sha256,
        limits=ScanLimits(1024),
    )
    with cache.reserve(item, next_bytes=1024, deadline=time.monotonic() + 10) as lease:
        result = lease.publish(
            source,
            source_owner=0,
            interpreter=source / "bin/python",
            interpreter_sha256=hashlib.sha256(payload).hexdigest(),
            expected_environment_sha256=observed.environment_sha256,
            deadline=time.monotonic() + 10,
        )
    return cache, result


@native
def test_recovered_publication_inspection_and_release_validate_and_remove_only_complete_history(
    api: Any,
    tmp_path: Path,
) -> None:
    cache, expected = ready(api, tmp_path, prior_failure=True)
    assert expected.origin_execution_id == "fresh_repair"
    current = invocation(execution_id="fresh_inspect", install_allowed=False)
    assert cache.inspect(current, expected, deadline=time.monotonic() + 10) == expected
    entry = cache.root / api.python_project_cache_key(current)
    assert (entry / "failed-original.reservation.json").is_file()
    with (
        pytest.raises(api.PythonProjectCacheError),
        cache.reserve(invocation(), next_bytes=1024, deadline=time.monotonic() + 5),
    ):
        pytest.fail("consumed original bypassed history fence after READY")
    assert cache.release_completed(current, expected, deadline=time.monotonic() + 10) is True
    assert not entry.exists()


@native
def test_reservation_is_consumed_after_failure_and_never_replayed(api: Any, tmp_path: Path) -> None:
    require_root()
    root = tmp_path / "cache"
    root.mkdir(mode=0o700)
    cache = api.PythonProjectEnvironmentCache(root, policy(api))
    with cache.reserve(invocation(), next_bytes=100, deadline=time.monotonic() + 2) as lease:
        assert lease.current(deadline=time.monotonic() + 2) is None
    with (
        pytest.raises(api.PythonProjectCacheError, match="cache_incomplete"),
        cache.reserve(
            invocation(execution_id="another"), next_bytes=100, deadline=time.monotonic() + 2
        ),
    ):
        pytest.fail("an incomplete install reservation was replayed")


@native
def test_unsafe_root_and_exhausted_disk_do_not_start_reservation(api: Any, tmp_path: Path) -> None:
    require_root()
    root = tmp_path / "cache"
    root.mkdir(mode=0o777)
    root.chmod(0o777)
    cache = api.PythonProjectEnvironmentCache(root, policy(api))
    with (
        pytest.raises(api.PythonProjectCacheError),
        cache.reserve(invocation(), next_bytes=100, deadline=time.monotonic() + 2),
    ):
        pytest.fail("unsafe cache root accepted")
    root.chmod(0o700)
    cache = api.PythonProjectEnvironmentCache(root, policy(api, disk_reserve_bytes=2**62))
    with (
        pytest.raises(api.PythonProjectCacheError, match="cache_quota"),
        cache.reserve(invocation(), next_bytes=100, deadline=time.monotonic() + 2),
    ):
        pytest.fail("disk reserve bypassed")
    assert list(root.iterdir()) == [root / "cache.lock"]


@native
def test_publish_reuse_and_read_only_inspection_bind_actual_reference(
    api: Any, tmp_path: Path
) -> None:
    cache, expected = ready(api, tmp_path)
    item = invocation(execution_id="candidate", source_kind="candidate", install_allowed=False)
    before = {
        str(path.relative_to(cache.root)): path.lstat().st_ino for path in cache.root.rglob("*")
    }
    assert cache.inspect(item, expected, deadline=time.monotonic() + 10) == expected
    after = {
        str(path.relative_to(cache.root)): path.lstat().st_ino for path in cache.root.rglob("*")
    }
    assert before == after
    with cache.reserve(item, next_bytes=1024, deadline=time.monotonic() + 10) as lease:
        assert lease.current(expected=expected, deadline=time.monotonic() + 10) == expected


@native
def test_inspect_miss_never_creates_lock_or_consumes_reservation(api: Any, tmp_path: Path) -> None:
    cache = cache_root(api, tmp_path)
    assert cache.inspect(invocation(), reference(api), deadline=time.monotonic() + 2) is None
    assert list(cache.root.iterdir()) == []
    with cache.reserve(invocation(), next_bytes=1024, deadline=time.monotonic() + 2) as lease:
        assert lease.current(deadline=time.monotonic() + 2) is None


@native
@pytest.mark.parametrize(
    "change", ["bytes", "bin-link", "hardlink", "control-mode", "bad-ref", "alias"]
)
def test_inspect_rejects_tampering_without_chmod_or_following_links(
    api: Any,
    tmp_path: Path,
    change: str,
) -> None:
    cache, expected = ready(api, tmp_path)
    entry = cache.root / api.python_project_cache_key(invocation())
    external = tmp_path / "host"
    external.write_bytes(b"host bytes")
    external.chmod(0o400)
    env = entry / "environment"
    if change == "bytes":
        (env / "lib/module.py").write_bytes(b"changed")
    elif change == "bin-link":
        (env / "bin/python").unlink()
        (env / "bin/python").symlink_to(external)
    elif change == "hardlink":
        os.link(env / "bin/python", tmp_path / "alias")
    elif change == "control-mode":
        (entry / "reference.json").chmod(0o644)
    elif change == "bad-ref":
        (entry / "reference.json").write_bytes(b"{}")
    else:
        entry.rename(tmp_path / "moved")
        entry.symlink_to(tmp_path / "moved", target_is_directory=True)
    with pytest.raises(api.PythonProjectCacheError):
        cache.inspect(invocation(), expected, deadline=time.monotonic() + 10)
    assert external.read_bytes() == b"host bytes"
    assert stat.S_IMODE(external.stat().st_mode) == 0o400


@native
@pytest.mark.parametrize("caller_failed", [False, True])
def test_real_linux_unlock_failure_closes_fd_and_never_reports_success(
    api: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caller_failed: bool,
) -> None:
    require_root()
    if sys.platform != "linux":
        pytest.skip("requires actual Linux flock")
    import fcntl

    cache = cache_root(api, tmp_path)
    original = fcntl.flock
    expected = asyncio.CancelledError("trusted cancellation")

    def failed_unlock(fd: int, operation: int) -> None:
        if operation == fcntl.LOCK_UN:
            raise OSError(5, "/private/unlock")
        original(fd, operation)

    with monkeypatch.context() as patch:
        patch.setattr(fcntl, "flock", failed_unlock)
        if caller_failed:
            with (
                pytest.raises(asyncio.CancelledError) as caught,
                cache._locked(time.monotonic() + 2),
            ):
                raise expected
            assert caught.value is expected
        else:
            with (
                pytest.raises(api.PythonProjectCacheError, match="^cache_unsafe$"),
                cache._locked(time.monotonic() + 2),
            ):
                pass
    # ExitStack must close the real descriptor even when its explicit unlock fails.
    with cache._locked(time.monotonic() + 2):
        pass


@native
def test_global_lock_remains_held_through_execution(api: Any, tmp_path: Path) -> None:
    cache = cache_root(api, tmp_path)
    with (
        cache.reserve(invocation(), next_bytes=1024, deadline=time.monotonic() + 3),
        pytest.raises(api.PythonProjectCacheError, match="cache_timeout"),
    ):
        cache.inspect(invocation(), reference(api), deadline=time.monotonic() + 0.05)


@native
def test_expired_pruning_respects_protected_runs_and_retains_unknown(
    api: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache, expected = ready(api, tmp_path)
    pending = invocation(project_id="pending")
    with cache.reserve(pending, next_bytes=1024, deadline=time.monotonic() + 2):
        pass
    wall = time.time() + 8000
    monkeypatch.setattr(api.time, "time", lambda: wall)
    protected = frozenset({(invocation().tenant_id, invocation().run_id)})
    assert cache.prune_expired(protected_runs=protected, deadline=time.monotonic() + 10) == 0
    assert cache.prune_expired(protected_runs=frozenset(), deadline=time.monotonic() + 10) == 1
    assert (cache.root / api.python_project_cache_key(pending)).is_dir()
    assert not (cache.root / api.python_project_cache_key(invocation())).exists()
    with (
        pytest.raises(api.PythonProjectCacheError, match="cache_incomplete"),
        cache.reserve(pending, next_bytes=1024, deadline=time.monotonic() + 2),
    ):
        pytest.fail("UNKNOWN reservation replayed")
    assert expected.scope == api._scope(invocation())


@native
def test_actual_expired_ready_reuses_without_install_or_retention_refresh(
    api: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache, expected = ready(api, tmp_path)
    entry = cache.root / api.python_project_cache_key(invocation())
    original_reservation = (entry / "reservation.json").read_bytes()
    wall = time.time() + cache.policy.retention_seconds + 1
    monkeypatch.setattr(api.time, "time", lambda: wall)
    reused = invocation(
        execution_id="after_approval_wait", source_kind="candidate", install_allowed=False
    )
    assert cache.inspect(reused, expected, deadline=time.monotonic() + 10) == expected
    with cache.reserve(reused, next_bytes=1024, deadline=time.monotonic() + 10) as lease:
        assert lease.current(deadline=time.monotonic() + 10, expected=expected) == expected
    assert (entry / "reservation.json").read_bytes() == original_reservation
    protected = frozenset({(reused.tenant_id, reused.run_id)})
    assert cache.prune_expired(protected_runs=protected, deadline=time.monotonic() + 10) == 0
    assert cache.prune_expired(protected_runs=frozenset(), deadline=time.monotonic() + 10) == 1


@native
@pytest.mark.parametrize("operation", ["prune", "release"])
def test_cleanup_requires_intact_ready_evidence_before_first_mutation(
    api: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    cache, expected = ready(api, tmp_path)
    entry = cache.root / api.python_project_cache_key(invocation())
    module = entry / "environment/lib/module.py"
    module.write_bytes(b"tampered")
    wall = time.time() + 8000
    monkeypatch.setattr(api.time, "time", lambda: wall)
    with pytest.raises(api.PythonProjectCacheError):
        if operation == "prune":
            cache.prune_expired(protected_runs=frozenset(), deadline=time.monotonic() + 10)
        else:
            cache.release_completed(invocation(), expected, deadline=time.monotonic() + 10)
    assert (entry / "reference.json").exists()
    assert module.read_bytes() == b"tampered"
    assert stat.S_IMODE((entry / "environment").stat().st_mode) == 0o555


@native
def test_trusted_completed_release_removes_only_the_validated_ready_entry(
    api: Any, tmp_path: Path
) -> None:
    cache, expected = ready(api, tmp_path)
    assert cache.release_completed(invocation(), expected, deadline=time.monotonic() + 10)
    assert list(cache.root.iterdir()) == [cache.root / "cache.lock"]
    assert not cache.release_completed(invocation(), expected, deadline=time.monotonic() + 10)


@native
@pytest.mark.parametrize("operation", ["release", "prune"])
def test_actual_ready_cache_remains_reclaimable_below_disk_reserve(
    api: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    cache, expected = ready(api, tmp_path)
    constrained = api.PythonProjectEnvironmentCache(
        cache.root, policy(api, disk_reserve_bytes=2**62)
    )
    with (
        pytest.raises(api.PythonProjectCacheError, match="cache_quota"),
        constrained.reserve(
            invocation(execution_id="new_attempt"), next_bytes=1024, deadline=time.monotonic() + 10
        ),
    ):
        pytest.fail("low disk permitted a new allocation")
    assert constrained.inspect(invocation(), expected, deadline=time.monotonic() + 10) == expected
    if operation == "release":
        assert constrained.release_completed(invocation(), expected, deadline=time.monotonic() + 10)
    else:
        expired = time.time() + constrained.policy.retention_seconds + 1
        monkeypatch.setattr(api.time, "time", lambda: expired)
        assert (
            constrained.prune_expired(protected_runs=frozenset(), deadline=time.monotonic() + 10)
            == 1
        )
    assert list(cache.root.iterdir()) == [cache.root / "cache.lock"]


@native
def test_crashed_process_reservation_is_not_replayed(api: Any, tmp_path: Path) -> None:
    if sys.platform != "linux":
        pytest.skip("requires Linux fork/waitpid")
    cache = cache_root(api, tmp_path)
    pid = os.fork()
    if pid == 0:
        try:
            with cache.reserve(invocation(), next_bytes=1024, deadline=time.monotonic() + 3):
                os._exit(0)
        finally:
            os._exit(1)
    _, status = os.waitpid(pid, 0)
    assert os.waitstatus_to_exitcode(status) == 0
    with (
        pytest.raises(api.PythonProjectCacheError, match="cache_incomplete"),
        cache.reserve(
            invocation(execution_id="retry"), next_bytes=1024, deadline=time.monotonic() + 3
        ),
    ):
        pytest.fail("dead process reservation was replayed")


@native
def test_release_uses_only_rooted_unlink_and_directory_chmod(
    api: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if sys.platform != "linux":
        pytest.skip("requires Linux fchmod and dir_fd unlink")
    cache, expected = ready(api, tmp_path)
    fchmod, unlink, rmdir = os.fchmod, os.unlink, os.rmdir
    changed: list[int] = []

    def directories_only(fd: int, mode: int) -> None:
        assert stat.S_ISDIR(os.fstat(fd).st_mode)
        changed.append(fd)
        fchmod(fd, mode)

    def rooted_unlink(name: str, *, dir_fd: int) -> None:
        assert "/" not in name and name not in {".", ".."}
        assert stat.S_ISDIR(os.fstat(dir_fd).st_mode)
        unlink(name, dir_fd=dir_fd)

    def rooted_rmdir(name: str, *, dir_fd: int) -> None:
        assert "/" not in name and name not in {".", ".."}
        assert stat.S_ISDIR(os.fstat(dir_fd).st_mode)
        rmdir(name, dir_fd=dir_fd)

    monkeypatch.setattr(api.os, "fchmod", directories_only)
    monkeypatch.setattr(api.os, "unlink", rooted_unlink)
    monkeypatch.setattr(api.os, "rmdir", rooted_rmdir)
    assert cache.release_completed(invocation(), expected, deadline=time.monotonic() + 10)
    assert changed


@native
@pytest.mark.parametrize("change", ["bin-link", "hardlink", "bad-reference"])
def test_release_rejects_unsafe_links_or_reference_without_host_mutation(
    api: Any,
    tmp_path: Path,
    change: str,
) -> None:
    cache, expected = ready(api, tmp_path)
    entry = cache.root / api.python_project_cache_key(invocation())
    host = tmp_path / "host"
    host.write_bytes(b"host must stay intact")
    host.chmod(0o400)
    python = entry / "environment/bin/python"
    if change == "bin-link":
        python.unlink()
        python.symlink_to(host)
    elif change == "hardlink":
        os.link(python, tmp_path / "hardlink")
    else:
        (entry / "reference.json").write_bytes(b"{}")
    with pytest.raises(api.PythonProjectCacheError):
        cache.release_completed(invocation(), expected, deadline=time.monotonic() + 10)
    assert entry.is_dir() and (entry / "reservation.json").exists()
    assert host.read_bytes() == b"host must stay intact"
    assert stat.S_IMODE(host.stat().st_mode) == 0o400
