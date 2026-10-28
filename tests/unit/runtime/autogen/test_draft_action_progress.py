"""Pre-capture draft observations must survive the actual action-v1 gate."""

import asyncio
import hashlib
import json
from collections.abc import Mapping
from decimal import Decimal
from uuid import uuid4

import pytest

from agent_hub.harness.types import HarnessToolCallResult
from agent_hub.models.gateway import GatewayCompletion
from agent_hub.models.types import ModelRequest, ModelResponse, TokenUsage, ToolCall
from agent_hub.runtime.artifacts import InMemoryArtifactRepository
from agent_hub.runtime.autogen.adapter import (
    AutoGenDiscussionRuntime,
    DiscussionParticipant,
    DiscussionPlan,
)
from agent_hub.runtime.autogen.engineering_delivery import (
    engineering_delivery_ready,
    engineering_progress,
)
from agent_hub.runtime.contracts import Artifact, EventKind, JsonValue, TaskContext, _mutable_json
from agent_hub.runtime.engineering_actions import required_engineering_action
from tests.unit.runtime.test_generated_draft_autogen import (
    EMPTY_HASH,
    TOOLS,
    DraftGateway,
    context,
    draft_artifact,
)

ALLOWED = {"implementer": (*TOOLS, "project.inspect")}


def action_context() -> TaskContext:
    ctx = context()
    return ctx.model_copy(update={"routing_decision": {
        **ctx.routing_decision, "engineering_action_version": 1,
        "engineering_delivery_contract_version": 1,
    }})


def append(ctx, rows, artifacts, artifact, *, status="succeeded") -> None:
    position = len(rows)
    name = artifact.content["tool_name"]
    args = artifact.content["arguments_sha256"]
    actor = artifact.producer
    rows.append({"position": position, "actor": actor, "name": name,
        "arguments_hash": args, "status": status, "replay_safe": False,
        "idempotency_key": hashlib.sha256(
            f"{ctx.run_id}:{actor}:{position}:{name}:{args}".encode()).hexdigest(),
        "artifact_id": str(artifact.id), "artifact_sha256": artifact.content_sha256})
    artifacts.append(artifact)


def result(name: str, value: dict[str, JsonValue]) -> Artifact:
    return Artifact(id=uuid4(), type="tool_result", producer="implementer", content={
        "tool_name": name, "arguments_sha256": EMPTY_HASH, "result": value,
        "call_id": f"observed-{name}",
    })


def draft_case():
    ctx = action_context()
    rows, artifacts = [], []
    append(ctx, rows, artifacts, result("project.inspect", {
        "schema_version": 1, "scope": {"tenant_id": str(ctx.tenant_id),
            "run_id": str(ctx.run_id), "project_id": "draft-project",
            "workspace_session_id": "draft-session"},
        "manifests": (), "operations": {}, "issues": (),
    }))
    append(ctx, rows, artifacts, result("workspace.write_text", {
        "path": "ACCEPTANCE.json", "sha256": "b" * 64, "size_bytes": 10,
    }))
    draft = draft_artifact(ctx)
    content = _mutable_json(draft.content)
    content["result"]["generated_draft_rejection"].update(
        reason="generated_acceptance_manifest_required", issues=["manifest_missing"])
    draft = Artifact(id=draft.id, type=draft.type, producer=draft.producer, content=content)
    append(ctx, rows, artifacts, draft, status="failed")
    return ctx, rows, artifacts


def test_valid_draft_is_failed_observation_until_actual_productive_write() -> None:
    ctx, rows, artifacts = draft_case()
    progress = engineering_progress(ctx, tool_entries=rows, artifacts=artifacts,
                                    allowed_tools=ALLOWED, completion_proposed=True)
    assert not progress.uncertain
    assert progress.inspected and progress.has_writes
    assert progress.verification_failed_current and not progress.verification_current
    assert required_engineering_action(progress) is None
    assert not engineering_delivery_ready(run_id=ctx.run_id, tool_entries=rows,
        artifacts=artifacts, allowed_tools=ALLOWED, context=ctx)
    append(ctx, rows, artifacts, result("workspace.write_text", {
        "path": "ACCEPTANCE.json", "sha256": "b" * 64, "size_bytes": 10,
    }))
    unchanged = engineering_progress(ctx, tool_entries=rows, artifacts=artifacts,
                                     allowed_tools=ALLOWED, completion_proposed=True)
    assert unchanged.verification_failed_current
    assert required_engineering_action(unchanged) is None
    append(ctx, rows, artifacts, result("workspace.write_text", {
        "path": "ACCEPTANCE.json", "sha256": "c" * 64, "size_bytes": 11,
    }))
    changed = engineering_progress(ctx, tool_entries=rows, artifacts=artifacts,
                                   allowed_tools=ALLOWED, completion_proposed=True)
    assert not changed.uncertain and not changed.verification_failed_current
    assert required_engineering_action(changed) == "project.verify"
    assert not changed.verification_current and not changed.bundle_current


