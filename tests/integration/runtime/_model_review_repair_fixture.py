"""A2 fault boundary around a REAL AutoGen adapter; never an execution grant.

Only the final SDK wire and configuration are controlled in the PG case.
CapacityPool uses the parent's existing owned Redis fixture. The PG case
uses production RunService, RunRepository, artifact/admission/review DAOs and
Root. An ACKed failure/write is a bounded observation, not delivery acceptance.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator, Awaitable, Callable, Mapping, Sequence
from contextlib import aclosing
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol, TypedDict, Unpack
from unittest.mock import patch
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agent_hub.api.routers.runs import CreateRunRequest
from agent_hub.auth.models import AuthenticatedPrincipal, Role
from agent_hub.capabilities.defaults import RuntimeCapabilityStack, build_runtime_capability_stack
from agent_hub.capabilities.review_contracts import ApprovalReviewConfig
from agent_hub.config.repository import ConfigRevision, ConfigStatus
from agent_hub.config.schema import PlatformConfig
from agent_hub.config.service import ConfigService
from agent_hub.domain.runs import RunStatus, TaskMode
from agent_hub.files.workspace import ProjectWorkspaceStore
from agent_hub.models.capacity import CapacityPool
from agent_hub.models.gateway import CapacityController, ModelGateway, ModelGatewayError
from agent_hub.models.litellm_client import LiteLLMClient, ModelTransportError
from agent_hub.models.physical_admission import _call as physical_call
from agent_hub.models.physical_budget import _BudgetAdmission, _run_budget
from agent_hub.models.run_admission_repository import PoolSnapshot, _WorkerLease
from agent_hub.models.types import Deployment, ModelCapability, ModelMessage, ModelRequest
from agent_hub.runs.artifacts import PostgresArtifactRepository
from agent_hub.runs.authorization import _AuthenticatedInput, _capture_authenticated_input
from agent_hub.runs.repository import RunRecord, RunRepository
from agent_hub.runs.service import RunService
from agent_hub.runs.workspace import WorkspaceSelection, workspace_selection
from agent_hub.runtime.artifacts import ArtifactRepository
from agent_hub.runtime.autogen.adapter import (
    AutoGenDiscussionRuntime,
    DiscussionPlan,
    _DiscussionDurability,
)
from agent_hub.runtime.autogen.adapter import (
    DiscussionParticipant as _DiscussionParticipant,
)
from agent_hub.runtime.contracts import (
    Artifact,
    EventKind,
    JsonValue,
    RunEvent,
    RuntimeCheckpoint,
    TaskContext,
)
from agent_hub.runtime.defaults import _gateway_for_config
from agent_hub.runtime.registry import RuntimeRegistry
from agent_hub.runtime.review_assembly import (
    REQUIRED_VERIFICATION_CHECKS,
    ProviderBillingContract,
    ReviewAssemblyBroker,
    WorkerReviewVerification,
    assemble_review_capability_stack,
    catalog_fingerprint,
    runtime_source_fingerprint,
)
from agent_hub.security.secrets import SecretService
from tests.integration.models.run_admission_typed_fixture import Storage
from tests.integration.runtime._model_review_repair_typed_fixture import (
    FixtureSecrets,
    ProjectionFixture,
    RepairDB,
    RepairFixture,
    claim,
    fixture_platform,
    repair_fixture,
    service_runtime_context,
    submitted_input_scope,
)
from tests.unit.harness.review_typed_fixture import StreamRuntimeStub, present
from tests.unit.harness.test_review_dispatch import ExecuteCall
from tests.unit.runs.project_auto_repair_typed_fixture import (
    UnexpectedTaskQueue,
    closable,
    mapping,
    sequence,
    string,
)

DiscussionParticipant = _DiscussionParticipant

PROJECT, SESSION = "a2-repair", "main"
TARGET, ORIGINAL, PATCHED = "app.txt", "value = 0\n", "value = 1\n"
UNKNOWN_PROBE = "A2 prior uncertain physical attempt. Never replay this request."
MESSAGE = (
    "Diagnose the missing observation, read app.txt from the current workspace, "
    "then correct its value from 0 to 1 using a newly reviewed exact file write. "
    "Keep the original permissions and budgets; do the correction yourself."
)


type CapacityFactory = Callable[
    [UUID, Sequence[Deployment]], Awaitable[CapacityPool | CapacityController]
]


class SentAttempt(TypedDict):
    purpose: str
    request_sha256: str
    deployment_id: str
    provider_model: str
    physical_id: UUID
    lease: _WorkerLease


class RedisCards(Protocol):
    async def zcard(self, key: str) -> int: ...


class ClosedBody(httpx.AsyncByteStream):
    def __init__(self, content: bytes, owner: RepairWire) -> None:
        self.content, self.closed = content, False
        owner.bodies.append(self)

    async def __aiter__(self) -> AsyncGenerator[bytes, None]:
        yield self.content

    async def aclose(self) -> None:
        self.closed = True


@dataclass
class RepairComponents:
    runtime: AckBoundary
    stack: RuntimeCapabilityStack
    wire: RepairWire
    root_entries: list[tuple[UUID, dict[str, object]]]
    root_effects: list[Mapping[str, JsonValue]]


@dataclass
class ServiceComponents(RepairComponents):
    service: RunService


@dataclass
class PGCase:
    factory: async_sessionmaker[AsyncSession]
    user: AuthenticatedPrincipal
    repository: RunRepository
    wire: RepairWire
    record: RunRecord
    root: Path
    evidence: _AuthenticatedInput
    capacities: list[CapacityPool]
    redis_client: RedisCards
    service: Callable[[str], Awaitable[ServiceComponents]]


@dataclass
class AcceptedCase(PGCase):
    source_sha: str
    root_before_sha: str
    before: PoolSnapshot
    unknown: tuple[UUID, int, str | None]
    unknown_state_before: str
    scope_before: str
    saved_checkpoint: RuntimeCheckpoint
    initial: ServiceComponents


class RepairWire:
    """OpenAI SDK HTTP seam; identity is taken from the real physical permit."""

    def __init__(
        self, *, unknown_review: bool = False, authorization_message: str = MESSAGE
    ) -> None:
        self.unknown_review = unknown_review
        self.authorization_message = authorization_message
        self.phase = "initial"
        self.calls: list[Mapping[str, object]] = []
        self.bodies: list[ClosedBody] = []
        self.sent: dict[UUID, SentAttempt] = {}
        self.reviews: list[Mapping[str, object]] = []
        self.contexts: list[TaskContext] = []
        self.tool_calls: list[tuple[str, str, dict[str, str]]] = []
        self.unknown_ids: set[UUID] = set()
        self.diagnosed = False

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        payload = mapping(json.loads(request.content))
        budget, call = _run_budget.get(), physical_call.get()
        assert budget is not None and call is not None and call.attempts
        attempt = call.attempts[-1]
        metadata = attempt.metadata
        admission = call.scope._admission
        assert isinstance(admission, _BudgetAdmission)
        issued = admission._attempts[attempt.permit.attempt_id]
        self.sent[attempt.permit.attempt_id] = {
            "purpose": metadata.purpose,
            "request_sha256": metadata.request_sha256,
            "deployment_id": metadata.deployment_id,
            "provider_model": metadata.provider_model,
            "physical_id": issued.physical_id,
            "lease": budget.lease,
        }
        self.calls.append(payload)
        text = "\n".join(
            str(message.get("content", ""))
            for raw_message in sequence(payload["messages"])
            for message in (mapping(raw_message),)
        )
        if UNKNOWN_PROBE in text:
            self.unknown_ids.add(attempt.permit.attempt_id)
            raise httpx.ReadError("controlled uncertain SDK response", request=request)
        schema = mapping(mapping(payload.get("response_format", {})).get("json_schema", {})).get(
            "schema", {}
        )
        tool: dict[str, object] | None = None
        if "action_digest" in mapping(mapping(schema).get("properties", {})):
            assert not payload.get("tools") and metadata.purpose == "review"
            material = mapping(
                json.loads(string(mapping(sequence(payload["messages"])[-1])["content"]))
            )
            assert (
                mapping(material["authenticated_authorization"])["message"]
                == self.authorization_message
            )
            assert mapping(mapping(material["exact_action"])["request"])["arguments"] == {
                "path": TARGET,
                "content": PATCHED,
            }
            self.reviews.append(material)
            if self.unknown_review:
                self.unknown_ids.add(attempt.permit.attempt_id)
                raise httpx.ReadError("controlled uncertain review", request=request)
            content = json.dumps(
                {
                    "schema_version": 1,
                    "decision": "allow",
                    "risk": "low",
                    "reason_code": "authorized_action",
                    "rationale": "Exact bounded correction.",
                    "action_digest": material["action_digest"],
                }
            )
        elif payload.get("tools"):
            choice = payload.get("tool_choice")
            required = (
                mapping(choice.get("function", {})).get("name")
                if isinstance(choice, Mapping)
                else None
            )
            arguments: dict[str, str]
            if required in {"project.inspect", "project_inspect"}:
                name, arguments = "project.inspect", {}
            elif self.phase == "initial":
                name, arguments = "workspace.read", {"path": TARGET}
            elif not self.diagnosed:
                assert "repair" in text.lower(), "actual repair feedback was not propagated"
                name, arguments = "workspace.read", {"path": TARGET}
                self.diagnosed = True
            else:
                assert "value = 0" in text, "patch must follow a fresh actual Root read"
                name, arguments = "workspace.write_text", {"path": TARGET, "content": PATCHED}
            available = {
                mapping(mapping(entry)["function"])["name"] for entry in sequence(payload["tools"])
            }
            native_name = next(
                candidate for candidate in (name, name.replace(".", "_")) if candidate in available
            )
            self.tool_calls.append((self.phase, name, arguments))
            tool = {
                "id": "a2-" + uuid4().hex,
                "type": "function",
                "function": {"name": native_name, "arguments": json.dumps(arguments)},
            }
            content = None
        else:
            content = "implementer"  # actual AutoGen SelectorGroupChat participant
        usage = {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5}
        if payload.get("stream"):
            delta: dict[str, object] = {"role": "assistant", "content": content}
            if tool is not None:
                delta = {"role": "assistant", "tool_calls": [{"index": 0, **tool}]}
            frames = [
                {
                    "id": "a2",
                    "object": "chat.completion.chunk",
                    "model": payload["model"],
                    "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
                },
                {
                    "id": "a2",
                    "object": "chat.completion.chunk",
                    "model": payload["model"],
                    "choices": [
                        {"index": 0, "delta": {}, "finish_reason": "tool_calls" if tool else "stop"}
                    ],
                    "usage": usage,
                },
            ]
            stream_body = (
                "".join("data: " + json.dumps(frame) + "\n\n" for frame in frames)
                + "data: [DONE]\n\n"
            ).encode()
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                stream=ClosedBody(stream_body, self),
            )
        message: dict[str, object] = {"role": "assistant", "content": content}
        if tool is not None:
            message["tool_calls"] = [tool]
        body = {
            "id": "a2",
            "object": "chat.completion",
            "created": 1,
            "model": payload["model"],
            "choices": [
                {"index": 0, "message": message, "finish_reason": "tool_calls" if tool else "stop"}
            ],
            "usage": usage,
        }
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            stream=ClosedBody(json.dumps(body).encode(), self),
        )


class AckBoundary:
    """Transparent fault/stop wrapper; model, tool, checkpoint and ACK stay real."""

    mode = TaskMode.DISCUSS

    def __init__(
        self,
        gateway: ModelGateway,
        stack: RuntimeCapabilityStack,
        artifacts: ArtifactRepository,
        wire: RepairWire,
        *,
        prime_unknown: bool = True,
    ) -> None:
        self.gateway, self.stack, self.artifacts, self.wire = gateway, stack, artifacts, wire
        self.prime_unknown = prime_unknown
        self.pending: RuntimeCheckpoint | None = None
        self.adapter: AutoGenDiscussionRuntime | None = None
        self.last_checkpoint: RuntimeCheckpoint | None = None
        self.loaded_checkpoint: RuntimeCheckpoint | None = None
        self.phase_checkpoint: RuntimeCheckpoint | None = None

    async def restore_checkpoint(self, checkpoint: RuntimeCheckpoint) -> None:
        self.pending = checkpoint

    async def save_checkpoint(self) -> RuntimeCheckpoint:
        return await present(self.adapter).save_checkpoint()

    async def cancel(self) -> None:
        if self.adapter is not None:
            await self.adapter.cancel()

    def validate_publication_checkpoint(
        self, checkpoint: RuntimeCheckpoint, context: TaskContext
    ) -> None:
        present(self.adapter).validate_publication_checkpoint(checkpoint, context)

    async def run(self, context: TaskContext) -> AsyncGenerator[RunEvent, None]:
        self.wire.contexts.append(context)
        repair = context.routing_decision.get("source") == "self_repair"
        self.wire.phase = "repair" if repair else "initial"
        if not repair and self.prime_unknown:
            # Prior unknown *physical* attempt, not an unknown tool receipt or a
            # permission to replay one. It uses the same real gateway/SDK/pool.
            try:
                await self.gateway.complete_with_context(
                    ModelRequest(
                        logical_model="primary",
                        messages=(ModelMessage(role="user", content=UNKNOWN_PROBE),),
                        required_capabilities=frozenset({ModelCapability.TEXT}),
                        max_output_tokens=128,
                    )
                )
            except (ModelGatewayError, ModelTransportError):
                if len(self.wire.unknown_ids) != 1:
                    raise
            else:
                raise AssertionError("controlled unknown probe unexpectedly succeeded")
        self.adapter = AutoGenDiscussionRuntime(
            self.gateway,
            DiscussionPlan(
                participants=(
                    DiscussionParticipant(
                        id="implementer",
                        role="Engineer",
                        goal="Diagnose and correct",
                        logical_model="primary",
                        allowed_tools=("workspace.read", "workspace.write_text", "project.inspect"),
                    ),
                    DiscussionParticipant(
                        id="observer",
                        role="Observer",
                        goal="Observe bounded evidence",
                        logical_model="primary",
                    ),
                ),
                selector_model="primary",
                max_turns=8,
                token_budget=1000000,
                wall_time_seconds=120,
            ),
            capability_gateway=self.stack.runtime_gateway,
            harness_tool_gateway=self.stack.harness_tool_gateway,
            artifact_repository=self.artifacts,
        )
        if self.pending is not None:
            await self.adapter.restore_checkpoint(self.pending)
            self.loaded_checkpoint = self.adapter._restored
        boundary = asyncio.Event()
        fail, succeed = _DiscussionDurability.fail_tool, _DiscussionDurability.succeed_tool

        async def finish(
            durable: _DiscussionDurability,
            artifact: Artifact,
            original: Callable[[_DiscussionDurability, Artifact], Awaitable[None]],
        ) -> None:
            await original(durable, artifact)  # includes real private storage + publication ACK
            if (not repair and original is succeed) or (
                repair and artifact.content.get("tool_name") == "workspace.write_text"
            ):
                self.last_checkpoint = await present(self.adapter).save_checkpoint()
                boundary.set()
                await asyncio.Event().wait()

        async def failing(durable: _DiscussionDurability, artifact: Artifact) -> None:
            await finish(durable, artifact, fail)

        async def succeeding(durable: _DiscussionDurability, artifact: Artifact) -> None:
            await finish(durable, artifact, succeed)

        with (
            patch.object(_DiscussionDurability, "fail_tool", failing),
            patch.object(_DiscussionDurability, "succeed_tool", succeeding),
        ):
            async with aclosing(closable(self.adapter.run(context))) as stream:
                stop = asyncio.create_task(boundary.wait())
                item: asyncio.Task[RunEvent] | None = None
                try:
                    while True:
                        item = asyncio.create_task(anext(stream))
                        await asyncio.wait((item, stop), return_when=asyncio.FIRST_COMPLETED)
                        if stop.done():
                            await self.adapter.cancel()
                            item.cancel()
                            await asyncio.gather(item, return_exceptions=True)
                            break
                        try:
                            event = item.result()
                            if (
                                event.checkpoint is not None
                                and "project_repair_phase" in event.checkpoint.state
                            ):
                                self.phase_checkpoint = event.checkpoint
                            yield event
                        except StopAsyncIteration:
                            return
                finally:
                    stop.cancel()
                    if item is not None and not item.done():
                        item.cancel()
                        await asyncio.gather(item, return_exceptions=True)
                    await asyncio.gather(stop, return_exceptions=True)
        yield RunEvent(
            kind=EventKind.RUNTIME_FAILED,
            sequence=1,
            run_id=context.run_id,
            reason="project repair stopped boundary failure",
        )


async def components(
    tmp_path: Path,
    repository: RunRepository,
    artifacts: ArtifactRepository,
    broker: ReviewAssemblyBroker,
    platform: PlatformConfig,
    configs: ConfigService,
    secrets: SecretService,
    capacity: CapacityFactory,
    wire: RepairWire,
    *,
    tenant_id: UUID,
) -> RepairComponents:
    transport = LiteLLMClient(_network_delegate_factory=lambda: httpx.MockTransport(wire))
    broker._transport = transport  # fixture's final SDK delegate, not a production source mutation
    gateway, _, _ = await _gateway_for_config(
        platform,
        tenant_id=tenant_id,
        secret_service=secrets,
        capacity_factory=capacity,
        transport=transport,
    )
    stack = assemble_review_capability_stack(
        broker,
        build_runtime_capability_stack,
        tenant_id=tenant_id,
        run_repository=repository,
        skill_store_dir=tmp_path / "skills",
        workspace_root=tmp_path,
        generated_artifact_dir=tmp_path / "generated",
        project_workspace_dir=tmp_path,
        artifact_repository=artifacts,
    )
    root_entries: list[tuple[UUID, dict[str, object]]] = []
    root_effects: list[Mapping[str, JsonValue]] = []
    execute = stack.runtime_gateway.execute

    async def observe_root(**kwargs: Unpack[ExecuteCall]) -> Mapping[str, JsonValue]:
        is_write = kwargs["name"] in {"workspace.write_text", "workspace_write_text"}
        if is_write:
            root_entries.append((kwargs["run_id"], dict(kwargs["arguments"])))
        result = await execute(**kwargs)  # original Root implementation, no substitute receipt
        if is_write:
            root_effects.append(result)
        return result

    # Only this instance observes calls; the production Root method stays bound above.
    pytest.MonkeyPatch().setattr(stack.runtime_gateway, "execute", observe_root)
    runtime = AckBoundary(gateway, stack, artifacts, wire)
    return RepairComponents(runtime, stack, wire, root_entries, root_effects)


def initial_route(
    selection: WorkspaceSelection, profile: Mapping[str, object], *, conversation_id: str | None
) -> dict[str, object]:
    """The native initial submission, before RunService claim adds worker flags."""
    return {
        **selection.routing_payload(),
        "conversation_id": conversation_id,
        "execution_backend": "systemd",
        "engineering_auto_repair_version": 1,
        "engineering_delivery_contract_version": 1,
        "engineering_checkpoint_ack_version": 1,
        "engineering_final_report_version": 1,
        "runtime_token_absolute_tokens": 1000000,
        "runtime_plan_token_budget": 1000000,
        "runtime_timeout_seconds": 120,
        **profile,
    }


async def pg_case(
    storage: Storage, tmp_path: Path, redis_client: RedisCards, *, unknown_review: bool = False
) -> PGCase:
    factory, ids = storage
    user = AuthenticatedPrincipal(uuid4(), uuid4(), Role.OPERATOR)
    repository = RunRepository(factory)
    artifacts = PostgresArtifactRepository(factory)
    platform, secrets = fixture_platform(), FixtureSecrets()
    capacities: list[CapacityPool] = []
    config = ApprovalReviewConfig(
        engine="rules_then_model",
        reviewer_logical_model="reviewer",
        max_input_tokens=1000000,
        max_output_tokens=512,
        max_cost_usd_per_run="100.00",
        require_distinct_model=True,
    )

    class Configs(ConfigService):
        def __init__(self) -> None:
            pass

        async def get_current(self, tenant: UUID) -> ConfigRevision:
            assert tenant == user.tenant_id
            return ConfigRevision(
                uuid4(),
                tenant,
                1,
                ConfigStatus.PUBLISHED,
                platform.model_dump(mode="json"),
                None,
                datetime.now(UTC),
            )

    async def current_config(tenant: UUID) -> ApprovalReviewConfig:
        assert tenant == user.tenant_id
        return config

    namespace = "a2-repair:" + uuid4().hex + ":"

    async def capacity(tenant: UUID, deployments: Sequence[Deployment]) -> CapacityPool:
        assert tenant == user.tenant_id

        async def fingerprint(reference: str) -> str:
            return await secrets.fingerprint(tenant, reference)

        controller = CapacityPool(
            redis_client,
            deployments=deployments,
            fingerprint_resolver=fingerprint,
            key_prefix=namespace,
        )
        capacities.append(controller)
        return controller

    configs = Configs()
    wire = RepairWire(unknown_review=unknown_review)
    broker = ReviewAssemblyBroker(
        config_service=configs,
        config_getter=current_config,
        run_repository=repository,
        session_factory=factory,
        secret_service=secrets,
        capacity_factory=capacity,
        billing_contracts=tuple(
            ProviderBillingContract(
                user.tenant_id,
                catalog_fingerprint(platform),
                name,
                "fixture://A2-SDK-only-not-production",
            )
            for name in ("primary_1", "reviewer_1")
        ),
        verification=WorkerReviewVerification(
            # Compiler protocol fixture, as in the existing native mode helper;
            # these two selectors only exercise actual DISCUSS repair.
            frozenset({TaskMode.DIRECT, TaskMode.DISPATCH, TaskMode.DISCUSS, TaskMode.HYBRID}),
            "fixture://A2-DISCUSS-only",
            source_sha256=runtime_source_fingerprint(),
            checks=REQUIRED_VERIFICATION_CHECKS,
        ),
    )
    profile = await broker.submission_profile(user.tenant_id, timeout_seconds=120)
    assert "approval_review_profile" in profile, "repair protocol fixture failed to enroll profile"
    body = CreateRunRequest(
        message=MESSAGE,
        mode=TaskMode.DISCUSS,
        conversation_id="a2-" + uuid4().hex,
        project_id=PROJECT,
        workspace_session_id=SESSION,
        sandbox_profile="workspace_write",
        requested_permissions=("workspace.read", "command.run"),
        permission_mode="auto_review",
        execution_backend="systemd",
        runtime_timeout_seconds=120,
    )
    selection = workspace_selection(
        project_id=PROJECT,
        session_id=SESSION,
        sandbox_profile="workspace_write",
        requested_permissions=("workspace.read", "command.run"),
        permission_mode="auto_review",
    )
    route = initial_route(selection, profile, conversation_id=body.conversation_id)
    evidence = _capture_authenticated_input(user, body.message, submitted_input_scope(body))
    record = await repository.create_run(
        tenant_id=user.tenant_id,
        actor_id=user.user_id,
        actor_role=user.role,
        request=MESSAGE,
        mode=TaskMode.DISCUSS,
        status=RunStatus.QUEUED,
        idempotency_key=None,
        enqueue=True,
        routing_decision=route,
        authenticated_input=evidence,
    )
    ids.append(record.id)
    root = ProjectWorkspaceStore(tmp_path).session_root(user.tenant_id, PROJECT, SESSION)
    root.mkdir(parents=True, exist_ok=True)
    (root / TARGET).write_text(ORIGINAL, encoding="utf-8")
    common = (tmp_path, repository, artifacts, broker, platform, configs, secrets, capacity, wire)

    async def service(worker: str) -> ServiceComponents:
        case = await components(*common, tenant_id=user.tenant_id)

        async def trusted_main_window() -> int:
            return 131072  # the fixed primary catalog's context window

        run_service = RunService(
            repository,
            runtime_registry=RuntimeRegistry((case.runtime,)),
            router=None,
            task_queue=UnexpectedTaskQueue(),
            worker_id=worker,
            runtime_timeout_seconds=120,
            runtime_token_budget=1000000,
            review_broker=broker,
            main_agent_context_window_getter=trusted_main_window,
        )
        return ServiceComponents(
            case.runtime, case.stack, case.wire, case.root_entries, case.root_effects, run_service
        )

    return PGCase(
        factory, user, repository, wire, record, root, evidence, capacities, redis_client, service
    )


async def capacity_clean(case: PGCase) -> None:
    for controller in case.capacities:
        for quota_scope in ("fixture-primary", "fixture-reviewer"):
            assert await case.redis_client.zcard(controller._keys(quota_scope)["leases"]) == 0


async def projection_case(tmp_path: Path, *, legacy: bool = False) -> ProjectionFixture:
    """Accepted real DAO facts at the local ORM seam; no PG/native claim."""
    from agent_hub.runs.self_repair import project_repair_scope_sha256

    private = {
        "project_verification_receipt": "fixture-private-receipt",
        "approval_checkpoint_worker_token": str(uuid4()),
    }
    f = await repair_fixture(tmp_path, accept=not legacy, route_extras=private)
    if legacy:
        route = dict(mapping(f.db.run.routing_decision))
        route.pop("approval_review_profile")
        proposal = dict(mapping(route["repair_proposal"]))
        proposal["automatic_scope_sha256"] = project_repair_scope_sha256(route)
        route["repair_proposal"] = proposal
        f.db.run.routing_decision = route
        await f.runs.accept_self_repair_and_enqueue(
            tenant_id=f.cp.tenant_id,
            run_id=f.cp.run_id,
            version=4,
            decision_token="token",
            automatic_checkpoint_sha256=f.cp.state_sha256,
        )
    context = await service_runtime_context(f, await claim(f), window=10000)
    return ProjectionFixture(
        f.f,
        f.db,
        f.runs,
        f.original_task,
        f.original,
        f.cp,
        f.before,
        f.old_token,
        context,
        private,
    )


async def projected_worker[T](
    f: RepairFixture, context: TaskContext, check: Callable[[TaskContext], Awaitable[T]]
) -> T:
    """Run a phase consumer inside the actual worker's private execution scope."""
    from agent_hub.runtime.worker_execution import worker_runtime_events

    values: list[T] = []

    class Probe(StreamRuntimeStub):
        async def run(self, actual: TaskContext) -> AsyncGenerator[RunEvent, None]:
            values.append(await check(actual))
            yield RunEvent(
                kind="runtime.started",
                sequence=1,
                run_id=actual.run_id,
                payload={"phase": "source-probe"},
            )

    async with worker_runtime_events(
        Probe(), context, worker_id="repair-worker", run_repository=f.runs, review_broker=f.f.broker
    ) as stream:
        async for _ in stream:
            pass
    return values[0]


def local_service_sql(db: RepairDB) -> None:
    """Enable real AsyncSession SQL event delivery at the dedicated local seam."""
    db.service_sql = True
