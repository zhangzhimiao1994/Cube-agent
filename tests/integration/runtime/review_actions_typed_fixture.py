"""Exact typed IO surfaces for action-review integration and local wire contracts."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, NotRequired, Protocol, TypedDict
from uuid import UUID, uuid4

import httpx

from agent_hub.api.routers.runs import CreateRunRequest
from agent_hub.auth.models import AuthenticatedPrincipal, Role
from agent_hub.capabilities.review_context import ConcreteModelReviewGate
from agent_hub.capabilities.review_repository import ReviewRepository
from agent_hub.config.repository import ConfigRevision, ConfigStatus
from agent_hub.config.schema import PlatformConfig
from agent_hub.config.service import ConfigService
from agent_hub.domain.runs import TaskMode
from agent_hub.models.admission_bounds import TrustedContextBound
from agent_hub.models.litellm_client import LiteLLMClient
from agent_hub.models.physical_budget import _RunBudgetContext
from agent_hub.models.run_admission_repository import RunAdmissionRepository
from agent_hub.models.types import Deployment
from agent_hub.runs.authorization import _capture_authenticated_input
from agent_hub.runtime.artifacts import InMemoryArtifactRepository
from agent_hub.runtime.contracts import (
    Artifact,
    JsonValue,
    RuntimeCheckpoint,
    TaskContext,
)
from agent_hub.runtime.defaults import (
    ConfigBackedDirectRuntime,
    ConfigBackedDiscussionRuntime,
    ConfigBackedDispatchRuntime,
    ConfigBackedHybridRuntime,
    _deployments,
)
from tests.integration.runtime.review_mode_import_closure_typed_fixture import (
    NativeCase,
    WireAttempt,
    WireDeployment,
    WireUsage,
)
from tests.integration.runtime.test_model_review_modes_helper import (
    ClosedBody,
    FixedWire,
    FixtureSecrets,
    fixture_platform,
    submitted_input_scope,
)
from tests.unit.capabilities.review_reachability_typed_fixture import PermissionMode, SandboxProfile
from tests.unit.capabilities.test_concrete_model_review import FixedLedgerReview, FocusedDB
from tests.unit.capabilities.test_model_review_reachability import full_scope
from tests.unit.harness.review_typed_fixture import ConcreteRepositoryAdapter, present
from tests.unit.runs.project_auto_repair_typed_fixture import mapping
from tests.unit.runtime.test_plain_file_action_helper import (
    LocalCapacityBoundary,
    plain_note_submission,
)


class ConfigFixture(ConfigService):
    def __init__(self) -> None:
        # Only get_current is used; subclasses supply that exact real contract.
        pass


def config_revision(tenant: UUID, platform: PlatformConfig) -> ConfigRevision:
    return ConfigRevision(
        uuid4(),
        tenant,
        1,
        ConfigStatus.PUBLISHED,
        platform.model_dump(mode="json"),
        None,
        datetime.now(UTC),
    )


@dataclass
class WireOwner:
    bodies: list[ClosedBody] = field(default_factory=list)


class NoteWireCase(Protocol):
    wire: FixedWire


class InvokeArguments(TypedDict, total=False):
    user_id: UUID | None
    role: Role | None


class ActionAttempt(WireAttempt):
    logical_model: str
    wire_model: str
    stage: str


class IdentityWire(TypedDict):
    deployment: WireDeployment
    purpose: str
    wire_model: str
    logical_model: NotRequired[str]


class ActionScope(TypedDict):
    actor: str
    actor_model: str
    actor_fallbacks: tuple[str, ...]
    action_digest: str
    proposal_attempt_id: UUID
    review_attempt_id: NotRequired[UUID]
    primary_attempt_ids: tuple[UUID, ...]
    acting_models: tuple[WireDeployment, ...]
    acting_wire_models: tuple[str, ...]


class AttemptIdentity(Protocol):
    @property
    def attempt_id(self) -> UUID: ...

    @property
    def deployment(self) -> Mapping[str, object]: ...


class AccountingRow(Protocol):
    @property
    def attempt_id(self) -> UUID: ...

    @property
    def receipt(self) -> dict[str, object] | None: ...

    @property
    def receipt_sha256(self) -> str | None: ...


if TYPE_CHECKING:
    from tests.integration.runtime.test_model_review_actions_modes import ActionWire


@dataclass
class ActionCase(NativeCase):
    wire: ActionWire
    path: str
    content: str
    source_sha: str


class EventArguments(TypedDict, total=False):
    step_id: str
    actor: str
    artifact: Artifact
    checkpoint: RuntimeCheckpoint
    inputs: tuple[Artifact, ...]
    session_id: str
    participants: tuple[str, ...]
    message: str
    payload: Mapping[str, JsonValue]


@dataclass
class IdentityAttempt:
    attempt_id: UUID
    deployment: WireDeployment
    purpose: str = "primary"


def action_factory(
    mode: TaskMode,
) -> type[ConfigBackedDispatchRuntime | ConfigBackedDiscussionRuntime | ConfigBackedHybridRuntime]:
    factories: dict[
        TaskMode,
        type[
            ConfigBackedDispatchRuntime | ConfigBackedDiscussionRuntime | ConfigBackedHybridRuntime
        ],
    ] = {
        TaskMode.DISPATCH: ConfigBackedDispatchRuntime,
        TaskMode.DISCUSS: ConfigBackedDiscussionRuntime,
        TaskMode.HYBRID: ConfigBackedHybridRuntime,
    }
    return factories[mode]


@dataclass
class LocalNoteCase:
    runtime: ConfigBackedDirectRuntime
    task: TaskContext
    budget: _RunBudgetContext
    db: FocusedDB
    ledger: ReviewRepository
    gate: ConcreteModelReviewGate
    body: CreateRunRequest
    path: str
    content: str


def typed_note_case(tmp_path: Path) -> LocalNoteCase:
    mode: PermissionMode = "auto_review"
    preauth = False
    profile: SandboxProfile = "workspace_write"
    s = full_scope(tmp_path, mode=mode, preauthorized=preauth, profile=profile)
    body, path, content = plain_note_submission()
    body = body.model_copy(
        update={
            "permission_mode": mode,
            "project_id": "demo",
            "workspace_session_id": "session",
            "conversation_id": s.body.conversation_id,
            "sandbox_profile": s.body.sandbox_profile,
            "requested_permissions": s.body.requested_permissions,
        }
    )
    evidence = _capture_authenticated_input(
        AuthenticatedPrincipal(
            user_id=present(s.task.actor_id),
            tenant_id=s.task.tenant_id,
            role=present(s.task.actor_role),
        ),
        body.message,
        submitted_input_scope(body),
    )
    s.task = s.task.model_copy(update={"request": body.message})
    s.runs.record = replace(s.runs.record, request=body.message)
    old = s.budget
    platform = fixture_platform()
    for model in platform.models.values():
        for deployment in model.deployments:
            deployment.context_window_tokens = 10000
            deployment.max_output_tokens = 1000
    s.budget = type(old)(
        repository=old.repository,
        pool=replace(old._pool, authorization_turn_id=evidence.turn_id),
        evidence=evidence,
        lease=old.lease,
        config=old.config,
        deadline=old.deadline,
        trusted_contexts=tuple(
            TrustedContextBound(d.id, d.logical_model, d.provider_model, 10000)
            for d in _deployments(platform)
        ),
    )
    s.runs.evidence = evidence
    s.db = FocusedDB(s)
    s.budget.repository = RunAdmissionRepository(s.db)
    s.ledger = ReviewRepository(s.db)
    s.fixed = FixedLedgerReview(s.ledger)
    s.gate.repository, s.gate.service = ConcreteRepositoryAdapter(s.ledger), s.fixed
    s.body = body
    wire_payloads: list[Mapping[str, object]] = []

    class Configs(ConfigFixture):
        async def get_current(self, tenant: UUID) -> ConfigRevision:
            return config_revision(tenant, platform)

    async def capacity(tenant: UUID, deployments: Sequence[Deployment]) -> LocalCapacityBoundary:
        return LocalCapacityBoundary()

    async def wire(request: httpx.Request) -> httpx.Response:
        payload = mapping(json.loads(request.content))
        wire_payloads.append(payload)
        text = json.dumps(
            {
                "workspace_batch": {"files": {path: content}, "complete": True, "continuation": ""},
                "summary": f"Saved {path}.",
            }
        )
        return httpx.Response(
            200,
            json={
                "id": "note",
                "object": "chat.completion",
                "created": 1,
                "model": payload["model"],
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": text},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5},
            },
        )

    runtime = ConfigBackedDirectRuntime(
        config_service=Configs(),
        secret_service=FixtureSecrets(),
        capacity_factory=capacity,
        transport=LiteLLMClient(_network_delegate_factory=lambda: httpx.MockTransport(wire)),
        artifact_repository=InMemoryArtifactRepository(),
        capability_gateway=s.backend,
        harness_tool_gateway=s.harness,
        action_capability_gateway=s.backend,
        action_harness_tool_gateway=s.harness,
    )
    return LocalNoteCase(runtime, s.task, s.budget, s.db, s.ledger, s.gate, s.body, path, content)


@dataclass
class NoteWireRecord:
    wire: FixedWire


class AccountingSent(TypedDict):
    deployment: WireDeployment
    physical_transport_id: str | None
    usage: WireUsage


class PurposeAccountingSent(AccountingSent):
    purpose: str


class ExecutionWire(Protocol):
    @property
    def attempts(self) -> Mapping[UUID, Mapping[str, object]]: ...


@dataclass
class ExecutionWireFixture:
    attempts: dict[UUID, dict[str, object]]
