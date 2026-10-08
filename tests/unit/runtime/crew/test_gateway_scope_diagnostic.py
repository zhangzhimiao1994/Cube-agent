"""Zero-provider Crew failure diagnostics through the real runtime boundary."""

from __future__ import annotations

import json
from collections.abc import Mapping
from decimal import Decimal
from typing import Literal
from uuid import UUID

import pytest

from agent_hub.auth.models import Role
from agent_hub.domain.runs import TaskMode
from agent_hub.models.failure_receipt import (
    GatewayFailureAttempt,
    get_gateway_failure_receipt,
)
from agent_hub.models.gateway import (
    GatewayCompletion,
    GatewayScopeDiagnostic,
    ScopeIncompletePhase,
    ScopeIncompleteReason,
    _GatewayFailureHistory,
    get_gateway_scope_diagnostic,
)
from agent_hub.models.litellm_client import ModelTransportError
from agent_hub.models.types import ModelRequest, ModelResponse, TokenUsage, ToolCall
from agent_hub.runtime.artifacts import InMemoryArtifactRepository
from agent_hub.runtime.contracts import (
    EventKind,
    JsonValue,
    RunEvent,
    RuntimeCheckpoint,
    TaskContext,
)
from agent_hub.runtime.crew.adapter import (
    CrewAgentDefinition,
    CrewDispatchRuntime,
    CrewLLMBridge,
    CrewTaskDefinition,
    RuntimeExecutionError,
    _gateway_scope_failure,
    _gateway_scope_payload,
    _GatewayScopeContractFailure,
    _GatewayScopeFailure,
    _ModelContractFailed,
)
from agent_hub.runtime.crew.plan import AgentSpec, DispatchPlan, DispatchStep
from agent_hub.runtime.failure_reason import runtime_failure_diagnostic_from_reason

RUN_ID = UUID("00000000-0000-4000-8000-000000000002")
TENANT_ID = UUID("00000000-0000-4000-8000-000000000001")
RAW_BODY = "private-provider-body api_key=fixture-never-export"
SCOPE_FIELDS = frozenset(
    {
        "gateway_scope_phase",
        "gateway_scope_reason",
        "gateway_scope_transport_entered_count",
        "gateway_scope_failure_attempt_count",
    }
)
type FailureMode = Literal["deadline", "capacity", "provider", "forged", "wrong_binding"]


class _Generation:
    async def execute(
        self,
        step_id: str,
        prompt: str,
        bridge: CrewLLMBridge,
        *,
        agent_id: str | None = None,
        storage_scope: tuple[UUID, UUID],
    ) -> str:
        del step_id, agent_id, storage_scope
        return await bridge.complete([{"role": "user", "content": prompt}])


class _Factory:
    def __init__(self, generation: _Generation | None = None) -> None:
        self.generation = generation if generation is not None else _Generation()

    def build(
        self,
        agents: tuple[CrewAgentDefinition, ...],
        tasks: tuple[CrewTaskDefinition, ...],
        *,
        share_crew: bool,
        telemetry_disabled: bool,
    ) -> _Generation:
        del agents, tasks, share_crew
        assert telemetry_disabled
        return self.generation


class _ForgedGeneration(_Generation):
    def __init__(self, copied: bool) -> None:
        reason = "model gateway failed: model transport failed (status=408)"
        self.error = _GatewayScopeFailure(
            reason,
            GatewayScopeDiagnostic(
                ScopeIncompletePhase.OUTER_DEADLINE,
                ScopeIncompleteReason.DEADLINE_EXHAUSTED,
                0,
                0,
            ),
        )
        if copied:
            issued = _gateway_scope_failure(reason, _failure("deadline"))
            assert _gateway_scope_payload(issued)
            self.error.__dict__.update(issued.__dict__)

    async def execute(
        self,
        step_id: str,
        prompt: str,
        bridge: CrewLLMBridge,
        *,
        agent_id: str | None = None,
        storage_scope: tuple[UUID, UUID],
    ) -> str:
        del step_id, prompt, bridge, agent_id, storage_scope
        raise self.error


