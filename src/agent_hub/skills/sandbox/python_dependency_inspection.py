"""Root-only, read-only v8 observations of a declared Python dependency closure."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
import threading
import time
from typing import TYPE_CHECKING, cast

from pydantic import JsonValue

from agent_hub.capabilities.python_dependency_closure import check_python_dependency_closure
from agent_hub.capabilities.python_project_inputs import PythonInterpreterIdentity
from agent_hub.capabilities.python_project_recipe import (
    PythonProjectRecipeError,
    parse_python_project_declaration,
)
from agent_hub.capabilities.scoped_read import ScopedReadError, _read_no_links
from agent_hub.skills.sandbox.project import ProjectInvocation, _scope, project_source_fingerprint
from agent_hub.skills.sandbox.python_dependency_index_store import (
    PythonDependencyIndexError,
    read_dependency_metadata_index,
)
from agent_hub.skills.sandbox.python_dependency_metadata import PythonDependencyMetadataIndex
from agent_hub.skills.sandbox.python_dependency_protocol import (
    PythonDependencyClosureError,
    PythonDependencyClosureObservation,
    PythonDependencyClosureRequest,
    PythonDependencyClosureResponse,
    _validated_request,
)
from agent_hub.skills.sandbox.python_input_supply_catalog import read_supply_configuration
from agent_hub.skills.sandbox.python_project_backend import RootConfig, read_root_configuration
from agent_hub.skills.sandbox.python_project_profile import PythonProjectProfileScope

if TYPE_CHECKING:
    from agent_hub.skills.sandbox.broker import BrokerPolicy

_MAX_CONTROLS = 256 * 1024
_UNAVAILABLE: PythonDependencyClosureError = "python_dependency_unavailable"
_ERRORS = (OSError, ValueError, TypeError, AttributeError, KeyError, RuntimeError, OverflowError)
_INDEX_ERRORS: dict[str, PythonDependencyClosureError] = {
    "metadata_unavailable": "python_dependency_metadata_unavailable",
    "metadata_untrusted": "python_dependency_metadata_untrusted",
    "catalog_changed": "python_dependency_catalog_changed",
}
# Retained by the actual worker after caller timeout/cancellation, until its IO exits.
_WORKER_SLOT = threading.BoundedSemaphore(1)


class _InspectionError(ValueError):
    def __init__(self, code: PythonDependencyClosureError) -> None:
        self.code = code
        super().__init__(code)


def _require_root() -> None:
    if (sys.platform != "linux" or os.name != "posix"
            or os.getuid() != 0 or os.geteuid() != 0
            or os.getgid() != 0 or os.getegid() != 0):
        raise _InspectionError(_UNAVAILABLE)


def _digest(payload: object) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"),
        ensure_ascii=True, allow_nan=False).encode("ascii")).hexdigest()


def _check(deadline: float) -> None:
    if time.monotonic() >= deadline:
        raise TimeoutError


def _target_environment(root: RootConfig) -> dict[str, str]:
    identity = root.interpreter
    # The configured identity validates implementation, version, ABI and platform.
    PythonInterpreterIdentity.from_payload(identity.to_payload())
    machine = {"linux_x86_64": "x86_64", "linux_aarch64": "aarch64"}.get(identity.platform)
    uname = getattr(os, "uname", None)
    if not callable(uname):
        raise _InspectionError(_UNAVAILABLE)
    host = uname()
    if machine is None or host.sysname != "Linux" or host.machine != machine:
        raise _InspectionError(_UNAVAILABLE)
    environment = {
        "implementation_name": identity.implementation,
        "implementation_version": identity.version,
        "os_name": "posix",
        "platform_machine": host.machine,
        "platform_release": host.release,
        "platform_system": host.sysname,
        "platform_version": host.version,
        "platform_python_implementation": "CPython",
        "python_full_version": identity.version,
        "python_version": ".".join(identity.version.split(".")[:2]),
        "sys_platform": "linux",
    }
    if any(type(value) is not str or not value or len(value) > 1024
           or not value.isascii() or any(ord(char) < 32 or ord(char) > 126 for char in value)
           for value in environment.values()):
        raise _InspectionError(_UNAVAILABLE)
    return environment


def _failure(
    request: PythonDependencyClosureRequest, code: PythonDependencyClosureError,
) -> PythonDependencyClosureResponse:
    response = PythonDependencyClosureResponse(
        scope=PythonProjectProfileScope.for_invocation(request.invocation), ok=False, error=code)
    response.require_for(request)
    return response


async def _source(
    request: PythonDependencyClosureRequest, policy: BrokerPolicy, deadline: float,
) -> str:
    _check(deadline)
    try:
        source = await project_source_fingerprint(policy.project_workspace_root, request.invocation)
    except TimeoutError:
        raise
    except (OSError, ValueError, RuntimeError):
        raise _InspectionError("python_dependency_draft_changed") from None
    _check(deadline)
    if source != request.expected_source_sha256:
        raise _InspectionError("python_dependency_draft_changed")
    return source


async def _observe(
    request: PythonDependencyClosureRequest, policy: BrokerPolicy, deadline: float,
) -> PythonDependencyClosureResponse:
    _check(deadline)
    root = read_root_configuration()
    root_sha256 = _digest(root.to_payload())
    _check(deadline)
    supply = read_supply_configuration()
    catalog_sha256 = supply.digest
    if catalog_sha256 != request.expected_catalog_sha256:
        raise _InspectionError("python_dependency_catalog_changed")
    _check(deadline)
    environment = _target_environment(root)
    await _source(request, policy, deadline)
    maximum = min(_MAX_CONTROLS, request.invocation.source_limit_bytes)
    try:
        _, parts = _scope(policy.project_workspace_root, request.invocation)
        manifest = _read_no_links(policy.project_workspace_root, (*parts, "pyproject.toml"),
                                  max_bytes=maximum)
        _check(deadline)
        lock = _read_no_links(policy.project_workspace_root, (*parts, "requirements.lock"),
                              max_bytes=maximum)
    except TimeoutError:
        raise
    except (OSError, ScopedReadError, ValueError):
        raise _InspectionError("python_dependency_draft_changed") from None
    _check(deadline)
    try:
        declaration = parse_python_project_declaration(manifest, lock,
            max_control_bytes=maximum, max_requirements=128)
    except PythonProjectRecipeError:
        raise _InspectionError("python_dependency_declaration_invalid") from None
    _check(deadline)
    # This reader alone owns the fixed state_root/dependency-metadata-<digest>.json path.
    index = read_dependency_metadata_index(supply, deadline=deadline)
    _check(deadline)
    try:
        if type(index) is not PythonDependencyMetadataIndex:
            raise ValueError
        index_sha256 = index.sha256
    except (ValueError, TypeError, AttributeError, KeyError, RecursionError, OverflowError):
        raise _InspectionError("python_dependency_metadata_untrusted") from None
    try:
        index.require_catalog(supply.catalog, catalog_sha256)
    except ValueError:
        raise _InspectionError("python_dependency_catalog_changed") from None
    _check(deadline)
    closure = check_python_dependency_closure(declaration.requirements, index.records, environment)
    _check(deadline)
    source_sha256 = await _source(request, policy, deadline)
    try:
        final_root_sha256 = _digest(read_root_configuration().to_payload())
        _check(deadline)
        final_catalog_sha256 = read_supply_configuration().digest
        if (root_sha256 != _digest(root.to_payload()) or final_root_sha256 != root_sha256
                or supply.digest != catalog_sha256 or final_catalog_sha256 != catalog_sha256):
            raise _InspectionError("python_dependency_catalog_changed")
    except TimeoutError:
        raise
    except (OSError, ValueError, TypeError, AttributeError, KeyError, RuntimeError):
        raise _InspectionError("python_dependency_catalog_changed") from None
    _check(deadline)
    response = PythonDependencyClosureResponse(
        scope=PythonProjectProfileScope.for_invocation(request.invocation), ok=True,
        observation=PythonDependencyClosureObservation(
            source_sha256=source_sha256,
            manifest_sha256=hashlib.sha256(manifest).hexdigest(),
            lock_sha256=hashlib.sha256(lock).hexdigest(),
            catalog_sha256=catalog_sha256,
            metadata_index_sha256=index_sha256,
            target_environment_sha256=_digest(environment), target_environment=environment,
            closure=cast(dict[str, JsonValue], closure.to_payload())))
    response.require_for(request)
    _check(deadline)
    return response


async def _bounded_observe(
    request: PythonDependencyClosureRequest, policy: BrokerPolicy, deadline: float,
) -> PythonDependencyClosureResponse:
    async with asyncio.timeout(max(0.0, deadline - time.monotonic())):
        return await _observe(request, policy, deadline)


def _worker(
    request: PythonDependencyClosureRequest, policy: BrokerPolicy, deadline: float,
) -> PythonDependencyClosureResponse:
    try:
        # All synchronous IO, including the fingerprint's reads, stays off the broker loop.
        return asyncio.run(_bounded_observe(request, policy, deadline))
    except _InspectionError as error:
        return _failure(request, error.code)
    except PythonDependencyIndexError as error:
        return _failure(request, _INDEX_ERRORS.get(error.code, _UNAVAILABLE))
    except _ERRORS as error:
        original = getattr(error, "code", "")
        code: PythonDependencyClosureError = ("python_dependency_metadata_unavailable"
                if original == "input_supply_configuration_missing"
                else "python_dependency_metadata_untrusted"
                if original in {"input_supply_configuration_untrusted", "python_project_runtime_untrusted"}
                else _UNAVAILABLE)
        return _failure(request, code)
    finally:
        _WORKER_SLOT.release()


def _drain(task: asyncio.Task[PythonDependencyClosureResponse]) -> None:
    if not task.cancelled():
        task.exception()


async def inspect_python_dependency_closure(
    request: PythonDependencyClosureRequest, *, policy: BrokerPolicy,
) -> PythonDependencyClosureResponse:
    # model_copy can hide undeclared __dict__ attributes from model_dump entirely.
    for value, fields in ((request, PythonDependencyClosureRequest.model_fields),
                          (getattr(request, "invocation", None), ProjectInvocation.model_fields)):
        if hasattr(value, "__dict__") and set(vars(value)) - fields.keys():
            raise ValueError("python_dependency_request_invalid")
    request = _validated_request(request)
    deadline = time.monotonic() + request.invocation.timeout_seconds
    try:
        async with asyncio.timeout(request.invocation.timeout_seconds):
            _require_root()
            while True:
                _check(deadline)
                if _WORKER_SLOT.acquire(blocking=False):
                    break
                await asyncio.sleep(min(0.01, max(0.0, deadline - time.monotonic())))
            try:
                pending = asyncio.create_task(asyncio.to_thread(_worker, request, policy, deadline))
            except BaseException:
                _WORKER_SLOT.release()
                raise
            pending.add_done_callback(_drain)
            return await asyncio.shield(pending)
    except _ERRORS:
        # Only the finite protocol vocabulary crosses Root; no host diagnostics escape.
        return _failure(request, _UNAVAILABLE)
