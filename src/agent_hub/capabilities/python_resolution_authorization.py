"""Finite resolution projections, never authority over a generic command."""
from __future__ import annotations

import asyncio
import hashlib
import re
from collections.abc import Awaitable, Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal, cast
from uuid import UUID

from agent_hub.capabilities.policy import normalize_resource
from agent_hub.capabilities.types import CapabilityRequest
from agent_hub.runtime.contracts import JsonValue
from agent_hub.skills.sandbox.python_registry_protocol import (
    digest,
    require_sha,
    requirement,
    target_environment,
)

if TYPE_CHECKING:
    from agent_hub.capabilities.gateway import CapabilityResult
    from agent_hub.skills.sandbox.python_metadata_plan_protocol import PythonMetadataScope
    from agent_hub.skills.sandbox.python_resolution_protocol import (
        ProtectedOriginalResolutionSource,
    )

PREFIX = "workspace/current/project-resolution/python/"
_FIELDS = {"purpose", "key", "source_sha256", "root_sha256", "catalog_sha256", "permissions_sha256",
           "original_deadline", "registry", "artifact_host", "install_allowed", "requirements",
           "environment", "max_wheel_bytes"}


class InvalidResolutionAuthorization(ValueError):
    """Invalid pre-dispatch data cannot establish an execution outcome."""


@dataclass(frozen=True, slots=True)
class ResolutionAuthorizationFailure:
    """This proposal failed before dispatch; says nothing about earlier IO."""

    def to_payload(self) -> dict[str, JsonValue]:
        return {"schema_version": 1, "stage": "before_dispatch", "code": "resolution_proposal_invalid",
                "action_dispatched": False, "retryable": False}


@dataclass(slots=True)
class _ResolutionPolicyBinding:
    identity: tuple[UUID, UUID, UUID, str]
    idempotency_key: str
    owner: asyncio.Task[object] | None
    authorize: Callable[[CapabilityRequest], Awaitable[bool]] | None
    active: bool = True
    busy: bool = False
    decision: tuple[str, CapabilityResult] | None = None
    deadline_scope: dict[str, object] | None = None
    original_deadline: datetime | None = None


_RESOLUTION_POLICY: ContextVar[_ResolutionPolicyBinding | None] = ContextVar('resolution_policy', default=None)


def bind_resolution_original_deadline(scope: Mapping[str, object], deadline: datetime) -> None:
    """Attach validated checkpoint data to this owned call, never a grant."""
    binding = _RESOLUTION_POLICY.get()
    if binding is None:
        return
    if (not binding.active or binding.owner is not asyncio.current_task()
            or tuple(str(value) for value in binding.identity)
            != tuple(scope.get(key) for key in ('tenant_id', 'run_id', 'user_id', 'actor'))
            or binding.deadline_scope is not None and (
                binding.deadline_scope != dict(scope) or binding.original_deadline != deadline)):
        raise InvalidResolutionAuthorization('project verification scope is not authorized')
    binding.deadline_scope, binding.original_deadline = dict(scope), deadline


def resolution_original_deadline(scope: Mapping[str, object]) -> datetime | None:
    binding = _RESOLUTION_POLICY.get()
    if binding is None or binding.deadline_scope is None:
        return None
    if (not binding.active or binding.owner is not asyncio.current_task()
            or binding.deadline_scope != dict(scope)):
        raise InvalidResolutionAuthorization('project verification scope is not authorized')
    return binding.original_deadline


@contextmanager
def resolution_policy_scope(
    expected: CapabilityRequest,
) -> Iterator[None]:
    """Owned Harness invocation only; inherited tasks cannot borrow this callback."""
    binding = _ResolutionPolicyBinding(
        (expected.tenant_id, expected.run_id, expected.user_id, expected.agent_id),
        expected.idempotency_key, asyncio.current_task(), None)
    token = _RESOLUTION_POLICY.set(binding)
    try:
        yield
    finally:
        binding.active = False
        _RESOLUTION_POLICY.reset(token)


def bind_resolution_policy_authorizer(
    expected: CapabilityRequest, authorize: Callable[[CapabilityRequest], Awaitable[bool]],
) -> None:
    binding = _RESOLUTION_POLICY.get()
    if (binding is None or not binding.active or binding.owner is not asyncio.current_task()
            or binding.identity != (expected.tenant_id, expected.run_id, expected.user_id, expected.agent_id)
            or binding.idempotency_key != expected.idempotency_key):
        raise InvalidResolutionAuthorization('project verification scope is not authorized')
    binding.authorize = authorize


