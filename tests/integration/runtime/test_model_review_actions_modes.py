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
from collections.abc import Mapping
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
from agent_hub.runtime.contracts import Artifact, EventKind, RunEvent, RuntimeCheckpoint
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
                    output_schema={"summary": "string", "evidence": "string[]"},
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


def _hybrid_discussion_completion(events):
    """Hybrid publishes child terminal checkpoints and stage advances, not discussion.completed."""
    boundaries = [event for event in events if event.kind is EventKind.CHECKPOINT_SAVED
        and event.checkpoint is not None and event.checkpoint.runtime_type == "hybrid"
        and event.checkpoint.state.get("terminal") is False
        and event.checkpoint.state.get("child_checkpoint") is None]
    assert [event.checkpoint.state.get("next_stage") for event in boundaries] == [1, 2, 3]
    dispatch_end, discussion_end, synthesis_end = boundaries
    starts = [event for event in events if event.kind is EventKind.DISCUSSION_STARTED]
    assert len(starts) == 1
    start = starts[0]
    terminals = [event for event in events if event.kind is EventKind.CHECKPOINT_SAVED
        and event.checkpoint is not None and event.checkpoint.runtime_type == "hybrid"
        and event.checkpoint.state.get("next_stage") == 1
        and isinstance(event.checkpoint.state.get("child_checkpoint"), Mapping)
        and event.checkpoint.state["child_checkpoint"].get("runtime_type") == "autogen"
        and event.checkpoint.state["child_checkpoint"].get("state", {}).get("terminal") is True]
    assert len(terminals) == 1, "missing actual terminal AutoGen child checkpoint"
    terminal = terminals[0]
    assert dispatch_end.sequence < start.sequence < terminal.sequence < discussion_end.sequence
    assert discussion_end.sequence < synthesis_end.sequence
    messages = [event for event in events if event.kind is EventKind.MESSAGE_CREATED
        and start.sequence < event.sequence < terminal.sequence]
    outputs = [event for event in events if event.kind is EventKind.ARTIFACT_CREATED
        and event.artifact is not None and event.artifact.type == "text"
        and event.artifact.producer in start.participants
        and start.sequence < event.sequence < terminal.sequence]
    assert messages and outputs, "missing executed discussion output"
    message, output = messages[-1], outputs[-1]
    assert message.actor == output.artifact.producer
    assert message.message == output.artifact.content.get("text")
    assert message.sequence < output.sequence < terminal.sequence
    for checkpoint_event in (terminal, discussion_end):
        registry = checkpoint_event.checkpoint.state.get("output_artifact_registry", {})
        assert registry.get(str(output.artifact.id)) == output.artifact.content_sha256
    syntheses = [event for event in events if event.kind is EventKind.ARTIFACT_CREATED
        and event.artifact is not None and event.artifact.type == "text"
        and event.artifact.producer == "main"
        and discussion_end.sequence < event.sequence < synthesis_end.sequence]
    assert len(syntheses) == 1, "missing executed synthesis output"
    final = syntheses[0].artifact
    assert synthesis_end.checkpoint.state["output_artifact_registry"].get(str(final.id)) == final.content_sha256
    return discussion_end


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
    _assert_delivery_text(final.content.get("text"), path)
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
        completion = _hybrid_discussion_completion(events)
        assert completion.sequence < final_event.sequence
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
    discussions = ([_hybrid_discussion_completion(events)] if mode is TaskMode.HYBRID else
        [event for event in events if event.kind == "discussion.completed"])
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


def _assert_action_review_independence(primary, review_attempt, scope, wire_attempts, *, actor, action_digest):
    """Compare actual issued primaries at this action, never later unrelated calls."""
    from agent_hub.models.physical_budget import _same_model
    from agent_hub.models.run_admission_repository import DeploymentIdentity

    def identity(value):
        return DeploymentIdentity(**dict(value))

    assert scope["actor"] == actor and scope["action_digest"] == action_digest
    assert scope["review_attempt_id"] == review_attempt.attempt_id
    before = set(scope["primary_attempt_ids"])
    assert before and scope["proposal_attempt_id"] in before
    issued = {row.attempt_id: row for row in primary}
    assert before <= issued.keys()
    assert all(wire_attempts[key]["purpose"] == "primary" for key in before)
    proposal = wire_attempts[scope["proposal_attempt_id"]]
    assert proposal["logical_model"] in (scope["actor_model"], *scope["actor_fallbacks"])
    acting = {identity(issued[key].deployment) for key in before}
    assert acting == {identity(value) for value in scope["acting_models"]}
    assert {wire_attempts[key]["wire_model"] for key in before} == set(scope["acting_wire_models"])
    reviewed = identity(review_attempt.deployment)
    assert all(not _same_model(reviewed, previous) for previous in acting)
    assert wire_attempts[review_attempt.attempt_id]["purpose"] == "review"
    assert wire_attempts[review_attempt.attempt_id]["wire_model"] not in scope["acting_wire_models"]