def _failure(mode: FailureMode, request: ModelRequest | None = None) -> ModelTransportError:
    error = ModelTransportError(RAW_BODY, status_code=408)
    history = _GatewayFailureHistory()
    if mode in {"deadline", "capacity", "wrong_binding"}:
        phase = (
            ScopeIncompletePhase.PRETRANSPORT_CAPACITY
            if mode == "capacity"
            else ScopeIncompletePhase.OUTER_DEADLINE
        )
        reason = (
            ScopeIncompleteReason.CAPACITY_UNAVAILABLE
            if mode == "capacity"
            else ScopeIncompleteReason.DEADLINE_EXHAUSTED
        )
        history.mark_incomplete(phase, reason)
        history.attach_diagnostic(error)
        assert get_gateway_scope_diagnostic(error) is not None
        if mode == "wrong_binding":
            other = ModelTransportError(RAW_BODY, status_code=408)
            other.__dict__.update(error.__dict__)
            return other
    elif mode == "forged":
        error.__dict__["_gateway_scope_diagnostic"] = (
            object(),
            error,
            GatewayScopeDiagnostic(
                ScopeIncompletePhase.OUTER_DEADLINE,
                ScopeIncompleteReason.DEADLINE_EXHAUSTED,
                0,
                0,
            ),
        )
    else:
        if request is not None:
            history.entered_count = 1
            history.attempted_logical_models.append(request.logical_model)
            history.attempts.append(
                GatewayFailureAttempt(
                    ordinal=1,
                    logical_model=request.logical_model,
                    deployment_id="owned",
                    provider_id="owned",
                    provider_model="owned/fixture",
                    outcome="transport_error",
                    status_code=408,
                )
            )
            history.attach(error, request)
            receipt = get_gateway_failure_receipt(error)
            assert receipt is not None and receipt.history_complete
        assert history.diagnostic() is None
    return error


class _Gateway:
    def __init__(self, mode: FailureMode) -> None:
        self.mode = mode
        self.requests: list[ModelRequest] = []

    async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
        self.requests.append(request)
        raise _failure(self.mode, request)


class _Capabilities:
    def __init__(self) -> None:
        self.calls = 0

    async def execute(
        self,
        *,
        tenant_id: UUID,
        run_id: UUID,
        actor: str,
        name: str,
        arguments: Mapping[str, JsonValue],
        idempotency_key: str,
    ) -> Mapping[str, JsonValue]:
        del tenant_id, run_id, actor, arguments, idempotency_key
        assert name == "web.search"
        self.calls += 1
        return {"items": ("owned fixture result",)}

    def is_replay_safe(self, name: str) -> bool:
        del name
        return False


class _ToolGateway(_Gateway):
    def __init__(self, recover: bool) -> None:
        super().__init__("deadline")
        self.recover = recover
        self.completions: list[GatewayCompletion] = []

    async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
        self.requests.append(request)
        index = len(self.requests)
        if index == 2 or (not self.recover and index > 2):
            raise _failure(self.mode, request)
        response = (
            ModelResponse(
                text=None,
                tool_calls=(
                    ToolCall(
                        id="owned-call",
                        name="web_search",
                        arguments={"q": "owned fixture"},
                    ),
                ),
                usage=TokenUsage(1, 1, 2),
            )
            if index in {1, 3}
            else ModelResponse(text="Owned final answer", usage=TokenUsage(1, 1, 2))
        )
        completion = GatewayCompletion(
            response=response,
            deployment_id="owned",
            logical_model=request.logical_model,
            provider_id="owned",
            provider_model="owned/fixture",
            cost_usd=Decimal("0.01"),
        )
        self.completions.append(completion)
        return completion


