"""Pending native action compositions for Dispatch, Discuss and Hybrid ONLY.

Actual production factories/adapters/worker/Gateway/SDK/Harness/Root, existing
PG and owned Redis fixtures. Only configuration metadata and final HTTP wire
are deterministic. Local protocol checks are NOT native mode acceptance.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import sys
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import func, select

from agent_hub.capabilities.defaults import build_runtime_capability_stack
from agent_hub.capabilities.review_contracts import ApprovalReviewConfig
from agent_hub.db.models import (
    RunAdmissionAttemptRow,
    RunApprovalRow,
    RunReviewEffectRow,
    RunReviewEventRow,
    RunReviewIntentRow,
    RunRow,
)
from agent_hub.domain.runs import TaskMode
from agent_hub.files.workspace import ProjectWorkspaceStore
from agent_hub.models.capacity import CapacityPool
from agent_hub.models.litellm_client import LiteLLMClient
from agent_hub.models.physical_admission import _call as physical_call
from agent_hub.models.physical_budget import _run_budget
from agent_hub.models.run_admission_repository import RunAdmissionRepository
from agent_hub.runs.authorization import _persist_authenticated_input
from agent_hub.runs.repository import RunRepository
from agent_hub.runs.workspace import workspace_selection
from agent_hub.runtime.artifacts import InMemoryArtifactRepository
from agent_hub.runtime.autogen.adapter import AutoGenDiscussionRuntime
from agent_hub.runtime.contracts import Artifact, EventKind, RunEvent
from agent_hub.runtime.crew.adapter import CrewAIObjectFactory, CrewDispatchRuntime
from agent_hub.runtime.direct import DirectRuntime
from agent_hub.runtime.hybrid import HybridRuntime
from agent_hub.runtime.review_assembly import (
    REQUIRED_VERIFICATION_CHECKS,
    ProviderBillingContract,
    ReviewAssemblyBroker,
    WorkerReviewVerification,
    assemble_review_capability_stack,
    catalog_fingerprint,
    runtime_source_fingerprint,
)
from agent_hub.runtime.role_planner import (
    RoleAssignment,
    RolePlan,
    RolePlanner,
    RolePlanningRequest,
    RolePurpose,
    TaskProfile,
)
from tests.integration.capabilities.test_review_repository import (
    database_url as database_url,  # noqa: PLC0414
)
from tests.integration.models.test_run_admission import prepare
from tests.integration.models.test_run_admission import storage as storage  # noqa: PLC0414
from tests.integration.runtime.test_model_review_action import _assert_accounting_receipt
from tests.integration.runtime.test_model_review_modes import (
    capacity_is_clean,
    collect,
)
from tests.integration.runtime.test_model_review_modes import (
    own_redis as own_redis,  # noqa: PLC0414
)
from tests.integration.runtime.test_model_review_modes import (
    owned_crewai_storage as owned_crewai_storage,  # noqa: PLC0414
)
from tests.integration.runtime.test_model_review_modes_helper import (
    FACTORIES,
    FINAL_TEXT,
    ClosedBody,
    FixedWire,
    FixtureSecrets,
    build_task_context,
    capture_submission_input,
    fixture_platform,
    submitted_input_scope,
)
from tests.unit.runtime.test_plain_file_action_helper import plain_note_submission

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="module")]
MISSING_MODES = (TaskMode.DISPATCH, TaskMode.DISCUSS, TaskMode.HYBRID)
_REVIEW_KEYS = frozenset(
    {"schema_version", "decision", "risk", "reason_code", "rationale", "action_digest"}
)


class DeclaredActionRoles(RolePlanner):
    """Declarative role-plan fixture at the factory's documented injection seam.

    Unlike AgentDefinition.skills (safe package IDs), RoleAssignment.allowed_tools
    carries actual dotted built-ins. This supplies no runtime/events or grants.
    Production factories still assign models and build/execute their real plans.
    """

    def plan(self, request):
        assert type(request) is RolePlanningRequest and not request.high_risk
        return RolePlan(
            mode=request.mode,
            profile=request.profile,
            profiles=request.profiles,
            roles=(
                RoleAssignment(
                    id="analyst",
                    role="File Writer",
                    purpose=RolePurpose.EXECUTE,
                    mission="Save the exact user-requested note using the declared tool.",
                    must_answer=("Which file was saved?",),
                    allowed_tools=("workspace.write_text",),
                    forbidden_actions=("Do not bypass review or budgets.",),
                    skills=(),
                    output_schema={
                        "status": "done | blocked | needs_user",
                        "summary": "string",
                        "evidence": "string[]",
                    },
                    model="primary",
                ),
                RoleAssignment(
                    id="critic",
                    role="Critic",
                    purpose=RolePurpose.CRITIQUE,
                    mission="Confirm the bounded result without executing tools.",
                    must_answer=("What evidence supports completion?",),
                    allowed_tools=(),
                    forbidden_actions=("Do not claim unobserved execution.",),
                    skills=(),
                    output_schema={},
                    model="primary",
                ),
            ),
        )


def _write_function(payload):
    names = [
        tool["function"]["name"]
        for tool in payload.get("tools", ())
        if tool.get("type") == "function"
        and tool.get("function", {}).get("name") in ("workspace.write_text", "workspace_write_text")
    ]
    assert len(names) <= 1, "ambiguous wire tool mapping"
    return names[0] if names else None


def _wire_reply(payload, *, text=None, tool=None):
    """OpenAI SDK's final HTTP protocol, including genuine native tool deltas."""
    assert (text is None) != (tool is None)
    usage = {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5}
    if payload.get("stream"):
        delta = (
            {"role": "assistant", "content": text}
            if tool is None
            else {"role": "assistant", "tool_calls": [{"index": 0, **tool}]}
        )
        frames = [
            {
                "id": "fixed-action",
                "object": "chat.completion.chunk",
                "model": payload["model"],
                "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
            },
            {
                "id": "fixed-action",
                "object": "chat.completion.chunk",
                "model": payload["model"],
                "choices": [
                    {
                        "index": 0,
                        "delta": {},
                        "finish_reason": "stop" if tool is None else "tool_calls",
                    }
                ],
                "usage": usage,
            },
        ]
        content = (
            "".join("data: " + json.dumps(frame) + "\n\n" for frame in frames) + "data: [DONE]\n\n"
        ).encode()
        return content, "text/event-stream"
    message = (
        {"role": "assistant", "content": text}
        if tool is None
        else {"role": "assistant", "content": None, "tool_calls": [tool]}
    )
    return json.dumps(
        {
            "id": "fixed-action",
            "object": "chat.completion",
            "created": 1,
            "model": payload["model"],
            "choices": [
                {
                    "index": 0,
                    "message": message,
                    "finish_reason": "stop" if tool is None else "tool_calls",
                }
            ],
            "usage": usage,
        }
    ).encode(), "application/json"