def note_resolution_policy_decision(request: CapabilityRequest, decision: CapabilityResult) -> None:
    from agent_hub.capabilities.approvals import fingerprint_capability_request

    binding = _RESOLUTION_POLICY.get()
    if binding is None or not binding.active or binding.owner is not asyncio.current_task():
        raise InvalidResolutionAuthorization('project verification scope is not authorized')
    # ALLOWED is re-evaluated, never used as a cached dispatch permission.
    binding.decision = None if decision.status == 'allowed' else (fingerprint_capability_request(request), decision)


def resolution_policy_decision(request: CapabilityRequest) -> CapabilityResult | None:
    from agent_hub.capabilities.approvals import fingerprint_capability_request

    binding = _RESOLUTION_POLICY.get()
    if (binding is None or not binding.active or binding.owner is not asyncio.current_task()
            or binding.decision is None or binding.decision[0] != fingerprint_capability_request(request)):
        return None
    return binding.decision[1]


async def resolution_policy_allowed(request: CapabilityRequest) -> bool | None:
    """Re-evaluate the current exact policy at every pre/post-dispatch fence."""
    binding = _RESOLUTION_POLICY.get()
    if binding is None:
        return None
    checked = validate_resolution_authorization(request)
    if (not binding.active or binding.busy or binding.owner is not asyncio.current_task()
            or binding.identity != (checked.tenant_id, checked.run_id, checked.user_id, checked.agent_id)
            or checked.idempotency_key != 'resolution-' + cast(str, checked.arguments['key'])):
        raise InvalidResolutionAuthorization('project verification scope is not authorized')
    binding.busy = True
    try:
        if binding.authorize is None:
            return False
        return await binding.authorize(checked)
    finally:
        binding.busy = False


def _protected_original_source(arguments: Mapping[str, object]) -> ProtectedOriginalResolutionSource:
    from agent_hub.skills.sandbox.python_resolution_protocol import (
        ProtectedOriginalResolutionSource,
    )

    raw = arguments["protected_original"]
    source = ProtectedOriginalResolutionSource.model_validate(raw, strict=True)
    if source.model_dump(mode="json") != raw or source.source_sha256 != arguments["source_sha256"]:
        raise ValueError
    return source


def original_resolution_scope(
    record: object, *, actor: str, source: ProtectedOriginalResolutionSource, live: bool = True,
) -> PythonMetadataScope:
    """Revalidate the locked original capture, without establishing approval."""
    from agent_hub.auth.models import Role
    from agent_hub.capabilities.project_baselines import ProjectBaselineReference
    from agent_hub.capabilities.scoped_read import authorized_workspace_scope
    from agent_hub.domain.runs import RunStatus
    from agent_hub.runs.repository import RunRecord, _pre_model_original_capture
    from agent_hub.skills.sandbox.python_metadata_plan_protocol import PythonMetadataScope
    from agent_hub.skills.sandbox.python_resolution_protocol import (
        ProtectedOriginalResolutionSource,
    )

    try:
        if (type(record) is not RunRecord or actor != "main_agent" or type(actor) is not str
                or type(source) is not ProtectedOriginalResolutionSource or type(live) is not bool):
            raise ValueError
        source = ProtectedOriginalResolutionSource.model_validate(source, strict=True)
        route = record.routing_decision
        role, user_id = record.actor_role, record.actor_id
        if (type(role) is not Role or role not in {Role.OPERATOR, Role.ADMIN, Role.SUPER_ADMIN}
                or type(user_id) is not UUID or not isinstance(route, Mapping)
                or not _pre_model_original_capture(route)):
            raise ValueError
        capture = route["project_input_preparation"]
        if (not isinstance(capture, Mapping) or capture["schema_version"] != 1
                or ProjectBaselineReference.from_payload(capture["reference"]).to_payload()
                != source.original_reference.model_dump(mode="json")):
            raise ValueError
        if record.status is RunStatus.RUNNING:
            worker, expires = record.worker_id, record.worker_lease_expires_at
            if (type(worker) is not str or not worker.strip() or worker != worker.strip()
                    or type(record.worker_lease_token) is not UUID
                    or not isinstance(expires, datetime) or expires.tzinfo is None
                    or expires <= datetime.now(UTC)):
                raise ValueError
        elif (live or record.status not in {RunStatus.WAITING_APPROVAL, RunStatus.QUEUED}
                or any(value is not None for value in (record.worker_id, record.worker_lease_token,
                       record.worker_lease_expires_at, record.worker_heartbeat_at))):
            raise ValueError
        project, session = authorized_workspace_scope(record, tenant_id=record.tenant_id, run_id=record.id)
        permissions = route["requested_permissions"]
        backend = route.get("execution_backend", "systemd")
        if (route["sandbox_profile"] != "workspace_write" or backend != "systemd"
                or not isinstance(permissions, (tuple, list))
                or not {"workspace.read", "workspace.write", "command.run"} <= set(permissions)
                or type(record.request) is not str):
            raise ValueError
        return PythonMetadataScope(
            tenant_id=record.tenant_id, run_id=record.id, user_id=user_id, actor=actor,
            project_id=project, session_id=session,
            actor_role=cast(Literal["operator", "admin", "super_admin"], role.value),
            request_sha256=hashlib.sha256(record.request.encode("utf-8")).hexdigest(),
            permissions_sha256=digest({"sandbox_profile": "workspace_write",
                                       "execution_backend": backend,
                                       "permissions": sorted(set(permissions))}),
        )
    except (ValueError, TypeError, KeyError, AttributeError, RuntimeError, OverflowError, RecursionError):
        raise InvalidResolutionAuthorization("project verification scope is not authorized") from None