class ActionWire(FixedWire):
    """No primary/Gateway/adapter replacement: fixed *last* SDK HTTP response."""

    def __init__(self, body, path, content):
        super().__init__()
        self.body, self.path, self.content = body, path, content
        self.proposals = []
        self.actual_types = set()
        self.review_scopes = []

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
        contracts = [entry for entry in self.budgets[-1]._trusted_contexts
                     if entry.deployment_id == meta.deployment_id]
        assert len(contracts) == 1
        self.attempts[attempt.permit.attempt_id] = {
            "logical_model": contracts[0].logical_model,
            "wire_model": payload["model"],
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
            actor = material["exact_action"]["request"]["agent_id"]
            participants = adapter._plan.agents if type(adapter) is CrewDispatchRuntime else adapter._plan.participants
            matches = [entry for entry in participants if entry.id == actor]
            assert len(matches) == 1 and "workspace.write_text" in matches[0].allowed_tools
            assert len(self.proposals) == 1 and self.budgets[-1].config.require_distinct_model
            acting = self.budgets[-1]
            self.review_scopes.append({
                "actor": actor, "actor_model": matches[0].logical_model,
                "actor_fallbacks": tuple(getattr(matches[0], "fallback_models", ())),
                "action_digest": material["action_digest"],
                "proposal_attempt_id": self.proposals[0], "review_attempt_id": attempt.permit.attempt_id,
                "primary_attempt_ids": tuple(key for key, value in self.attempts.items() if value["purpose"] == "primary"),
                "acting_models": tuple({"provider_id": value.provider_id, "deployment_id": value.deployment_id,
                                        "model_id": value.model_id} for value in acting.acting_models),
                "acting_wire_models": tuple(acting.acting_wire_models),
            })
            assert self.attempts[self.proposals[0]]["stage"] == stage
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


def _action_review_config():
    return ApprovalReviewConfig(
        engine="rules_then_model",
        require_distinct_model=True,
        reviewer_logical_model="reviewer",
        max_input_tokens=1000000,
        max_output_tokens=512,
        max_cost_usd_per_run="100.00",
    )


async def _case(storage, tmp_path, mode, redis_client):
    """New action config fixture, existing PG/Redis ownership and actual factories."""
    factory = storage[0]
    _, binding, _ = await prepare(storage, open_now=False)
    repository = RunRepository(factory)
    platform = fixture_platform()
    config = _action_review_config()

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
        assert len(case.wire.review_scopes) == 1 and action["request"]["agent_id"] == call.actor
        _assert_action_review_independence(primary, review_attempt, case.wire.review_scopes[0],
            case.wire.attempts, actor=call.actor, action_digest=review.action_digest)
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
        if mode is TaskMode.HYBRID:
            # Synthetic assertion inputs mirror the real public checkpoint schema.
            discussion_artifact = Artifact(id=uuid4(), type="text", producer="critic",
                content={"text": "Discussion finished."})
            registry = {str(discussion_artifact.id): discussion_artifact.content_sha256}
            def boundary(stage, child=None):
                checkpoint = RuntimeCheckpoint(id=uuid4(), runtime_type="hybrid", runtime_version="3",
                    run_id=run_id, tenant_id=run_id, mode=mode, state={"terminal": False,
                        "next_stage": stage, "child_checkpoint": child,
                        "output_artifact_registry": dict(registry)})
                emit(EventKind.CHECKPOINT_SAVED, checkpoint=checkpoint)
            emit(EventKind.STEP_COMPLETED, step_id="final_response_step", actor="final_synthesizer")
            boundary(1)
            emit(EventKind.DISCUSSION_STARTED, actor="analyst", session_id=str(run_id),
                participants=("analyst", "critic"))
            emit(EventKind.MESSAGE_CREATED, actor="critic", message="Discussion finished.")
            emit(EventKind.ARTIFACT_CREATED, artifact=discussion_artifact)
            child = RuntimeCheckpoint(id=uuid4(), runtime_type="autogen", runtime_version="1",
                run_id=run_id, tenant_id=run_id, mode=TaskMode.DISCUSS, state={"terminal": True})
            boundary(1, child.to_payload())
            boundary(2)
            emit(EventKind.ARTIFACT_CREATED, artifact=artifact)
            registry[str(artifact.id)] = artifact.content_sha256
            boundary(3)
        else:
            if mode is TaskMode.DISCUSS:
                emit(EventKind.MESSAGE_CREATED, actor=artifact.producer, message=text)
            emit(EventKind.ARTIFACT_CREATED, artifact=artifact)
            if mode is TaskMode.DISPATCH:
                emit(EventKind.STEP_COMPLETED, step_id="final_response_step",
                    actor=artifact.producer, inputs=(artifact,))
            else:
                emit("discussion.completed", payload={"summary": text})
    emit(
        EventKind.RUNTIME_COMPLETED,
        inputs=(artifact,) if mode is TaskMode.DISPATCH and defect != "missing" else (),
    )
    if defect == "intermediate_only":
        events = [
            event
            for event in events
            if event.kind not in (EventKind.STEP_COMPLETED, EventKind.CHECKPOINT_SAVED, "discussion.completed")
        ]
    elif defect == "dropped_final_event":
        events = [event for event in events if event.kind is not EventKind.ARTIFACT_CREATED]
    return events


@pytest.mark.parametrize("mode", MISSING_MODES, ids=lambda mode: mode.value)
async def test_action_mode_final_delivery_accepts_actual_adapter_contract(mode):
    events = _local_delivery_events(mode)
    final = _assert_final_delivery(events, mode, "synthetic-note.txt")
    assert final.id == [event.artifact.id for event in events if event.artifact][-1]


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
    events = _local_delivery_events(TaskMode.HYBRID)
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


def _local_declared_mode_plan(tmp_path, mode):
    from agent_hub.capabilities.runtime import RuntimeCapabilityGateway
    from agent_hub.runtime.contracts import TaskContext
    from agent_hub.runtime.defaults import _assign_models_to_roles, _role_tools_by_id

    body, _, _ = plain_note_submission()
    context = TaskContext(run_id=uuid4(), tenant_id=uuid4(), mode=mode, request=body.message,
        timeout_seconds=180, token_budget=1000000, routing_decision={
            "project_id": body.project_id, "workspace_session_id": body.workspace_session_id,
            "sandbox_profile": body.sandbox_profile, "permission_mode": body.permission_mode,
            "requested_permissions": list(body.requested_permissions), "execution_backend": body.execution_backend})
    backend = RuntimeCapabilityGateway(skill_store_dir=tmp_path / "skills", project_workspace_dir=tmp_path)
    platform = fixture_platform()
    declared = DeclaredActionRoles().plan(RolePlanningRequest(task=context.request, mode=mode,
        profile=TaskProfile.GENERAL, default_model="primary")).roles
    assigned = _assign_models_to_roles(declared, platform, default_model="primary", task=context.request,
        role_tools_by_id=_role_tools_by_id(declared, context, capability_gateway=backend))
    return context, backend, platform, assigned


@pytest.mark.parametrize("mode", (TaskMode.DISPATCH, TaskMode.HYBRID), ids=lambda mode: mode.value)
async def test_action_mode_declarations_construct_current_dispatch_handoffs(tmp_path, mode):
    from agent_hub.runtime.crew.plan import DispatchPlan
    from agent_hub.runtime.defaults import _dispatch_plan

    context, backend, platform, roles = _local_declared_mode_plan(tmp_path, mode)
    plan = _dispatch_plan(roles, context, config=platform, capability_gateway=backend)
    assert type(plan) is DispatchPlan
    dependent = {source for step in plan.steps for source in step.depends_on}
    agents = {agent.id: agent for agent in plan.agents}
    assert all(agents[step.agent].output_schema for step in plan.steps if step.id in dependent)
    assert "workspace.write_text" in agents["analyst"].allowed_tools
    assert agents["critic"].allowed_tools == ()
    from agent_hub.runtime.crew.adapter import _agent_response_schema, _parse_structured_role_output

    for agent in (agents["analyst"], agents["critic"]):
        schema = _agent_response_schema(agent)
        assert schema is not None
        wire = ActionWire(None, "synthetic-note.txt", "synthetic")
        payload = {"model": "fixture-primary", "messages": [{"role": "user", "content": "Return bounded evidence."}],
                   "response_format": {"json_schema": {"schema": schema.schema}}}
        text = wire.primary_text_for(payload)
        raw, _ = _wire_reply(payload, text=text)
        actual = json.loads(raw)["choices"][0]["message"]["content"]
        parsed = _parse_structured_role_output(agent, actual)
        assert set(parsed) == set(agent.output_schema)
        _assert_delivery_text(parsed["summary"], "synthetic-note.txt")



async def test_action_mode_current_discussion_actor_binding_allows_only_scoped_review(tmp_path):
    from agent_hub.runtime.defaults import _discussion_plan

    context, backend, _platform, roles = _local_declared_mode_plan(tmp_path, TaskMode.DISCUSS)
    plan = _discussion_plan(roles, "primary", context, capability_gateway=backend)
    actors = {participant.id: participant for participant in plan.participants}
    assert actors["analyst"].logical_model == "primary"
    assert actors["critic"].logical_model == "reviewer"
    assert "workspace.write_text" in actors["analyst"].allowed_tools and not actors["critic"].allowed_tools
    from agent_hub.auth.models import Role
    from agent_hub.capabilities.execution_authority import (
        bind_execution_authority,
        get_verification_authority,
    )
    from agent_hub.capabilities.model_review import _ReviewAdmission
    from agent_hub.models import physical_budget as budget_api
    from agent_hub.models.physical_admission import (
        _PhysicalAdmissionStopped,
        _PhysicalAttemptMetadata,
    )
    from agent_hub.runtime.contracts import TaskContext
    from tests.unit.capabilities.test_model_review import ReviewStore, snapshot
    from tests.unit.models.test_physical_budget import configured, logical_request

    budget, store, writer, reviewer = configured(config=_action_review_config())
    binding = TaskContext(run_id=budget.run_id, tenant_id=budget.tenant_id, mode=TaskMode.DISCUSS,
        request=budget.evidence.message, actor_id=budget.evidence.actor_id, actor_role=Role.OPERATOR,
        execution_token=budget.lease.token, routing_decision=context.routing_decision)
    wire_attempts = {}
    async def issue_primary(deployment):
        admission = budget_api._BudgetAdmission(budget)
        selected, _request, selection = admission.select(deployment,
            logical_request(logical_model=deployment.logical_model, max_output_tokens=100), None)
        meta = _PhysicalAttemptMetadata(selected.id, selected.provider_model, selection.request_sha256,
            "primary", "chat_completions", False, 1, selection)
        permit = await admission.before_send(meta, deadline=budget.deadline)
        identity = {"provider_id": budget_api._identity(selected).provider_id,
                    "deployment_id": selected.id, "model_id": selected.provider_model}
        row = SimpleNamespace(attempt_id=permit.attempt_id, deployment=identity, purpose="primary")
        wire_attempts[row.attempt_id] = {"deployment": identity, "purpose": "primary",
            "logical_model": selected.logical_model, "wire_model": budget_api._wire_model(selected)}
        return row

    proposal = await issue_primary(writer)
    with bind_execution_authority(binding, worker_id=budget.lease.worker_id, actor=actors["analyst"].id):
        current = get_verification_authority(tenant_id=budget.tenant_id, run_id=budget.run_id,
            user_id=budget.evidence.actor_id, role=Role.OPERATOR)
        scope = {"actor": current.actor, "actor_model": actors[current.actor].logical_model,
            "actor_fallbacks": (), "action_digest": "a" * 64, "proposal_attempt_id": proposal.attempt_id,
            "primary_attempt_ids": tuple(wire_attempts),
            "acting_models": tuple({"provider_id": d.provider_id, "deployment_id": d.deployment_id,
                                     "model_id": d.model_id} for d in budget.acting_models),
            "acting_wire_models": tuple(budget.acting_wire_models)}
    review = _ReviewAdmission(budget, ReviewStore(store), snapshot(budget))
    review.select(reviewer, logical_request(logical_model="reviewer", max_output_tokens=100), None)
    with pytest.raises(_PhysicalAdmissionStopped):
        review.select(writer, logical_request(max_output_tokens=100), None)
    review_row = SimpleNamespace(attempt_id=uuid4(), deployment={
        "provider_id": budget_api._identity(reviewer).provider_id, "deployment_id": reviewer.id,
        "model_id": reviewer.provider_model})
    wire_attempts[review_row.attempt_id] = {"deployment": review_row.deployment, "purpose": "review",
                                         "wire_model": budget_api._wire_model(reviewer)}
    scope["review_attempt_id"] = review_row.attempt_id
    later = await issue_primary(reviewer)  # Actual current factory assigns the critic to this alias.
    with pytest.raises(_PhysicalAdmissionStopped):
        review.select(reviewer, logical_request(logical_model="reviewer", max_output_tokens=100), None)
    _assert_action_review_independence([proposal, later], review_row, scope, wire_attempts,
        actor=current.actor, action_digest="a" * 64)


async def test_action_mode_fixture_enforces_distinct_model_policy():
    assert _action_review_config().require_distinct_model is True


async def test_action_mode_real_hybrid_events_match_delivery_contract(tmp_path, record_property):
    """Real factories/adapters/SDK wire; local lifecycle proof, no action/PG proof."""
    from agent_hub.runtime.contracts import TaskContext
    from tests.unit.runtime.test_configured_runtime import ImmediateCapacity

    platform = fixture_platform()
    context = TaskContext(run_id=uuid4(), tenant_id=uuid4(), mode=TaskMode.HYBRID,
        request="Return a bounded note summary naming synthetic-note.txt.",
        timeout_seconds=30, token_budget=1000000)
    artifacts = InMemoryArtifactRepository()
    text_wire = ActionWire(None, "synthetic-note.txt", "synthetic")
    stages, actual_adapters, child_events = [], [], []

    class Configs:
        async def get_current(self, tenant):
            assert tenant == context.tenant_id
            return SimpleNamespace(version=1, document=platform.model_dump(mode="json"))

    async def capacity(tenant, deployments):
        assert tenant == context.tenant_id
        return ImmediateCapacity(deployments)

    async def wire(request):
        stage, adapter = _executing_adapter(runtime)
        stages.append(stage)
        actual_adapters.append(type(adapter))
        payload = json.loads(request.content)
        raw, content_type = _wire_reply(payload, text=text_wire.primary_text_for(payload))
        return httpx.Response(200, headers={"content-type": content_type}, content=raw)

    # Observe original AutoGen output without replacing or manufacturing events.
    original_run = AutoGenDiscussionRuntime.run
    async def observe_discussion(adapter, child_context):
        async for event in original_run(adapter, child_context):
            child_events.append(event)
            yield event

    # Real production construction remains untouched.
    runtime = FACTORIES[TaskMode.HYBRID](config_service=Configs(),
        secret_service=FixtureSecrets(), capacity_factory=capacity,
        transport=LiteLLMClient(_network_delegate_factory=lambda: httpx.MockTransport(wire)),
        artifact_repository=artifacts, role_planner=DeclaredActionRoles())
    # Capture the factory's real AutoGen stream at its public method boundary.
    from unittest.mock import patch
    with patch.object(AutoGenDiscussionRuntime, "run", observe_discussion):
        events = [event async for event in runtime.run(context)]
    assert set(actual_adapters) == {CrewDispatchRuntime, AutoGenDiscussionRuntime, DirectRuntime}
    assert set(stages) == {"dispatch", "discuss", "synthesis"}
    assert stages == sorted(stages, key=("dispatch", "discuss", "synthesis").index)
    assert len([event for event in child_events if event.kind == "discussion.completed"]) == 1
    assert child_events[-1].kind is EventKind.RUNTIME_COMPLETED
    record_property("local_hybrid_event_contract", json.dumps({
        "wire_stages": stages, "events": [
            {"kind": str(event.kind), "sequence": event.sequence,
             "next_stage": event.checkpoint.state.get("next_stage") if event.checkpoint else None}
            for event in events]}, sort_keys=True))
    final = _assert_final_delivery(events, TaskMode.HYBRID, "synthetic-note.txt")
    # Corrupt only observed outputs after the real run; never feed fabricated events to runtime.
    terminal_child = next(event for event in events if event.checkpoint is not None
        and isinstance(event.checkpoint.state.get("child_checkpoint"), Mapping)
        and event.checkpoint.state["child_checkpoint"].get("runtime_type") == "autogen"
        and event.checkpoint.state["child_checkpoint"]["state"].get("terminal") is True)
    discussion_end = _hybrid_discussion_completion(events)
    for removed in (terminal_child, discussion_end):
        with pytest.raises(AssertionError):
            _assert_final_delivery([event for event in events if event is not removed],
                TaskMode.HYBRID, "synthetic-note.txt")
    final_event = next(event for event in events if event.artifact is final)
    with pytest.raises(AssertionError):
        _assert_final_delivery([event for event in events if event is not final_event],
            TaskMode.HYBRID, "synthetic-note.txt")
    with pytest.raises(ValueError, match="artifact text must be nonblank"):
        Artifact(id=final.id, type="text", producer="main", content={"text": ""})
    corrupted = Artifact(id=final.id, type="text", producer="main",
        content={"text": "Saved different-note.txt."})
    with pytest.raises(AssertionError, match="exact saved filename"):
        _assert_final_delivery([event.model_copy(update={"artifact": corrupted})
            if event is final_event else event for event in events],
            TaskMode.HYBRID, "synthetic-note.txt")