def _assert_delivery_text(text, path):
    assert isinstance(text, str) and text.strip(), "empty final delivery"
    assert not text.lstrip().startswith("{"), "raw structured output is not a readable final"
    assert re.search(
        r"(?<![\w/.-])" + re.escape(path) + r"(?=$|[\s\x60\"'，。,:;!?)]|\.(?:$|\s))", text
    ), "final delivery must name the exact saved filename"
    assert re.search(r"[A-Za-z\u4e00-\u9fff]{2,}", text.replace(path, "")), "missing readable reply"


def _assert_final_delivery(events, mode, path):
    """Select actual adapter delivery, never tool/model evidence or generic done."""
    assert events and events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert not any(event.kind is EventKind.RUNTIME_FAILED for event in events)
    deliveries = [
        event
        for event in events
        if event.kind is EventKind.ARTIFACT_CREATED
        and event.artifact is not None
        and event.artifact.type == "text"
    ]
    assert deliveries, "missing public final artifact"
    final_event = deliveries[-1]
    final = final_event.artifact
    if mode is TaskMode.DISPATCH:
        assert len(events[-1].inputs) == 1, "missing terminal delivery reference"
        terminal = events[-1].inputs[0]
        assert (terminal.id, terminal.content_sha256) == (final.id, final.content_sha256)
        completions = [
            event
            for event in events
            if event.kind is EventKind.STEP_COMPLETED and event.step_id == "final_response_step"
        ]
        assert len(completions) == 1 and completions[0].inputs == (final,)
        assert final_event.sequence < completions[0].sequence < events[-1].sequence
    elif mode is TaskMode.DISCUSS:
        completions = [event for event in events if event.kind == "discussion.completed"]
        assert len(completions) == 1
        completion = completions[0]
        messages = [event for event in events if event.kind is EventKind.MESSAGE_CREATED]
        assert messages and messages[-1].actor == final.producer
        assert (
            messages[-1].message == final.content.get("text") == completion.payload.get("summary")
        )
        assert (
            messages[-1].sequence < final_event.sequence < completion.sequence < events[-1].sequence
        )
    else:
        assert mode is TaskMode.HYBRID and final.producer == "main", "missing synthesis final"
        completions = [event for event in events if event.kind == "discussion.completed"]
        assert len(completions) == 1 and completions[0].sequence < final_event.sequence
    text = final.content.get("text")
    _assert_delivery_text(text, path)
    return final


def _executing_adapter(runtime):
    """Read the active ownership chain at physical wire time, not child reachability."""
    pending, seen, executing = [runtime], set(), []
    while pending:
        item = pending.pop()
        if item is None or id(item) in seen:
            continue
        seen.add(id(item))
        if type(item) is HybridRuntime:
            child = item._active_child
            assert child is not None, "Hybrid has no executing child"
            assert (
                sum(child is part for part in (item._dispatch, item._discussion, item._synthesizer))
                == 1
            )
            pending.append(child)
        elif type(item) in (CrewDispatchRuntime, AutoGenDiscussionRuntime, DirectRuntime):
            executing.append(item)
        else:
            active = getattr(item, "_active", None)
            if isinstance(active, dict):
                pending.extend(active.values())
            pending.append(getattr(item, "_child", None))
    assert len(executing) == 1, "exactly one actual adapter must own this wire call"
    adapter = executing[0]
    stage = {
        CrewDispatchRuntime: "dispatch",
        AutoGenDiscussionRuntime: "discuss",
        DirectRuntime: "synthesis",
    }[type(adapter)]
    return stage, adapter


