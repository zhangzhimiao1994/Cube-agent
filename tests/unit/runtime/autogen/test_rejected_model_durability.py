"""Rejected output is a durable stopped outcome, never a completion or free retry."""
import asyncio
from collections.abc import Mapping
from dataclasses import replace
from decimal import Decimal
from typing import Any, NoReturn, cast
from uuid import UUID, uuid4

import pytest

from agent_hub.models.gateway import (
    GatewayCompletion,
    GatewayNegotiatedRejectedOutput,
    GatewayRejectedOutput,
)
from agent_hub.models.types import (
    ModelMessage,
    ModelRequest,
    RejectedOutputEvidence,
    RejectedUsageStatus,
    TokenUsage,
)
from agent_hub.runtime.autogen.adapter import AutoGenDiscussionRuntime, _DiscussionDurability
from agent_hub.runtime.autogen.termination import DiscussionUsage
from agent_hub.runtime.contracts import Artifact, EventKind, JsonValue, RuntimeCheckpoint
from tests.unit.runtime.autogen.test_engineering_completion import Capabilities, plan
from tests.unit.runtime.autogen.test_final_delivery_runtime import ReportGateway, report_context

SECRET_TEXT = "DO_NOT_PERSIST_REJECTED_BODY_OR_TOOL_ARGUMENTS"


class RejectedGateway:
    def __init__(self, evidence: RejectedOutputEvidence | None, *, cost: Decimal | None = Decimal("0.002"),
                 negotiated: bool = False) -> None:
        self.evidence, self.cost, self.negotiated = evidence, cost, negotiated
        self.requests: list[ModelRequest] = []

    async def complete_with_context(self, request: ModelRequest) -> NoReturn:
        self.requests.append(request)
        error_type = GatewayNegotiatedRejectedOutput if self.negotiated else GatewayRejectedOutput
        raise error_type(evidence=self.evidence, logical_model=request.logical_model,
            deployment_id="shared", provider_id="test", provider_model="test/model", cost_usd=self.cost)


@pytest.mark.parametrize("kind", ["completed", "incomplete", "refused", "unknown", "missing", "invalid", "absent", "negotiated"])
async def test_rejected_output_is_checkpointed_and_cold_restore_never_repeats_call(kind: str) -> None:
    usage_known = kind not in {"missing", "invalid", "absent"}
    evidence = None if kind == "absent" else RejectedOutputEvidence(
        final_text=SECRET_TEXT if kind in {"completed", "missing", "invalid", "negotiated"} else None,
        usage=TokenUsage(7, 3, 10) if usage_known else None,
        usage_status=cast(RejectedUsageStatus, kind) if kind in {"missing", "invalid"} else "known",
        status="incomplete" if kind == "incomplete" else "refused" if kind == "refused"
               else "unknown" if kind == "unknown" else "completed",
        reason="invalid_tool" if kind not in {"incomplete", "refused"} else "incomplete" if kind == "incomplete" else "refusal",
    )
    gateway = RejectedGateway(evidence, cost=None if not usage_known else Decimal("0.002"),
                              negotiated=kind == "negotiated")
    ctx, runtime_plan, backend = report_context(), replace(plan(), max_turns=16), Capabilities()
    runtime = AutoGenDiscussionRuntime(gateway, runtime_plan, capability_gateway=backend)
    events = [event async for event in runtime.run(ctx)]
    assert events[-1].kind is EventKind.RUNTIME_FAILED
    assert len(gateway.requests) == 1 and backend.calls == []
    checkpoint = await runtime.save_checkpoint()
    rows = cast(tuple[Mapping[str, JsonValue], ...], checkpoint.state["model_ledger"])
    assert rows[-1]["status"] == "rejected", "received rejection must not remain an unknown running call"
    record = cast(Mapping[str, JsonValue], rows[-1]["rejection"])
    assert record["evidence_present"] is (evidence is not None)
    assert record["usage_status"] == ("missing" if evidence is None else evidence.usage_status)
    assert record["cost_usd"] == ("0.002" if usage_known else None)
    assert SECRET_TEXT not in str(checkpoint.to_payload())
    assert cast(Mapping[str, JsonValue], checkpoint.state["usage"])["tokens"] == (10 if usage_known else 0)
    assert not any(event.kind is EventKind.RUNTIME_COMPLETED or event.artifact is not None for event in events)
    assert events[-1].reason and "Traceback" not in events[-1].reason

    other = RejectedGateway(evidence)
    restored = AutoGenDiscussionRuntime(other, runtime_plan, capability_gateway=backend,
                                       artifact_repository=runtime._repository)
    await restored.restore_checkpoint(checkpoint)
    resumed = [event async for event in restored.run(ctx.model_copy(update={"checkpoint": checkpoint}))]
    assert resumed[-1].kind is EventKind.RUNTIME_FAILED
    assert resumed[-1].reason == events[-1].reason
    assert other.requests == [] and backend.calls == []
    assert (await restored.save_checkpoint()).state["usage"] == checkpoint.state["usage"]