class _ReviewGateway(_Gateway):
    async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
        self.requests.append(request)
        if len(self.requests) > 1:
            raise _failure(self.mode, request)
        return GatewayCompletion(
            response=ModelResponse(text="Owned worker answer", usage=TokenUsage(1, 1, 2)),
            deployment_id="owned",
            logical_model=request.logical_model,
            provider_id="owned",
            provider_model="owned/fixture",
            cost_usd=Decimal("0.01"),
        )


def _runtime(
    gateway: _Gateway,
    capabilities: _Capabilities | None = None,
    repository: InMemoryArtifactRepository | None = None,
    *,
    factory: _Factory | None = None,
    reviewer_retries: int | None = None,
) -> CrewDispatchRuntime:
    tools = ("web.search",) if capabilities is not None else ()
    agents: tuple[AgentSpec, ...] = (
        AgentSpec(
            id="writer",
            role="writer",
            goal="Write",
            logical_model="general",
            allowed_tools=tools,
        ),
    )
    if reviewer_retries is not None:
        agents += (
            AgentSpec(id="reviewer", role="reviewer", goal="Review", logical_model="general"),
        )
    return CrewDispatchRuntime(
        gateway,
        DispatchPlan(
            agents=agents,
            steps=(
                DispatchStep(
                    id="final",
                    agent="writer",
                    task="Answer",
                    token_budget=100,
                    cost_budget_usd=Decimal(1),
                    final_synthesizer=True,
                    tools=tools,
                    reviewer="reviewer" if reviewer_retries is not None else None,
                    reviewer_retries=reviewer_retries if reviewer_retries is not None else 0,
                ),
            ),
            total_token_budget=100,
            total_cost_usd=Decimal(1 if reviewer_retries is None else 2 + 2 * reviewer_retries),
            allowed_tools=tools,
        ),
        crew_factory=factory if factory is not None else _Factory(),
        capability_gateway=capabilities,
        artifact_repository=repository,
    )


def _context(checkpoint: RuntimeCheckpoint | None = None) -> TaskContext:
    return TaskContext(
        run_id=RUN_ID,
        tenant_id=TENANT_ID,
        mode=TaskMode.DISPATCH,
        request="Owned diagnostic fixture",
        token_budget=1000,
        actor_id=UUID("00000000-0000-4000-8000-000000000003"),
        actor_role=Role.OPERATOR,
        checkpoint=checkpoint,
    )


async def _failed_events(
    runtime: CrewDispatchRuntime,
    checkpoint: RuntimeCheckpoint | None = None,
) -> tuple[list[RunEvent], RuntimeExecutionError]:
    events: list[RunEvent] = []
    with pytest.raises(RuntimeExecutionError) as captured:
        async for event in runtime.run(_context(checkpoint)):
            events.append(event)
    return events, captured.value


@pytest.mark.parametrize("mode", ["deadline", "capacity"])
async def test_issuer_scope_survives_step_and_runtime_failure(
    mode: FailureMode,
    caplog: pytest.LogCaptureFixture,
) -> None:
    gateway = _Gateway(mode)
    events, error = await _failed_events(_runtime(gateway))
    terminals = [
        event for event in events if event.kind in {EventKind.STEP_FAILED, EventKind.RUNTIME_FAILED}
    ]
    assert [event.kind for event in terminals] == [EventKind.STEP_FAILED, EventKind.RUNTIME_FAILED]
    for event in terminals:
        expected = {
            "gateway_scope_phase": "pretransport_capacity"
            if mode == "capacity"
            else "outer_deadline",
            "gateway_scope_reason": "capacity_unavailable"
            if mode == "capacity"
            else "deadline_exhausted",
            "gateway_scope_transport_entered_count": 0,
            "gateway_scope_failure_attempt_count": 0,
        }
        assert {key: event.payload.get(key) for key in SCOPE_FIELDS} == expected
        assert event.reason == "model gateway failed: model transport failed (status=408)"
    failed = terminals[0]
    assert failed.payload["error_code"] == "model.provider_transient_failed"
    assert failed.payload["recovery_status"] == "failed_after_compact_retry"
    assert failed.payload["recovery_attempts"] == 1
    assert len(gateway.requests) == 2
    public = json.dumps([event.to_payload() for event in events], default=str)
    assert RAW_BODY not in public + str(error) + repr(error) + caplog.text
    assert error.__cause__ is None
    assert error.__context__ is None


