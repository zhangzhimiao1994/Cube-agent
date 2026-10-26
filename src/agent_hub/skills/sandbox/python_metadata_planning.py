"""Root readonly pre-capture planning against actual scoped controls and cache."""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Callable
from contextlib import ExitStack
from typing import TYPE_CHECKING

from agent_hub.capabilities.scoped_read import _read_no_links
from agent_hub.skills.sandbox import python_dependency_index_store as store
from agent_hub.skills.sandbox.project import _scope, project_source_fingerprint
from agent_hub.skills.sandbox.python_dependency_inspection import _WORKER_SLOT, _require_root
from agent_hub.skills.sandbox.python_dependency_metadata import extract_python_wheel_metadata
from agent_hub.skills.sandbox.python_input_fetch import WheelDownload
from agent_hub.skills.sandbox.python_input_supply_catalog import (
    RootSupplyConfig,
    _directory,
    _DirectoryPin,
    _stamp,
    read_supply_configuration,
)
from agent_hub.skills.sandbox.python_metadata_plan import _digest, build_python_metadata_plan
from agent_hub.skills.sandbox.python_metadata_plan_protocol import (
    MetadataPlanningError,
    PythonMetadataPlanningRequest,
    PythonMetadataPlanningResponse,
)
from agent_hub.skills.sandbox.python_project_backend import RootConfig, read_root_configuration
from agent_hub.skills.sandbox.python_project_profile import PythonProjectProfileScope

if TYPE_CHECKING:
    from agent_hub.skills.sandbox.broker import BrokerPolicy

read_dependency_metadata_index = store.read_dependency_metadata_index


class _PlanningError(ValueError):
    def __init__(self, code: MetadataPlanningError) -> None:
        self.code = code
        super().__init__(code)


def _check(deadline: float) -> None:
    if time.monotonic() >= deadline:
        raise _PlanningError("metadata_plan_timeout")


def _failure(request: PythonMetadataPlanningRequest, code: MetadataPlanningError) -> PythonMetadataPlanningResponse:
    return PythonMetadataPlanningResponse(scope=PythonProjectProfileScope.for_invocation(request.invocation),
        ok=False, error=code)


async def _source(request: PythonMetadataPlanningRequest, policy: BrokerPolicy, deadline: float) -> None:
    _check(deadline)
    try:
        observed = await project_source_fingerprint(policy.project_workspace_root, request.invocation)
    except (OSError, ValueError, RuntimeError):
        raise _PlanningError("metadata_plan_draft_changed") from None
    _check(deadline)
    if observed != request.draft.source_sha256:
        raise _PlanningError("metadata_plan_draft_changed")


def _controls(request: PythonMetadataPlanningRequest, policy: BrokerPolicy, deadline: float) -> None:
    import hashlib

    try:
        _, parts = _scope(policy.project_workspace_root, request.invocation)
        for name, maximum, expected in (
            ("ACCEPTANCE.json", 128 * 1024, request.draft.acceptance_sha256),
            ("pyproject.toml", 256 * 1024, request.draft.manifest_sha256),
            ("requirements.lock", 256 * 1024, request.draft.lock_sha256),
        ):
            _check(deadline)
            bound = min(maximum, request.invocation.source_limit_bytes)
            raw = _read_no_links(policy.project_workspace_root, (*parts, name), max_bytes=bound)
            if len(raw) > bound or hashlib.sha256(raw).hexdigest() != expected:
                raise _PlanningError("metadata_plan_draft_changed")
        _check(deadline)
    except _PlanningError:
        raise
    except (OSError, ValueError, RuntimeError):
        raise _PlanningError("metadata_plan_draft_changed") from None