async def test_rejected_final_report_cannot_promote_existing_verified_bundle_to_success() -> None:
    class RejectReport(ReportGateway):
        async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
            if any("Main Agent final delivery" in str(message.content) for message in request.messages
                   if message.role == "system"):
                self.requests.append(request)
                self.final_requests.append(request)
                raise GatewayRejectedOutput(evidence=RejectedOutputEvidence(final_text=SECRET_TEXT,
                    usage=TokenUsage(7, 3, 10), usage_status="known", status="completed", reason="invalid_json"),
                    logical_model=request.logical_model, deployment_id="shared", provider_id="test",
                    provider_model="test/model", cost_usd=Decimal("0.002"))
            return await super().complete_with_context(request)

    gateway, backend = RejectReport(), Capabilities()
    ctx, runtime_plan = report_context(), replace(plan(), max_turns=16)
    runtime = AutoGenDiscussionRuntime(gateway, runtime_plan, capability_gateway=backend)
    events = [event async for event in runtime.run(ctx)]
    assert events[-1].kind is EventKind.RUNTIME_FAILED
    assert len(gateway.final_requests) == 1
    assert backend.calls == ["workspace.write_text", "project.verify", "workspace.bundle"]
    assert not any(event.kind is EventKind.RUNTIME_COMPLETED or event.kind == "discussion.completed"
                   or event.artifact and event.artifact.content.get("presentation") == "final_answer"
                   for event in events)
    saved = await runtime.save_checkpoint()
    assert SECRET_TEXT not in str(saved.to_payload())
    for _ in range(2):
        unused = RejectReport()
        restored = AutoGenDiscussionRuntime(unused, runtime_plan, capability_gateway=backend,
                                           artifact_repository=runtime._repository)
        await restored.restore_checkpoint(saved)
        resumed = [event async for event in restored.run(ctx.model_copy(update={"checkpoint": saved}))]
        assert resumed[-1].kind is EventKind.RUNTIME_FAILED
        assert resumed[-1].reason == events[-1].reason and not unused.requests
        assert len(backend.calls) == 3
        assert (await restored.save_checkpoint()).state["usage"] == saved.state["usage"]


@pytest.mark.parametrize("fault", ["version", "version_bool", "position_bool", "tenant", "run",
    "request", "usage_bool", "usage_sum", "negative_cost", "nan_cost", "provenance", "fallback",
    "extra_text", "accepted_mix", "artifact_mix", "later_row"])