@pytest.mark.parametrize("mode", ["provider", "forged", "wrong_binding"])
async def test_missing_or_forged_issuer_has_no_scope_fields(mode: FailureMode) -> None:
    assert get_gateway_scope_diagnostic(_failure(mode)) is None
    gateway = _Gateway(mode)
    events, error = await _failed_events(_runtime(gateway))
    assert len(gateway.requests) == 2
    for event in events:
        assert not SCOPE_FIELDS.intersection(event.payload)
    assert RAW_BODY not in str(error) + repr(error)


def test_runtime_failure_contract_accepts_fixed_scope_fields() -> None:
    payload = runtime_failure_diagnostic_from_reason(
        "model gateway failed: model transport failed (status=408)"
    )
    payload.update(
        gateway_scope_phase="outer_deadline",
        gateway_scope_reason="deadline_exhausted",
        gateway_scope_transport_entered_count=0,
        gateway_scope_failure_attempt_count=0,
    )
    event = RunEvent(
        run_id=RUN_ID,
        sequence=1,
        kind=EventKind.RUNTIME_FAILED,
        reason="model gateway failed: model transport failed (status=408)",
        payload=payload,
    )
    assert event.payload["gateway_scope_phase"] == "outer_deadline"


async def test_diagnostic_does_not_change_failed_ledger_or_terminal_restore_calls() -> None:
    gateway = _Gateway("deadline")
    runtime = _runtime(gateway)
    _, _ = await _failed_events(runtime)
    assert len(gateway.requests) == 2
    checkpoint = await runtime.save_checkpoint()
    models = checkpoint.state["models"]
    assert isinstance(models, Mapping)
    for state in models.values():
        assert isinstance(state, Mapping)
        assert not SCOPE_FIELDS.intersection(state)
        assert state["status"] == "failed"
    restored_gateway = _Gateway("deadline")
    restored = _runtime(restored_gateway)
    await restored.restore_checkpoint(checkpoint)
    _, _ = await _failed_events(restored, checkpoint)
    assert restored_gateway.requests == []


def _event(kind: EventKind, fields: Mapping[str, JsonValue]) -> RunEvent:
    payload: dict[str, JsonValue] = dict(runtime_failure_diagnostic_from_reason("runtime_failed"))
    payload.update(fields)
    return RunEvent(
        run_id=RUN_ID,
        sequence=1,
        kind=kind,
        reason="runtime_failed",
        payload=payload,
        step_id="final" if kind is EventKind.STEP_FAILED else None,
        actor="writer" if kind is EventKind.STEP_FAILED else None,
    )


def _valid_fields() -> dict[str, JsonValue]:
    return {
        "gateway_scope_phase": "outer_deadline",
        "gateway_scope_reason": "deadline_exhausted",
        "gateway_scope_transport_entered_count": 1,
        "gateway_scope_failure_attempt_count": 0,
    }


@pytest.mark.parametrize("kind", [EventKind.STEP_FAILED, EventKind.RUNTIME_FAILED])
@pytest.mark.parametrize("scope", [False, True])
def test_legacy_and_scoped_failures_roundtrip(kind: EventKind, scope: bool) -> None:
    event = _event(kind, _valid_fields() if scope else {})
    assert RunEvent.from_payload(event.to_payload()).to_payload() == event.to_payload()
    assert RunEvent.model_validate_json(event.model_dump_json()).to_payload() == event.to_payload()


