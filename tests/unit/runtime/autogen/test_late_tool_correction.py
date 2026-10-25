"""An admitted tool rejection owns one correction, never another soft window."""

import asyncio
from collections.abc import AsyncGenerator, Mapping
from dataclasses import dataclass, replace
from decimal import Decimal
from typing import cast

import pytest

from agent_hub.models.gateway import GatewayCompletion, GatewayRejectedOutput
from agent_hub.models.types import ModelRequest, RejectedOutputEvidence, TokenUsage
from agent_hub.runtime.autogen.adapter import (
    GatewayChatCompletionClient,
    ModelOutcomeUncertain,
    RuntimeExecutionError,
    _DiscussionDurability,
)
from agent_hub.runtime.autogen.model_rejection import RejectedModelRecord, validate_rejection
from agent_hub.runtime.autogen.model_tool_correction import tool_correction_request
from agent_hub.runtime.autogen.termination import DiscussionUsage
from agent_hub.runtime.contracts import Artifact, EventKind, JsonValue, RunEvent
from agent_hub.runtime.failure_reason import runtime_failure_diagnostic_from_reason
from tests.unit.runtime.autogen.test_soft_model_admission import Journey
from tests.unit.runtime.autogen.test_tool_correction_client import (
    PRIVATE,
    TOOLS,
    Journal,
    Model,
    request,
)


class LateCorrectionJourney(Journey):
    def __init__(self, *, late: bool = True, soft_admission: JsonValue = 1) -> None:
        super().__init__(policy=soft_admission)
        self.context = self.context.model_copy(update={"routing_decision": {
            **self.context.routing_decision, "engineering_tool_correction_version": 1,
        }})
        self.late = late
        self.rejected = False
        self.admitted_deadline = 0.0
        self.admitted_absolute = 0.0
        self.corrected_deadline: float | None = None

    async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
        if request.required_tool == "workspace_list":
            budget = self.runner._wall_budget
            assert budget is not None
            if not self.rejected:
                self.requests.append(request)
                self.rejected = True
                self.admitted_deadline = budget.deadline
                self.admitted_absolute = budget.absolute_deadline
                assert budget.remaining(now=asyncio.get_running_loop().time()) > 0
                if self.late:
                    await self.cross_soft()
                raise GatewayRejectedOutput(
                    evidence=RejectedOutputEvidence(final_text=PRIVATE,
                        usage=TokenUsage(7, 3, 10), usage_status="known",
                        status="completed", reason="invalid_tool"),
                    logical_model=request.logical_model, deployment_id="shared",
                    provider_id="test", provider_model="test/model", cost_usd=None,
                )
            self.corrected_deadline = budget.deadline
            assert budget.absolute_deadline == self.admitted_absolute
            assert 0 < request.timeout_seconds <= budget.absolute_remaining(
                now=asyncio.get_running_loop().time()) + 0.01
        return await super().complete_with_context(request)


async def collect(case: Journey, *, delay_correction_ack: bool = False) -> list[RunEvent]:
    events: list[RunEvent] = []
    stream = cast(AsyncGenerator[RunEvent, None], case.runner.run(case.context))
    try:
        async with asyncio.timeout(5):
            async for event in stream:
                events.append(event)
                if delay_correction_ack and event.checkpoint is not None:
                    rows = event.checkpoint.state["model_ledger"]
                    if (isinstance(rows, tuple) and rows and isinstance(rows[-1], Mapping)
                            and rows[-1].get("status") == "running"
                            and "correction_for" in rows[-1]):
                        await case.cross_soft()
    finally:
        await stream.aclose()
    return events


def assert_wall_failure(event: RunEvent) -> None:
    assert event.kind is EventKind.RUNTIME_FAILED
    assert event.reason == "engineering_delivery_missing"
    assert event.payload["termination_reason"] == "wall_time"
    assert event.payload["error_code"] == "runtime.discussion_budget_exhausted"
    assert event.payload["error_stage"] == "runtime_budget"
    assert event.payload["error_category"] == "budget_exhausted"
    assert event.payload["retryable"] is False
    assert event.payload["runtime_timeout_kind"] == "soft"
    assert event.payload["remaining_soft_timeout_seconds"] == 0.0
    assert "compression_trigger" not in event.payload
    RunEvent.from_payload(event.to_payload())