async def test_tampered_rejection_is_rejected_before_any_call(fault: str) -> None:
    evidence = RejectedOutputEvidence(final_text=None, usage=TokenUsage(7, 3, 10),
        usage_status="known", status="completed", reason="invalid_tool")
    gateway, backend = RejectedGateway(evidence), Capabilities()
    ctx, runtime_plan = report_context(), replace(plan(), max_turns=16)
    runtime = AutoGenDiscussionRuntime(gateway, runtime_plan, capability_gateway=backend)
    assert [event async for event in runtime.run(ctx)][-1].kind is EventKind.RUNTIME_FAILED
    payload = cast(dict[str, Any], (await runtime.save_checkpoint()).to_payload())
    row = payload["state"]["model_ledger"][-1]
    record = row["rejection"]
    if fault == "version":
        record["schema_version"] = 2
    elif fault == "version_bool":
        record["schema_version"] = True
    elif fault == "position_bool":
        record["position"] = False
    elif fault in {"tenant", "run"}:
        record[fault + "_id"] = str(uuid4())
    elif fault == "request":
        record["request_hash"] = "a" * 64
    elif fault == "usage_bool":
        record["usage"]["prompt_tokens"] = True
    elif fault == "usage_sum":
        record["usage"]["total_tokens"] = 11
    elif fault in {"negative_cost", "nan_cost"}:
        record["cost_usd"] = "-0.1" if fault == "negative_cost" else "NaN"
    elif fault == "provenance":
        record["provenance"]["provider_model"] = "another/model"
    elif fault == "fallback":
        record["fallback_used"] = True
    elif fault == "extra_text":
        record["final_text"] = SECRET_TEXT
    elif fault == "accepted_mix":
        row["status"] = "succeeded"
    elif fault == "artifact_mix":
        row.update(artifact_id=str(uuid4()), artifact_sha256="a" * 64)
    else:
        payload["state"]["model_ledger"].append({"status": "running", "request_hash": "a" * 64})
    payload["state_sha256"] = ""
    modified = RuntimeCheckpoint.from_payload(payload)
    other = RejectedGateway(evidence)
    restored = AutoGenDiscussionRuntime(other, runtime_plan, capability_gateway=backend,
                                       artifact_repository=runtime._repository)
    await restored.restore_checkpoint(modified)
    events = [event async for event in restored.run(ctx.model_copy(update={"checkpoint": modified}))]
    assert events[-1].kind is EventKind.RUNTIME_FAILED
    # Evidence admission now validates the receipt before artifact hydration.
    assert events[-1].reason == "runtime checkpoint is incompatible"
    assert not other.requests and not backend.calls


@pytest.mark.parametrize("cancel", [False, True])
async def test_rejection_ack_failure_keeps_running_unknown_and_rolls_back_usage(cancel: bool) -> None:
    async def publish(artifacts, models, tools):  # type: ignore[no-untyped-def]
        if models[-1]["status"] == "rejected":
            if cancel:
                raise asyncio.CancelledError
            raise OSError("simulated checkpoint commit failure")

    async def store(artifact: Artifact) -> UUID:
        raise AssertionError("rejection must not create an accepted artifact")

    async def abort(write_id: UUID) -> bool:
        raise AssertionError("no artifact write is expected")

    def finalize(write_id: UUID) -> None:
        raise AssertionError("no accepted result is expected")

    durability = _DiscussionDurability(publish, store, abort, finalize,
                                       run_id=uuid4(), tenant_id=uuid4())
    request = ModelRequest(logical_model="shared", messages=(ModelMessage(role="user", content="build"),))
    assert await durability.before_model(request) is None
    usage = DiscussionUsage(tokens=5, cost_usd=Decimal("0.01"))
    error = GatewayRejectedOutput(evidence=RejectedOutputEvidence(final_text=None, usage=TokenUsage(7, 3, 10),
        usage_status="known", status="completed", reason="invalid_tool"), logical_model="shared",
        deployment_id="shared", provider_id="test", provider_model="test/model", cost_usd=Decimal("0.002"))
    with pytest.raises(asyncio.CancelledError if cancel else OSError):
        await durability.reject_model(request, error, usage=usage, tool_names={})
    assert durability.model_entries == [{"status": "running", "request_hash": durability.request_hash(request)}]
    assert usage.tokens == 5 and usage.cost_usd == Decimal("0.01")
    assert durability.failure_reason == "model_outcome_uncertain" and not durability._model_lock.locked()