def _assert_execution_observed(events, wire, mode):
    required = {
        TaskMode.DISPATCH: {"dispatch"},
        TaskMode.DISCUSS: {"discuss"},
        TaskMode.HYBRID: {"dispatch", "discuss", "synthesis"},
    }[mode]
    primary = {key: value for key, value in wire.attempts.items() if value["purpose"] == "primary"}
    assert {value["stage"] for value in primary.values()} == required
    final_steps = [
        event
        for event in events
        if event.kind is EventKind.STEP_COMPLETED and event.step_id == "final_response_step"
    ]
    discussions = [event for event in events if event.kind == "discussion.completed"]
    syntheses = [
        event
        for event in events
        if event.kind is EventKind.ARTIFACT_CREATED
        and event.artifact is not None
        and event.artifact.type == "text"
        and event.artifact.producer == "main"
    ]
    completed = {"dispatch": final_steps, "discuss": discussions, "synthesis": syntheses}
    observed = {}
    for stage in sorted(required):
        assert len(completed[stage]) == 1, "missing actual child completion"
        attempts = {key: value for key, value in primary.items() if value["stage"] == stage}
        assert attempts and all(value["physical_transport_id"] for value in attempts.values())
        observed[stage] = {
            "primary_attempt_ids": sorted(str(key) for key in attempts),
            "physical_transport_ids": sorted(
                {value["physical_transport_id"] for value in attempts.values()}
            ),
            "lifecycle_completed": len(completed[stage]),
        }
    if mode is TaskMode.HYBRID:
        assert final_steps[0].sequence < discussions[0].sequence < syntheses[0].sequence
        stages = [value["stage"] for value in primary.values()]
        assert stages == sorted(stages, key=("dispatch", "discuss", "synthesis").index)
    return observed


class ActionWire(FixedWire):
    """No primary/Gateway/adapter replacement: fixed *last* SDK HTTP response."""

    def __init__(self, body, path, content):
        super().__init__()
        self.body, self.path, self.content = body, path, content
        self.proposals = []
        self.actual_types = set()

    def primary_text_for(self, payload):
        # Preserve the actual selector and exact schema/enum structure. Only the
        # free-text fixture value changes, inside the final SDK HTTP response.
        text = FixedWire.text_for(self, payload).replace(
            FINAL_TEXT, f"Saved the note as \u0060{self.path}\u0060."
        )
        schema = payload.get("response_format", {}).get("json_schema", {}).get("schema", {})
        status = schema.get("properties", {}).get("status", {})
        if "done" in status.get("enum", ()) or "done" in status.get("description", "").split(" | "):
            value = json.loads(text)
            value["status"] = "done"
            return json.dumps(value)
        return text

    async def __call__(self, request):
        payload = json.loads(request.content)
        self.calls.append(payload)
        self.budgets.append(_run_budget.get())
        assert self.budgets[-1] is not None, "actual model call escaped original pool"
        for item in self.native_objects():
            self.actual_types.add(type(item))
            if type(item) is CrewDispatchRuntime:
                assert type(item._factory) is CrewAIObjectFactory
        call = physical_call.get()
        assert call is not None and call.attempts
        attempt = call.attempts[-1]
        meta = attempt.metadata
        stage, adapter = _executing_adapter(self.runtime)
        assert type(adapter) in self.actual_types
        assert payload["model"] == meta.provider_model.rsplit("/", 1)[-1]
        physical = call.scope._admission._attempts[attempt.permit.attempt_id].physical_id
        assert physical is not None
        self.attempts[attempt.permit.attempt_id] = {
            "stage": stage,
            "request_sha256": meta.request_sha256,
            "purpose": meta.purpose,
            "physical_transport_id": str(physical),
            "deployment": {
                "provider_id": meta.provider_model.split("/", 1)[0],
                "deployment_id": meta.deployment_id,
                "model_id": meta.provider_model,
            },
            "usage": {
                "prompt_tokens": 2,
                "completion_tokens": 3,
                "total_tokens": 5,
                "cost_usd": {"decimal": "0.000008"},
            },
        }
        self.entered.set()
        schema = payload.get("response_format", {}).get("json_schema", {}).get("schema", {})
        if set(schema.get("properties", {})) == _REVIEW_KEYS:
            assert meta.purpose == "review" and not payload.get("tools")
            material = json.loads(payload["messages"][-1]["content"])
            assert material["authenticated_authorization"]["message"] == self.body.message
            assert material["exact_action"]["request"]["arguments"] == {
                "path": self.path,
                "content": self.content,
            }
            text, tool = FixedWire.text_for(self, payload), None
        else:
            assert meta.purpose == "primary"
            name = _write_function(payload)
            if name is not None and not self.proposals:
                self.proposals.append(attempt.permit.attempt_id)
                tool = {
                    "id": "native-reviewed-file-write",
                    "type": "function",
                    "function": {
                        "name": name,
                        "arguments": json.dumps({"path": self.path, "content": self.content}),
                    },
                }
                text = None
            else:
                text, tool = self.primary_text_for(payload), None
        content, media = _wire_reply(payload, text=text, tool=tool)
        return httpx.Response(
            200, headers={"content-type": media}, stream=ClosedBody(content, self)
        )