async def test_late_admitted_rejection_corrects_same_tool_once_without_extending_soft() -> None:
    case = LateCorrectionJourney()
    events = await collect(case)
    attempts = [request for request in case.requests if request.required_tool == "workspace_list"]
    assert len(attempts) == 2, "the admitted request must finish its one same-tool correction"
    original, corrected = attempts
    assert corrected.tools == original.tools
    assert corrected.required_tool == original.required_tool == "workspace_list"
    assert corrected.logical_model == original.logical_model
    assert corrected.messages != original.messages
    assert 0 < corrected.max_output_tokens <= original.max_output_tokens - 10
    assert 0 < corrected.timeout_seconds <= original.timeout_seconds
    assert case.corrected_deadline == case.admitted_deadline
    assert PRIVATE not in str(corrected.messages)
    rows = next(event.checkpoint.state["model_ledger"] for event in events
        if event.checkpoint is not None and isinstance(event.checkpoint.state["model_ledger"], tuple)
        and any(isinstance(row, Mapping) and "correction_for" in row and row["status"] == "succeeded"
                for row in event.checkpoint.state["model_ledger"]))
    assert isinstance(rows, tuple)
    assert isinstance(rows[-1], Mapping) and isinstance(rows[-2], Mapping)
    assert rows[-1]["correction_for"] == len(rows) - 2
    assert rows[-2]["status"] == "rejected"
    assert PRIVATE not in str(rows)


@pytest.mark.parametrize("prior_reason", [
    "model gateway failed: model response rejected (reason=invalid_tool; status=completed; usage_status=known)",
    "model response incomplete",
])
async def test_soft_expiry_after_rejection_emits_budget_diagnostic_without_validation_error(
    monkeypatch: pytest.MonkeyPatch, prior_reason: str,
) -> None:
    case = LateCorrectionJourney(late=False, soft_admission=None)
    prior = runtime_failure_diagnostic_from_reason(prior_reason)
    safe: dict[str, JsonValue] = {
        "gateway_scope_phase": "pretransport_capacity",
        "gateway_scope_reason": "capacity_backend_failure",
        "gateway_scope_transport_entered_count": 0,
        "gateway_scope_failure_attempt_count": 0,
        "capacity_operation": "initialize", "capacity_backend_reason": "timeout",
        "capacity_exception_category": "builtin_timeout",
        "capacity_timeout_origin": "local_deadline", "capacity_commit_state": "unknown",
    }
    # Validate fixture input before checkpoint waits and deadline cancellation begin.
    validated_prior = RunEvent(kind=EventKind.RUNTIME_FAILED, sequence=1,
        run_id=case.context.run_id, reason=prior_reason, payload={**prior, **safe})
    # Exercise the real deadline/failure path with a valid retained diagnostic.
    # Only the durability payload input is controlled; admission and AutoGen run normally.
    before_model = _DiscussionDurability.before_model

    async def retain_prior_failure(
        self: _DiscussionDurability, request: ModelRequest, *,
        tool_names: Mapping[str, str] | None = None, bounded_no_tool: bool = False,
        report_binding: Mapping[str, JsonValue] | None = None, correction_for: int | None = None,
    ) -> GatewayCompletion | RejectedModelRecord | None:
        result = await before_model(self, request, tool_names=tool_names,
            bounded_no_tool=bounded_no_tool, report_binding=report_binding, correction_for=correction_for)
        if correction_for is not None:
            self.failure_reason = prior_reason
            self.failure_payload = validated_prior.payload
        return result

    monkeypatch.setattr(_DiscussionDurability, "before_model", retain_prior_failure)
    # The correction intent has not been acknowledged when the legacy soft timer fires.
    events = await collect(case, delay_correction_ack=True)
    assert len([r for r in case.requests if r.required_tool == "workspace_list"]) == 1
    assert_wall_failure(events[-1])
    assert {key: events[-1].payload[key] for key in safe} == safe