@pytest.mark.parametrize("kind", [EventKind.STEP_FAILED, EventKind.RUNTIME_FAILED])
@pytest.mark.parametrize("entry", ["constructor", "payload", "json"])
@pytest.mark.parametrize(
    "changes",
    [
        {"gateway_scope_phase": "invented"},
        {"gateway_scope_reason": "invented"},
        {"gateway_scope_phase": ("outer_deadline",)},
        {"gateway_scope_reason": ScopeIncompleteReason.DEADLINE_EXHAUSTED},
        {"gateway_scope_transport_entered_count": True},
        {"gateway_scope_failure_attempt_count": False},
        {"gateway_scope_transport_entered_count": -1},
        {"gateway_scope_failure_attempt_count": -1},
        {"gateway_scope_transport_entered_count": 65},
        {"gateway_scope_failure_attempt_count": 65},
        {"gateway_scope_failure_attempt_count": 2},
        {"gateway_scope_transport_entered_count": 1.0},
        {"gateway_scope_failure_attempt_count": "0"},
        {"gateway_scope_extra": "opaque"},
    ],
)
def test_scope_schema_rejects_invalid_fields(
    kind: EventKind,
    entry: str,
    changes: dict[str, JsonValue],
) -> None:
    fields = _valid_fields()
    fields.update(changes)
    if entry == "constructor":
        with pytest.raises((ValueError, TypeError)):
            _event(kind, fields)
        return
    payload = _event(kind, {}).to_payload()
    event_payload = payload["payload"]
    assert isinstance(event_payload, dict)
    event_payload.update(fields)
    # JSON normalizes StrEnum into strings, so it is a valid wire value there.
    if entry == "json" and isinstance(changes.get("gateway_scope_reason"), ScopeIncompleteReason):
        assert (
            RunEvent.model_validate_json(json.dumps(payload)).payload["gateway_scope_reason"]
            == "deadline_exhausted"
        )
        return
    with pytest.raises((ValueError, TypeError)):
        if entry == "payload":
            RunEvent.from_payload(payload)
        else:
            RunEvent.model_validate_json(json.dumps(payload))


@pytest.mark.parametrize("kind", [EventKind.STEP_FAILED, EventKind.RUNTIME_FAILED])
@pytest.mark.parametrize("missing", sorted(SCOPE_FIELDS))
def test_scope_schema_requires_all_four_fields(kind: EventKind, missing: str) -> None:
    fields = _valid_fields()
    del fields[missing]
    with pytest.raises((ValueError, TypeError)):
        _event(kind, fields)


@pytest.mark.parametrize("recover", [False, True])
async def test_scope_forwarding_does_not_replay_tool_or_rebill_completion(recover: bool) -> None:
    gateway = _ToolGateway(recover)
    capabilities = _Capabilities()
    repository = InMemoryArtifactRepository()
    runtime = _runtime(gateway, capabilities, repository)
    if recover:
        events = [event async for event in runtime.run(_context())]
        assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    else:
        events, _ = await _failed_events(runtime)
        failed = next(event for event in events if event.kind is EventKind.STEP_FAILED)
        assert failed.payload["gateway_scope_phase"] == "outer_deadline"
    assert len(gateway.requests) == (4 if recover else 3)
    assert len(gateway.completions) == (3 if recover else 1)
    assert capabilities.calls == 1
    saved = await runtime.save_checkpoint()
    checkpoint = RuntimeCheckpoint.from_payload(json.loads(json.dumps(saved.to_payload())))
    usage = checkpoint.state["usage"]
    assert isinstance(usage, Mapping)
    assert usage["tokens"] == 2 * len(gateway.completions)
    assert Decimal(str(usage["cost_usd"])) == Decimal("0.01") * len(gateway.completions)
    models = checkpoint.state["models"]
    assert isinstance(models, Mapping)
    succeeded = [
        value
        for value in models.values()
        if isinstance(value, Mapping) and value["status"] == "succeeded"
    ]
    assert len(succeeded) == len(gateway.completions)
    assert all(
        value["provenance"]
        == {
            "logical_model": "general",
            "deployment_id": "owned",
            "provider_id": "owned",
            "provider_model": "owned/fixture",
        }
        for value in succeeded
    )
    restored_gateway = _ToolGateway(recover)
    restored = _runtime(restored_gateway, capabilities, repository)
    snapshot = checkpoint.to_payload()
    await restored.restore_checkpoint(checkpoint)
    if recover:
        resumed = [event async for event in restored.run(_context(checkpoint))]
        assert resumed[-1].kind is EventKind.RUNTIME_COMPLETED
        assert resumed[-1].inputs == events[-1].inputs
    else:
        resumed, _ = await _failed_events(restored, checkpoint)
        assert resumed[-1].kind is EventKind.RUNTIME_FAILED
    assert restored_gateway.requests == []
    assert restored_gateway.completions == []
    assert capabilities.calls == 1
    assert checkpoint.to_payload() == snapshot
    assert checkpoint.state["usage"] == usage
    for event in resumed:
        if event.checkpoint is not None:
            assert event.checkpoint.state["usage"] == usage