async def _case(storage, tmp_path, mode, redis_client):
    """New action config fixture, existing PG/Redis ownership and actual factories."""
    factory = storage[0]
    _, binding, _ = await prepare(storage, open_now=False)
    repository = RunRepository(factory)
    platform = fixture_platform()
    config = ApprovalReviewConfig(
        engine="rules_then_model",
        reviewer_logical_model="reviewer",
        max_input_tokens=1000000,
        max_output_tokens=512,
        max_cost_usd_per_run="100.00",
    )

    class Configs:
        async def get_current(self, tenant):
            assert tenant == binding["tenant_id"]
            return SimpleNamespace(version=1, document=platform.model_dump(mode="json"))

    configs = Configs()
    body, path, content = plain_note_submission()
    body = body.model_copy(update={"mode": mode, "agent_ids": ()})
    wire = ActionWire(body, path, content)
    transport = LiteLLMClient(_network_delegate_factory=lambda: httpx.MockTransport(wire))
    capacities = []
    namespace = "task7-action-mode:" + uuid4().hex + ":"

    async def capacity(tenant, deployments):
        async def fingerprint(reference):
            return await FixtureSecrets().fingerprint(tenant, reference)

        value = CapacityPool(
            redis_client,
            deployments=deployments,
            fingerprint_resolver=fingerprint,
            key_prefix=namespace,
        )
        capacities.append(value)
        return value

    async def current_config(tenant):
        assert tenant == binding["tenant_id"]
        return config

    source_sha = runtime_source_fingerprint()
    broker = ReviewAssemblyBroker(
        config_service=configs,
        config_getter=current_config,
        run_repository=repository,
        session_factory=factory,
        secret_service=FixtureSecrets(),
        capacity_factory=capacity,
        transport=transport,
        billing_contracts=tuple(
            ProviderBillingContract(
                binding["tenant_id"],
                catalog_fingerprint(platform),
                name,
                "fixture://native-action-mode-protocol",
            )
            for name in ("primary_1", "reviewer_1")
        ),
        verification=WorkerReviewVerification(
            frozenset(FACTORIES),
            "fixture://not-production-verification",
            source_sha256=source_sha,
            checks=REQUIRED_VERIFICATION_CHECKS,
        ),
    )
    profile = await broker.submission_profile(binding["tenant_id"], timeout_seconds=180)
    assert "approval_review_profile" in profile
    selection = workspace_selection(
        project_id=body.project_id,
        session_id=body.workspace_session_id,
        sandbox_profile=body.sandbox_profile,
        requested_permissions=body.requested_permissions,
        permission_mode=body.permission_mode,
    )
    route = {
        **selection.routing_payload(),
        "conversation_id": body.conversation_id,
        "execution_backend": body.execution_backend,
        "selected_agent_ids": list(body.agent_ids),
        "runtime_token_absolute_tokens": 10000000,
        "runtime_plan_token_budget": 10000000,
        "runtime_timeout_seconds": 180,
        **profile,
    }
    async with factory() as session, session.begin():
        row = await session.get(RunRow, binding["run_id"])
        row.request, row.mode, row.routing_decision = body.message, mode.value, route
        evidence = capture_submission_input(row, body)
        await _persist_authenticated_input(session, row, evidence)
    record = await repository.get(binding["tenant_id"], binding["run_id"])
    context = build_task_context(record, binding, body, route)
    artifacts = InMemoryArtifactRepository()
    stack = assemble_review_capability_stack(
        broker,
        build_runtime_capability_stack,
        tenant_id=context.tenant_id,
        run_repository=repository,
        skill_store_dir=tmp_path / "skills",
        workspace_root=tmp_path,
        generated_artifact_dir=tmp_path / "generated",
        project_workspace_dir=tmp_path,
        artifact_repository=artifacts,
    )
    runtime = FACTORIES[mode](
        config_service=configs,
        secret_service=FixtureSecrets(),
        capacity_factory=capacity,
        transport=transport,
        capability_gateway=stack.runtime_gateway,
        harness_tool_gateway=stack.harness_tool_gateway,
        artifact_repository=artifacts,
        role_planner=DeclaredActionRoles(),
    )
    wire.runtime = runtime
    return SimpleNamespace(
        factory=factory,
        repository=repository,
        broker=broker,
        context=context,
        binding=binding,
        wire=wire,
        runtime=runtime,
        stack=stack,
        capacities=capacities,
        redis=redis_client,
        body=body,
        path=path,
        content=content,
        source_sha=source_sha,
    )


class _WriteAudit:
    """Observe open(O_WRONLY/O_RDWR) of exact target; NEVER patch Root/store."""

    def __init__(self, target):
        self.target = os.path.normcase(str(target.resolve()))
        self.count = 0
        self.active = True

    def __call__(self, event, args):
        if self.active and event == "open" and isinstance(args[0], (str, bytes)):
            path, flags = os.fsdecode(args[0]), args[2]
            if (
                os.path.normcase(os.path.abspath(path)) == self.target
                and type(flags) is int
                and flags & (os.O_WRONLY | os.O_RDWR)
            ):
                self.count += 1


