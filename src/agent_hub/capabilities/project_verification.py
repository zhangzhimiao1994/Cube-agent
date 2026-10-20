"""Task-authorized project verification and source-bound delivery receipts."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
from collections.abc import Awaitable, Mapping
from dataclasses import dataclass
from inspect import isawaitable
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, cast
from uuid import UUID, uuid4

from agent_hub.capabilities.approvals import capability_approval_scope
from agent_hub.capabilities.execution_diagnostics import ExecutionDiagnostic
from agent_hub.capabilities.project_prerequisites import (
    ProjectPrerequisiteObservation,
    validate_project_prerequisite_report,
)
from agent_hub.capabilities.scoped_read import authorized_workspace_scope
from agent_hub.capabilities.types import CapabilityRequest
from agent_hub.runtime.contracts import JsonValue, _freeze_object
from agent_hub.skills.sandbox.base import SkillResult
from agent_hub.subprocess_cleanup import ProcessCleanupIncomplete

if TYPE_CHECKING:
    from agent_hub.capabilities.project_baselines import (
        ProjectBaselineReference,
        StoredProjectBaseline,
    )
    from agent_hub.capabilities.python_project_inputs import PythonProjectInputs
    from agent_hub.capabilities.python_project_reports import PythonProjectFailure
    from agent_hub.capabilities.python_project_service import (
        PythonOriginalContext,
        PythonProjectExecutor,
    )
    from agent_hub.skills.sandbox.project import ProjectInvocation
    from agent_hub.skills.sandbox.python_project_protocol import PythonProjectInvocation

PROJECT_VERIFY_TOOL = "project.verify"
PROJECT_VERIFY_RESOURCE = "workspace/current/project-verification"
_SHA = re.compile(r"[a-f0-9]{64}")


@dataclass(frozen=True, slots=True)
class ProjectVerificationFailure:
    """Validated failed execution metadata, not a successful delivery receipt."""

    report: Mapping[str, JsonValue]

    def __post_init__(self) -> None:
        report = _freeze_object(self.report, name="project verification failure")
        source = report.get("source_sha256")
        if (set(report) != {"schema_version", "status", "source_sha256", "checks", "runtime"}
            or type(report.get("schema_version")) is not int or report["schema_version"] != 1
            or report.get("status") != "failed" or not isinstance(source, str)
            or _SHA.fullmatch(source) is None):
            raise ValueError("invalid project verification failure")
        checks, runtime = report["checks"], report["runtime"]
        if (not isinstance(checks, tuple) or len(checks) != 3
            or not isinstance(runtime, Mapping) or set(runtime) - {"node", "npm"}
            or any(not isinstance(value, str)
                   or re.fullmatch(r"v?[0-9][A-Za-z0-9.+-]{0,63}", value) is None
                   for value in runtime.values())):
            raise ValueError("invalid project verification failure")
        failed = False
        for phase, check in zip(("install", "build", "test"), checks, strict=True):
            if not isinstance(check, Mapping) or check.get("phase") != phase:
                raise ValueError("invalid project verification failure")
            status = check.get("status")
            if status == "not_run":
                if not failed or set(check) != {"phase", "status"}:
                    raise ValueError("invalid project verification failure")
                continue
            diagnostic = check.get("execution_diagnostic")
            if (failed or status not in {"passed", "failed"}
                or set(check) != {"phase", "status", "execution_diagnostic"}
                or not isinstance(diagnostic, Mapping)
                or set(diagnostic) != {"schema_version", "backend", "exit_code", "timed_out",
                                       "stdout_metadata", "stderr_metadata"}
                or type(diagnostic["schema_version"]) is not int or diagnostic["schema_version"] != 1
                or diagnostic["backend"] != "systemd"
                or type(diagnostic["timed_out"]) is not bool
                or (diagnostic["exit_code"] is not None and type(diagnostic["exit_code"]) is not int)):
                raise ValueError("invalid project verification failure")
            if ((status == "passed" and (diagnostic["exit_code"] != 0 or diagnostic["timed_out"]))
                or (status == "failed" and not diagnostic["timed_out"]
                    and diagnostic["exit_code"] in (None, 0))):
                raise ValueError("invalid project verification failure")
            for stream in ("stdout_metadata", "stderr_metadata"):
                metadata = diagnostic[stream]
                if (not isinstance(metadata, Mapping)
                    or set(metadata) != {"sanitized_bytes", "returned_bytes", "sha256", "truncated",
                                         "redacted", "redacted_lines"}
                    or any(type(metadata[key]) is not int or not 0 <= cast(int, metadata[key]) <= 2**63 - 1
                           for key in ("sanitized_bytes", "returned_bytes", "redacted_lines"))
                    or cast(int, metadata["returned_bytes"]) > cast(int, metadata["sanitized_bytes"])
                    or any(type(metadata[key]) is not bool for key in ("truncated", "redacted"))
                    or not isinstance(metadata["sha256"], str)
                    or _SHA.fullmatch(metadata["sha256"]) is None):
                    raise ValueError("invalid project verification failure")
            failed = status == "failed"
        if not failed:
            raise ValueError("invalid project verification failure")
        object.__setattr__(self, "report", report)

    def to_payload(self) -> Mapping[str, JsonValue]:
        return self.report


def validated_project_failure_payload(
    value: object, *, diagnostic: Mapping[str, JsonValue],
) -> Mapping[str, JsonValue] | None:
    """Validate persisted failure metadata and bind it to its process diagnostic."""
    if not isinstance(value, Mapping):
        return None
    try:
        report = ProjectVerificationFailure(value).to_payload()
        checks = cast(tuple[Mapping[str, JsonValue], ...], report["checks"])
        failed = next(check for check in checks if check["status"] == "failed")
        if failed["execution_diagnostic"] != {
            key: item for key, item in diagnostic.items() if key not in {"stdout", "stderr"}
        }:
            return None
        return report
    except (ValueError, TypeError, RecursionError):
        return None


class ProjectVerificationError(RuntimeError):
    def __init__(self, message: str, diagnostic: ExecutionDiagnostic | None = None, *,
                 project_failure: ProjectVerificationFailure | None = None,
                 project_prerequisite: ProjectPrerequisiteObservation | None = None,
                 python_project_failure: PythonProjectFailure | None = None) -> None:
        if python_project_failure is not None:
            from agent_hub.capabilities.python_project_reports import PythonProjectFailure
            if (type(python_project_failure) is not PythonProjectFailure
                    or project_failure is not None or project_prerequisite is not None
                    or diagnostic is not python_project_failure.diagnostic):
                raise TypeError('Python failure must remain separate from Node observations')
        if project_prerequisite is not None and (
            type(project_prerequisite) is not ProjectPrerequisiteObservation
            or diagnostic is not None or project_failure is not None
        ):
            raise TypeError("project prerequisite must be a separate typed blocked observation")
        super().__init__(message)
        self.diagnostic = diagnostic
        self.project_failure = project_failure
        self.project_prerequisite = project_prerequisite
        self.python_project_failure = python_project_failure


def _validate_original_diagnostic(value: object, *, retain_tail: bool = False) -> None:
    if (not isinstance(value, Mapping)
            or set(value) != {'schema_version', 'backend', 'exit_code', 'timed_out',
                              'stdout', 'stderr', 'stdout_metadata', 'stderr_metadata'}
            or type(value['schema_version']) is not int or value['schema_version'] != 1
            or value['backend'] != 'systemd' or type(value['timed_out']) is not bool
            or (value['exit_code'] is not None and type(value['exit_code']) is not int)
            or (value['exit_code'] is None and not value['timed_out'])):
        raise ValueError('invalid original process diagnostic')
    for stream in ('stdout', 'stderr'):
        text, metadata = value[stream], value[f'{stream}_metadata']
        if (not isinstance(text, str) or len(text.encode('utf-8')) > 4096
                or not isinstance(metadata, Mapping)
                or set(metadata) != {'sanitized_bytes', 'returned_bytes', 'sha256', 'truncated',
                                     'redacted', 'redacted_lines'}
                or any(type(metadata[key]) is not int or not 0 <= metadata[key] <= 2**63 - 1
                       for key in ('sanitized_bytes', 'returned_bytes', 'redacted_lines'))
                or metadata['returned_bytes'] != len(text.encode('utf-8'))
                or metadata['sanitized_bytes'] < metadata['returned_bytes']
                or any(type(metadata[key]) is not bool for key in ('truncated', 'redacted'))
                or metadata['redacted'] != (metadata['redacted_lines'] > 0)
                or not isinstance(metadata['sha256'], str)
                or _SHA.fullmatch(metadata['sha256']) is None):
            raise ValueError('invalid original process diagnostic')
        if metadata['truncated'] and (
            metadata['sanitized_bytes'] <= 4096
            or not (text.endswith('\n[truncated]')
                    or retain_tail and '\n[truncated]\n' in text)
            or metadata['returned_bytes'] < 4093
        ):
            raise ValueError('invalid original process diagnostic')
        if not metadata['truncated'] and (
            metadata['sanitized_bytes'] != metadata['returned_bytes']
            or metadata['sha256'] != hashlib.sha256(text.encode('utf-8')).hexdigest()
        ):
            raise ValueError('invalid original process diagnostic')


def validated_original_observation(
    value: object, *, expected_source: str, expected_contract: str,
    expected_python_inputs: PythonProjectInputs | None = None,
    expected_python_invocation: PythonProjectInvocation | None = None,
) -> Mapping[str, JsonValue]:
    """Decode trusted persisted metadata; never create an observation from model text."""
    if expected_python_inputs is not None or expected_python_invocation is not None:
        from agent_hub.capabilities.python_project_service import (
            validate_python_original_observation,
        )
        if expected_python_inputs is None or expected_python_invocation is None:
            raise ProjectVerificationError('original project observation is unavailable or changed')
        return validate_python_original_observation(value, inputs=expected_python_inputs,
            invocation=expected_python_invocation, expected_source=expected_source,
            expected_contract=expected_contract)
    try:
        item = _freeze_object(value, name='original project observation')
        failed = item.get('status') == 'failed'
        binding = item.get('original_check_contract')
        verification = item.get('verification')
        if (set(item) != {'schema_version', 'status', 'verification', 'original_check_contract'}
                | ({'execution_diagnostic'} if failed else set())
                or type(item.get('schema_version')) is not int or item['schema_version'] != 1
                or item.get('status') not in {'passed', 'failed'}
                or not isinstance(binding, Mapping)
                or type(binding.get('schema_version')) is not int
                or binding != {'schema_version': 1, 'contract_sha256': expected_contract,
                               'protected_sha256': expected_contract}
                or not isinstance(verification, Mapping)
                or verification.get('source_sha256') != expected_source
                or verification.get('status') != item['status']):
            raise ValueError
        if failed:
            diagnostic = item.get('execution_diagnostic')
            _validate_original_diagnostic(diagnostic)
            if (not isinstance(diagnostic, Mapping)
                    or validated_project_failure_payload(verification, diagnostic=diagnostic) is None):
                raise ValueError
        else:
            if (set(verification) != {'schema_version', 'status', 'source_sha256', 'checks', 'runtime'}
                    or type(verification.get('schema_version')) is not int or verification['schema_version'] != 1
                    or not isinstance(verification.get('checks'), tuple)
                    or len(cast(tuple[JsonValue, ...], verification['checks'])) != 3):
                raise ValueError
            # Re-use the native decoder to check completed phase structure. The original
            # metadata hash remains unchanged; bounded diagnostic text is not re-hashed.
            checks: list[dict[str, object]] = []
            for phase, check in zip(('install', 'build', 'test'),
                                    cast(tuple[JsonValue, ...], verification['checks']), strict=True):
                if (not isinstance(check, Mapping) or set(check) != {'phase', 'status', 'execution_diagnostic'}
                        or check.get('phase') != phase or check.get('status') != 'passed'
                        or not isinstance(check.get('execution_diagnostic'), Mapping)):
                    raise ValueError
                diagnostic = cast(Mapping[str, JsonValue], check['execution_diagnostic'])
                _validate_original_diagnostic(diagnostic)
                if (type(diagnostic.get('schema_version')) is not int or diagnostic['schema_version'] != 1
                        or diagnostic.get('backend') != 'systemd'
                        or type(diagnostic.get('exit_code')) is not int or diagnostic['exit_code'] != 0
                        or diagnostic.get('timed_out') is not False):
                    raise ValueError
                checks.append({'phase': phase, 'status': 'passed',
                    **{key: diagnostic.get(key) for key in ('exit_code', 'timed_out', 'stdout', 'stderr')}})
            _validate_report(json.dumps({'schema_version': 1, 'status': 'passed', 'source_sha256': expected_source,
                'runtime': verification.get('runtime'), 'checks': checks}, default=dict),
                expected_source=expected_source)
        return item
    except Exception:  # noqa: BLE001 - malformed persisted observation has no repair or replay authority.
        raise ProjectVerificationError('original project observation is unavailable or changed') from None


class ProjectExecutor(Protocol):
    async def run(self, invocation: ProjectInvocation) -> SkillResult: ...


@dataclass(frozen=True, slots=True)
class _OriginalScope:
    stored: StoredProjectBaseline
    observation_sha256: str | None

    def binding(self, source: str) -> Mapping[str, JsonValue]:
        if self.observation_sha256 is None:
            raise ProjectVerificationError('original project observation is unavailable or changed')
        return {'schema_version': 1, 'contract_sha256': self.stored.contract.protected_sha256,
                'protected_sha256': self.stored.contract.protected_sha256,
                'baseline_envelope_sha256': self.stored.reference.contract_sha256,
                'baseline_source_sha256': self.stored.reference.input_source_sha256,
                'baseline_observation_sha256': self.observation_sha256, 'candidate_source_sha256': source}


class ProjectVerificationService:
    def __init__(self, *, root: Path, repository: object, executor: ProjectExecutor | None,
                 python_executor: PythonProjectExecutor | None = None) -> None:
        from agent_hub.capabilities.python_project_service import PythonProjectService
        self.root = root
        self.repository = repository
        self.executor = executor
        self._receipts: dict[tuple[UUID, UUID], tuple[ProjectInvocation, str]] = {}
        self._original_receipts: dict[tuple[UUID, UUID], Mapping[str, JsonValue]] = {}
        self._python = PythonProjectService(self, python_executor)

    async def authorization_request(self, request: CapabilityRequest) -> CapabilityRequest | None:
        return await self._python.authorization_request(request)

    async def prepare_python_original(
        self, *, invocation: ProjectInvocation, reference: ProjectBaselineReference,
    ) -> PythonProjectInputs | None:
        return await self._python.prepare(invocation, reference)

    async def python_original_context(self, tenant_id: UUID, run_id: UUID) -> PythonOriginalContext | None:
        return await self._python.original_context(tenant_id, run_id)

    async def _original_scope(
        self, invocation: ProjectInvocation, *, reference: ProjectBaselineReference | None = None,
        original: bool = False,
    ) -> _OriginalScope | None:
        from agent_hub.capabilities.project_baselines import (
            ProjectBaselineReference,
            ProjectBaselineStore,
        )
        from agent_hub.capabilities.project_contracts import ManagedProjectInput

        try:
            getter = getattr(self.repository, 'get', None)
            if not callable(getter):
                raise TypeError
            record = await getter(invocation.tenant_id, invocation.run_id)
            project, session = authorized_workspace_scope(
                record, tenant_id=invocation.tenant_id, run_id=invocation.run_id,
            )
            route = record.routing_decision
            if ('project_input_contract_version' not in route and 'project_input' not in route
                    and reference is None):
                return None
            selection = ManagedProjectInput.from_payload(route.get('project_input'))
            state = route.get('project_input_preparation')
            if (type(route.get('project_input_contract_version')) is not int
                    or route['project_input_contract_version'] != 1
                    or (project, session) != (invocation.project_id, invocation.session_id)
                    or not isinstance(state, Mapping)
                    or type(state.get('schema_version')) is not int or state['schema_version'] != 1
                    or state.get('phase') != ('executing' if original else 'observed')):
                raise ValueError
            trusted_reference = ProjectBaselineReference.from_payload(state.get('reference'))
            if reference is not None and reference != trusted_reference:
                raise ValueError
            loaded = await asyncio.to_thread(ProjectBaselineStore(self.root).load, invocation, trusted_reference)
            if loaded.contract.editable_paths != selection.editable_paths:
                raise ValueError
            observation_sha: str | None = None
            if original:
                if (set(state) != {'schema_version', 'phase', 'reference', 'execution_id'}
                        or state['execution_id'] != invocation.execution_id):
                    raise ValueError
            else:
                observed = validated_original_observation(
                    state.get('observation'), expected_source=trusted_reference.input_source_sha256,
                    expected_contract=loaded.contract.protected_sha256,
                )
                observation_sha = hashlib.sha256(json.dumps(observed, default=dict, ensure_ascii=True,
                    sort_keys=True, separators=(',', ':')).encode('ascii')).hexdigest()
                if state.get('observation_sha256') != observation_sha:
                    raise ValueError
            return _OriginalScope(loaded, observation_sha)
        except Exception:  # noqa: BLE001 - no project data or alternate scope can replace the original.
            raise ProjectVerificationError('original project contract is unavailable or changed') from None

    async def _run_original_contract(
        self, invocation: ProjectInvocation, stored: StoredProjectBaseline, *, original: bool,
    ) -> Mapping[str, JsonValue]:
        from agent_hub.skills.sandbox.project import (
            ContractProjectInvocation,
            ProjectSystemdSandbox,
            project_source_fingerprint,
            project_source_manifest,
        )

        try:
            files = await project_source_manifest(self.root, invocation)
            stored.contract.require_current(files)
            before = await project_source_fingerprint(self.root, invocation)
            if original and before != stored.reference.input_source_sha256:
                raise ValueError
        except Exception:  # noqa: BLE001 - unreadable or unsafe source is never executed.
            raise ProjectVerificationError('original project checks or source changed before execution') from None
        scoped = ContractProjectInvocation.model_validate({
            **invocation.model_dump(), 'source_kind': 'original' if original else 'candidate',
            'original_reference': stored.reference.to_payload(),
        }, strict=True)
        try:
            result = await (self.executor or ProjectSystemdSandbox()).run(scoped)
            if result.exit_code != 0 or result.timed_out:
                raise ProjectVerificationError('project execution outcome is not confirmed')
            if await project_source_fingerprint(self.root, invocation) != before:
                raise ProjectVerificationError('project source changed during verification')
            # The native runner seals every candidate byte, not just conventional tests/**.
            report = json.loads(result.stdout, object_pairs_hook=_strict_object)
            binding: dict[str, JsonValue] = {
                'schema_version': 1, 'contract_sha256': stored.contract.protected_sha256,
                'protected_sha256': stored.contract.protected_sha256,
            }
            native_binding = report.get('original_check_contract') if isinstance(report, dict) else None
            if (not isinstance(native_binding, dict) or type(native_binding.get('schema_version')) is not int
                    or native_binding != binding):
                raise ProjectVerificationError('original project contract execution is not confirmed')
            try:
                receipt = _validate_report(result.stdout, expected_source=before)
            except ProjectVerificationError as error:
                if not original or error.project_failure is None or error.diagnostic is None:
                    raise
                return {'schema_version': 1, 'status': 'failed',
                        'verification': error.project_failure.to_payload(),
                        'execution_diagnostic': error.diagnostic.to_payload(),
                        'original_check_contract': binding}
            return {'schema_version': 1, 'status': 'passed', 'verification': receipt,
                    'original_check_contract': binding}
        except (ProjectVerificationError, ProcessCleanupIncomplete):
            raise
        except Exception:  # noqa: BLE001 - unexpected executor output cannot become a native observation.
            raise ProjectVerificationError('original project contract execution is not confirmed') from None

    async def observe_original(
        self, *, tenant_id: UUID, run_id: UUID, actor: str, reference: ProjectBaselineReference,
        execution_id: str, timeout_seconds: float,
    ) -> Mapping[str, JsonValue]:
        python = await self._python.select(tenant_id, run_id, actor, phases=('executing',))
        if python is not None:
            await self._python._approval(python, actor)
            return await self._python.observe_original(python, actor=actor, reference=reference,
                execution_id=execution_id, timeout_seconds=timeout_seconds)
        invocation = await self._invocation(tenant_id, run_id, actor)
        invocation = invocation.model_copy(update={
            'execution_id': execution_id,
            'timeout_seconds': min(invocation.timeout_seconds, max(1, math.ceil(timeout_seconds))),
        })
        stored = await self._original_scope(invocation, reference=reference, original=True)
        if stored is None:
            raise ProjectVerificationError('original project contract is unavailable or changed')
        return await self._run_original_contract(invocation, stored.stored, original=True)

    async def _invocation(
        self, tenant_id: UUID, run_id: UUID, actor: str,
    ) -> ProjectInvocation:
        from agent_hub.skills.sandbox.project import ProjectInvocation

        python = await self._python.select(tenant_id, run_id, actor,
                                          phases=('captured', 'executing', 'observed'))
        if python is not None:
            await self._python._approval(python, actor)
            return python.invocation
        try:
            getter = getattr(self.repository, "get", None)
            if not callable(getter):
                raise TypeError
            record = await getter(tenant_id, run_id)
            project, session = authorized_workspace_scope(record, tenant_id=tenant_id, run_id=run_id)
            routing = getattr(record, "routing_decision", None)
            if not isinstance(routing, Mapping):
                raise TypeError
            permissions = routing.get("requested_permissions", ())
            profile = routing.get("sandbox_profile")
            if (profile not in {"restricted", "workspace_write"}
                or not isinstance(permissions, (tuple, list)) or "command.run" not in permissions):
                raise ValueError
            if routing.get("execution_backend", "systemd") != "systemd":
                raise ProjectVerificationError("project execution backend is unsupported")
            install_allowed = "network.read" in permissions
            if not install_allowed:
                actor_id = getattr(record, "actor_id", None)
                checker = getattr(self.repository, "is_capability_approval_scope_approved", None)
                if isinstance(actor_id, UUID) and callable(checker):
                    scope = capability_approval_scope(CapabilityRequest(
                        tenant_id=tenant_id, user_id=actor_id, agent_id=actor,
                        capability="command", operation="run", resource=PROJECT_VERIFY_RESOURCE,
                        arguments={}, run_id=run_id, idempotency_key="project-verification-scope",
                    ))
                    approved = checker(tenant_id, run_id, scope)
                    install_allowed = bool(
                        await cast(Awaitable[object], approved) if isawaitable(approved) else approved
                    )
            if not install_allowed:
                raise ProjectVerificationError("project dependency installation approval required")
            raw_timeout = routing.get("runtime_timeout_seconds", 300)
            if (not isinstance(raw_timeout, (int, float)) or isinstance(raw_timeout, bool)
                or not math.isfinite(raw_timeout) or raw_timeout <= 0):
                raise ValueError
            timeout = min(3600, max(1, math.ceil(raw_timeout)))
            return ProjectInvocation(
                execution_id="project_" + uuid4().hex, tenant_id=tenant_id, run_id=run_id,
                project_id=project, session_id=session, sandbox_profile=profile,
                install_allowed=True, timeout_seconds=timeout,
            )
        except ProjectVerificationError:
            raise
        except Exception:  # noqa: BLE001 - no repository/path details cross this boundary.
            raise ProjectVerificationError("project verification scope is not authorized") from None

    async def verify(self, *, tenant_id: UUID, run_id: UUID, actor: str) -> Mapping[str, JsonValue]:
        from agent_hub.skills.sandbox.project import (
            ProjectSystemdSandbox,
            project_source_fingerprint,
        )

        key = (tenant_id, run_id)
        self._receipts.pop(key, None)
        self._original_receipts.pop(key, None)
        self._python.receipts.pop(key, None)
        python = await self._python.select(tenant_id, run_id, actor)
        if python is not None:
            return await self._python.verify(python, actor)
        invocation = await self._invocation(tenant_id, run_id, actor)
        stored = await self._original_scope(invocation)
        if stored is not None:
            contract_result = await self._run_original_contract(invocation, stored.stored, original=False)
            receipt = cast(Mapping[str, JsonValue], contract_result['verification'])
            source = cast(str, receipt['source_sha256'])
            binding = stored.binding(source)
            self._receipts[key] = (invocation, source)
            self._original_receipts[key] = binding
            return {'verification': receipt, 'original_check_contract': binding,
                    'summary': 'Original checks preserved; dependencies, build and tests verified in isolation.'}
        try:
            before = await project_source_fingerprint(self.root, invocation)
            try:
                result = await (self.executor or ProjectSystemdSandbox()).run(invocation)
            except Exception:  # noqa: BLE001 - executor exceptions are never trusted observations.
                # Only our decoded, source-checked report can create process evidence.
                raise ProjectVerificationError("project execution backend is unavailable") from None
            if result.exit_code != 0 or result.timed_out:
                raise ProjectVerificationError("project execution outcome is not confirmed")
            current = await project_source_fingerprint(self.root, invocation)
            if current != before:
                raise ProjectVerificationError("project source changed during verification")
            receipt = _validate_report(result.stdout, expected_source=before)
        except ProjectVerificationError:
            raise
        except Exception:  # noqa: BLE001 - backend failures are not trusted process evidence.
            raise ProjectVerificationError("project execution backend is unavailable") from None
        self._receipts[key] = (invocation, current)
        return {"verification": receipt, "summary": "Dependencies, build and tests verified in isolation."}

    async def require_current_receipt(self, tenant_id: UUID, run_id: UUID) -> str:
        from agent_hub.skills.sandbox.project import project_source_fingerprint

        if (tenant_id, run_id) in self._python.receipts:
            return await self._python.require_current(tenant_id, run_id)
        record = self._receipts.get((tenant_id, run_id))
        if record is not None:
            invocation, expected = record
            try:
                original = await self._original_scope(invocation)
                if original is not None:
                    from agent_hub.skills.sandbox.project import project_source_manifest

                    original.stored.contract.require_current(await project_source_manifest(self.root, invocation))
                    if self._original_receipts.get((tenant_id, run_id)) != original.binding(expected):
                        raise ValueError
                elif (tenant_id, run_id) in self._original_receipts:
                    raise ValueError
                if await project_source_fingerprint(self.root, invocation) == expected:
                    return expected
            except Exception:  # noqa: BLE001 - a stale/unreadable source cannot be delivered as verified.
                self._receipts.pop((tenant_id, run_id), None)
        raise ProjectVerificationError("project verification required before delivery")

    def original_receipt_binding(self, tenant_id: UUID, run_id: UUID) -> Mapping[str, JsonValue] | None:
        return self._original_receipts.get((tenant_id, run_id))


def _strict_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _validate_report(text: str, *, expected_source: str) -> Mapping[str, JsonValue]:
    try:
        if len(text.encode("utf-8")) > 262144:
            raise ValueError
        report: object = json.loads(text, object_pairs_hook=_strict_object)
        if (not isinstance(report, dict) or type(report.get("schema_version")) is not int
            or report.get("schema_version") != 1):
            raise ValueError
        if (report.get("source_sha256") != expected_source
            or _SHA.fullmatch(expected_source) is None):
            raise ValueError
        if report.get("status") == "blocked":
            prerequisite = validate_project_prerequisite_report(report, expected_source)
            raise ProjectVerificationError("project verification blocked by execution prerequisites",
                                           project_prerequisite=prerequisite)
        checks = report.get("checks")
        runtime = report.get("runtime")
        if not isinstance(checks, list) or len(checks) != 3 or not isinstance(runtime, dict):
            raise ValueError
        safe_checks: list[JsonValue] = []
        failure: tuple[str, ExecutionDiagnostic] | None = None
        for phase, check in zip(("install", "build", "test"), checks, strict=True):
            if not isinstance(check, dict) or check.get("phase") != phase:
                raise ValueError
            status = check.get("status")
            observed = SkillResult.model_validate({
                name: check[name] for name in ("exit_code", "timed_out", "stdout", "stderr")
            }, strict=True)
            if status not in {"passed", "failed", "not_run"}:
                raise ValueError
            if report.get("status") == "failed" and (
                (failure is None and status == "not_run")
                or (failure is not None and status != "not_run")
            ):
                raise ValueError
            if status == "not_run":
                if (observed.exit_code is not None or observed.timed_out
                    or observed.stdout or observed.stderr):
                    raise ValueError
                safe_checks.append({"phase": phase, "status": "not_run"})
                continue
            if status == "passed" and (observed.exit_code != 0 or observed.timed_out):
                raise ValueError
            diagnostic = ExecutionDiagnostic(backend="systemd", result=observed)
            safe_checks.append({"phase": phase, "status": status,
                                "execution_diagnostic": diagnostic.to_payload()})
            if status == "failed" and failure is None:
                if observed.exit_code in (0, None) and not observed.timed_out:
                    raise ValueError
                failure = (phase, diagnostic)
        if report.get("status") == "failed" and failure is not None:
            safe_failure_checks: list[JsonValue] = []
            for item in safe_checks:
                check = cast(Mapping[str, JsonValue], item)
                safe_check = dict(check)
                diagnostic_payload = check.get("execution_diagnostic")
                if isinstance(diagnostic_payload, Mapping):
                    safe_check["execution_diagnostic"] = {
                        key: value for key, value in diagnostic_payload.items()
                        if key not in {"stdout", "stderr"}
                    }
                safe_failure_checks.append(safe_check)
            failure_runtime: dict[str, JsonValue] = {}
            for name in ("node", "npm"):
                if name not in runtime:
                    continue
                value = runtime[name]
                if (not isinstance(value, str)
                    or re.fullmatch(r"v?[0-9][A-Za-z0-9.+-]{0,63}", value) is None):
                    raise ValueError
                failure_runtime[name] = value
            observation = ProjectVerificationFailure({
                "schema_version": 1, "status": "failed", "source_sha256": expected_source,
                "checks": tuple(safe_failure_checks), "runtime": failure_runtime,
            })
            raise ProjectVerificationError(f"project {failure[0]} failed", failure[1],
                                           project_failure=observation)
        if report.get("status") != "passed" or any(
            check["status"] != "passed" for check in checks
        ):
            raise ValueError
        safe_runtime: dict[str, JsonValue] = {}
        for name in ("node", "npm"):
            value = runtime.get(name)
            if not isinstance(value, str) or re.fullmatch(r"v?[0-9][A-Za-z0-9.+-]{0,63}", value) is None:
                raise ValueError
            safe_runtime[name] = value
        return {"schema_version": 1, "status": "passed", "source_sha256": expected_source,
                "checks": tuple(safe_checks), "runtime": safe_runtime}
    except ProjectVerificationError:
        raise
    except Exception:  # noqa: BLE001 - never accept malformed/untrusted success or emit its content.
        raise ProjectVerificationError("project execution report is invalid") from None