@pytest.mark.parametrize("defect", [
    "actor", "tenant", "user", "run", "request", "project", "session", "producer",
    "callid", "arguments", "sha", "content_sha", "succeeded", "running", "UNKNOWN",
    "truncated", "execution_started", "phase", "extra", "duplicate",
])
def test_corrupt_draft_never_becomes_recoverable_progress(defect: str) -> None:
    ctx, rows, artifacts = draft_case()
    original = artifacts[-1]
    content = _mutable_json(original.content)
    rejection = content["result"]["generated_draft_rejection"]
    fields = {"actor": "proposer_actor", "tenant": "tenant_id", "user": "user_id",
              "run": "run_id", "request": "request_sha256", "project": "project_id",
              "session": "session_id"}
    if defect in fields:
        rejection[fields[defect]] = "f" * 64 if defect == "request" else str(uuid4())
    elif defect == "callid":
        content["call_id"] = ""
    elif defect == "arguments":
        content["arguments_sha256"] = "d" * 64
        rows[-1]["arguments_hash"] = "d" * 64
        rows[-1]["idempotency_key"] = hashlib.sha256(
            f"{ctx.run_id}:implementer:2:project.verify:{'d' * 64}".encode()).hexdigest()
    elif defect == "truncated":
        content["result"]["truncated"] = True
    elif defect in {"execution_started", "phase"}:
        rejection[defect] = True if defect == "execution_started" else "captured"
    elif defect == "extra":
        content["result"]["approval_id"] = str(uuid4())
    elif defect in {"succeeded", "running", "UNKNOWN"}:
        rows[-1]["status"] = defect
    changed = Artifact(id=original.id, type=original.type,
        producer="other" if defect == "producer" else original.producer, content=content)
    artifacts[-1] = changed
    rows[-1]["artifact_sha256"] = "f" * 64 if defect == "sha" else changed.content_sha256
    if defect == "content_sha":
        artifacts[-1] = changed.model_copy(update={"content_sha256": "f" * 64})
        rows[-1]["artifact_sha256"] = "f" * 64
    elif defect == "duplicate":
        artifacts.append(changed)
    progress = engineering_progress(ctx, tool_entries=rows, artifacts=artifacts,
                                    allowed_tools=ALLOWED, completion_proposed=True)
    assert progress.uncertain, defect
    assert required_engineering_action(progress) is None
    assert not progress.verification_current and not progress.bundle_current