@pytest.mark.parametrize("mode", MISSING_MODES, ids=lambda mode: mode.value)
async def test_native_action_review_composition_missing_modes(
    storage,
    own_redis,
    tmp_path,
    mode,
    request,
    record_property,
):
    case = await _case(storage, tmp_path, mode, own_redis)
    assert type(case.runtime) is FACTORIES[mode]
    assert case.body.permission_mode == "auto_review"
    assert "workspace.write" not in case.body.requested_permissions
    root = ProjectWorkspaceStore(tmp_path).session_root(
        case.context.tenant_id, case.body.project_id, case.body.workspace_session_id
    )
    target = root / case.path
    assert not target.exists()
    audit = _WriteAudit(target)
    sys.addaudithook(audit)
    invoked = []
    original = case.stack.harness_tool_gateway.invoke

    async def observe(tenant_id, request, **kwargs):
        result = await original(tenant_id, request, **kwargs)
        call = request
        if call.tool_name == "workspace.write_text":
            invoked.append((call, result))
        return result

    case.stack.harness_tool_gateway.invoke = observe
    try:
        evidence = await case.repository.load_authenticated_input(
            case.context.tenant_id, case.context.run_id
        )
        assert evidence.message == case.body.message
        assert json.loads(evidence.input_scope) == submitted_input_scope(case.body)
        events = await asyncio.wait_for(collect(case), timeout=120)
        final_delivery = _assert_final_delivery(events, mode, case.path)
        execution_observed = _assert_execution_observed(events, case.wire, mode)
        required = {
            TaskMode.DISPATCH: {CrewDispatchRuntime},
            TaskMode.DISCUSS: {AutoGenDiscussionRuntime},
            TaskMode.HYBRID: {HybridRuntime, CrewDispatchRuntime, AutoGenDiscussionRuntime},
        }[mode]
        assert required <= case.wire.actual_types
        assert (
            len(case.wire.proposals) == case.wire.reviewer_calls == len(invoked) == audit.count == 1
        )
        assert all(budget is case.wire.budgets[0] for budget in case.wire.budgets)
        budget = case.wire.budgets[0]
        assert (
            budget.run_id,
            budget.tenant_id,
            budget.evidence.turn_id,
            budget.lease.token,
            budget.lease.worker_id,
        ) == (
            case.context.run_id,
            case.context.tenant_id,
            evidence.turn_id,
            case.context.execution_token,
            case.binding["lease"].worker_id,
        )
        assert not budget._active
        assert target.read_bytes() == case.content.encode()
        digest = hashlib.sha256(target.read_bytes()).hexdigest()
        files = ProjectWorkspaceStore(tmp_path).list_files(
            case.context.tenant_id, case.body.project_id, case.body.workspace_session_id
        )
        assert [file.path for file in files] == [case.path] and files[0].sha256 == digest
        call, result = invoked[0]
        assert result.status == "succeeded" and result.failure_reason is None
        assert call.run_id == case.context.run_id and dict(call.arguments) == {
            "path": case.path,
            "content": case.content,
        }
        assert result.payload["file"]["path"] == case.path
        assert result.payload["file"]["sha256"] == digest
        async with case.factory() as session:
            attempts = list(
                await session.scalars(
                    select(RunAdmissionAttemptRow).where(
                        RunAdmissionAttemptRow.run_id == case.context.run_id
                    )
                )
            )
            reviews = list(
                await session.scalars(
                    select(RunReviewIntentRow).where(
                        RunReviewIntentRow.run_id == case.context.run_id
                    )
                )
            )
            transitions = list(
                await session.scalars(
                    select(RunReviewEventRow)
                    .where(RunReviewEventRow.run_id == case.context.run_id)
                    .order_by(RunReviewEventRow.sequence)
                )
            )
            effects = list(
                await session.scalars(
                    select(RunReviewEffectRow).where(
                        RunReviewEffectRow.run_id == case.context.run_id
                    )
                )
            )
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(RunApprovalRow)
                    .where(RunApprovalRow.run_id == case.context.run_id)
                )
                == 0
            )
        assert len(reviews) == len(effects) == 1
        review = reviews[0]
        assert review.status == "allow" and review.consumed
        assert effects[0].review_id == review.review_id
        assert [row.kind for row in transitions] == ["prepared", "issued", "terminal", "consumed"]
        assert {row.attempt_id for row in attempts} == set(case.wire.attempts)
        primary = [row for row in attempts if row.purpose == "primary"]
        reviewed = [row for row in attempts if row.purpose == "review"]
        assert len(primary) >= 1 and len(reviewed) == case.wire.reviewer_calls == 1
        for row in attempts:
            sent = case.wire.attempts[row.attempt_id]
            assert row.state == "completed" and row.request_sha256 == sent["request_sha256"]
            assert row.deployment == sent["deployment"] and row.purpose == sent["purpose"]
            assert str(row.physical_transport_id) == sent["physical_transport_id"]
            assert (
                row.worker_id == budget.lease.worker_id
                and row.worker_lease_token == budget.lease.token
            )
            _assert_accounting_receipt(row, sent)
        review_attempt = reviewed[0]
        assert all(row.deployment != review_attempt.deployment for row in primary)
        assert (review.attempt_id, review.request_sha256, review.physical_identity) == (
            review_attempt.attempt_id,
            review_attempt.request_sha256,
            review_attempt.physical_transport_id,
        )
        receipt = json.loads(review.private_receipt)
        assert hashlib.sha256(review.private_receipt.encode()).hexdigest() == review.receipt_sha256
        assert receipt["accounting_receipt_sha256"] == review_attempt.receipt_sha256
        assert receipt["evidence"]["usage"] == review_attempt.receipt["usage"]
        assert receipt["evidence"]["model_identity"] == review_attempt.deployment
        assert receipt["evidence"]["verdict"]["action_digest"] == review.action_digest
        action = json.loads(review.private_action)
        assert action["request"]["arguments"] == {"path": case.path, "content": case.content}
        assert action["normalized_effect"]["target"] == case.path
        root_identity = action["source_binding"]["root_identity"]
        actual_root = tmp_path.resolve(strict=True)
        assert root_identity == {
            "location_sha256": hashlib.sha256(
                os.path.normcase(str(actual_root)).encode()
            ).hexdigest(),
            "device": actual_root.stat().st_dev,
            "inode": actual_root.stat().st_ino,
        }
        assert review.authorization_turn_id == evidence.turn_id
        pool = await RunAdmissionRepository(case.factory).get_pool(
            case.context.tenant_id, case.context.run_id
        )
        assert pool.run_id == case.context.run_id and pool.authorization_turn_id == evidence.turn_id
        assert pool.original_token_grant == 10000000 and not pool.closed
        assert pool.token_charged == 5 * len(attempts)
        assert pool.review_token_charged == 5 and pool.review_count_charged == 1
        assert str(pool.review_cost_charged) == "0.000008"
        record = await case.repository.get(case.context.tenant_id, case.context.run_id)
        assert record.routing_decision["requested_permissions"] == ["workspace.read", "command.run"]
        assert all(response.closed for response in case.wire.bodies)
        await capacity_is_clean(case)
        assert runtime_source_fingerprint() == case.source_sha
        adapter = {
            TaskMode.DISPATCH: CrewDispatchRuntime,
            TaskMode.DISCUSS: AutoGenDiscussionRuntime,
            TaskMode.HYBRID: HybridRuntime,
        }[mode]
        # Collector input ONLY. Parent producer must bind actual native/PG nonce,
        # bundle_source_sha and clean PASS/teardown, emit the exact v1 report,
        # and link its SHA in result.mode_review_compositions[selector].
        facts = {
            "mode": mode.value,
            "phase": "review_action_composition",
            "selector": request.node.nodeid,
            "runtime_source_sha256": case.source_sha,
            "factory": type(case.runtime).__name__,
            "adapter": adapter.__name__,
            "tool": "workspace.write_text",
            "review_calls": case.wire.reviewer_calls,
            "reviewed_backend_effects": audit.count,
            "consumed_intents": int(review.consumed),
            "primary_attempts": len(primary),
            "review_attempts": len(reviewed),
            "action_digest": review.action_digest,
            "backend_write_sha256": digest,
            "review_receipt_sha256": review.receipt_sha256,
            "pool_id": str(pool.run_id),
            "run_id": str(case.context.run_id),
        }
        record_property("review_action_composition_observed", json.dumps(facts, sort_keys=True))
        record_property(
            "review_action_assertions",
            json.dumps(
                {
                    "same_original_pool": True,
                    "same_lease": True,
                    "root_bytes_match": True,
                    "result_status": result.status,
                    "final_status": events[-1].kind.value,
                    "write_count": audit.count,
                    "final_delivery": {
                        "artifact_id": str(final_delivery.id),
                        "artifact_sha256": final_delivery.content_sha256,
                        "text_sha256": hashlib.sha256(
                            final_delivery.content["text"].encode()
                        ).hexdigest(),
                        "text_bytes": len(final_delivery.content["text"].encode()),
                        "filename_sha256": hashlib.sha256(case.path.encode()).hexdigest(),
                    },
                    "execution_observed": execution_observed,
                },
                sort_keys=True,
            ),
        )
    finally:
        audit.active = False
        case.wire.release.set()
        await case.runtime.cancel_run_owned(case.context.run_id, case.context.execution_token)