def _cached(
    root: RootConfig, supply: RootSupplyConfig, stack: ExitStack, deadline: float,
) -> tuple[tuple[WheelDownload, ...], Callable[[], None]]:
    cached: list[WheelDownload] = []
    files: list[tuple[str, tuple[int, ...] | None]] = []
    pins: list[_DirectoryPin] = []
    try:
        parent = _directory(root.wheel_root, stack, pins)
        for entry in supply.catalog:
            store._directories(pins, deadline)
            try:
                os.stat(entry.filename, dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                files.append((entry.filename, None))
                continue
            with ExitStack() as wheel_stack:
                raw, pin = store._read_file(parent, entry.filename, entry.size_bytes, wheel_stack,
                    deadline, expected_size=entry.size_bytes, private=False)
                extract_python_wheel_metadata(entry, raw)
                _check(deadline)
                pin.check()
                files.append((entry.filename, pin.identity))
                cached.append(entry)
    except _PlanningError:
        raise
    except (OSError, ValueError, RuntimeError) as error:
        code: MetadataPlanningError = ("metadata_plan_timeout"
            if isinstance(error, store.PythonDependencyIndexError) and error.code == "timeout"
            else "metadata_plan_cache_untrusted")
        raise _PlanningError(code) from None

    def check() -> None:
        try:
            store._directories(pins, deadline)
            for name, expected in files:
                _check(deadline)
                try:
                    current = _stamp(os.stat(name, dir_fd=parent, follow_symlinks=False))
                except FileNotFoundError:
                    current = None
                if current != expected:
                    raise _PlanningError("metadata_plan_cache_untrusted")
        except _PlanningError:
            raise
        except (OSError, ValueError, RuntimeError) as error:
            raise _PlanningError("metadata_plan_timeout"
                if isinstance(error, store.PythonDependencyIndexError) and error.code == "timeout"
                else "metadata_plan_cache_untrusted") from None

    check()
    return tuple(cached), check


async def _observe(
    request: PythonMetadataPlanningRequest, policy: BrokerPolicy, deadline: float,
) -> PythonMetadataPlanningResponse:
    _check(deadline)
    root = read_root_configuration()
    supply = read_supply_configuration()
    root_sha, catalog_sha = _digest(root.to_payload()), supply.digest
    if (root_sha != request.expected_root_config_sha256
            or catalog_sha != request.expected_catalog_sha256):
        raise _PlanningError("metadata_plan_catalog_changed")
    await _source(request, policy, deadline)
    _controls(request, policy, deadline)
    _check(deadline)
    try:
        index = read_dependency_metadata_index(supply, deadline=deadline)
    except store.PythonDependencyIndexError as error:
        if error.code != "metadata_unavailable":
            codes: dict[str, MetadataPlanningError] = {
                "metadata_untrusted": "metadata_plan_metadata_untrusted",
                "catalog_changed": "metadata_plan_catalog_changed", "timeout": "metadata_plan_timeout",
            }
            raise _PlanningError(codes.get(error.code, "metadata_plan_unavailable")) from None
        index = None
    _check(deadline)
    with ExitStack() as stack:
        if index is not None:
            cached: tuple[WheelDownload, ...] = ()
            check_cache: Callable[[], None] = lambda: _check(deadline)
        else:
            if sum(e.size_bytes for e in supply.catalog) > min(supply.max_total_bytes, root.wheel_max_bytes):
                raise _PlanningError("metadata_plan_quota")
            cached, check_cache = _cached(root, supply, stack, deadline)
        plan = build_python_metadata_plan(request, root=root, supply=supply, cached=cached, index=index)
        await _source(request, policy, deadline)
        _controls(request, policy, deadline)
        if (_digest(read_root_configuration().to_payload()) != root_sha
                or read_supply_configuration().digest != catalog_sha):
            raise _PlanningError("metadata_plan_catalog_changed")
        check_cache()
        _check(deadline)
    result = PythonMetadataPlanningResponse(scope=PythonProjectProfileScope.for_invocation(request.invocation),
        ok=True, plan=plan)
    result.require_for(request)
    return result


def _worker(request: PythonMetadataPlanningRequest, policy: BrokerPolicy, deadline: float) -> PythonMetadataPlanningResponse:
    try:
        return asyncio.run(_observe(request, policy, deadline))
    except _PlanningError as error:
        return _failure(request, error.code)
    except (OSError, ValueError, TypeError, AttributeError, RuntimeError, OverflowError):
        return _failure(request, "metadata_plan_unavailable")
    finally:
        _WORKER_SLOT.release()


def _drain(task: asyncio.Task[PythonMetadataPlanningResponse]) -> None:
    if not task.cancelled():
        task.exception()


async def plan_python_dependency_metadata(
    request: PythonMetadataPlanningRequest, *, policy: BrokerPolicy,
) -> PythonMetadataPlanningResponse:
    if type(request) is not PythonMetadataPlanningRequest:
        raise ValueError("metadata_plan_request_invalid")
    try:
        request = PythonMetadataPlanningRequest.model_validate(request, strict=True)
    except (ValueError, TypeError, AttributeError, RecursionError):
        raise ValueError("metadata_plan_request_invalid") from None
    deadline = time.monotonic() + request.invocation.timeout_seconds
    try:
        async with asyncio.timeout(request.invocation.timeout_seconds):
            _require_root()
            while not _WORKER_SLOT.acquire(blocking=False):
                _check(deadline)
                await asyncio.sleep(min(0.01, max(0.0, deadline - time.monotonic())))
            try:
                pending = asyncio.create_task(asyncio.to_thread(_worker, request, policy, deadline))
            except BaseException:
                _WORKER_SLOT.release()
                raise
            pending.add_done_callback(_drain)
            return await asyncio.shield(pending)
    except (TimeoutError, _PlanningError):
        return _failure(request, "metadata_plan_timeout")
    except (OSError, ValueError, RuntimeError):
        return _failure(request, "metadata_plan_unavailable")