async def test_real_action_v1_runtime_receives_manifest_missing_then_writes_correction() -> None:
    ctx = action_context()
    correction_seen = False

    class NativeDraftGateway(DraftGateway):
        def is_available(self, tenant_id, name):
            return tenant_id == ctx.tenant_id and name in ALLOWED["implementer"]

        async def invoke(self, tenant_id, request, *, user_id=None, role=None):
            assert tenant_id == ctx.tenant_id and user_id == ctx.actor_id and role == ctx.actor_role
            if request.tool_name == "project.verify":
                row = await super().invoke(tenant_id, request, user_id=user_id, role=role)
                payload = _mutable_json(row.payload)
                payload["generated_draft_rejection"].update(
                    reason="generated_acceptance_manifest_required", issues=["manifest_missing"])
                return HarnessToolCallResult(call_id=row.call_id, tool_name=row.tool_name,
                    status=row.status, failure_reason=row.failure_reason, payload=payload)
            self.calls.append(request)
            if request.tool_name == "project.inspect":
                payload = _mutable_json(draft_case_for_context(ctx))
            else:
                assert request.tool_name == "workspace.write_text"
                text = request.arguments["content"]
                payload = {"path": request.arguments["path"],
                           "sha256": hashlib.sha256(text.encode()).hexdigest(),
                           "size_bytes": len(text.encode())}
            return HarnessToolCallResult(call_id=request.call_id, tool_name=request.tool_name,
                                         status="succeeded", payload=payload)

    class Model:
        def __init__(self):
            self.requests = []
            self.stage = 0

        async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
            nonlocal correction_seen
            self.requests.append(request)
            if request.logical_model == "selector":
                response = ModelResponse(text="implementer", usage=TokenUsage(1, 1, 2))
            else:
                stage = self.stage
                self.stage += 1
                assert stage < 4, "no blind verify loop or fabricated completion"
                names = ("project.inspect", "workspace.write_text", "project.verify", "workspace.write_text")
                name = names[stage]
                if stage == 3:
                    for message in request.messages:
                        prefix = "UNTRUSTED_CAPABILITY_RESULTS_JSON="
                        if message.role == "user" and str(message.content).startswith(prefix):
                            for item in json.loads(message.content[len(prefix):]):
                                try:
                                    value = json.loads(item["content"].removeprefix("Error: "))
                                except json.JSONDecodeError:
                                    continue
                                if "generated_draft_rejection" in value:
                                    assert item["is_error"] is True
                                    assert value["generated_draft_rejection"]["issues"] == ["manifest_missing"]
                                    correction_seen = True
                    assert correction_seen, "action-v1 must expose the scoped failed draft to the next model"
                arguments = ({"path": "ACCEPTANCE.json" if stage == 3 else "package.json",
                              "content": "corrected manifest" if stage == 3 else "initial source"}
                             if name == "workspace.write_text" else {})
                response = ModelResponse(text=None, tool_calls=(ToolCall(id=f"actual-{stage}",
                    name=request.required_tool or name.replace(".", "_"), arguments=arguments),),
                    usage=TokenUsage(1, 1, 2))
            return GatewayCompletion(response=response, deployment_id="fixture",
                logical_model=request.logical_model, provider_id="fixture",
                provider_model="fixture/model", cost_usd=Decimal(0))

    gateway, model = NativeDraftGateway(ctx), Model()
    runner = AutoGenDiscussionRuntime(model, DiscussionPlan(participants=(
        DiscussionParticipant(id="implementer", role="Implementer", goal="Correct draft",
                              logical_model="general", allowed_tools=ALLOWED["implementer"]),
        DiscussionParticipant(id="reviewer", role="Reviewer", goal="Review", logical_model="general"),
    ), selector_model="selector", max_turns=8, token_budget=500), capability_gateway=gateway,
        harness_tool_gateway=gateway, artifact_repository=InMemoryArtifactRepository())
    events = []
    stream = runner.run(ctx)
    try:
        async with asyncio.timeout(10):
            async for event in stream:
                events.append(event)
                if (event.kind is EventKind.TOOL_COMPLETED and event.tool_name == "workspace.write_text"
                        and sum(call.tool_name == "workspace.write_text" for call in gateway.calls) == 2):
                    break
    finally:
        await stream.aclose()
    assert correction_seen, [(event.kind, event.reason) for event in events[-3:]]
    assert [call.tool_name for call in gateway.calls] == [
        "project.inspect", "workspace.write_text", "project.verify", "workspace.write_text"]
    assert all(call.run_id == ctx.run_id for call in gateway.calls)
    assert not any(event.kind is EventKind.RUNTIME_COMPLETED for event in events)
    checkpoints = [event.checkpoint for event in events if event.checkpoint is not None]
    assert any(checkpoint.state.get("engineering_tool_prefix") == 3 for checkpoint in checkpoints)


def draft_case_for_context(ctx: TaskContext) -> Mapping[str, JsonValue]:
    return {"schema_version": 1, "scope": {"tenant_id": str(ctx.tenant_id),
        "run_id": str(ctx.run_id), "project_id": "draft-project", "workspace_session_id": "draft-session"},
        "manifests": (), "operations": {}, "issues": ()}
