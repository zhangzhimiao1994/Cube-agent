import asyncio
import copy
from collections.abc import AsyncIterator, Mapping
from typing import Any, cast
from uuid import UUID, uuid4

import pytest

from agent_hub.domain.runs import TaskMode
from agent_hub.models.failure_receipt import GatewayFailureAttempt, GatewayFailureReceipt
from agent_hub.runs.repository import _public_event_payload
from agent_hub.runtime.contracts import (
    Artifact,
    EventKind,
    GatewayProvenance,
    JsonValue,
    RunEvent,
    RuntimeCheckpoint,
    TaskContext,
)
from agent_hub.runtime.hybrid import HybridRuntime
from agent_hub.runtime.model_scope import validate_model_scope_artifact
from tests.unit.runtime.test_hybrid import MultiArtifactRuntime, UnusedRuntime, final_zip_artifact
from tests.unit.test_real_user_four_scale_acceptance_script import load_script


def scope_events(
    run_id: UUID, tenant_id: UUID, *, scope_id: UUID | None = None,
    part_index: int = 1, part_count: int = 1,
) -> list[RunEvent]:
    provenance = GatewayProvenance(logical_model="primary", deployment_id="deployment",
                                   provider_id="provider", provider_model="provider/model")
    receipt = GatewayFailureReceipt(
        call_id=str(uuid4()), requested_logical_model="primary", allow_fallback=False,
        history_complete=True, attempted_logical_models=("primary",),
        attempts=(GatewayFailureAttempt(
            ordinal=1, logical_model="primary", deployment_id="deployment",
            provider_id="provider", provider_model="provider/model",
            outcome="empty_response", status_code=200,
        ),),
    )
    call: dict[str, JsonValue] = {
        "outcome": "failed", "receipt": cast(Mapping[str, JsonValue], receipt.to_payload()),
    }
    artifact = Artifact(
        id=uuid4(), type="model_attempt", producer="main_agent", provenance=provenance,
        content={"schema_version": 1, "source": "direct_runtime", "run_id": str(run_id),
                 "tenant_id": str(tenant_id), "requested_logical_model": "primary",
                 "history_complete": True, "call_count": part_count,
                 "scope_id": str(scope_id or uuid4()), "part_index": part_index,
                 "part_count": part_count, "call_offset": part_index - 1, "calls": (call,)},
    )
    payload: dict[str, JsonValue] = {
        "artifact_id": str(artifact.id), "logical_model": "primary",
        "requested_logical_model": "primary", "attempted_logical_models": ("primary",),
        "deployment": "deployment", "provider": "provider", "upstream_model": "provider/model",
    }
    return [
        RunEvent(kind=EventKind.MODEL_STARTED, run_id=run_id, sequence=1,
                 actor="main_agent", payload={"logical_model": "primary"}),
        RunEvent(kind=EventKind.ARTIFACT_CREATED, run_id=run_id, sequence=2,
                 actor="main_agent", artifact=artifact, payload=payload),
        RunEvent(kind="model.failure_receipt", run_id=run_id, sequence=3,
                 payload={**payload, "actor": "main_agent"}),
        RunEvent(kind=EventKind.RUNTIME_FAILED, run_id=run_id, sequence=4,
                 reason="model gateway failed: model response text is empty"),
    ]


class ScopeRuntime:
    def __init__(self, mode: TaskMode, events: list[RunEvent]) -> None:
        self.mode = mode
        self.events = events

    async def run(self, context: TaskContext) -> AsyncIterator[RunEvent]:
        for event in self.events:
            yield event

    async def save_checkpoint(self) -> RuntimeCheckpoint:
        raise AssertionError("failed scope has no checkpoint")

    async def restore_checkpoint(self, checkpoint: RuntimeCheckpoint) -> None:
        raise AssertionError("unused")

    async def cancel(self) -> None:
        pass