@dataclass
class Admission:
    soft_open: bool = True
    hard_seconds: float = 5.0
    token_budget: int = 100
    cost_budget: Decimal = Decimal("0.01")
    checks: int = 0

    def admit(self) -> None:
        self.checks += 1
        if not self.soft_open:
            raise RuntimeExecutionError("soft admission exhausted")

    def remaining(self, usage: DiscussionUsage) -> tuple[int, float]:
        return (max(0, self.token_budget - usage.tokens)
                if usage.cost_usd < self.cost_budget else 0, self.hard_seconds)


class LateModel(Model):
    def __init__(self, journal: Journal, admission: Admission, *, kind: str = "invalid_tool",
                 always_reject: bool = False, exhausted: str | None = None) -> None:
        super().__init__(journal, kind=kind, always_reject=always_reject)
        self.admission, self.exhausted = admission, exhausted

    async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
        try:
            return await super().complete_with_context(request)
        except GatewayRejectedOutput:
            self.admission.soft_open = False
            if self.exhausted == "absolute":
                self.admission.hard_seconds = 0.0
            elif self.exhausted == "token":
                self.admission.token_budget = 10
            elif self.exhausted == "cost":
                self.admission.cost_budget = Decimal(0)
            if self.kind == "unknown":
                raise GatewayRejectedOutput(evidence=None, logical_model=request.logical_model,
                    deployment_id="shared", provider_id="test", provider_model="test/model") from None
            raise


def late_client(model: Model, durable: _DiscussionDurability, usage: DiscussionUsage,
                admission: Admission, *, enabled: bool = True) -> GatewayChatCompletionClient:
    return GatewayChatCompletionClient(model, "shared", usage, durability=durable,
        supports_tool_calls=True, tool_correction_enabled=True,
        admitted_tool_correction=enabled,
        engineering_budget=lambda: admission.remaining(usage),
        request_timeout=lambda: admission.hard_seconds, model_admission=admission.admit)


async def test_late_correction_consumes_original_admission_and_cannot_admit_fresh_request() -> None:
    journal, usage, admission = Journal(), DiscussionUsage(), Admission()
    model = LateModel(journal, admission)
    durable = journal.durability()
    client = late_client(model, durable, usage, admission)
    result, counted = await client._complete_model_request(request(), TOOLS, None)
    assert result.response.tool_calls[0].name == "workspace_list"
    assert admission.checks == 2, "only the original intent and transport need soft admission"
    assert len(model.requests) == 2 and usage.tokens == 13
    assert counted.prompt_tokens == 9 and counted.completion_tokens == 4
    assert usage.cost_usd == Decimal("0.001")
    assert model.requests[1].max_output_tokens == 90
    assert model.requests[1].timeout_seconds == 5.0
    with pytest.raises(RuntimeExecutionError, match="soft admission exhausted"):
        await client._complete_model_request(request(), TOOLS, None)
    assert len(model.requests) == 2 and len(durable.model_entries) == 2
    assert usage.tokens == 13 and admission.checks == 3


@pytest.mark.parametrize("kind", ["incomplete", "refusal", "missing", "negotiated", "unknown"])
async def test_late_ineligible_rejection_cannot_use_continued_admission(kind: str) -> None:
    journal, usage, admission = Journal(), DiscussionUsage(), Admission()
    model = LateModel(journal, admission, kind=kind)
    durable = journal.durability()
    with pytest.raises(RuntimeExecutionError, match="model response rejected|negotiation exhausted"):
        await late_client(model, durable, usage, admission)._complete_model_request(request(), TOOLS, None)
    assert len(model.requests) == 1 and admission.checks == 2
    assert [row["status"] for row in durable.model_entries] == ["rejected"]
    assert usage.tokens == (0 if kind in {"missing", "unknown"} else 10)


