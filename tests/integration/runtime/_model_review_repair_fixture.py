"""A2 fault boundary around a REAL AutoGen adapter; never an execution grant.

Only the final SDK wire and configuration are controlled in the PG case.
CapacityPool uses the parent's existing owned Redis fixture. The PG case
uses production RunService, RunRepository, artifact/admission/review DAOs and
Root. An ACKed failure/write is a bounded observation, not delivery acceptance.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import aclosing
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

import httpx

from agent_hub.api.routers.runs import CreateRunRequest
from agent_hub.auth.models import AuthenticatedPrincipal, Role
from agent_hub.capabilities.defaults import build_runtime_capability_stack
from agent_hub.capabilities.review_contracts import ApprovalReviewConfig
from agent_hub.domain.runs import RunStatus, TaskMode
from agent_hub.files.workspace import ProjectWorkspaceStore
from agent_hub.models.capacity import CapacityPool
from agent_hub.models.gateway import ModelGatewayError
from agent_hub.models.litellm_client import LiteLLMClient, ModelTransportError
from agent_hub.models.physical_admission import _call as physical_call
from agent_hub.models.physical_budget import _run_budget
from agent_hub.models.types import ModelCapability, ModelMessage, ModelRequest
from agent_hub.runs.artifacts import PostgresArtifactRepository
from agent_hub.runs.authorization import _capture_authenticated_input
from agent_hub.runs.repository import RunRepository
from agent_hub.runs.service import RunService
from agent_hub.runs.workspace import workspace_selection
from agent_hub.runtime.autogen.adapter import (
    AutoGenDiscussionRuntime,
    DiscussionParticipant,
    DiscussionPlan,
    _DiscussionDurability,
)
from agent_hub.runtime.contracts import EventKind, RunEvent
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
from tests.integration.runtime.test_model_review_modes_helper import (
    ClosedBody,
    FixtureSecrets,
    fixture_platform,
    submitted_input_scope,
)

PROJECT, SESSION = "a2-repair", "main"
TARGET, ORIGINAL, PATCHED = "app.txt", "value = 0\n", "value = 1\n"
UNKNOWN_PROBE = "A2 prior uncertain physical attempt. Never replay this request."
MESSAGE = (
    "Diagnose the missing observation, read app.txt from the current workspace, "
    "then correct its value from 0 to 1 using a newly reviewed exact file write. "
    "Keep the original permissions and budgets; do the correction yourself."
)


class RepairWire:
    """OpenAI SDK HTTP seam; identity is taken from the real physical permit."""

    def __init__(self, *, unknown_review=False, authorization_message=MESSAGE):
        self.unknown_review = unknown_review
        self.authorization_message = authorization_message
        self.phase = "initial"
        self.calls, self.bodies, self.sent, self.reviews = [], [], {}, []
        self.contexts, self.tool_calls = [], []
        self.unknown_ids = set()
        self.diagnosed = False

    async def __call__(self, request):
        payload = json.loads(request.content)
        budget, call = _run_budget.get(), physical_call.get()
        assert budget is not None and call is not None and call.attempts
        attempt = call.attempts[-1]
        metadata = attempt.metadata
        issued = call.scope._admission._attempts[attempt.permit.attempt_id]
        self.sent[attempt.permit.attempt_id] = {
            "purpose": metadata.purpose,
            "request_sha256": metadata.request_sha256,
            "deployment_id": metadata.deployment_id,
            "provider_model": metadata.provider_model,
            "physical_id": issued.physical_id,
            "lease": budget.lease,
        }
        self.calls.append(payload)
        text = "\n".join(str(message.get("content", "")) for message in payload["messages"])
        if UNKNOWN_PROBE in text:
            self.unknown_ids.add(attempt.permit.attempt_id)
            raise httpx.ReadError("controlled uncertain SDK response", request=request)
        schema = payload.get("response_format", {}).get("json_schema", {}).get("schema", {})
        tool = None
        if "action_digest" in schema.get("properties", {}):
            assert not payload.get("tools") and metadata.purpose == "review"
            material = json.loads(payload["messages"][-1]["content"])
            assert material["authenticated_authorization"]["message"] == self.authorization_message
            assert material["exact_action"]["request"]["arguments"] == {
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
            required = choice.get("function", {}).get("name") if isinstance(choice, dict) else None
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
            available = {entry["function"]["name"] for entry in payload["tools"]}
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
            delta = {"role": "assistant", "content": content}
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
            body = (
                "".join("data: " + json.dumps(frame) + "\n\n" for frame in frames)
                + "data: [DONE]\n\n"
            ).encode()
            return httpx.Response(
                200, headers={"content-type": "text/event-stream"}, stream=ClosedBody(body, self)
            )
        message = {"role": "assistant", "content": content}
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

    def __init__(self, gateway, stack, artifacts, wire, *, prime_unknown=True):
        self.gateway, self.stack, self.artifacts, self.wire = gateway, stack, artifacts, wire
        self.prime_unknown = prime_unknown
        self.pending = self.adapter = self.last_checkpoint = None
        self.loaded_checkpoint = self.phase_checkpoint = None

    async def restore_checkpoint(self, checkpoint):
        self.pending = checkpoint

    async def save_checkpoint(self):
        return await self.adapter.save_checkpoint()

    async def cancel(self):
        if self.adapter is not None:
            await self.adapter.cancel()

    def validate_publication_checkpoint(self, checkpoint, context):
        self.adapter.validate_publication_checkpoint(checkpoint, context)

    async def run(self, context):
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

        async def finish(durable, artifact, original):
            await original(durable, artifact)  # includes real private storage + publication ACK
            if (not repair and original is succeed) or (
                repair and artifact.content.get("tool_name") == "workspace.write_text"
            ):
                self.last_checkpoint = await self.adapter.save_checkpoint()
                boundary.set()
                await asyncio.Event().wait()

        async def failing(durable, artifact):
            await finish(durable, artifact, fail)

        async def succeeding(durable, artifact):
            await finish(durable, artifact, succeed)

        with (
            patch.object(_DiscussionDurability, "fail_tool", failing),
            patch.object(_DiscussionDurability, "succeed_tool", succeeding),
        ):
            async with aclosing(self.adapter.run(context)) as stream:
                stop = asyncio.create_task(boundary.wait())
                item = None
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
    tmp_path, repository, artifacts, broker, platform, configs, secrets, capacity, wire
):
    transport = LiteLLMClient(_network_delegate_factory=lambda: httpx.MockTransport(wire))
    broker._transport = transport  # fixture's final SDK delegate, not a production source mutation
    gateway, _, _ = await _gateway_for_config(
        platform,
        tenant_id=broker._a2_tenant,
        secret_service=secrets,
        capacity_factory=capacity,
        transport=transport,
    )
    stack = assemble_review_capability_stack(
        broker,
        build_runtime_capability_stack,
        tenant_id=broker._a2_tenant,
        run_repository=repository,
        skill_store_dir=tmp_path / "skills",
        workspace_root=tmp_path,
        generated_artifact_dir=tmp_path / "generated",
        project_workspace_dir=tmp_path,
        artifact_repository=artifacts,
    )
    root_entries, root_effects = [], []
    execute = stack.runtime_gateway.execute

    async def observe_root(**kwargs):
        is_write = kwargs["name"] in {"workspace.write_text", "workspace_write_text"}
        if is_write:
            root_entries.append((kwargs["run_id"], dict(kwargs["arguments"])))
        result = await execute(**kwargs)  # original Root implementation, no substitute receipt
        if is_write:
            root_effects.append(result)
        return result

    stack.runtime_gateway.execute = observe_root
    runtime = AckBoundary(gateway, stack, artifacts, wire)
    return SimpleNamespace(
        runtime=runtime,
        stack=stack,
        wire=wire,
        root_entries=root_entries,
        root_effects=root_effects,
    )


def initial_route(selection, profile, *, conversation_id):
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


async def pg_case(storage, tmp_path, redis_client, *, unknown_review=False):
    factory, ids = storage
    user = AuthenticatedPrincipal(uuid4(), uuid4(), Role.OPERATOR)
    repository = RunRepository(factory)
    artifacts = PostgresArtifactRepository(factory)
    platform, secrets, capacities = fixture_platform(), FixtureSecrets(), []
    config = ApprovalReviewConfig(
        engine="rules_then_model",
        reviewer_logical_model="reviewer",
        max_input_tokens=1000000,
        max_output_tokens=512,
        max_cost_usd_per_run="100.00",
        require_distinct_model=True,
    )

    class Configs:
        async def get_current(self, tenant):
            assert tenant == user.tenant_id
            return SimpleNamespace(version=1, document=platform.model_dump(mode="json"))

    async def current_config(tenant):
        assert tenant == user.tenant_id
        return config

    namespace = "a2-repair:" + uuid4().hex + ":"

    async def capacity(tenant, deployments):
        assert tenant == user.tenant_id

        async def fingerprint(reference):
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
    broker._a2_tenant = user.tenant_id
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

    async def service(worker):
        case = await components(*common)

        async def trusted_main_window():
            return 131072  # the fixed primary catalog's context window

        case.service = RunService(
            repository,
            runtime_registry=RuntimeRegistry((case.runtime,)),
            router=None,
            task_queue=SimpleNamespace(),
            worker_id=worker,
            runtime_timeout_seconds=120,
            runtime_token_budget=1000000,
            review_broker=broker,
            main_agent_context_window_getter=trusted_main_window,
        )
        return case

    return SimpleNamespace(**locals())


async def capacity_clean(case):
    for controller in case.capacities:
        for quota_scope in ("fixture-primary", "fixture-reviewer"):
            assert await case.redis_client.zcard(controller._keys(quota_scope)["leases"]) == 0


async def projection_case(tmp_path, *, legacy=False):
    """Accepted real DAO facts at the local ORM seam; no PG/native claim."""
    from agent_hub.runs.self_repair import project_repair_scope_sha256
    from tests.unit.runtime.test_review_repair_continuity import (
        claim,
        repair_fixture,
        service_runtime_context,
    )

    private = {
        "project_verification_receipt": "fixture-private-receipt",
        "approval_checkpoint_worker_token": str(uuid4()),
    }
    f = await repair_fixture(tmp_path, accept=not legacy, route_extras=private)
    if legacy:
        route = dict(f.db.run.routing_decision)
        route.pop("approval_review_profile")
        proposal = dict(route["repair_proposal"])
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
    f.context = await service_runtime_context(f, await claim(f), window=10000)
    f.private = private
    return f


async def projected_worker(f, context, check):
    """Run a phase consumer inside the actual worker's private execution scope."""
    from agent_hub.runtime.worker_execution import worker_runtime_events

    values = []

    class Probe:
        async def run(self, actual):
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


