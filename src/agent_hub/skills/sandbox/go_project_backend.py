"""Trusted synchronous Root Go diagnostics with protected candidate snapshots.

No CLI, RPC admission, DB grant adapter, binary export or business receipt lives
here. The trusted caller supplies live authority, execution/UNKNOWN fences and
an already-created private stage, and owns its lifetime. Never retry uncertain
cleanup or delete any stage here. Call only in a single-threaded Root helper.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import os
import re
import stat
import sys
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Protocol, cast

from agent_hub.capabilities.go_project_inputs import GoProjectInputs, bind_go_project_inputs
from agent_hub.capabilities.go_project_recipe import (
    parse_go_project_declaration,
    validate_go_source_paths,
)
from agent_hub.capabilities.project_baselines import ProjectBaselineReference, StoredProjectBaseline
from agent_hub.capabilities.project_contracts import _files
from agent_hub.capabilities.project_stack_registry import is_stack_control_path
from agent_hub.capabilities.scoped_read import _read_no_links
from agent_hub.skills.sandbox import python_project_backend as _python
from agent_hub.skills.sandbox.base import SkillResult
from agent_hub.skills.sandbox.broker import BrokerPolicy
from agent_hub.skills.sandbox.go_project_command import build_go_project_systemd_command
from agent_hub.skills.sandbox.go_project_protocol import (
    GO_PROJECT_DIAGNOSTIC_MAX_BYTES,
    GoProjectError,
    GoProjectInvocation,
    GoProjectRequest,
    GoProjectResponse,
    _json,
    base_go_invocation,
    parse_go_project_request,
)
from agent_hub.skills.sandbox.go_project_runner import GoProjectRunnerPlan
from agent_hub.skills.sandbox.project import _Execution, snapshot_project_source
from agent_hub.skills.sandbox.project_contract_broker import (
    _encoded,
    _Observed,
    _open_directory,
    _root,
    _seal_readonly,
    _stamp,
    _write_all,
    stage_original_contract,
)
from agent_hub.skills.sandbox.python_project_backend import _admission, _source_files
from agent_hub.skills.sandbox.python_project_process import (
    PythonPhaseProcessResult,
    run_python_phase,
)
from agent_hub.skills.sandbox.toolchain_catalog import ToolchainCatalog, source_copy_peak
from agent_hub.skills.sandbox.toolchain_store import observe_published_toolchain
from agent_hub.skills.sandbox.toolchain_worker import toolchain_reference
from agent_hub.subprocess_cleanup import ProcessCleanupIncomplete

_MAX_PLAN = 32 * 1024**2
_RESERVE = 3 * 1024**3


class GoProjectAuthority(Protocol):
    """Trusted live check of the exact full scope/recipe; deny by raising.

    Supply receipts and serialized approved booleans are never authority. A
    real service adapter must check current ownership, approval and revocation.
    """

    def __call__(self, inputs: GoProjectInputs, inv: GoProjectInvocation) -> None: ...


class GoProjectBackendError(ValueError):
    def __init__(self, code: GoProjectError) -> None:
        self.code = code
        super().__init__(code)


def _need(value: object, code: GoProjectError = 'go_project_execution_unavailable') -> None:
    if not value:
        raise GoProjectBackendError(code)


def _check(deadline: float) -> None:
    _need(time.monotonic() < deadline, 'go_project_timeout')


def _native() -> None:
    if sys.platform != 'linux':
        raise GoProjectBackendError('go_project_unsupported')
    _need(os.geteuid() == 0 and os.getegid() == 0
          and threading.active_count() == 1
          and threading.current_thread() is threading.main_thread(), 'go_project_unsupported')
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return
    raise GoProjectBackendError('go_project_unsupported')


def _authorize(authority: GoProjectAuthority, inputs: GoProjectInputs,
               invocation: GoProjectInvocation) -> None:
    try:
        _need(callable(authority), 'go_project_recipe_approval_required')
        result = cast(Callable[[GoProjectInputs, GoProjectInvocation], object], authority)(
            inputs, invocation)
        if inspect.iscoroutine(result):
            result.close()
        _need(result is None, 'go_project_recipe_approval_required')
    except ProcessCleanupIncomplete:
        raise
    except Exception:  # noqa: BLE001 - live grant failures expose no service/private data
        raise GoProjectBackendError('go_project_recipe_approval_required') from None


@dataclass(frozen=True, slots=True)
class _Roots:
    stage_fd: int
    admission_fd: int
    pins: list[_Observed]


@contextmanager
def _roots(policy: BrokerPolicy, catalog: ToolchainCatalog, stage: Path) -> Iterator[_Roots]:
    _need(type(policy) is BrokerPolicy and type(policy.allowed_uid) is int
          and 0 < policy.allowed_uid < 2**32 - 1 and type(policy.allowed_gid) is int
          and 0 < policy.allowed_gid < 2**32 - 1, 'go_project_request_invalid')
    _need(policy.systemd_run == '/usr/bin/systemd-run'
          and policy.systemctl == '/usr/bin/systemctl'
          and re.fullmatch(r'[a-z_][a-z0-9_-]{0,31}', policy.service_group) is not None,
          'go_project_unsupported')
    _need(isinstance(stage, Path) and stage.parent == Path(catalog.stage_root)
          and stage.parent == policy.toolchain_runtime_root, 'go_project_request_invalid')
    private = (Path(catalog.stage_root), Path(catalog.runtime_root), Path(catalog.state_root),
               policy.python_project_runtime_root)
    for index, left in enumerate(private):
        for right in (*private[index + 1:], policy.project_workspace_root, policy.source_path):
            _need(not left.is_relative_to(right) and not right.is_relative_to(left),
                  'go_project_environment_unavailable')
    if sys.platform != 'linux':
        raise GoProjectBackendError('go_project_unsupported')
    with ExitStack() as stack:
        pins: list[_Observed] = []
        identities: set[tuple[int, int]] = set()
        opened: dict[Path, int] = {}
        for path in (*private, stage, policy.source_path):
            fd = _root(path, 0, stack, pins, stage=path in (*private, stage))
            info = os.fstat(fd)
            identity = (info.st_dev, info.st_ino)
            _need(identity not in identities, 'go_project_environment_unavailable')
            identities.add(identity)
            opened[path] = fd
        _root(policy.project_workspace_root, policy.allowed_uid, stack, pins)
        python = Path(policy.python)
        parent = _root(python.parent, 0, stack, pins)
        fd = os.open(python.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                     dir_fd=parent)
        stack.callback(os.close, fd)
        info = os.fstat(fd)
        _need(stat.S_ISREG(info.st_mode) and info.st_uid == 0 and info.st_mode & 0o111
              and not info.st_mode & 0o022, 'go_project_environment_unavailable')
        pins.append(_Observed(parent, python.name, _stamp(info, full=True), True, fd))
        # The outer DynamicUser must acquire exactly this export group.
        import grp

        _need(grp.getgrnam(policy.service_group).gr_gid == policy.allowed_gid,
              'go_project_environment_unavailable')
        _python._host_disk(opened[stage])
        _python._host_disk(opened[policy.python_project_runtime_root])
        roots = _Roots(opened[stage], opened[policy.python_project_runtime_root], pins)
        for pin in pins:
            pin.check()
        yield roots
        for pin in pins:
            pin.check()


def _disk_budget(fd: int, inv: GoProjectInvocation, catalog: ToolchainCatalog) -> None:
    """Conservative peak before copying, on the pinned stage filesystem."""
    statvfs = getattr(os, 'fstatvfs', None)
    _need(callable(statvfs), 'go_project_unsupported')
    assert callable(statvfs)
    disk = statvfs(fd)
    block = disk.f_frsize
    _need(type(block) is int and 0 < block <= 1024**2, 'go_project_capacity')
    # Candidate execution retains both sealed copies alongside controls and work.
    copies = 2 if inv.source_kind == 'candidate' else 1
    required = (copies * source_copy_peak(inv.source_limit_bytes, inv.file_limit * 10, block)
                + _MAX_PLAN + 1024**2 + inv.workspace_limit_bytes)
    _need(disk.f_bavail * block >= required + max(_RESERVE, catalog.min_free_bytes),
          'go_project_capacity')


def _measured_inputs(original: StoredProjectBaseline, inv: GoProjectInvocation,
                     catalog: ToolchainCatalog, deadline: float) -> GoProjectInputs:
    _check(deadline)
    _need(original.generated_metadata is None, 'go_project_unsupported')
    files = _source_files(original.snapshot, inv, deadline)
    _need(files == original.source_files, 'go_project_protected_input_changed')
    raw: dict[str, bytes] = {}
    remaining = inv.source_limit_bytes
    for name in files:
        _check(deadline)
        data = _read_no_links(original.snapshot, tuple(name.split('/')), max_bytes=remaining)
        remaining -= len(data)
        _need(remaining >= 0, 'go_project_source_unavailable')
        raw[name] = data
    declaration = parse_go_project_declaration(raw, max_source_bytes=inv.source_limit_bytes,
                                               max_files=inv.file_limit)
    artifact = catalog.select(declaration)
    _need(artifact.artifact_sha256 == inv.toolchain.artifact_sha256,
          'go_project_toolchain_changed')
    published = observe_published_toolchain(Path(catalog.runtime_root), artifact=artifact,
                                           deadline=deadline)
    reference = toolchain_reference(published)
    _need(reference == inv.toolchain, 'go_project_toolchain_changed')
    inputs = bind_go_project_inputs(stored=original, scope=inv.scope, toolchain=reference,
                                   source_limit_bytes=inv.source_limit_bytes,
                                   file_limit=inv.file_limit)
    _need(inputs.declaration == declaration and inputs.scope == inv.scope
          and inputs.original_reference.to_payload() == inv.original_reference.model_dump()
          and inputs.sha256 == inv.recipe_sha256
          and inputs.environment_input_sha256 == inv.environment_input_sha256,
          'go_project_recipe_changed')
    inputs.require_current(files)
    _check(deadline)
    return inputs


def _require_current(inputs: GoProjectInputs, source_files: Mapping[str, str]) -> None:
    # Classify at validator boundaries; exception messages are never protocol codes.
    try:
        current = _files(source_files)
    except ValueError:
        raise GoProjectBackendError('go_project_source_unavailable') from None
    _need(len(current) <= inputs.file_limit, 'go_project_capacity')
    controls = {path: value for path, value in current.items() if is_stack_control_path(path)}
    original_controls = {path: value for path, value in inputs.source_files.items()
                         if is_stack_control_path(path)}
    _need(controls == original_controls, 'go_project_protected_input_changed')
    try:
        validate_go_source_paths(tuple(current))
    except ValueError:
        raise GoProjectBackendError('go_project_unsupported') from None
    try:
        inputs.require_current(current)
    except ValueError:
        raise GoProjectBackendError('go_project_protected_input_changed') from None


def _recheck_source(original: StoredProjectBaseline, inputs: GoProjectInputs,
                    inv: GoProjectInvocation, snapshot: Path, digest: str,
                    deadline: float) -> None:
    files = _source_files(original.snapshot, inv, deadline)
    _need(files == original.source_files, 'go_project_protected_input_changed')
    current = files if snapshot == original.snapshot else _source_files(snapshot, inv, deadline)
    _require_current(inputs, current)
    _need(hashlib.sha256(_encoded(current)).hexdigest() == digest,
          'go_project_source_unavailable')
    _check(deadline)


def _source_snapshot(original: StoredProjectBaseline, inputs: GoProjectInputs,
                     inv: GoProjectInvocation, policy: BrokerPolicy, stage: Path,
                     deadline: float) -> str:
    _check(deadline)
    if inv.source_kind == 'original':
        snapshot, digest = original.snapshot, original.reference.input_source_sha256
    else:
        snapshot = stage / 'snapshot'
        manifest: dict[str, str] = {}
        try:
            digest = asyncio.run(snapshot_project_source(policy.project_workspace_root,
                base_go_invocation(inv), destination=snapshot, _manifest=manifest))
        except ProcessCleanupIncomplete:
            raise
        except RuntimeError:
            raise GoProjectBackendError('go_project_source_unavailable') from None
        _require_current(inputs, manifest)
    _recheck_source(original, inputs, inv, snapshot, digest, deadline)
    return digest


def _write_plan(plan: GoProjectRunnerPlan, parent: int, stage: Path) -> Path:
    if sys.platform != 'linux':
        raise GoProjectBackendError('go_project_unsupported')
    raw = plan.model_dump_json(warnings='error').encode('utf-8')
    _need(len(raw) <= _MAX_PLAN, 'go_project_recipe_unavailable')
    fd = os.open('plan.json', os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                 0o600, dir_fd=parent)
    try:
        _write_all(fd, raw)
        _seal_readonly(fd, 0o444)
    finally:
        os.close(fd)
    os.fsync(parent)
    return stage / 'plan.json'


def _make_export(parent: int, stage: Path, gid: int) -> Path:
    if sys.platform != 'linux':
        raise GoProjectBackendError('go_project_unsupported')
    os.mkdir('export', 0o700, dir_fd=parent)
    fd = os.open('export', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                 dir_fd=parent)
    try:
        _need(os.fstat(fd).st_uid == 0)
        os.fchown(fd, 0, gid)
        os.fchmod(fd, 0o770)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.fsync(parent)
    return stage / 'export'


def _export_empty(parent: int, deadline: float) -> None:
    with ExitStack() as stack:
        pins: list[_Observed] = []
        fd = _open_directory(parent, 'export', 0, stack, pins, mode=0o770, full=True)
        _check(deadline)
        with os.scandir(fd) as entries:
            _need(next(entries, None) is None, 'go_project_environment_changed')
        for pin in pins:
            pin.check()


def _quiesce(policy: BrokerPolicy, job: _Execution) -> None:
    try:
        _python._quiesce(policy, job)
    except BaseException:  # noqa: BLE001 - UNKNOWN must survive every cleanup failure
        raise ProcessCleanupIncomplete('go_project_cleanup_incomplete') from None


def _report(process: PythonPhaseProcessResult, inv: GoProjectInvocation,
            inputs: GoProjectInputs, source_sha256: str) -> dict[str, Any]:
    """Validate the complete outer process stream without claiming project pass."""
    import json

    _need(type(process) is PythonPhaseProcessResult)
    _need(type(process.exit_code) is int and process.exit_code == 0
          and process.timed_out is False and process.stdout_truncated is False
          and process.stderr_truncated is False)
    sizes = []
    for name in ('stdout', 'stderr'):
        stream = getattr(process, name)
        observed = getattr(process, name + '_observed_bytes')
        _need(type(stream) is str and type(observed) is int)
        size = len(stream.encode('utf-8', errors='strict'))
        _need(size == observed)
        sizes.append(size)
    _need(sum(sizes) <= GO_PROJECT_DIAGNOSTIC_MAX_BYTES)
    raw = process.stdout.encode('utf-8')
    _json(raw, GO_PROJECT_DIAGNOSTIC_MAX_BYTES)
    payload: dict[str, Any] = json.loads(raw)
    expected = {
        'schema_version': 1, 'kind': 'go_project', 'evidence': 'native_process_only',
        'execution_id': inv.execution_id, 'scope': inputs.scope.to_payload(),
        'source_kind': inv.source_kind, 'source_sha256': source_sha256,
        'recipe_sha256': inputs.sha256, 'environment_input_sha256': inputs.environment_input_sha256,
        'original_reference': inputs.original_reference.to_payload(),
        'original_check_contract': inputs.contract.protected_sha256,
        'toolchain': inputs.toolchain.model_dump(mode='json'), 'toolchain_measurement': 'not_run',
        'business_gate': 'not_run', 'cache_reuse': 'none',
    }
    _need(type(payload.get('schema_version')) is int
          and all(key in payload and _encoded(payload[key]) == _encoded(value)
                  for key, value in expected.items()))
    allowed = {*expected, 'status', 'checks', 'canary', 'failed_phase', 'inventory',
               'binary', 'test_evidence', 'canary_evidence', 'diagnostics_withheld'}
    _need(set(payload) <= allowed and payload.get('status') in {'incomplete', 'failed'}
          and payload.get('canary') in {'not_run', 'completed', 'failed'})
    checks = payload.get('checks')
    _need(type(checks) is list and len(checks) <= 8)
    checks = cast(list[dict[str, Any]], checks)
    phases = {'source', 'toolchain', 'source_integrity', 'inspect', 'build', 'test', 'canary'}
    for row in checks:
        _need(type(row) is dict and row.get('phase') in phases
              and row.get('status') in {'completed', 'failed'})
        if row.get('error') == 'go_project_cleanup_incomplete':
            raise ProcessCleanupIncomplete('go_project_cleanup_incomplete')
        if 'error' in row:
            _need(type(row['error']) is str
                  and re.fullmatch(r'go_project_[a-z_]{1,80}', row['error']) is not None)
        streams = {'stdout', 'stderr', 'stdout_truncated', 'stderr_truncated',
                   'stdout_observed_bytes', 'stderr_observed_bytes', 'exit_code', 'timed_out'}
        _need(set(row) <= {'phase', 'status', 'error', *streams,
                           'stdout_preview_withheld', 'stderr_preview_withheld'})
        if set(row) & streams:
            _need(streams <= set(row) and type(row['timed_out']) is bool
                  and (row['exit_code'] is None or type(row['exit_code']) is int))
            for name in ('stdout', 'stderr'):
                observed, truncated = row[name + '_observed_bytes'], row[name + '_truncated']
                withheld = row.get(name + '_preview_withheld', False)
                _need(type(row[name]) is str and type(observed) is int and observed >= 0
                      and type(truncated) is bool and type(withheld) is bool)
                size = len(row[name].encode('utf-8', errors='strict'))
                _need((truncated or withheld) or size == observed)
    if payload['status'] == 'failed':
        _need(payload.get('failed_phase') in phases
              and any(row['status'] == 'failed' for row in checks))
    return payload


def _response(request: GoProjectRequest, **outcome: Any) -> GoProjectResponse:
    inv = request.invocation
    response = GoProjectResponse(action=request.action, execution_id=inv.execution_id,
        scope=inv.scope, recipe_sha256=inv.recipe_sha256,
        environment_input_sha256=inv.environment_input_sha256, toolchain=inv.toolchain, **outcome)
    response.require_for(request)
    return response


def execute_go_project_request(
    request: GoProjectRequest, *, policy: BrokerPolicy, catalog: ToolchainCatalog,
    stage: Path, authorize: GoProjectAuthority,
) -> GoProjectResponse:
    """Caller-authorized Root helper only; never RPC/event-loop/thread admission.

    Full archive/tree observations before and after are Root's own acceptance
    checks. They do not rewrite the runner's toolchain_measurement=not_run or
    turn its diagnostic result into a service approval or business receipt.
    """
    _need(type(request) is GoProjectRequest, 'go_project_request_invalid')
    try:
        request = parse_go_project_request(request.model_dump_json(warnings='error').encode())
    except (ValueError, TypeError, AttributeError):
        raise GoProjectBackendError('go_project_request_invalid') from None
    inv = request.invocation
    try:
        _native()
        _need(callable(authorize), 'go_project_recipe_approval_required')
        _need(type(catalog) is ToolchainCatalog, 'go_project_environment_unavailable')
        catalog_bytes = catalog.to_json()
        catalog = ToolchainCatalog.from_json(catalog_bytes)
        _need(type(policy) is BrokerPolicy, 'go_project_request_invalid')
        policy_snapshot = replace(policy)
        deadline = time.monotonic() + inv.timeout_seconds
        with _roots(policy, catalog, stage) as roots, _admission(roots.admission_fd, deadline):
            def recheck() -> None:
                _native()
                _check(deadline)
                _need(catalog.to_json() == catalog_bytes and policy == policy_snapshot,
                      'go_project_environment_changed')
                for pin in roots.pins:
                    pin.check()

            recheck()
            _disk_budget(roots.stage_fd, inv, catalog)
            original = stage_original_contract(policy.project_workspace_root,
                base_go_invocation(inv),
                ProjectBaselineReference.from_payload(inv.original_reference.model_dump()),
                stage, policy.allowed_uid)
            inputs = _measured_inputs(original, inv, catalog, deadline)
            _authorize(authorize, inputs, inv)
            _native()
            _check(deadline)
            source_sha256 = _source_snapshot(original, inputs, inv, policy, stage, deadline)
            snapshot = original.snapshot if inv.source_kind == 'original' else stage / 'snapshot'

            def remeasure() -> None:
                recheck()
                _need(_measured_inputs(original, inv, catalog, deadline) == inputs,
                      'go_project_environment_changed')
                _recheck_source(original, inputs, inv, snapshot, source_sha256, deadline)
                recheck()

            if request.action == 'inspect_go_project_environment':
                remeasure()
                response = _response(request, ok=True, environment=inputs.toolchain)
                _authorize(authorize, inputs, inv)
            else:
                plan = GoProjectRunnerPlan(invocation=inv, inputs=inputs.to_payload(),
                                           source_sha256=source_sha256)
                plan_path = _write_plan(plan, roots.stage_fd, stage)
                export = _make_export(roots.stage_fd, stage, policy.allowed_gid)
                artifact = catalog.select(inputs.declaration)
                command = build_go_project_systemd_command(inv, policy,
                    snapshot=snapshot, original=original.snapshot, plan=plan_path,
                    toolchain_root=Path(catalog.runtime_root) / artifact.artifact_sha256 / 'payload/go',
                    export=export)
                _export_empty(roots.stage_fd, deadline)
                remeasure()
                job = _Execution(inv, None, stage=stage, launched=True,
                                 source_sha256=source_sha256)
                _authorize(authorize, inputs, inv)
                remeasure()
                try:
                    process = run_python_phase(command, cwd=stage, deadline=deadline,
                        capture_limit_bytes=GO_PROJECT_DIAGNOSTIC_MAX_BYTES)
                except BaseException:  # noqa: BLE001 - launcher uncertainty is never retryable
                    raise ProcessCleanupIncomplete('go_project_cleanup_incomplete') from None
                finally:
                    _quiesce(policy, job)
                _report(process, inv, inputs, source_sha256)
                _export_empty(roots.stage_fd, deadline)
                remeasure()
                response = _response(request, ok=True, result=SkillResult(
                    exit_code=process.exit_code, stdout=process.stdout, stderr=process.stderr,
                    timed_out=False))
                _authorize(authorize, inputs, inv)
            remeasure()
            _native()
            _check(deadline)
        return response
    except ProcessCleanupIncomplete:
        raise
    except GoProjectBackendError as error:
        code = error.code
    except Exception:  # noqa: BLE001 - finite boundary; never expose source or host exceptions
        code = 'go_project_execution_unavailable'
    return _response(request, ok=False, error=code)