def validate_resolution_authorization(
    request: CapabilityRequest, *, expected: CapabilityRequest | None = None,
) -> CapabilityRequest:
    try:
        if type(request) is not CapabilityRequest:
            raise ValueError
        checked = CapabilityRequest.model_validate(dict(vars(request)) | (request.model_extra or {}), strict=True)
        args = checked.arguments
        fields = _FIELDS | ({"plan_sha256", "artifacts"} if args.get("purpose") == "python_dependency_publication"
                            else set())
        if "protected_original" in args:
            fields |= {"protected_original"}
            key = args["key"]
            if (type(key) is not str or checked.agent_id != "main_agent"
                    or checked.idempotency_key != "resolution-" + key):
                raise ValueError
            _protected_original_source(args)
        if (checked.tool_origin != "builtin" or (checked.capability, checked.operation) != ("command", "run")
                or normalize_resource(checked.resource) != checked.resource
                or re.fullmatch(re.escape(PREFIX) + r"[a-f0-9]{64}", checked.resource) is None
                or set(args) != fields
                or args.get("purpose") not in {"python_dependency_resolution", "python_dependency_publication"}
                or checked.resource != PREFIX + digest(args)
                or args["registry"] != "https://pypi.org" or args["artifact_host"] != "files.pythonhosted.org"
                or args["install_allowed"] is not False or type(args["max_wheel_bytes"]) is not int
                or args["max_wheel_bytes"] != 64 * 1024**2):
            raise ValueError
        if expected is not None and any(getattr(checked, key) != getattr(expected, key)
                for key in ("tenant_id", "user_id", "run_id", "agent_id", "idempotency_key")):
            raise ValueError
        for key in ("key", "source_sha256", "root_sha256", "catalog_sha256", "permissions_sha256"):
            require_sha(cast(str, args[key]))
        deadline = args["original_deadline"]
        if type(deadline) is not str or len(deadline) > 64 or datetime.fromisoformat(deadline).tzinfo is None:
            raise ValueError
        needs = args["requirements"]
        if type(needs) is not list or not 0 < len(needs) <= 128:
            raise ValueError
        for item in needs:
            requirement(item)
        if type(args["environment"]) is not dict:
            raise ValueError
        target_environment(cast(dict[str, str], args["environment"]))
        if args["purpose"] == "python_dependency_publication":
            require_sha(cast(str, args["plan_sha256"]))
            artifacts = args["artifacts"]
            if type(artifacts) is not list or not 0 < len(artifacts) <= 128:
                raise ValueError
            total = 0
            names: set[str] = set()
            for entry in artifacts:
                if (type(entry) is not list or len(entry) != 3 or type(entry[0]) is not str
                        or not entry[0].endswith("-py3-none-any.whl") or "/" in entry[0] or "\\" in entry[0]
                        or entry[0] in names or type(entry[2]) is not int or entry[2] <= 0):
                    raise ValueError
                require_sha(entry[1])
                names.add(entry[0])
                total += entry[2]
            if total > 64 * 1024**2:
                raise ValueError
        return checked
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError, RecursionError):
        pass
    raise InvalidResolutionAuthorization("project verification scope is not authorized") from None