@pytest.mark.parametrize("copied", [False, True])
async def test_generation_cannot_forge_or_copy_registered_scope_carrier(copied: bool) -> None:
    gateway = _Gateway("deadline")
    runtime = _runtime(gateway, factory=_Factory(_ForgedGeneration(copied)))
    events, error = await _failed_events(runtime)
    assert gateway.requests == []
    terminals = [
        event for event in events if event.kind in {EventKind.STEP_FAILED, EventKind.RUNTIME_FAILED}
    ]
    assert len(terminals) == 2
    assert all(not SCOPE_FIELDS.intersection(event.payload) for event in terminals)
    assert not _gateway_scope_payload(error)


@pytest.mark.parametrize("reviewer_retries", [0, 1])
async def test_reviewer_cached_failure_keeps_only_live_verified_scope(
    reviewer_retries: int,
) -> None:
    repository = InMemoryArtifactRepository()
    gateway = _ReviewGateway("deadline")
    runtime = _runtime(gateway, repository=repository, reviewer_retries=reviewer_retries)
    events, error = await _failed_events(runtime)
    assert len(gateway.requests) == 3
    terminals = [
        event for event in events if event.kind in {EventKind.STEP_FAILED, EventKind.RUNTIME_FAILED}
    ]
    assert len(terminals) == 2
    for event in terminals:
        assert event.payload["gateway_scope_phase"] == "outer_deadline"
        assert event.payload["gateway_scope_reason"] == "deadline_exhausted"
    assert RAW_BODY not in json.dumps([event.to_payload() for event in events], default=str)
    assert error.__cause__ is None and error.__context__ is None
    saved = await runtime.save_checkpoint()
    checkpoint = RuntimeCheckpoint.from_payload(json.loads(json.dumps(saved.to_payload())))
    models = checkpoint.state["models"]
    assert isinstance(models, Mapping)
    assert all(
        isinstance(value, Mapping) and not SCOPE_FIELDS.intersection(value)
        for value in models.values()
    )
    assert "gateway_scope_diagnostics" not in checkpoint.state
    usage = checkpoint.state["usage"]
    assert isinstance(usage, Mapping)
    assert usage["tokens"] == 2
    assert Decimal(str(usage["cost_usd"])) == Decimal("0.01")
    restored_gateway = _ReviewGateway("deadline")
    restored = _runtime(
        restored_gateway,
        repository=repository,
        reviewer_retries=reviewer_retries,
    )
    await restored.restore_checkpoint(checkpoint)
    resumed, _ = await _failed_events(restored, checkpoint)
    assert restored_gateway.requests == []
    assert all(not SCOPE_FIELDS.intersection(event.payload) for event in resumed)
    assert checkpoint.state["usage"] == usage


def test_issued_structured_correction_scope_retains_non_retryable_type() -> None:
    error = _gateway_scope_failure(
        "structured correction failed", _failure("deadline"), contract=True
    )
    assert isinstance(error, _GatewayScopeContractFailure)
    assert isinstance(error, _ModelContractFailed)
    assert _gateway_scope_payload(error)["gateway_scope_phase"] == "outer_deadline"
