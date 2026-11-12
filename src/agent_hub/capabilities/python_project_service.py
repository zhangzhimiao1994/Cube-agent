"""Python-only service wiring over immutable originals and the explicit v4 client.

Persisted observations are historical data. New delivery additionally requires
current source, exact approval and a live root cache inspection; no lease or
authority is inferred from native stdout or a matching stored receipt.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import re
import tomllib
from collections.abc import Awaitable, Mapping
from contextlib import ExitStack
from dataclasses import dataclass
from inspect import isawaitable
from pathlib import Path
from typing import TYPE_CHECKING, NoReturn, Protocol, cast, get_args
from uuid import UUID, uuid4

from agent_hub.auth.models import Role
from agent_hub.capabilities.approvals import capability_approval_scope
from agent_hub.capabilities.project_baselines import (
    ProjectBaselineReference,
    ProjectBaselineStore,
    StoredProjectBaseline,
)
from agent_hub.capabilities.project_contracts import ManagedProjectInput
from agent_hub.capabilities.project_verification import (
    ProjectVerificationError,
    _OriginalScope,
    _strict_object,
    _validate_original_diagnostic,
)
from agent_hub.capabilities.python_original_metadata import (
    StandardPythonIntent,
    StandardPythonIntentError,
    standard_python_intent,
)
from agent_hub.capabilities.python_project_input_store import PythonProjectInputStore
from agent_hub.capabilities.python_project_inputs import (
    PythonInterpreterIdentity,
    PythonProjectInputs,
)
from agent_hub.capabilities.python_project_receipts import validate_python_passed_receipt
from agent_hub.capabilities.python_project_recipe import (
    PythonProjectDeclaration,
    parse_python_project_declaration,
)
from agent_hub.capabilities.python_project_reports import (
    PythonEphemeralProjectReport,
    PythonProjectFailure,
    PythonProjectReport,
    decode_python_project_report,
    validate_python_failure,
)
from agent_hub.capabilities.python_project_wheels import PythonWheelBinding
from agent_hub.capabilities.scoped_read import _read_no_links, authorized_workspace_scope
from agent_hub.capabilities.types import CapabilityRequest
from agent_hub.runtime.contracts import JsonValue, _freeze_object
from agent_hub.skills.sandbox.project import (
    ProjectInvocation,
    project_source_fingerprint,
    project_source_manifest,
)
from agent_hub.skills.sandbox.python_project_client import PythonProjectSystemdSandbox
from agent_hub.skills.sandbox.python_project_protocol import (
    PythonEnvironmentReference,
    PythonEphemeralEnvironmentEvidence,
    PythonProjectBrokerError,
    PythonProjectBrokerResponse,
    PythonProjectInputsObservation,
    PythonProjectInvocation,
    PythonProjectProbeInvocation,
    _reference_matches,
    _scope_payload,
    base_python_invocation,
)
from agent_hub.subprocess_cleanup import ProcessCleanupIncomplete

if TYPE_CHECKING:
    from agent_hub.capabilities.project_verification import ProjectVerificationService
    from agent_hub.runs.original_python_offline_review import OriginalPythonOfflineReview
    from agent_hub.runs.python_recipe_review import PythonRecipeReview

_ERROR = "python_project_service_unavailable"
_METADATA_REQUIRED = "python_original_metadata_preparation_required"
_CONTROL_BYTES = 256 * 1024
_MAX_REQUIREMENTS = 128
_REFERENCE_PREFIX = "python-environment-"
_MAX_REFERENCES = 128


def _need(value: object, code: str = _ERROR) -> None:
    if not value:
        raise ProjectVerificationError(code)


def _encoded(value: object) -> bytes:
    return json.dumps(
        value,
        default=dict,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("ascii")


def python_recipe_marker(inputs: PythonProjectInputs) -> Mapping[str, JsonValue]:
    return {
        "schema_version": 1,
        "recipe_sha256": inputs.sha256,
        "environment_input_sha256": inputs.environment_input_sha256,
    }


def python_original_invocation(
    invocation: ProjectInvocation,
    inputs: PythonProjectInputs,
) -> PythonProjectInvocation:
    """Build expected protocol data; this function grants no installation right."""
    return PythonProjectInvocation.model_validate(
        {
            **invocation.model_dump(),
            "source_kind": "original",
            "original_reference": inputs.original_reference.to_payload(),
            "recipe_sha256": inputs.sha256,
            "environment_input_sha256": inputs.environment_input_sha256,
        },
        strict=True,
    )


def _declaration(stored: StoredProjectBaseline) -> Mapping[str, object] | StandardPythonIntent | None:
    if "pyproject.toml" not in stored.source_files:
        return None
    manifest = _read_no_links(stored.snapshot, ("pyproject.toml",), max_bytes=_CONTROL_BYTES)
    document = tomllib.loads(manifest.decode("utf-8-sig"))
    tool = document.get("tool", {})
    agent = tool.get("agent-hub", {}) if isinstance(tool, dict) else {}
    if not isinstance(agent, dict) or "python-project" not in agent:
        project = document.get("project")
        build_system = document.get("build-system")
        name = project.get("name") if isinstance(project, dict) else None
        backend = build_system.get("build-backend") if isinstance(build_system, dict) else None
        if isinstance(name, str) and name.strip() and (
            isinstance(backend, str) and backend.strip()
            or any(path.endswith(".py") for path in stored.source_files)
        ):
            _need(stored.generated_metadata is None, _METADATA_REQUIRED)
            try:
                intent = standard_python_intent(manifest, paths=stored.source_files.keys(),
                    protected=stored.contract.protected_files.keys())
            except StandardPythonIntentError:
                raise ProjectVerificationError(_METADATA_REQUIRED) from None
            _need(intent is not None, _METADATA_REQUIRED)
            return intent
        return None
    lock = _read_no_links(stored.snapshot, ("requirements.lock",), max_bytes=_CONTROL_BYTES)
    return parse_python_project_declaration(
        manifest, lock, max_control_bytes=_CONTROL_BYTES, max_requirements=_MAX_REQUIREMENTS
    ).to_payload()


def load_python_original_inputs(
    root: Path,
    invocation: ProjectInvocation,
    reference: ProjectBaselineReference,
    marker: object,
) -> PythonProjectInputs:
    _need(
        isinstance(marker, Mapping)
        and set(marker) == {"schema_version", "recipe_sha256", "environment_input_sha256"}
    )
    assert isinstance(marker, Mapping)
    _need(type(marker["schema_version"]) is int and marker["schema_version"] == 1)
    inputs = PythonProjectInputStore(root).load(
        invocation, reference, expected_sha256=cast(str, marker["recipe_sha256"])
    )
    _need(marker == python_recipe_marker(inputs))
    return inputs


def validate_python_original_observation(
    value: object,
    *,
    inputs: PythonProjectInputs,
    invocation: PythonProjectInvocation,
    expected_source: str,
    expected_contract: str,
) -> Mapping[str, JsonValue]:
    """Validate safe persisted Python data selected by the stored recipe, never raw.kind."""
    try:
        _need(type(inputs) is PythonProjectInputs and type(invocation) is PythonProjectInvocation)
        inputs = PythonProjectInputs.from_payload(inputs.to_payload())
        invocation = PythonProjectInvocation.model_validate(invocation.model_dump(), strict=True)
        _need(
            (invocation.tenant_id, invocation.run_id, invocation.project_id, invocation.session_id)
            == (
                inputs.contract.tenant_id,
                inputs.contract.run_id,
                inputs.contract.project_id,
                inputs.contract.session_id,
            )
            and invocation.original_reference.model_dump() == inputs.original_reference.to_payload()
            and invocation.recipe_sha256 == inputs.sha256
            and invocation.environment_input_sha256 == inputs.environment_input_sha256
        )
        item = _freeze_object(value, name="Python original observation")
        failed = item.get("status") == "failed"
        ephemeral = item.get("schema_version") == 3
        _need(
            set(item)
            == {
                "schema_version",
                "status",
                "verification",
                "original_check_contract",
                "python_recipe",
                "environment_reference",
                "execution_id",
            }
            | ({"execution_diagnostic"} if failed else set())
            | ({"ephemeral_environment"} if ephemeral else set())
        )
        _need(type(item["schema_version"]) is int and item["schema_version"] == (3 if ephemeral else 2))
        _need(
            item["status"] in ("passed", "failed")
            and item["execution_id"] == invocation.execution_id
        )
        _need(
            expected_source == inputs.original_reference.input_source_sha256
            and expected_contract == inputs.contract.protected_sha256
            and item["python_recipe"] == python_recipe_marker(inputs)
            and invocation.source_kind == "original"
        )
        for name in ("python_recipe", "original_check_contract"):
            nested = item[name]
            _need(isinstance(nested, Mapping))
            assert isinstance(nested, Mapping)
            _need(type(nested.get("schema_version")) is int and nested["schema_version"] == 1)
        binding: dict[str, JsonValue] = {
            "schema_version": 1,
            "contract_sha256": expected_contract,
            "protected_sha256": expected_contract,
        }
        _need(item["original_check_contract"] == binding)
        payload = item["environment_reference"]
        reference = (
            None
            if payload is None
            else PythonEnvironmentReference.model_validate_json(_encoded(payload), strict=True)
        )
        if reference is not None:
            _need(_reference_matches(invocation, reference))
        evidence: PythonEphemeralEnvironmentEvidence | None = None
        if ephemeral:
            _need(reference is None)
            evidence = PythonEphemeralEnvironmentEvidence.model_validate_json(
                _encoded(item["ephemeral_environment"]), strict=True,
            )
            evidence.require_for(invocation)
            _need(evidence.interpreter_sha256 == inputs.interpreter.binary_sha256)
        if failed:
            failure = validate_python_failure(
                item["verification"],
                invocation=invocation,
                inputs=inputs,
                expected_source_sha256=expected_source,
                environment_reference=reference,
                ephemeral_environment=evidence,
            )
            diagnostic = item["execution_diagnostic"]
            _validate_original_diagnostic(diagnostic, retain_tail=True)
            assert isinstance(diagnostic, Mapping)
            checks = cast(tuple[Mapping[str, JsonValue], ...], failure.report["checks"])
            _need(
                checks[-1]["execution_diagnostic"]
                == {
                    key: entry
                    for key, entry in diagnostic.items()
                    if key not in {"stdout", "stderr"}
                }
            )
        elif evidence is not None:
            verification = item["verification"]
            _need(isinstance(verification, Mapping))
            assert isinstance(verification, Mapping)
            PythonEphemeralProjectReport(verification, evidence, expected_source)
        else:
            _need(reference is not None)
            assert reference is not None
            receipt = validate_python_passed_receipt(
                item["verification"],
                expected_source=expected_source,
                expected_recipe=inputs.sha256,
                expected_environment_inputs=inputs.environment_input_sha256,
                expected_environment=reference.environment_sha256,
            )
            _need(
                receipt["execution_id"] == invocation.execution_id
                and receipt["runtime"] == inputs.interpreter.to_payload()
            )
            PythonProjectReport(receipt, reference, expected_source)
        return item
    except Exception:  # noqa: BLE001 - persisted data cannot manufacture process evidence or replay.
        raise ProjectVerificationError(
            "original project observation is unavailable or changed"
        ) from None


def _validate_expired_original_observation(
    state: Mapping[str, JsonValue], invocation: PythonProjectInvocation,
) -> None:
    """Validate historical selection metadata, never a live recipe or grant."""
    _need(set(state) == {'schema_version', 'phase', 'reference', 'python_recipe',
                        'execution_id', 'observation', 'observation_sha256'}
          and type(state['schema_version']) is int and state['schema_version'] == 2
          and state['phase'] == 'observed'
          and state['reference'] == invocation.original_reference.model_dump(mode='json'))
    ProjectBaselineReference.from_payload(state['reference'])
    marker = {'schema_version': 1, 'recipe_sha256': invocation.recipe_sha256,
              'environment_input_sha256': invocation.environment_input_sha256}
    stored_marker = state['python_recipe']
    _need(isinstance(stored_marker, Mapping) and type(stored_marker.get('schema_version')) is int
          and stored_marker == marker)
    observation = _freeze_object(state['observation'], name='expired original observation')
    _need(type(state['observation_sha256']) is str
          and hashlib.sha256(_encoded(observation)).hexdigest() == state['observation_sha256'])
    failed = observation.get('status') == 'failed'
    ephemeral = observation.get('schema_version') == 3
    _need(set(observation) == {'schema_version', 'status', 'verification', 'original_check_contract',
                              'python_recipe', 'environment_reference', 'execution_id'}
          | ({'execution_diagnostic'} if failed else set())
          | ({'ephemeral_environment'} if ephemeral else set())
          and type(observation['schema_version']) is int and observation['schema_version'] == (3 if ephemeral else 2)
          and observation['status'] in {'passed', 'failed'}
          and observation['execution_id'] == state['execution_id']
          and observation['python_recipe'] == marker)
    observed_marker = observation['python_recipe']
    _need(isinstance(observed_marker, Mapping) and type(observed_marker.get('schema_version')) is int)
    binding = observation['original_check_contract']
    _need(isinstance(binding, Mapping))
    assert isinstance(binding, Mapping)
    protected = binding.get('protected_sha256')
    _need(type(protected) is str and re.fullmatch('[a-f0-9]{64}', protected) is not None
          and type(binding.get('schema_version')) is int
          and binding == {'schema_version': 1, 'contract_sha256': protected, 'protected_sha256': protected})
    # This validates history only. Root independently selects any actual READY;
    # a removed original environment never becomes a cache reference.
    reference: PythonEnvironmentReference | None = None
    evidence: PythonEphemeralEnvironmentEvidence | None = None
    if ephemeral:
        _need(observation['environment_reference'] is None)
        evidence = PythonEphemeralEnvironmentEvidence.model_validate_json(
            _encoded(observation['ephemeral_environment']), strict=True)
        _need(evidence.scope.model_dump() == _scope_payload(invocation)
              and evidence.recipe_sha256 == invocation.recipe_sha256
              and evidence.environment_input_sha256 == invocation.environment_input_sha256
              and evidence.execution_id == state['execution_id'])
    else:
        reference = PythonEnvironmentReference.model_validate_json(
            _encoded(observation['environment_reference']), strict=True)
        _need(_reference_matches(invocation, reference))
    if failed:
        report = PythonProjectFailure(cast(Mapping[str, JsonValue], observation['verification']), reference,
                                      ephemeral_environment=evidence).to_payload()
        _need(report['original_check_contract'] == protected)
        diagnostic = observation['execution_diagnostic']
        _validate_original_diagnostic(diagnostic, retain_tail=True)
        assert isinstance(diagnostic, Mapping)
        checks = cast(tuple[Mapping[str, JsonValue], ...], report['checks'])
        _need(checks[-1]['execution_diagnostic'] == {
            key: value for key, value in diagnostic.items() if key not in {'stdout', 'stderr'}})
    elif evidence is not None:
        report = PythonEphemeralProjectReport(cast(Mapping[str, JsonValue], observation['verification']),
            evidence, invocation.original_reference.input_source_sha256).to_payload()
    else:
        assert reference is not None
        report = PythonProjectReport(cast(Mapping[str, JsonValue], observation['verification']),
            reference, invocation.original_reference.input_source_sha256).to_payload()
    _need(report['source_sha256'] == invocation.original_reference.input_source_sha256
          and report['execution_id'] == state['execution_id']
          and report['recipe_sha256'] == invocation.recipe_sha256
          and report['environment_input_sha256'] == invocation.environment_input_sha256)


class PythonProjectExecutor(Protocol):
    async def probe(
        self, invocation: PythonProjectProbeInvocation
    ) -> PythonProjectInputsObservation: ...
    async def run(self, invocation: PythonProjectInvocation) -> PythonProjectBrokerResponse: ...
    async def inspect_environment(
        self,
        invocation: PythonProjectInvocation,
        *,
        expected_environment: PythonEnvironmentReference,
    ) -> PythonEnvironmentReference: ...


@dataclass(frozen=True)
class PythonOriginalContext:
    """Trusted history and durable root references, not a current delivery grant."""

    inputs: PythonProjectInputs
    invocation: PythonProjectInvocation
    stored: StoredProjectBaseline
    observation: Mapping[str, JsonValue]
    environment_reference: PythonEnvironmentReference | None
    environment_references: tuple[PythonEnvironmentReference, ...]


@dataclass(frozen=True)
class _Context:
    invocation: ProjectInvocation
    stored: StoredProjectBaseline
    inputs: PythonProjectInputs
    state: Mapping[str, JsonValue]
    user_id: UUID
    authority: tuple[object, ...]
    observation_sha256: str | None


@dataclass(frozen=True)
class _Receipt:
    context: _Context
    invocation: PythonProjectInvocation
    source: str
    reference: PythonEnvironmentReference | PythonEphemeralEnvironmentEvidence
    actor: str
    product: Mapping[str, JsonValue] | None = None


class PythonProjectService:
    def __init__(
        self, owner: ProjectVerificationService, executor: PythonProjectExecutor | None
    ) -> None:
        self.owner = owner
        self.executor = executor or PythonProjectSystemdSandbox()
        self.receipts: dict[tuple[UUID, UUID], _Receipt] = {}
        self.environments: dict[tuple[UUID, UUID], PythonEnvironmentReference] = {}
        self.recipe_review: PythonRecipeReview | None = None

    async def _record(self, tenant_id: UUID, run_id: UUID) -> object:
        getter = getattr(self.owner.repository, "get", None)
        if not callable(getter):
            raise ProjectVerificationError(_ERROR)
        return await cast(Awaitable[object], getter(tenant_id, run_id))

    def _base(
        self,
        record: object,
        tenant_id: UUID,
        run_id: UUID,
        *,
        readonly: bool = False,
    ) -> tuple[ProjectInvocation, tuple[object, ...], UUID]:
        project, session = authorized_workspace_scope(record, tenant_id=tenant_id, run_id=run_id)
        route = getattr(record, "routing_decision", None)
        _need(isinstance(route, Mapping))
        assert isinstance(route, Mapping)
        permissions = route.get("requested_permissions")
        user_id, role = getattr(record, "actor_id", None), getattr(record, "actor_role", None)
        _need(
            type(user_id) is UUID
            and isinstance(role, Role)
            and (readonly or role is not Role.VIEWER)
        )
        assert isinstance(user_id, UUID)
        required = (
            {"workspace.read"} if readonly else {"workspace.read", "workspace.write", "command.run"}
        )
        _need(isinstance(permissions, (tuple, list)) and required <= set(permissions))
        assert isinstance(permissions, (tuple, list))
        _need(
            readonly
            or (
                route.get("sandbox_profile") == "workspace_write"
                and route.get("execution_backend", "systemd") == "systemd"
            )
        )
        timeout = route.get("runtime_timeout_seconds", 300)
        _need(
            type(timeout) in (int, float)
            and math.isfinite(cast(float, timeout))
            and cast(float, timeout) > 0
        )
        invocation = ProjectInvocation(
            execution_id="python_" + uuid4().hex,
            tenant_id=tenant_id,
            run_id=run_id,
            project_id=project,
            session_id=session,
            sandbox_profile="workspace_write",
            install_allowed=False,
            timeout_seconds=min(3600, max(1, math.ceil(cast(float, timeout)))),
        )
        return invocation, (user_id, role, tuple(sorted(permissions))), user_id

    async def prepare(
        self, invocation: ProjectInvocation, reference: ProjectBaselineReference
    ) -> PythonProjectInputs | None:
        try:
            stored = await asyncio.to_thread(
                ProjectBaselineStore(self.owner.root).load, invocation, reference
            )
            declaration = await asyncio.to_thread(_declaration, stored)
            if declaration is None:
                return None
            probe = PythonProjectProbeInvocation.model_validate(
                {
                    **invocation.model_dump(),
                    "install_allowed": False,
                    "original_reference": reference.to_payload(),
                },
                strict=True,
            )
            measured = await self.executor.probe(probe)
            _need(type(measured) is PythonProjectInputsObservation)
            measured = PythonProjectInputsObservation.model_validate(
                measured.model_dump(), strict=True
            )
            _need(measured.scope.model_dump() == _scope_payload(probe))
            private_declaration = None
            if isinstance(declaration, StandardPythonIntent):
                private_declaration = PythonProjectDeclaration.from_payload(measured.declaration)
            else:
                _need(measured.declaration == declaration)
            interpreter = PythonInterpreterIdentity.from_payload(measured.interpreter)
            wheels = tuple(
                PythonWheelBinding(**cast(dict[str, str], wheel)) for wheel in measured.wheels
            )
            return await asyncio.to_thread(
                PythonProjectInputStore(self.owner.root).capture_or_load_matching,
                invocation,
                reference,
                interpreter=interpreter,
                wheels=wheels,
                max_control_bytes=_CONTROL_BYTES,
                max_requirements=_MAX_REQUIREMENTS,
                private_declaration=private_declaration,
            )
        except ProcessCleanupIncomplete:
            raise
        except Exception as error:  # noqa: BLE001 - root/runtime errors are finite, never host path text.
            self._raise_backend(error)

    @staticmethod
    def _raise_backend(error: Exception) -> NoReturn:
        code = str(error)
        allowed = set(get_args(PythonProjectBrokerError)) | {
            _METADATA_REQUIRED,
            "python_project_systemd_linux_unsupported",
            "python_project_recipe_invalid",
            "python_project_recipe_unsupported",
            "python_project_lock_invalid",
            "python_project_declaration_limit",
            "python_project_response_invalid",
            "python_project_request_invalid",
            "python_project_install_approval_required",
        }
        raise ProjectVerificationError(code if code in allowed else _ERROR) from None

    async def select(
        self,
        tenant_id: UUID,
        run_id: UUID,
        actor: str,
        *,
        phases: tuple[str, ...] = ("observed",),
        approve: bool = False,
        readonly: bool = False,
    ) -> _Context | None:
        try:
            record = await self._record(tenant_id, run_id)
            route = getattr(record, "routing_decision", None)
            _need(isinstance(route, Mapping))
            assert isinstance(route, Mapping)
            state = route.get("project_input_preparation")
            if not isinstance(state, Mapping):
                _need(
                    not isinstance(state, (tuple, list))
                    and "project_input" not in route
                    and "project_input_contract_version" not in route,
                    "original project contract is unavailable or changed",
                )
                return None
            is_python = type(state.get("schema_version")) is int and state["schema_version"] == 2
            reference = ProjectBaselineReference.from_payload(state.get("reference"))
            # Read only the immutable baseline to distinguish Node from both Python paths.
            project, session = authorized_workspace_scope(
                record, tenant_id=tenant_id, run_id=run_id
            )
            base = ProjectInvocation(
                execution_id="python_select",
                tenant_id=tenant_id,
                run_id=run_id,
                project_id=project,
                session_id=session,
                sandbox_profile="workspace_write",
                timeout_seconds=300,
            )
            stored = await asyncio.to_thread(
                ProjectBaselineStore(self.owner.root).load, base, reference
            )
            declared = await asyncio.to_thread(_declaration, stored)
            if declared is None:
                _need(not is_python, "python_project_recipe_invalid")
                return None
            _need(is_python, _METADATA_REQUIRED if isinstance(declared, StandardPythonIntent)
                  else "python_project_recipe_unavailable")
            _need(
                type(route.get("project_input_contract_version")) is int
                and route["project_input_contract_version"] == 1
            )
            selection = ManagedProjectInput.from_payload(route.get("project_input"))
            _need(selection.editable_paths == stored.contract.editable_paths)
            base, authority, user_id = self._base(record, tenant_id, run_id, readonly=readonly)
            _need(state.get("phase") in phases)
            phase = state["phase"]
            expected = {"schema_version", "phase", "reference", "python_recipe"}
            if phase in ("executing", "observed"):
                expected.add("execution_id")
            if phase == "observed":
                expected |= {"observation", "observation_sha256"}
            _need(set(state) == expected)
            state = _freeze_object(state, name="Python preparation")
            inputs = await asyncio.to_thread(
                load_python_original_inputs,
                self.owner.root,
                base,
                reference,
                state["python_recipe"],
            )
            observation_sha = None
            if phase in ("executing", "observed"):
                original = python_original_invocation(
                    base.model_copy(
                        update={"execution_id": state["execution_id"], "install_allowed": True}
                    ),
                    inputs,
                )
                if phase == "observed":
                    observation = validate_python_original_observation(
                        state["observation"],
                        inputs=inputs,
                        invocation=original,
                        expected_source=reference.input_source_sha256,
                        expected_contract=stored.contract.protected_sha256,
                    )
                    observation_sha = hashlib.sha256(_encoded(observation)).hexdigest()
                    _need(state["observation_sha256"] == observation_sha)
            context = _Context(base, stored, inputs, state, user_id, authority, observation_sha)
            if approve:
                await self._approval(context, actor)
            return context
        except ProjectVerificationError:
            raise
        except Exception:  # noqa: BLE001 - malformed/missing originals never select an alternate backend.
            raise ProjectVerificationError(
                "original project contract is unavailable or changed"
            ) from None

    async def _approval(self, context: _Context, actor: str) -> None:
        request = context.inputs.approval_request(
            user_id=context.user_id,
            agent_id=actor,
            idempotency_key="python-project-verification-scope",
        )
        if self.recipe_review is not None:
            from agent_hub.runs.python_recipe_review import PythonRecipeReview

            if (type(self.recipe_review) is PythonRecipeReview
                    and await self.recipe_review.review(request, context.authority[1])):
                return
        checker = getattr(self.owner.repository, "is_capability_approval_scope_approved", None)
        if not callable(checker):
            raise ProjectVerificationError("python_project_install_approval_required")
        scope = capability_approval_scope(request)
        _need(type(scope) is str, "python_project_install_approval_required")
        result = checker(request.tenant_id, request.run_id, scope)
        approved = await cast(Awaitable[object], result) if isawaitable(result) else result
        _need(approved is True, "python_project_install_approval_required")

    async def authorization_request(self, request: CapabilityRequest) -> CapabilityRequest | None:
        try:
            _need(type(request) is CapabilityRequest)
            request = CapabilityRequest.model_validate(
                request.model_dump(warnings="error"), strict=True
            )
        except (ValueError, TypeError, RecursionError):
            raise ProjectVerificationError(_ERROR) from None
        context = await self.select(
            request.tenant_id,
            request.run_id,
            request.agent_id,
            phases=("captured", "executing", "observed"),
        )
        if context is None:
            return None
        _need(request.user_id == context.user_id)
        if context.inputs.matches_approval_request(request):
            return request
        _need(
            request.capability == "command"
            and request.operation == "run"
            and request.resource == "workspace/current/project-verification"
            and request.arguments == {}
        )
        return context.inputs.approval_request(
            user_id=request.user_id,
            agent_id=request.agent_id,
            idempotency_key=request.idempotency_key,
        )

    async def _run(
        self,
        context: _Context,
        actor: str,
        *,
        original: bool,
        execution_id: str | None = None,
        timeout_seconds: float | None = None,
        offline_review: OriginalPythonOfflineReview | None = None,
    ) -> tuple[PythonProjectReport | PythonEphemeralProjectReport | PythonProjectFailure, PythonProjectInvocation, str,
               Mapping[str, JsonValue] | None]:
        base = context.invocation
        if execution_id is not None:
            base = base.model_copy(update={"execution_id": execution_id})
        if timeout_seconds is not None:
            _need(math.isfinite(timeout_seconds) and timeout_seconds > 0)
            base = base.model_copy(
                update={
                    "timeout_seconds": min(base.timeout_seconds, max(1, math.ceil(timeout_seconds)))
                }
            )
        before = await project_source_fingerprint(self.owner.root, base)
        context.stored.contract.require_current(
            await project_source_manifest(self.owner.root, base)
        )
        _need(
            not original or before == context.stored.reference.input_source_sha256,
            "python_project_source_changed",
        )
        invocation = python_original_invocation(
            base.model_copy(update={"install_allowed": True}), context.inputs
        )
        invocation = invocation.model_copy(
            update={"source_kind": "original" if original else "candidate"}
        )
        if offline_review is not None:
            from agent_hub.runs.original_python_offline_review import OriginalPythonOfflineReview

            _need(original and type(offline_review) is OriginalPythonOfflineReview,
                  "python_project_install_approval_required")
        if not original:
            await self.owner._begin_verification(tenant_id=invocation.tenant_id, run_id=invocation.run_id,
                actor=actor, execution_id=invocation.execution_id, source_sha256=before)
        from agent_hub.capabilities.python_product_delivery import (
            consume_response,
            discard_product,
            persist_product,
        )

        async def verify_offline(*, consume: bool = False) -> None:
            if offline_review is not None:
                await offline_review.verify(self.owner.repository, context.inputs, actor=actor,
                                            execution_id=invocation.execution_id, consume=consume)

        await verify_offline(consume=True)
        response = await self.executor.run(invocation)
        product = None
        try:
            with consume_response(response, invocation, before) as (response, stream):
                _need(response.ok and response.result is not None)
                _need(
                    await project_source_fingerprint(self.owner.root, base) == before,
                    "python_project_source_changed",
                )
                context.stored.contract.require_current(
                    await project_source_manifest(self.owner.root, base)
                )
                current = await self.select(
                    base.tenant_id,
                    base.run_id,
                    actor,
                    phases=("executing",) if original else ("observed",),
                    approve=offline_review is None,
                )
                _need(
                    current is not None
                    and current.inputs == context.inputs
                    and current.state == context.state
                    and current.authority == context.authority,
                    "python_project_scope_changed",
                )
                await verify_offline()
                assert response.result is not None
                report = decode_python_project_report(
                    invocation=invocation,
                    inputs=context.inputs,
                    expected_source_sha256=before,
                    environment_reference=response.environment_reference,
                    ephemeral_environment=response.ephemeral_environment,
                    result=response.result,
                )
                key = (base.tenant_id, base.run_id)
                if report.environment_reference is not None:
                    await asyncio.to_thread(self._persist_reference, context, report.environment_reference)
                    _need(
                        await project_source_fingerprint(self.owner.root, base) == before,
                        "python_project_source_changed",
                    )
                    self.environments[key] = report.environment_reference
                final = await self.select(
                    base.tenant_id,
                    base.run_id,
                    actor,
                    phases=("executing",) if original else ("observed",),
                    approve=offline_review is None,
                )
                _need(
                    final is not None
                    and final.state == context.state
                    and final.inputs == context.inputs
                    and final.authority == context.authority,
                    "python_project_scope_changed",
                )
                await verify_offline()
                product = None
                if not original and isinstance(report, (PythonProjectReport, PythonEphemeralProjectReport)):
                    product = await persist_product(self.owner.generated_file_store, response, stream,
                        invocation, before, report.to_payload())
                    try:
                        current = await self.select(base.tenant_id, base.run_id, actor, approve=True)
                        _need(current is not None and current.state == context.state
                              and current.inputs == context.inputs and current.authority == context.authority,
                              "python_project_scope_changed")
                        _need(await project_source_fingerprint(self.owner.root, base) == before,
                              "python_project_source_changed")
                        context.stored.contract.require_current(await project_source_manifest(self.owner.root, base))
                    except BaseException:
                        discard_product(self.owner.generated_file_store, product)
                        raise
                outcome = (report, invocation, before, product)
            return outcome
        except BaseException:
            discard_product(self.owner.generated_file_store, product)
            raise

    def _persist_reference(self, context: _Context, reference: PythonEnvironmentReference) -> None:
        """Service-UID once records outside the original v1 envelope; never overwrite."""
        store = ProjectBaselineStore(self.owner.root)
        name = _REFERENCE_PREFIX + reference.origin_execution_id + ".json"
        data = _encoded(reference.model_dump(mode="json"))
        with ExitStack() as stack:
            controls = store._controls(context.invocation, stack, create=False)
            existing = self._read_references(context)
            _need(reference in existing or len(existing) < _MAX_REFERENCES)
            try:
                store._write_once(controls, name, data)
            except FileExistsError:
                _need(controls.read(name, _CONTROL_BYTES) == data)
            controls.check()

    def _read_references(self, context: _Context) -> tuple[PythonEnvironmentReference, ...]:
        store = ProjectBaselineStore(self.owner.root)
        expected = python_original_invocation(context.invocation, context.inputs)
        result: list[PythonEnvironmentReference] = []
        with ExitStack() as stack:
            controls = store._controls(context.invocation, stack, create=False)
            with os.scandir(controls.path) as members:
                names = sorted(
                    member.name for member in members if member.name.startswith(_REFERENCE_PREFIX)
                )
            _need(len(names) <= _MAX_REFERENCES)
            for name in names:
                raw = controls.read(name, _CONTROL_BYTES)
                payload = json.loads(raw, object_pairs_hook=_strict_object)
                reference = PythonEnvironmentReference.model_validate_json(
                    _encoded(payload), strict=True
                )
                _need(
                    _reference_matches(expected, reference)
                    and name == _REFERENCE_PREFIX + reference.origin_execution_id + ".json"
                    and raw == _encoded(reference.model_dump(mode="json"))
                )
                result.append(reference)
            controls.check()
        return tuple(result)

    async def expired_environment_invocation(
        self, tenant_id: UUID, run_id: UUID,
    ) -> PythonProjectInvocation | None:
        """Internal metadata-only cache selector, never an execution context.

        Expired dependency cleanup does not require mutable projects/baselines to
        remain present. Lifecycle owns locked terminal/history eligibility; Root
        independently obtains and verifies its native reference, TTL and fences.
        This deliberately does not call normal generated.load/original.select.
        """
        try:
            record = await self._record(tenant_id, run_id)
            route = getattr(record, 'routing_decision', None)
            _need(isinstance(route, Mapping))
            assert isinstance(route, Mapping)
            if {'generated_project_contract_version', 'generated_project_preparation'} & route.keys():
                from agent_hub.capabilities.generated_project_service import (
                    generated_authority,
                    validate_generated_preparation,
                )

                _need(not {'project_input', 'project_input_contract_version', 'project_input_preparation'} & route.keys())
                _need(type(route.get('generated_project_contract_version')) is int
                      and route['generated_project_contract_version'] == 1)
                authority = generated_authority(record, tenant_id, run_id, readonly=True)
                state = validate_generated_preparation(route.get('generated_project_preparation'))
                _need(state['stack'] == 'python' and state['phase'] == 'python_ready'
                      and state['user_id'] == str(authority.user_id)
                      and state['request_sha256'] == authority.request_sha256)
                base, source_kind = authority.invocation, 'candidate'
            else:
                value = route.get('project_input_preparation')
                if value is None:
                    return None
                _need(isinstance(value, Mapping))
                assert isinstance(value, Mapping)
                observed = value.get('phase') == 'observed'
                _need(set(value) == {'schema_version', 'phase', 'reference', 'python_recipe', 'execution_id'}
                      | ({'observation', 'observation_sha256'} if observed else set()))
                _need(type(value['schema_version']) is int and value['schema_version'] == 2
                      and value['phase'] in {'executing', 'observed'}
                      and type(value['execution_id']) is str
                      and re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,127}', value['execution_id']) is not None
                      and type(route.get('project_input_contract_version')) is int
                      and route['project_input_contract_version'] == 1)
                ManagedProjectInput.from_payload(route.get('project_input'))
                state = value
                base, _, _ = self._base(record, tenant_id, run_id, readonly=True)
                source_kind = 'original'
            reference = ProjectBaselineReference.from_payload(state['reference'])
            marker = state['python_recipe']
            _need(isinstance(marker, Mapping) and set(marker) == {
                'schema_version', 'recipe_sha256', 'environment_input_sha256'})
            assert isinstance(marker, Mapping)
            _need(type(marker['schema_version']) is int and marker['schema_version'] == 1)
            result = PythonProjectInvocation.model_validate(base.model_dump() | {
                'execution_id': 'python_release_expired_' + uuid4().hex,
                'install_allowed': False, 'timeout_seconds': 30, 'source_kind': source_kind,
                'original_reference': reference.to_payload(), 'recipe_sha256': marker['recipe_sha256'],
                'environment_input_sha256': marker['environment_input_sha256'],
            }, strict=True)
            if source_kind == 'original' and state['phase'] == 'observed':
                _validate_expired_original_observation(state, result)
            return result
        except ProjectVerificationError:
            raise
        except Exception:  # noqa: BLE001 - no raw persisted metadata/path reaches diagnostics.
            raise ProjectVerificationError('python_expired_environment_metadata_invalid') from None

    async def original_context(self, tenant_id: UUID, run_id: UUID) -> PythonOriginalContext | None:
        """Read history for modes/terminal cleanup without reexecution or approval."""
        try:
            context = await self.select(tenant_id, run_id, "main_agent", readonly=True)
            if context is None:
                return None
            invocation = python_original_invocation(
                context.invocation.model_copy(
                    update={"execution_id": context.state["execution_id"], "install_allowed": True}
                ),
                context.inputs,
            )
            observation = cast(Mapping[str, JsonValue], context.state["observation"])
            payload = observation["environment_reference"]
            reference = (
                None
                if payload is None
                else PythonEnvironmentReference.model_validate_json(_encoded(payload), strict=True)
            )
            references = await asyncio.to_thread(self._read_references, context)
            # The DB observation is also a trusted historical issuance record.
            if reference is not None and reference not in references:
                references = (*references, reference)
            return PythonOriginalContext(
                context.inputs, invocation, context.stored, observation, reference, references
            )
        except ProjectVerificationError:
            raise
        except Exception:  # noqa: BLE001 - malformed private evidence must not expose paths.
            raise ProjectVerificationError(
                "original project observation is unavailable or changed"
            ) from None

    async def observe_original(
        self,
        context: _Context,
        *,
        actor: str,
        reference: ProjectBaselineReference,
        execution_id: str,
        timeout_seconds: float,
        offline_review: OriginalPythonOfflineReview | None = None,
    ) -> Mapping[str, JsonValue]:
        try:
            _need(
                reference == context.stored.reference
                and context.state["execution_id"] == execution_id
            )
            report, invocation, _, _ = await self._run(
                context,
                actor,
                original=True,
                execution_id=execution_id,
                timeout_seconds=timeout_seconds,
                offline_review=offline_review,
            )
            binding: dict[str, JsonValue] = {
                "schema_version": 1,
                "contract_sha256": context.stored.contract.protected_sha256,
                "protected_sha256": context.stored.contract.protected_sha256,
            }
            ephemeral = (report.ephemeral_environment
                         if isinstance(report, (PythonEphemeralProjectReport, PythonProjectFailure)) else None)
            observation: dict[str, JsonValue] = {
                "schema_version": 3 if ephemeral is not None else 2,
                "status": report.report["status"],
                "verification": report.to_payload(),
                "original_check_contract": binding,
                "python_recipe": python_recipe_marker(context.inputs),
                "execution_id": execution_id,
                "environment_reference": None
                if report.environment_reference is None
                else report.environment_reference.model_dump(mode="json"),
            }
            if ephemeral is not None:
                observation["ephemeral_environment"] = ephemeral.model_dump(mode="json")
            if isinstance(report, PythonProjectFailure):
                _need(report.diagnostic is not None)
                assert report.diagnostic is not None
                observation["execution_diagnostic"] = report.diagnostic.to_payload()
            return validate_python_original_observation(
                observation,
                inputs=context.inputs,
                invocation=invocation,
                expected_source=reference.input_source_sha256,
                expected_contract=context.stored.contract.protected_sha256,
            )
        except (ProjectVerificationError, ProcessCleanupIncomplete):
            raise
        except Exception as error:  # noqa: BLE001 - executor exceptions are never phase observations.
            self._raise_backend(error)

    async def verify(self, context: _Context, actor: str) -> Mapping[str, JsonValue]:
        key = (context.invocation.tenant_id, context.invocation.run_id)
        self.receipts.pop(key, None)
        self.owner._pending_python_products.pop(key, None)
        from agent_hub.capabilities.python_product_delivery import discard_product

        product = None
        committed = False
        try:
            await self._approval(context, actor)
            report, invocation, source, product = await self._run(context, actor, original=False)
            if product is not None:
                self.owner._pending_python_products[key] = product
            if isinstance(report, PythonProjectFailure):
                raise ProjectVerificationError(
                    "python_project_checks_failed", report.diagnostic, python_project_failure=report
                )
            binding = _OriginalScope(context.stored, context.observation_sha256).binding(source)
            self.receipts[key] = _Receipt(
                context, invocation, source,
                report.ephemeral_environment if isinstance(report, PythonEphemeralProjectReport)
                else report.environment_reference, actor, product
            )
            self.owner._original_receipts[key] = binding
            result: dict[str, JsonValue] = {
                **({"python_product": product} if product is not None else {}),
                "verification": report.to_payload(),
                "original_check_contract": binding,
                "python_recipe": python_recipe_marker(context.inputs),
                # Root issuance evidence for trusted tool history, not a fresh grant.
                **({"python_ephemeral_environment": report.ephemeral_environment.model_dump(mode="json")}
                   if isinstance(report, PythonEphemeralProjectReport) else
                   {"python_environment_reference": report.environment_reference.model_dump(mode="json")}),
                "summary": "Original checks preserved; Python build and tests verified in isolation.",
            }
            committed = True
            return result
        except (ProjectVerificationError, ProcessCleanupIncomplete):
            raise
        except Exception as error:  # noqa: BLE001 - no arbitrary backend text becomes public evidence.
            self._raise_backend(error)
        finally:
            if not committed:
                self.receipts.pop(key, None)
                self.owner._pending_python_products.pop(key, None)
                discard_product(self.owner.generated_file_store, product)

    async def require_current(self, tenant_id: UUID, run_id: UUID) -> str:
        key = (tenant_id, run_id)
        try:
            receipt = self.receipts[key]
            current = await self.select(tenant_id, run_id, receipt.actor, approve=True)
            _need(
                current is not None
                and current.inputs == receipt.context.inputs
                and current.authority == receipt.context.authority
                and current.observation_sha256 == receipt.context.observation_sha256
            )
            assert current is not None
            binding = _OriginalScope(current.stored, current.observation_sha256).binding(
                receipt.source
            )
            _need(self.owner._original_receipts.get(key) == binding)
            base = base_python_invocation(receipt.invocation)
            current.stored.contract.require_current(
                await project_source_manifest(self.owner.root, base)
            )
            _need(await project_source_fingerprint(self.owner.root, base) == receipt.source)
            if isinstance(receipt.reference, PythonEphemeralEnvironmentEvidence):
                receipt.reference.require_for(receipt.invocation)
                _need(receipt.reference.source_sha256 == receipt.source
                      and receipt.reference.interpreter_sha256 == current.inputs.interpreter.binary_sha256)
            else:
                inspect_invocation = PythonProjectInvocation.model_validate(
                    {
                        **receipt.invocation.model_dump(),
                        "execution_id": "python_inspect_" + uuid4().hex,
                        "install_allowed": False,
                    },
                    strict=True,
                )
                actual = await self.executor.inspect_environment(
                    inspect_invocation, expected_environment=receipt.reference
                )
                _need(type(actual) is PythonEnvironmentReference and actual == receipt.reference)
            _need(await project_source_fingerprint(self.owner.root, base) == receipt.source)
            after = await self.select(tenant_id, run_id, receipt.actor, approve=True)
            _need(
                after is not None
                and after.state == current.state
                and after.authority == current.authority
            )
            return receipt.source
        except ProcessCleanupIncomplete:
            self.receipts.pop(key, None)
            self.owner._original_receipts.pop(key, None)
            raise
        except asyncio.CancelledError:
            self.receipts.pop(key, None)
            self.owner._original_receipts.pop(key, None)
            raise
        except Exception:  # noqa: BLE001 - stale or revoked evidence cannot create a new bundle.
            self.receipts.pop(key, None)
            self.owner._original_receipts.pop(key, None)
            raise ProjectVerificationError(
                "project verification required before delivery"
            ) from None