def resolution_scope_resource(
    scope: object, *, tenant_id: UUID, user_id: UUID | None, run_id: UUID,
) -> str | None:
    """Decode a full actor-specific run scope, never a prefix or conversation grant."""
    if type(scope) is not str or type(user_id) is not UUID:
        return None
    prefix = (f"capability-scope:v1:tenant={tenant_id}:user={user_id}:run={run_id}:"
              "capability=command:operation=run:resource=")
    match = re.fullmatch(re.escape(prefix) + "(" + re.escape(PREFIX)
                         + r"[a-f0-9]{64}):agent=([a-z0-9][a-z0-9_-]{0,127})", scope)
    return None if match is None else match.group(1)


def resolution_approval_binding(record: object, request: CapabilityRequest) -> dict[str, object]:
    """Bind the trusted row's identity, session, permissions and original request."""
    from agent_hub.capabilities.python_metadata_preparation import metadata_scope

    checked = validate_resolution_authorization(request)
    if "protected_original" in checked.arguments:
        scope = original_resolution_scope(
            record, actor=checked.agent_id, source=_protected_original_source(checked.arguments),
        )
        # Keep the hashed arguments so an approved read can verify the resource
        # against the exact snapshot, even after the original lease is cleared.
        binding = {"schema_version": 2, "arguments": checked.model_dump(mode="json")["arguments"]}
    else:
        scope = metadata_scope(record, actor=checked.agent_id, live=True)
        binding = {"schema_version": 1}
    if ((scope.tenant_id, scope.run_id, scope.user_id) != (checked.tenant_id, checked.run_id, checked.user_id)
            or checked.arguments['permissions_sha256'] != scope.permissions_sha256):
        raise InvalidResolutionAuthorization('project verification scope is not authorized')
    return {**binding, 'scope': scope.model_dump(mode='json'), 'resource': checked.resource}


def resolution_binding_matches(record: object, binding: object, approval_scope: object) -> bool:
    from agent_hub.capabilities.python_metadata_preparation import metadata_scope

    try:
        if (not isinstance(binding, Mapping)
                or type(binding.get('schema_version')) is not int
                or not isinstance(binding.get('scope'), Mapping)):
            return False
        actor = binding['scope'].get('actor')
        if type(actor) is not str:
            return False
        fields = {'schema_version', 'scope', 'resource'}
        if binding['schema_version'] == 2 and set(binding) == fields | {'arguments'}:
            args = binding['arguments']
            resource_value = binding['resource']
            if type(args) is not dict or type(resource_value) is not str:
                return False
            current = original_resolution_scope(
                record, actor=actor, source=_protected_original_source(args), live=False,
            )
            request = CapabilityRequest(
                tenant_id=current.tenant_id, run_id=current.run_id, user_id=current.user_id,
                agent_id=actor, capability="command", operation="run", resource=resource_value,
                arguments=args, idempotency_key="resolution-" + args["key"],
            )
            validate_resolution_authorization(request)
            if args["permissions_sha256"] != current.permissions_sha256:
                return False
        elif binding['schema_version'] == 1 and set(binding) == fields:
            current = metadata_scope(record, actor=actor)
        else:
            return False
        resource = resolution_scope_resource(approval_scope, tenant_id=current.tenant_id,
            user_id=current.user_id, run_id=current.run_id)
        return (resource is not None and resource == binding['resource']
                and approval_scope == (f'capability-scope:v1:tenant={current.tenant_id}:user={current.user_id}:'
                    f'run={current.run_id}:capability=command:operation=run:resource={resource}:agent={actor}')
                and binding['scope'] == current.model_dump(mode='json'))
    except (ValueError, TypeError, AttributeError, KeyError, RuntimeError, OverflowError, RecursionError):
        return False