def local_service_sql(db):
    """Only SQL transport is local; service/DAO classification and accept stay real."""
    from sqlalchemy.sql import operators
    from sqlalchemy.sql.dml import Insert
    from sqlalchemy.sql.elements import BooleanClauseList, Grouping
    from sqlalchemy.sql.selectable import Exists

    from agent_hub.db import models
    from tests.unit.runtime.test_review_repair_continuity import matches

    select_rows = db.select

    def artifact_match(condition, artifact):
        if isinstance(condition, Grouping):
            return artifact_match(condition.element, artifact)
        if isinstance(condition, BooleanClauseList):
            values = (artifact_match(clause, artifact) for clause in condition.clauses)
            return all(values) if condition.operator is operators.and_ else any(values)
        if isinstance(condition, Exists):
            # The continuation DAO's exact correlated written-artifact lookup.
            # Compare actual stored write rows, never claim an unwritten row exists.
            return any(
                type(row) is models.RuntimeArtifactWriteRow
                and row.status == "written"
                and (row.tenant_id, row.run_id, row.artifact_id, row.content_sha256)
                == (artifact.tenant_id, artifact.run_id, artifact.id, artifact.content_sha256)
                for row in db.rows
            )
        return matches(condition, artifact)

    def local_select(statement):
        if statement.column_descriptions[0].get("entity") is models.RuntimeArtifactRow:
            return [
                row
                for row in db.rows
                if type(row) is models.RuntimeArtifactRow
                and all(artifact_match(c, row) for c in statement._where_criteria)
            ]
        return select_rows(statement)

    db.select = local_select
    scalar, execute = db.scalar, db.execute
    tables = {
        cls.__tablename__: cls
        for cls in (
            models.RunEventRow,
            models.RunCheckpointRow,
            models.RunArtifactRow,
            models.RunUsageRow,
            models.RunApprovalRow,
        )
    }

    def inserted(statement):
        cls = tables[statement.table.name]
        params = statement.compile().params
        keys = (
            ("run_id", "sequence")
            if cls in (models.RunEventRow, models.RunCheckpointRow)
            else ("id",)
        )
        if any(
            type(row) is cls and all(getattr(row, key) == params[key] for key in keys)
            for row in db.rows
        ):
            return None
        row = cls(**params)
        db.add(row)
        return row.id

    async def local_scalar(statement):
        if isinstance(statement, Insert):
            return inserted(statement)
        if getattr(statement.column_descriptions[0]["expr"], "name", None) == "count":
            table = statement.get_final_froms()[0].name
            return sum(type(row).__tablename__ == table for row in db.rows)
        expr = statement.column_descriptions[0]["expr"]
        if (
            statement.column_descriptions[0].get("entity") is models.RunRow
            and getattr(expr, "key", None) == "created_at"
        ):
            selected = db.select(statement)
            return selected[0].created_at if selected else None
        return await scalar(statement)

    async def local_execute(statement):
        if isinstance(statement, Insert):
            identity = inserted(statement)
            return SimpleNamespace(scalar_one_or_none=lambda: identity)
        if getattr(statement.column_descriptions[0]["expr"], "name", None) == "count":
            artifacts = [row for row in db.rows if type(row) is models.RuntimeArtifactRow]
            return SimpleNamespace(
                one=lambda: (len(artifacts), sum(row.byte_size for row in artifacts))
            )
        return await execute(statement)

    async def local_get(cls, identity, **kwargs):
        values = identity if isinstance(identity, tuple) else (identity,)
        keys = [column.key for column in cls.__mapper__.primary_key]
        return next(
            (
                row
                for row in db.rows
                if type(row) is cls
                and all(getattr(row, key) == value for key, value in zip(keys, values, strict=True))
            ),
            None,
        )

    db.scalar, db.execute = local_scalar, local_execute
    db.get = local_get