@pytest.mark.parametrize("exhausted", ["absolute", "token", "cost"])
async def test_continued_admission_cannot_bypass_hard_or_accounting_budget(exhausted: str) -> None:
    journal, usage, admission = Journal(), DiscussionUsage(), Admission()
    model = LateModel(journal, admission, exhausted=exhausted)
    durable = journal.durability()
    with pytest.raises(RuntimeExecutionError, match="model response rejected"):
        await late_client(model, durable, usage, admission)._complete_model_request(request(), TOOLS, None)
    assert len(model.requests) == 1 and usage.tokens == 10
    assert len(durable.model_entries) == 1 and not durable._model_lock.locked()


async def test_second_late_rejection_cannot_inherit_a_third_attempt() -> None:
    journal, usage, admission = Journal(), DiscussionUsage(), Admission()
    model = LateModel(journal, admission, always_reject=True)
    durable = journal.durability()
    with pytest.raises(RuntimeExecutionError, match="model response rejected"):
        await late_client(model, durable, usage, admission)._complete_model_request(request(), TOOLS, None)
    assert len(model.requests) == 2 and usage.tokens == 20 and admission.checks == 2
    assert [row["status"] for row in durable.model_entries] == ["rejected", "rejected"]
    assert durable.model_entries[1]["correction_for"] == 0 and not journal.artifacts


async def test_client_without_opt_in_still_requires_soft_admission_for_correction() -> None:
    journal, usage, admission = Journal(), DiscussionUsage(), Admission()
    model = LateModel(journal, admission)
    with pytest.raises(RuntimeExecutionError, match="soft admission exhausted"):
        await late_client(model, journal.durability(), usage, admission,
            enabled=False)._complete_model_request(request(), TOOLS, None)
    assert len(model.requests) == 1 and usage.tokens == 10


async def test_unadmitted_original_request_never_obtains_correction_allowance() -> None:
    journal, usage, admission = Journal(), DiscussionUsage(), Admission(soft_open=False)
    model = LateModel(journal, admission)
    durable = journal.durability()
    with pytest.raises(RuntimeExecutionError, match="soft admission exhausted"):
        await late_client(model, durable, usage, admission)._complete_model_request(request(), TOOLS, None)
    assert not model.requests and not durable.model_entries and usage.tokens == 0


async def test_unknown_original_cannot_reenter_provider_or_correct_after_soft_expiry() -> None:
    journal, usage, admission = Journal(), DiscussionUsage(), Admission(soft_open=False)
    original = journal.durability()
    await original.before_model(request(), tool_names=TOOLS)
    original.fail_model()
    restored = journal.durability(models=tuple(dict(row) for row in journal.snapshots[-1]))
    model = Model(journal, forbidden=True)
    with pytest.raises(ModelOutcomeUncertain, match="model outcome is uncertain"):
        await late_client(model, restored, usage, admission)._complete_model_request(request(), TOOLS, None)
    assert not model.requests and usage.tokens == 0 and admission.checks == 0
    assert restored.model_entries[0]["status"] == "running"


async def test_absolute_expiry_during_correction_intent_ack_blocks_transport() -> None:
    admission = Admission()

    class SlowAck(Journal):
        async def publish(self, artifacts: tuple[Artifact, ...],
                          models: tuple[dict[str, JsonValue], ...],
                          tools: tuple[dict[str, JsonValue], ...]) -> None:
            await super().publish(artifacts, models, tools)
            if models[-1].get("correction_for") == 0:
                admission.hard_seconds = 0.0

    journal, usage = SlowAck(), DiscussionUsage()
    model = LateModel(journal, admission)
    durable = journal.durability()
    with pytest.raises(RuntimeExecutionError, match="discussion model deadline exhausted"):
        await late_client(model, durable, usage, admission)._complete_model_request(request(), TOOLS, None)
    assert len(model.requests) == 1 and usage.tokens == 10
    assert admission.checks == 2 and not durable._model_lock.locked()
    assert [row["status"] for row in durable.model_entries] == ["rejected", "running"]