@pytest.mark.parametrize("stream", [False, True], ids=["json", "sse"])
async def test_action_mode_wire_protocol_keeps_actual_native_function_call(stream):
    """Local protocol contract only; no PG/Redis/adapter/native PASS claim."""
    tool = {
        "id": "fixed",
        "type": "function",
        "function": {
            "name": "workspace_write_text",
            "arguments": json.dumps({"path": "synthetic.txt", "content": "synthetic"}),
        },
    }
    raw, media = _wire_reply({"model": "fixture-primary", "stream": stream}, tool=tool)
    if stream:
        frames = [
            json.loads(frame[6:])
            for frame in raw.decode().split("\n\n")
            if frame.startswith("data: {")
        ]
        actual = frames[0]["choices"][0]["delta"]["tool_calls"][0]
        assert actual["function"] == tool["function"]
        assert frames[-1]["choices"][0]["finish_reason"] == "tool_calls"
        assert frames[-1]["usage"]["total_tokens"] == 5
        assert media == "text/event-stream"
    else:
        reply = json.loads(raw)
        assert reply["choices"][0]["message"]["tool_calls"] == [tool]
        assert reply["choices"][0]["message"]["content"] is None
        assert reply["choices"][0]["finish_reason"] == "tool_calls"
        assert reply["usage"]["total_tokens"] == 5
        assert media == "application/json"