@pytest.mark.parametrize("distinct_run", [False, True])
async def test_hybrid_preserves_verified_atomic_scope_and_failure_receipt(
    distinct_run: bool,
) -> None:
    parent_id, tenant_id = uuid4(), uuid4()
    child_id = uuid4() if distinct_run else parent_id
    original = scope_events(child_id, tenant_id)
    runtime = HybridRuntime(ScopeRuntime(TaskMode.DISPATCH, original),
                            UnusedRuntime(TaskMode.DISCUSS, "unused"),
                            UnusedRuntime(TaskMode.DIRECT, "unused"))
    events = [event async for event in runtime.run(TaskContext(
        run_id=parent_id, tenant_id=tenant_id, mode=TaskMode.HYBRID, request="answer",
    ))]
    assert "model.failure_receipt" in [event.kind for event in events]
    assert events[-1].kind is EventKind.RUNTIME_FAILED
    assert not any(event.kind is EventKind.RUNTIME_COMPLETED for event in events)
    assert [event.sequence for event in events] == list(range(1, len(events) + 1))
    assert all(event.run_id == parent_id for event in events)
    scope = original[1].artifact
    assert scope is not None
    for kind, source_sequence in ((EventKind.ARTIFACT_CREATED, 2), ("model.failure_receipt", 3)):
        event = next(event for event in events if event.kind == kind
                     and event.payload.get("artifact_id") == str(scope.id))
        assert event.payload["model_scope_origin"] == {
            "schema_version": 1, "source": "hybrid_runtime", "run_id": str(child_id),
            "sequence": source_sequence, "parent_run_id": str(parent_id),
            "artifact_id": str(scope.id), "content_sha256": scope.content_sha256,
        }
        public = _public_event_payload(event.to_payload())
        public_payload = public["payload"]
        assert isinstance(public_payload, Mapping)
        origin = event.payload["model_scope_origin"]
        assert isinstance(origin, Mapping)
        assert public_payload["model_scope_origin"] == dict(origin)
        with pytest.raises(TypeError):
            cast(dict[str, JsonValue], origin)["run_id"] = str(uuid4())
        RunEvent.from_payload(event.to_payload())
        if kind == "model.failure_receipt":
            assert event.actor is None
            assert event.payload["actor"] == "main_agent"
        else:
            assert event.artifact is scope
            public_artifact = public["artifact"]
            assert isinstance(public_artifact, Mapping)
            assert public_artifact["content_redacted"] is False
            restored = Artifact.from_payload({key: value for key, value in public_artifact.items()
                                              if key not in {"public_content_sha256", "content_redacted"}})
            validate_model_scope_artifact(restored, str(child_id))
            assert restored.content_sha256 == scope.content_sha256
    start = next(event for event in events if event.kind is EventKind.MODEL_STARTED)
    start_origin = start.payload["model_scope_origin"]
    assert isinstance(start_origin, Mapping)
    assert start_origin["run_id"] == str(child_id)
    assert start_origin["sequence"] == 1


async def test_hybrid_scope_only_failure_is_not_partial_completion() -> None:
    parent_id, tenant_id = uuid4(), uuid4()
    runtime = HybridRuntime(
        MultiArtifactRuntime(TaskMode.DISPATCH, ()),
        MultiArtifactRuntime(TaskMode.DISCUSS, ()),
        ScopeRuntime(TaskMode.DIRECT, scope_events(uuid4(), tenant_id)),
    )
    events = [event async for event in runtime.run(TaskContext(
        run_id=parent_id, tenant_id=tenant_id, mode=TaskMode.HYBRID, request="answer",
    ))]
    assert events[-1].kind is EventKind.RUNTIME_FAILED
    assert not any(event.kind is EventKind.RUNTIME_COMPLETED for event in events)
    assert any(event.artifact is not None and event.artifact.type == "model_attempt"
               for event in events)
    assert any(event.artifact is not None
               and event.artifact.producer == "harness_failure_closure" for event in events)


