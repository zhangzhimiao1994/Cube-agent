"""Local permission test DTOs and adapters; no external authority or database."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import TypedDict
from uuid import UUID, uuid4

from fastapi.testclient import TestClient

from agent_hub.api.routers.admin import InMemoryAdminResourceService
from agent_hub.app import create_app
from agent_hub.auth.models import AuthenticatedPrincipal, Role
from agent_hub.capabilities.policy import CapabilityPolicy, CapabilityRule
from agent_hub.capabilities.types import CapabilityRequest
from agent_hub.domain.runs import RunStatus, TaskMode
from agent_hub.runs.authorization import _AuthenticatedInput
from agent_hub.runs.repository import RunRecord, RunRepository
from agent_hub.runs.service import RunService, SubmittedRun
from agent_hub.runtime.contracts import JsonValue
from tests.api.test_runs_api import StubAuthService, StubRunService
from tests.unit.runs.project_auto_repair_typed_fixture import present
from tests.unit.runs.test_conversation_mode import ConversationModeRepository

__all__ = ["present"]


@dataclass
class ModeRecord:
    tenant_id: UUID
    id: UUID
    actor_id: UUID | None
    actor_role: Role
    status: RunStatus
    routing_decision: Mapping[str, object] | None


class ExternalInvocation(TypedDict):
    tenant_id: UUID
    user_id: UUID
    run_id: UUID
    actor: str
    name: str
    arguments: Mapping[str, JsonValue]
    idempotency_key: str


def permission_client(
    *,
    settings_service: InMemoryAdminResourceService | None = None,
) -> tuple[TestClient, StubRunService, AuthenticatedPrincipal]:
    """Same legacy client construction with its real settings-service contract."""
    principal = AuthenticatedPrincipal(uuid4(), uuid4(), Role.OPERATOR)
    service = StubRunService([])
    app = create_app(
        auth_service=StubAuthService(principal),
        rate_limiter=object(),
        config_service=object(),
        admin_resource_service=settings_service,
        run_service=service,
    )
    return TestClient(app), service, principal


class PermissionSubmissionRepository(RunRepository):
    """Delegate exercised operations to the unchanged legacy in-memory fixture."""

    # The original fixture had no conversation approval persistence support.
    supports_conversation_capability_approval = False

    def __init__(self, previous_mode: TaskMode | None) -> None:
        self.original = ConversationModeRepository(previous_mode)

    @property
    def record(self) -> RunRecord | None:
        return self.original.record

    @property
    def created(self) -> list[dict[str, object]]:
        return self.original.created

    async def latest_resolved_mode_for_conversation(
        self,
        *,
        tenant_id: UUID,
        actor_id: UUID,
        conversation_id: str,
    ) -> TaskMode | None:
        return await self.original.latest_resolved_mode_for_conversation(
            tenant_id=tenant_id,
            actor_id=actor_id,
            conversation_id=conversation_id,
        )

    async def create_run(
        self,
        *,
        tenant_id: UUID,
        actor_id: UUID,
        actor_role: Role | None = None,
        request: str,
        mode: TaskMode | None,
        status: RunStatus,
        idempotency_key: str | None,
        routing_decision: dict[str, object] | None = None,
        blocked_by_run_id: UUID | None = None,
        enqueue: bool,
        authenticated_input: _AuthenticatedInput | None = None,
    ) -> RunRecord:
        assert blocked_by_run_id is None and authenticated_input is None
        return await self.original.create_run(
            tenant_id=tenant_id,
            actor_id=actor_id,
            actor_role=actor_role,
            request=request,
            mode=mode,
            status=status,
            idempotency_key=idempotency_key,
            routing_decision=routing_decision,
            enqueue=enqueue,
        )

    async def get(
        self,
        tenant_id: UUID,
        run_id: UUID,
        *,
        actor_id: UUID | None = None,
    ) -> RunRecord:
        result = await self.original.get(tenant_id, run_id)
        assert actor_id is None or actor_id == result.actor_id
        return result


def untrusted_rule(fields: Mapping[str, object]) -> CapabilityRule:
    """Call the actual constructor; malformed fields reach its __post_init__."""
    constructor: object = CapabilityRule
    assert callable(constructor)
    result: object = constructor(**fields)
    assert isinstance(result, CapabilityRule)
    return result


def untrusted_request(fields: Mapping[str, object]) -> CapabilityRequest:
    constructor: object = CapabilityRequest
    assert callable(constructor)
    result: object = constructor(**fields)
    assert isinstance(result, CapabilityRequest)
    return result


def untrusted_policy(rules: tuple[object, ...]) -> CapabilityPolicy:
    """Preserve the non-rule negative input and the actual policy rejection."""
    constructor: object = CapabilityPolicy
    assert callable(constructor)
    result: object = constructor(rules)
    assert isinstance(result, CapabilityPolicy)
    return result


async def submit_untrusted_permissions(
    service: RunService,
    *,
    tenant_id: UUID,
    actor_id: UUID,
    message: str,
    mode: TaskMode,
    permission_mode: str,
    sandbox_profile: str,
    requested_permissions: tuple[str, ...],
) -> SubmittedRun:
    """Pass invalid mode/profile data unchanged into the actual service boundary."""
    submit: object = service.submit
    assert callable(submit)
    result: object = await submit(
        tenant_id=tenant_id,
        actor_id=actor_id,
        message=message,
        mode=mode,
        permission_mode=permission_mode,
        sandbox_profile=sandbox_profile,
        requested_permissions=requested_permissions,
    )
    assert isinstance(result, SubmittedRun)
    return result