async def test_action_mode_protocol_does_not_fabricate_tools_or_expand_user_grant():
    assert _write_function({"tools": []}) is None
    with pytest.raises(AssertionError, match="ambiguous"):
        _write_function(
            {
                "tools": [
                    {"type": "function", "function": {"name": name}}
                    for name in ("workspace.write_text", "workspace_write_text")
                ]
            }
        )
    with pytest.raises(AssertionError):
        _wire_reply({"model": "fixture"}, text="printed fake action", tool={"id": "fake"})
    platform = fixture_platform()
    assert all(agent.skills == [] for agent in platform.agents)
    planner = DeclaredActionRoles()
    for mode in MISSING_MODES:
        plan = planner.plan(
            RolePlanningRequest(
                task="Save a plain note.",
                mode=mode,
                profile=TaskProfile.GENERAL,
                default_model="primary",
            )
        )
        assert plan.roles[0].allowed_tools == ("workspace.write_text",)
        assert plan.roles[1].allowed_tools == ()
    for mode in MISSING_MODES:
        body, _, _ = plain_note_submission()
        body = body.model_copy(update={"mode": mode, "agent_ids": ()})
        assert body.mode is mode and body.permission_mode == "auto_review"
        assert body.sandbox_profile == "workspace_write"
        assert body.requested_permissions == ("workspace.read", "command.run")


@pytest.mark.parametrize("mode", MISSING_MODES, ids=lambda mode: mode.value)
@pytest.mark.parametrize(
    "defect", ["missing", "wrong_filename", "intermediate_only", "dropped_final_event"]
)
async def test_action_mode_final_delivery_rejects_invalid_output(mode, defect):
    """Assertion contract only: synthetic events never count as native evidence."""
    events = _local_delivery_events(mode, defect=defect)
    with pytest.raises(AssertionError):
        _assert_final_delivery(events, mode, "synthetic-note.txt")


def _local_delivery_events(mode, *, defect=None):
    run_id = uuid4()
    events = []

    def emit(kind, **kwargs):
        events.append(RunEvent(kind=kind, sequence=len(events) + 1, run_id=run_id, **kwargs))

    text = "Saved the note as synthetic-note.txt."
    if defect == "wrong_filename":
        text = "Saved the note as different-note.txt."
    artifact = Artifact(
        id=uuid4(),
        type="text",
        producer={
            TaskMode.DISPATCH: "final_synthesizer",
            TaskMode.DISCUSS: "critic",
            TaskMode.HYBRID: "main",
        }[mode],
        content={"text": text},
    )
    if defect != "missing":
        if mode is TaskMode.DISCUSS:
            emit(EventKind.MESSAGE_CREATED, actor=artifact.producer, message=text)
        emit(EventKind.ARTIFACT_CREATED, artifact=artifact)
        if mode is TaskMode.DISPATCH:
            emit(
                EventKind.STEP_COMPLETED,
                step_id="final_response_step",
                actor=artifact.producer,
                inputs=(artifact,),
            )
        elif mode is TaskMode.DISCUSS:
            emit("discussion.completed", payload={"summary": text})
        else:
            # Earlier discussion text is never the Hybrid final synthesis.
            discussion = events.pop()
            emit("discussion.completed", payload={"summary": "Discussion finished."})
            emit(EventKind.ARTIFACT_CREATED, artifact=discussion.artifact)
    emit(
        EventKind.RUNTIME_COMPLETED,
        inputs=(artifact,) if mode is TaskMode.DISPATCH and defect != "missing" else (),
    )
    if defect == "intermediate_only":
        events = [
            event
            for event in events
            if event.kind not in (EventKind.STEP_COMPLETED, "discussion.completed")
        ]
    elif defect == "dropped_final_event":
        events = [event for event in events if event.kind is not EventKind.ARTIFACT_CREATED]
    return events


@pytest.mark.parametrize("mode", MISSING_MODES, ids=lambda mode: mode.value)
async def test_action_mode_final_delivery_accepts_actual_adapter_contract(mode):
    events = _local_delivery_events(mode)
    final = _assert_final_delivery(events, mode, "synthetic-note.txt")
    assert final.id == next(event.artifact.id for event in events if event.artifact)


@pytest.mark.parametrize("text", ["", "   ", "synthetic-note.txt", "Saved synthetic-note.txt.bak"])
async def test_action_mode_final_delivery_rejects_empty_or_nonreadable_text(text):
    with pytest.raises(AssertionError):
        _assert_delivery_text(text, "synthetic-note.txt")