@pytest.mark.parametrize("mutation", [
    "artifact_run", "tenant", "hash", "artifact_actor", "payload_model", "missing_start",
    "receipt_id", "receipt_run", "receipt_actor", "receipt_history", "receipt_sequence",
])
async def test_hybrid_rejects_contradictory_scope_linkage(mutation: str) -> None:
    parent_id, child_id, tenant_id = uuid4(), uuid4(), uuid4()
    original = scope_events(child_id, tenant_id)
    envelope: dict[str, Any] = original[1].to_payload()
    receipt: dict[str, Any] = original[2].to_payload()
    if mutation in {"artifact_run", "tenant"}:
        key = "run_id" if mutation == "artifact_run" else "tenant_id"
        envelope["artifact"]["content"][key] = str(uuid4())
        envelope["artifact"].pop("content_sha256")
    elif mutation == "hash":
        scope = original[1].artifact
        assert scope is not None
        original[1] = original[1].model_copy(update={
            "artifact": scope.model_copy(update={"content_sha256": "0" * 64}),
        })
    elif mutation == "artifact_actor":
        envelope["actor"] = "other_agent"
    elif mutation == "payload_model":
        envelope["payload"]["logical_model"] = "foreign"
    elif mutation == "receipt_id":
        receipt["payload"]["artifact_id"] = str(uuid4())
    elif mutation == "receipt_run":
        receipt["run_id"] = str(uuid4())
    elif mutation == "receipt_actor":
        receipt["payload"]["actor"] = "other_agent"
    elif mutation == "receipt_history":
        receipt["payload"]["attempted_logical_models"] = ["foreign", "primary"]
    elif mutation == "receipt_sequence":
        receipt["sequence"] = 1
    if mutation != "hash":
        original[1] = RunEvent.from_payload(envelope)
    original[2] = RunEvent.from_payload(receipt)
    original[-1] = RunEvent(kind=EventKind.RUNTIME_COMPLETED, sequence=4, run_id=child_id)
    if mutation == "missing_start":
        original.pop(0)
    runtime = HybridRuntime(ScopeRuntime(TaskMode.DISPATCH, original),
                            UnusedRuntime(TaskMode.DISCUSS, "unused"),
                            UnusedRuntime(TaskMode.DIRECT, "unused"))
    events = [event async for event in runtime.run(TaskContext(
        run_id=parent_id, tenant_id=tenant_id, mode=TaskMode.HYBRID, request="answer",
    ))]
    assert events[-1].kind is EventKind.RUNTIME_FAILED
    assert not any(event.kind == "model.failure_receipt" for event in events)
    assert not any(event.kind is EventKind.CHECKPOINT_SAVED for event in events)


async def test_hybrid_preserves_incomplete_scope_extension_without_completion() -> None:
    parent_id, child_id, tenant_id = uuid4(), uuid4(), uuid4()
    payload: dict[str, JsonValue] = {
        "actor": "main_agent", "logical_model": "primary",
        "requested_logical_model": "primary", "history_complete": False, "reason": "unknown_call",
    }
    original = [
        RunEvent(kind=EventKind.MODEL_STARTED, sequence=1, run_id=child_id,
                 actor="main_agent", payload={"logical_model": "primary"}),
        RunEvent(kind="model.scope_incomplete", sequence=2, run_id=child_id, payload=payload),
        RunEvent(kind=EventKind.RUNTIME_FAILED, sequence=3, run_id=child_id,
                 reason="model gateway failed"),
    ]
    runtime = HybridRuntime(ScopeRuntime(TaskMode.DISPATCH, original),
                            UnusedRuntime(TaskMode.DISCUSS, "unused"),
                            UnusedRuntime(TaskMode.DIRECT, "unused"))
    events = [event async for event in runtime.run(TaskContext(
        run_id=parent_id, tenant_id=tenant_id, mode=TaskMode.HYBRID, request="answer",
    ))]
    assert "model.scope_incomplete" in [event.kind for event in events]
    event = next(event for event in events if event.kind == "model.scope_incomplete")
    assert event.run_id == parent_id and event.actor is None
    assert all(event.payload[key] == value for key, value in payload.items())
    assert event.payload["model_scope_origin"] == {
        "schema_version": 1, "source": "hybrid_runtime", "run_id": str(child_id),
        "sequence": 2, "parent_run_id": str(parent_id),
    }
    RunEvent.from_payload(_public_event_payload(event.to_payload()))
    assert events[-1].kind is EventKind.RUNTIME_FAILED