@pytest.mark.parametrize("field", ["run_id", "tenant_id", "position", "request_hash"])
async def test_continued_admission_rejects_receipt_scope_or_request_binding_mismatch(field: str) -> None:
    class CorruptReceipt(Journal):
        async def publish(self, artifacts: tuple[Artifact, ...],
                          models: tuple[dict[str, JsonValue], ...],
                          tools: tuple[dict[str, JsonValue], ...]) -> None:
            await super().publish(artifacts, models, tools)
            if models[-1].get("status") == "rejected":
                receipt = models[-1]["rejection"]
                assert isinstance(receipt, dict)
                receipt[field] = (1 if field == "position" else "0" * 64 if field == "request_hash"
                    else "00000000-0000-0000-0000-000000000000")

    journal, usage, admission = CorruptReceipt(), DiscussionUsage(), Admission()
    model = LateModel(journal, admission)
    durable = journal.durability()
    with pytest.raises(RuntimeExecutionError, match="tool correction admission mismatch"):
        await late_client(model, durable, usage, admission)._complete_model_request(request(), TOOLS, None)
    assert len(model.requests) == 1 and len(durable.model_entries) == 1
    assert usage.tokens == 10 and admission.checks == 2


async def test_running_correction_stays_unknown_after_soft_expiry() -> None:
    journal, usage, admission = Journal(), DiscussionUsage(), Admission()
    model = LateModel(journal, admission)
    await late_client(model, journal.durability(), usage, admission)._complete_model_request(request(), TOOLS, None)
    intent = next(rows for rows in journal.snapshots
        if rows[-1].get("correction_for") == 0 and rows[-1]["status"] == "running")
    restored = journal.durability(models=tuple(dict(row) for row in intent))
    forbidden = Model(journal, forbidden=True)
    with pytest.raises(ModelOutcomeUncertain, match="model outcome is uncertain"):
        await late_client(forbidden, restored, usage, admission)._complete_model_request(request(), TOOLS, None)
    assert not forbidden.requests and usage.tokens == 13
    assert [row["status"] for row in restored.model_entries] == ["rejected", "running"]


@pytest.mark.parametrize("changed", ["tool", "schema", "messages", "model", "tokens", "timeout"])
async def test_continued_admission_rejects_any_change_to_the_exact_correction(changed: str) -> None:
    journal, usage, admission = Journal(), DiscussionUsage(), Admission()
    model = LateModel(journal, admission)
    durable = journal.durability()
    with pytest.raises(RuntimeExecutionError, match="soft admission exhausted"):
        await late_client(model, durable, usage, admission,
            enabled=False)._complete_model_request(request(), TOOLS, None)
    original = request()
    receipt = durable.model_entries[0]["rejection"]
    assert isinstance(receipt, Mapping)
    record = validate_rejection(receipt, run_id=journal.run_id, tenant_id=journal.tenant_id,
        position=0, request_hash=durable.model_entries[0]["request_hash"])
    corrected = tool_correction_request(original, record, max_output_tokens=90, timeout_seconds=5.0)
    if changed == "tool":
        corrected = replace(corrected, required_tool="workspace_write_text",
            tools=(replace(corrected.tools[0], name="workspace_write_text"),))
    elif changed == "schema":
        corrected = replace(corrected, tools=(replace(corrected.tools[0],
            parameters={"type": "object", "additionalProperties": True}),))
    elif changed == "messages":
        corrected = replace(corrected, messages=original.messages)
    elif changed == "model":
        corrected = replace(corrected, logical_model="other")
    elif changed == "tokens":
        corrected = replace(corrected, max_output_tokens=101)
    else:
        corrected = replace(corrected, timeout_seconds=11.0)
    with pytest.raises(RuntimeExecutionError, match="tool correction admission mismatch"):
        await late_client(model, durable, usage, admission)._complete_model_attempt(
            corrected, TOOLS, None, correction_for=0, admitted_request=original)
    assert len(model.requests) == 1 and len(durable.model_entries) == 1 and usage.tokens == 10