async def test_action_mode_passive_hybrid_graph_is_not_execution():
    # Local observer counterexample only. No run(), native acceptance, or wire call.
    hybrid = object.__new__(HybridRuntime)
    hybrid._dispatch = object.__new__(CrewDispatchRuntime)
    hybrid._discussion = object.__new__(AutoGenDiscussionRuntime)
    hybrid._synthesizer = object.__new__(DirectRuntime)
    hybrid._active_child = None
    wire = ActionWire(None, "synthetic-note.txt", "synthetic")
    wire.runtime = SimpleNamespace(_active={"local-probe": SimpleNamespace(_child=hybrid)})
    assert {CrewDispatchRuntime, AutoGenDiscussionRuntime, HybridRuntime} <= {
        type(item) for item in wire.native_objects()
    }
    with pytest.raises(AssertionError, match="no executing child"):
        _executing_adapter(wire.runtime)
    # Changing only the actual active-child pointer changes which adapter may be
    # attributed to a future physical call; other constructed children stay excluded.
    for name, child in (
        ("dispatch", hybrid._dispatch),
        ("discuss", hybrid._discussion),
        ("synthesis", hybrid._synthesizer),
    ):
        hybrid._active_child = child
        stage, actual = _executing_adapter(wire.runtime)
        assert stage == name and actual is child


@pytest.mark.parametrize("stream", [False, True], ids=["json", "sse"])
@pytest.mark.parametrize("structured", [False, True], ids=["readable_final", "actual_role_schema"])
async def test_action_mode_wire_text_preserves_schema_and_filename(stream, structured):
    from agent_hub.runtime.crew.adapter import _agent_response_schema, _parse_structured_role_output
    from agent_hub.runtime.crew.plan import AgentSpec

    role = (
        DeclaredActionRoles()
        .plan(
            RolePlanningRequest(
                task="Save a plain note.",
                mode=TaskMode.DISPATCH,
                profile=TaskProfile.GENERAL,
                default_model="primary",
            )
        )
        .roles[0]
    )
    agent = AgentSpec(
        id=role.id,
        role=role.role,
        goal=role.mission,
        logical_model=role.model,
        output_schema=role.output_schema,
    )
    schema = _agent_response_schema(agent)
    assert schema is not None
    payload = {
        "model": "fixture-primary",
        "stream": stream,
        "messages": [{"role": "user", "content": "Return the bounded result."}],
    }
    if structured:
        payload["response_format"] = {"json_schema": {"schema": schema.schema}}
    wire = ActionWire(None, "synthetic-note.txt", "synthetic")
    text = wire.primary_text_for(payload)
    raw, _ = _wire_reply(payload, text=text)
    if stream:
        frame = json.loads(raw.decode().split("\n\n")[0].removeprefix("data: "))
        actual = frame["choices"][0]["delta"]["content"]
    else:
        actual = json.loads(raw)["choices"][0]["message"]["content"]
    if structured:
        value = _parse_structured_role_output(agent, actual)
        assert set(value) == set(role.output_schema)
        assert value["status"] == "done" and isinstance(value["evidence"], tuple)
        _assert_delivery_text(value["summary"], "synthetic-note.txt")
    else:
        _assert_delivery_text(actual, "synthetic-note.txt")
    selector = {"messages": [{"role": "system", "content": "Select from ['analyst', 'critic']"}]}
    assert wire.primary_text_for(selector) == "analyst"
    assert wire.primary_text_for(selector) == "critic"


@pytest.mark.parametrize("defect", ["missing_wire_stage", "missing_lifecycle", "wrong_wire_order"])
async def test_action_mode_hybrid_execution_requires_wire_and_lifecycle(defect):
    # Synthetic assertion-input contract only, never substitute these for native events.
    run = uuid4()
    artifact = Artifact(
        id=uuid4(), type="text", producer="main", content={"text": "Saved synthetic-note.txt."}
    )
    events = [
        RunEvent(
            kind=EventKind.STEP_COMPLETED,
            sequence=1,
            run_id=run,
            step_id="final_response_step",
            actor="final_synthesizer",
        ),
        RunEvent(
            kind="discussion.completed",
            sequence=2,
            run_id=run,
            payload={"summary": "Discussion finished."},
        ),
        RunEvent(kind=EventKind.ARTIFACT_CREATED, sequence=3, run_id=run, artifact=artifact),
    ]
    attempts = {
        uuid4(): {"stage": stage, "purpose": "primary", "physical_transport_id": str(uuid4())}
        for stage in ("dispatch", "discuss", "synthesis")
    }
    wire = SimpleNamespace(attempts=attempts)
    valid = _assert_execution_observed(events, wire, TaskMode.HYBRID)
    assert set(valid) == {"dispatch", "discuss", "synthesis"}
    assert all(item["lifecycle_completed"] == 1 for item in valid.values())
    if defect == "missing_wire_stage":
        attempts.pop(next(key for key, value in attempts.items() if value["stage"] == "discuss"))
    elif defect == "missing_lifecycle":
        events.pop(1)
    else:
        attempts = dict(reversed(tuple(attempts.items())))
        wire.attempts = attempts
    with pytest.raises(AssertionError):
        _assert_execution_observed(events, wire, TaskMode.HYBRID)