async def test_hybrid_preserves_each_scope_shard_and_original_hash() -> None:
    parent_id, child_id, tenant_id, scope_id = (uuid4() for _ in range(4))
    first = scope_events(child_id, tenant_id, scope_id=scope_id, part_index=1, part_count=2)
    second = scope_events(child_id, tenant_id, scope_id=scope_id, part_index=2, part_count=2)
    original = [*first[:3], second[1].model_copy(update={"sequence": 4}),
                second[2].model_copy(update={"sequence": 5}),
                second[3].model_copy(update={"sequence": 6})]
    runtime = HybridRuntime(ScopeRuntime(TaskMode.DISPATCH, original),
                            UnusedRuntime(TaskMode.DISCUSS, "unused"),
                            UnusedRuntime(TaskMode.DIRECT, "unused"))
    events = [event async for event in runtime.run(TaskContext(
        run_id=parent_id, tenant_id=tenant_id, mode=TaskMode.HYBRID, request="answer",
    ))]
    shards = [event for event in events
              if event.artifact is not None and event.artifact.type == "model_attempt"]
    receipts = [event for event in events if event.kind == "model.failure_receipt"]
    assert len(shards) == len(receipts) == 2
    assert [event.sequence for event in events] == list(range(1, len(events) + 1))
    for index, (forwarded, receipt, source) in enumerate(
        zip(shards, receipts, (first[1], second[1]), strict=True), 1,
    ):
        scope = forwarded.artifact
        assert scope is not None and scope is source.artifact
        assert scope.content["scope_id"] == str(scope_id)
        assert scope.content["part_index"] == index
        assert scope.content["part_count"] == 2
        assert scope.content["call_offset"] == index - 1
        assert scope.content["call_count"] == 2
        validate_model_scope_artifact(scope, str(child_id))
        assert scope.content_sha256 == scope.recompute_content_sha256()
        for event in (forwarded, receipt):
            origin = event.payload["model_scope_origin"]
            assert isinstance(origin, Mapping)
            assert origin["run_id"] == str(child_id)
            assert origin["parent_run_id"] == str(parent_id)
            assert origin["artifact_id"] == str(scope.id)
            assert origin["content_sha256"] == scope.content_sha256
    assert events[-1].kind is EventKind.RUNTIME_FAILED


async def test_hybrid_cancel_between_scope_artifact_and_receipt_keeps_evidence() -> None:
    parent_id, child_id, tenant_id = uuid4(), uuid4(), uuid4()
    original = scope_events(child_id, tenant_id)
    published = asyncio.Event()

    class SuspendedScopeRuntime(ScopeRuntime):
        async def run(self, context: TaskContext) -> AsyncIterator[RunEvent]:
            yield self.events[0]
            yield self.events[1]
            await asyncio.Future[None]()

    runtime = HybridRuntime(SuspendedScopeRuntime(TaskMode.DISPATCH, original),
                            UnusedRuntime(TaskMode.DISCUSS, "unused"),
                            UnusedRuntime(TaskMode.DIRECT, "unused"))
    events: list[RunEvent] = []

    async def consume() -> None:
        async for event in runtime.run(TaskContext(
            run_id=parent_id, tenant_id=tenant_id, mode=TaskMode.HYBRID, request="answer",
        )):
            events.append(event)
            if event.artifact is not None:
                published.set()

    pending = asyncio.create_task(consume())
    try:
        await asyncio.wait_for(published.wait(), timeout=2)
        await runtime.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
    finally:
        if not pending.done():
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
    assert events[-1].kind is EventKind.RUNTIME_CANCELLED
    assert not any(event.kind in {"model.failure_receipt", EventKind.RUNTIME_COMPLETED,
                                  EventKind.CHECKPOINT_SAVED} for event in events)
    scope = original[1].artifact
    assert scope is not None
    assert next(event.artifact for event in events if event.artifact is not None) is scope
    validate_model_scope_artifact(scope, str(child_id))


@pytest.mark.parametrize("distinct_run", [False, True])
@pytest.mark.parametrize("origin_variant", [
    "valid", "wrong_parent", "wrong_hash", "wrong_child", "noncanonical_child", "wrong_sequence",
])
async def test_hybrid_public_scope_collector_retains_failed_repair_chain(
    distinct_run: bool, origin_variant: str,
) -> None:
    from agent_hub.models.types import ModelResponse, TokenUsage
    from agent_hub.runtime.direct import DirectRuntime, RuntimeExecutionError
    from tests.unit.models.test_failure_receipt import OutcomesTransport, make_gateway
    from tests.unit.models.test_gateway import CapacityStub, lease

    module = load_script()
    original_id, repair_id, final_id, tenant_id = (uuid4() for _ in range(4))
    child_id = uuid4() if distinct_run else repair_id
    successes = DirectRuntime(
        make_gateway(OutcomesTransport([
            ModelResponse(text="original", usage=TokenUsage(2, 1, 3)),
            ModelResponse(text="final", usage=TokenUsage(2, 1, 3)),
        ]), CapacityStub([lease("primary"), lease("primary")])),
        logical_model="primary", available_model_attempts=1,
    )
    by_run: dict[str, dict[str, Any]] = {}
    for run_id in (original_id, final_id):
        context = TaskContext(run_id=run_id, tenant_id=tenant_id, mode=TaskMode.DIRECT,
                              request="answer")
        public = [_public_event_payload(event.to_payload())
                  async for event in successes.run(context)]
        by_run[str(run_id)] = {"status": "completed", "events": public}

    failed = DirectRuntime(
        make_gateway(OutcomesTransport([ModelResponse(text="", usage=TokenUsage(2, 0, 2))]),
                     CapacityStub([lease("primary")])),
        logical_model="primary", available_model_attempts=1,
    )
    atomic_events: list[RunEvent] = []
    with pytest.raises(RuntimeExecutionError):
        async for event in failed.run(TaskContext(
            run_id=child_id, tenant_id=tenant_id, mode=TaskMode.DIRECT, request="answer",
        )):
            atomic_events.append(event)
    atomic_events.append(RunEvent(
        kind=EventKind.RUNTIME_FAILED, sequence=atomic_events[-1].sequence + 1, run_id=child_id,
        reason="model gateway failed: model response text is empty",
    ))
    hybrid = HybridRuntime(ScopeRuntime(TaskMode.DISPATCH, atomic_events),
                           UnusedRuntime(TaskMode.DISCUSS, "unused"),
                           UnusedRuntime(TaskMode.DIRECT, "unused"))
    forwarded = [_public_event_payload(event.to_payload()) async for event in hybrid.run(
        TaskContext(run_id=repair_id, tenant_id=tenant_id, mode=TaskMode.HYBRID, request="answer"),
    )]
    assert forwarded[-1]["kind"] == "runtime.failed"
    receipt = next(event for event in forwarded if event["kind"] == "model.failure_receipt")
    receipt_payload = receipt["payload"]
    assert isinstance(receipt_payload, dict)
    origin = receipt_payload["model_scope_origin"]
    assert isinstance(origin, dict)
    if origin_variant == "wrong_parent":
        origin["parent_run_id"] = str(uuid4())
    elif origin_variant == "wrong_hash":
        origin["content_sha256"] = "0" * 64
    elif origin_variant == "wrong_child":
        origin["run_id"] = str(uuid4())
    elif origin_variant == "noncanonical_child":
        origin["run_id"] = "NOT-A-UUID"
    elif origin_variant == "wrong_sequence":
        origin["sequence"] = 1
    by_run[str(repair_id)] = {"status": "failed", "events": forwarded}

    class Client:
        def request_json(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
            assert method == "GET" and all(value is None for value in kwargs.values())
            run_id = path.split("/")[4]
            run = by_run[run_id]
            if path.endswith("/events"):
                return {"items": copy.deepcopy(run["events"])}
            return {"id": run_id, "status": run["status"]}

    evidence = module._collect_model_scope_evidence(
        module.RealUserAcceptanceClient(Client()), logical_model="primary",
        submitted_run_ids=[str(original_id), str(repair_id)], accepted_repair_run_ids=[],
        result_run_id=str(final_id),
    )
    assert evidence["ok"] is (origin_variant == "valid"), evidence["errors"]
    assert [run["run_id"] for run in evidence["runs"]] == [
        str(original_id), str(repair_id), str(final_id),
    ]
    repair_events = evidence["runs"][1]["model_events"]
    assert not module._has_direct_model_completion(repair_events)
    if origin_variant == "valid":
        assert module._has_failed_attempt_scope(repair_events)


def completed_scope_events(
    run_id: UUID, tenant_id: UUID, *, variant: str, checkpoint: bool,
) -> list[RunEvent]:
    scope_id = uuid4()
    first = scope_events(run_id, tenant_id, scope_id=scope_id, part_index=1, part_count=2)
    second = scope_events(run_id, tenant_id, scope_id=scope_id, part_index=2, part_count=2)
    events = [first[0], first[1], first[2], second[1], second[2]]
    if variant in {"duplicate_call", "cross_scope_duplicate_call"}:
        first_artifact = first[1].artifact
        assert first_artifact is not None
        first_payload: dict[str, Any] = first_artifact.to_payload()
        raw: dict[str, Any] = events[3].to_payload()
        raw["artifact"]["content"]["calls"][0]["receipt"]["call_id"] = (
            first_payload["content"]["calls"][0]["receipt"]["call_id"]
        )
        raw["artifact"].pop("content_sha256")
        events[3] = RunEvent.from_payload(raw)
    if variant == "cross_scope_duplicate_call":
        for position in (1, 3):
            raw = cast(dict[str, Any], events[position].to_payload())
            raw["artifact"]["content"].update(
                scope_id=str(uuid4()), part_index=1, part_count=1, call_offset=0, call_count=1,
            )
            raw["artifact"].pop("content_sha256")
            events[position] = RunEvent.from_payload(raw)
    elif variant == "missing_shard":
        events = events[:3]
    elif variant == "missing_receipt":
        events.pop(2)
    elif variant == "offset_gap":
        raw = cast(dict[str, Any], events[3].to_payload())
        raw["artifact"]["content"]["call_offset"] = 0
        raw["artifact"].pop("content_sha256")
        events[3] = RunEvent.from_payload(raw)
    events = [event.model_copy(update={"sequence": index})
              for index, event in enumerate(events, 1)]
    if checkpoint:
        events.append(RunEvent(
            kind=EventKind.CHECKPOINT_SAVED, sequence=len(events) + 1, run_id=run_id,
            checkpoint=RuntimeCheckpoint(id=uuid4(), runtime_type="direct", runtime_version="2",
                                         run_id=run_id, tenant_id=tenant_id, mode=TaskMode.DISPATCH,
                                         state={"completed": True}),
        ))
    events.append(RunEvent(kind=EventKind.RUNTIME_COMPLETED, sequence=len(events) + 1,
                           run_id=run_id))
    return events


@pytest.mark.parametrize("checkpoint", [False, True])
@pytest.mark.parametrize("variant", [
    "missing_receipt", "missing_shard", "duplicate_call", "cross_scope_duplicate_call", "offset_gap",
])
async def test_hybrid_success_boundary_rejects_unsealed_scope(
    variant: str, checkpoint: bool,
) -> None:
    parent_id, child_id, tenant_id = uuid4(), uuid4(), uuid4()
    original = completed_scope_events(child_id, tenant_id, variant=variant, checkpoint=checkpoint)
    before = [event.artifact.to_payload() for event in original if event.artifact is not None]
    runtime = HybridRuntime(
        ScopeRuntime(TaskMode.DISPATCH, original), MultiArtifactRuntime(TaskMode.DISCUSS, ()),
        MultiArtifactRuntime(TaskMode.DIRECT, ()),
    )
    events = [event async for event in runtime.run(TaskContext(
        run_id=parent_id, tenant_id=tenant_id, mode=TaskMode.HYBRID, request="answer",
    ))]
    assert events[-1].kind is EventKind.RUNTIME_FAILED
    assert not any(event.kind in {EventKind.RUNTIME_COMPLETED, EventKind.CHECKPOINT_SAVED}
                   for event in events)
    forwarded = [event.artifact for event in events if event.artifact is not None]
    originals = [event.artifact for event in original if event.artifact is not None]
    assert all(any(artifact is source for source in originals) for artifact in forwarded)
    assert [artifact.to_payload() for artifact in originals if artifact is not None] == before


@pytest.mark.parametrize("checkpoint", [False, True])
async def test_hybrid_success_boundary_accepts_complete_scope_shards(checkpoint: bool) -> None:
    parent_id, child_id, tenant_id = uuid4(), uuid4(), uuid4()
    original = completed_scope_events(child_id, tenant_id, variant="valid", checkpoint=checkpoint)
    runtime = HybridRuntime(
        ScopeRuntime(TaskMode.DISPATCH, original), MultiArtifactRuntime(TaskMode.DISCUSS, ()),
        MultiArtifactRuntime(TaskMode.DIRECT, ()),
    )
    events = [event async for event in runtime.run(TaskContext(
        run_id=parent_id, tenant_id=tenant_id, mode=TaskMode.HYBRID, request="answer",
    ))]
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert sum(event.kind == "model.failure_receipt" for event in events) == 2
    assert [event.sequence for event in events] == list(range(1, len(events) + 1))
    for source in original:
        if source.artifact is not None:
            assert next(event.artifact for event in events
                        if event.artifact is not None and event.artifact.id == source.artifact.id
                        ) is source.artifact


@pytest.mark.parametrize("checkpoint", [False, True])
async def test_hybrid_bad_scope_cannot_complete_partially_after_final_attachment(checkpoint: bool) -> None:
    parent_id, child_id, tenant_id = uuid4(), uuid4(), uuid4()
    original = completed_scope_events(child_id, tenant_id, variant="missing_receipt",
                                      checkpoint=checkpoint)
    runtime = HybridRuntime(
        MultiArtifactRuntime(TaskMode.DISPATCH, (final_zip_artifact(),)),
        MultiArtifactRuntime(TaskMode.DISCUSS, ()), ScopeRuntime(TaskMode.DIRECT, original),
    )
    events = [event async for event in runtime.run(TaskContext(
        run_id=parent_id, tenant_id=tenant_id, mode=TaskMode.HYBRID, request="answer",
    ))]
    assert events[-1].kind is EventKind.RUNTIME_FAILED
    assert not any(event.kind is EventKind.RUNTIME_COMPLETED for event in events)
    first_scope_sequence = next(event.sequence for event in events
                                if event.artifact is not None and event.artifact.type == "model_attempt")
    assert not any(event.kind is EventKind.CHECKPOINT_SAVED and event.sequence > first_scope_sequence
                   for event in events)


@pytest.mark.parametrize("checkpoint", [False, True])
async def test_hybrid_rejects_call_uuid_replay_across_child_stages(checkpoint: bool) -> None:
    parent_id, tenant_id = uuid4(), uuid4()
    first = completed_scope_events(parent_id, tenant_id, variant="valid", checkpoint=False)
    second = completed_scope_events(parent_id, tenant_id, variant="valid", checkpoint=checkpoint)
    first_artifact = first[1].artifact
    assert first_artifact is not None
    first_payload: dict[str, Any] = first_artifact.to_payload()
    raw: dict[str, Any] = second[1].to_payload()
    raw["artifact"]["content"]["calls"][0]["receipt"]["call_id"] = (
        first_payload["content"]["calls"][0]["receipt"]["call_id"]
    )
    raw["artifact"].pop("content_sha256")
    second[1] = RunEvent.from_payload(raw)
    second_artifact = second[1].artifact
    assert second_artifact is not None
    runtime = HybridRuntime(
        ScopeRuntime(TaskMode.DISPATCH, first), ScopeRuntime(TaskMode.DISCUSS, second),
        MultiArtifactRuntime(TaskMode.DIRECT, ()),
    )
    events = [event async for event in runtime.run(TaskContext(
        run_id=parent_id, tenant_id=tenant_id, mode=TaskMode.HYBRID, request="answer",
    ))]
    assert events[-1].kind is EventKind.RUNTIME_FAILED
    assert not any(event.kind is EventKind.RUNTIME_COMPLETED for event in events)
    replay_sequence = next(event.sequence for event in events
                           if event.artifact is not None and event.artifact.id == second_artifact.id)
    assert not any(event.kind is EventKind.CHECKPOINT_SAVED and event.sequence > replay_sequence
                   for event in events)
