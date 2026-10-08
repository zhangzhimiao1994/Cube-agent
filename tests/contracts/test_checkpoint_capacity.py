from __future__ import annotations

import json
from collections.abc import Mapping
from typing import cast
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from agent_hub.domain.runs import TaskMode
from agent_hub.runtime.contracts import (
    Artifact,
    EventKind,
    JsonValue,
    RunEvent,
    RuntimeCheckpoint,
    RuntimeContractError,
    TaskContext,
)

RUN_ID = UUID("11111111-1111-4111-8111-111111111111")
TENANT_ID = UUID("22222222-2222-4222-8222-222222222222")


def ledger_state() -> dict[str, object]:
    return {
        "tools": {
            f"read-{index}": {
                "status": "succeeded",
                "argument_sha256": f"{index:064x}",
                "result_artifact_id": str(uuid4()),
                "metadata": {f"field-{field}": field for field in range(20)},
            }
            for index in range(140)
        },
        "models": {},
        "artifact_registry": {},
        "usage": {"tokens": 12345, "cost_usd": "0.10"},
    }


def checkpoint(state: object, *, runtime_type: str = "crew") -> RuntimeCheckpoint:
    return RuntimeCheckpoint(
        id=uuid4(),
        runtime_type=runtime_type,
        runtime_version="1",
        run_id=RUN_ID,
        tenant_id=TENANT_ID,
        mode=TaskMode.DISPATCH,
        state=cast(Mapping[str, JsonValue], state),
    )


def test_cumulative_bounded_ledgers_round_trip_without_dropping_history() -> None:
    state = ledger_state()
    saved = checkpoint(state)
    state["tools"] = {}
    assert len(cast(Mapping[str, JsonValue], saved.state["tools"])) == 140
    assert saved.state_sha256 == saved.recompute_state_sha256()
    for payload in (saved.to_payload(), saved.model_dump(), json.loads(saved.model_dump_json())):
        restored = RuntimeCheckpoint.from_payload(payload)
        assert restored == saved
        assert restored.state["usage"] == saved.state["usage"]


def test_ledger_checkpoint_event_and_context_round_trip() -> None:
    saved = checkpoint(ledger_state())
    event = RunEvent(kind=EventKind.CHECKPOINT_SAVED, sequence=1, run_id=RUN_ID, checkpoint=saved)
    assert RunEvent.from_payload(event.to_payload()) == event
    context = TaskContext(
        run_id=RUN_ID,
        tenant_id=TENANT_ID,
        mode=TaskMode.DISPATCH,
        request="fixture",
        checkpoint=saved,
    )
    assert TaskContext.from_payload(context.to_payload()) == context
    assert context.validated_internal_clone() == context


def test_checkpoint_envelope_does_not_consume_state_node_allowance() -> None:
    # State has 4092 nodes; the checkpoint envelope adds 16 more.
    state = {"tools": {"read": {"values": list(range(4085))}}}
    saved = checkpoint(state)
    assert RuntimeCheckpoint.from_payload(saved.to_payload()) == saved


def test_hybrid_checkpoint_preserves_large_child_hash_and_history() -> None:
    child = checkpoint(ledger_state())
    outer = checkpoint({"child_checkpoint": child.to_payload()}, runtime_type="hybrid")
    restored = RuntimeCheckpoint.from_payload(outer.to_payload())
    assert restored == outer
    child_payload = json.loads(restored.model_dump_json())["state"]["child_checkpoint"]
    assert RuntimeCheckpoint.from_payload(child_payload) == child


@pytest.mark.parametrize("runtime_type", ["crew", "hybrid", "direct"])
def test_checkpoint_extra_metadata_cannot_borrow_ledger_capacity(runtime_type: str) -> None:
    state = ledger_state() if runtime_type == "crew" else {}
    state["unrelated"] = {f"key-{index}": index for index in range(2200)}
    with pytest.raises((ValidationError, ValueError), match="structural"):
        checkpoint(state, runtime_type=runtime_type)


def test_single_ledger_record_remains_bounded() -> None:
    with pytest.raises((ValidationError, ValueError), match="structural"):
        checkpoint({"tools": {"read": {f"key-{index}": index for index in range(2200)}}})


@pytest.mark.parametrize(
    "field",
    [
        "tools",
        "models",
        "rejected_outputs",
        "structured_repairs",
        "review_refs",
        "artifact_registry",
        "step_usage",
    ],
)
def test_each_cumulative_record_collection_has_independent_entry_budgets(field: str) -> None:
    state = {field: {f"entry-{index}": {"values": list(range(20))} for index in range(200)}}
    saved = checkpoint(state)
    assert RuntimeCheckpoint.from_payload(saved.to_payload()) == saved


def test_noncrew_runtime_cannot_borrow_crew_ledger_allowance() -> None:
    with pytest.raises((ValidationError, ValueError), match="structural"):
        checkpoint(ledger_state(), runtime_type="direct")


def test_typed_carrier_does_not_reset_nested_checkpoint_depth() -> None:
    value: object = 1
    for _ in range(16):
        value = [value]
    saved = checkpoint({"tools": {"read": {"value": value}}})
    context = {
        "run_id": str(RUN_ID),
        "tenant_id": str(TENANT_ID),
        "mode": "dispatch",
        "request": "fixture",
        "checkpoint": saved.to_payload(),
    }
    with pytest.raises(RuntimeContractError):
        TaskContext.from_payload(context)


@pytest.mark.parametrize("bad", ["depth", "width", "bytes", "sensitive", "cycle"])
def test_checkpoint_aggregate_keeps_other_security_bounds(bad: str) -> None:
    state = ledger_state()
    tools = cast(dict[str, object], state["tools"])
    if bad == "depth":
        value: object = 1
        for _ in range(21):
            value = [value]
        tools["deep"] = value
    elif bad == "width":
        tools.update({f"wide-{index}": 1 for index in range(4097)})
    elif bad == "bytes":
        tools.update({f"large-{index}": "x" * 500000 for index in range(5)})
    elif bad == "sensitive":
        tools["private"] = {"api_key": "fixture-secret"}
    else:
        tools["cycle"] = state
    with pytest.raises((ValidationError, ValueError)):
        checkpoint(state)


def test_hash_mismatch_cannot_use_large_checkpoint_allowance() -> None:
    saved = checkpoint(ledger_state())
    payload = saved.to_payload()
    payload["state_sha256"] = "0" * 64
    with pytest.raises(RuntimeContractError):
        RuntimeCheckpoint.from_payload(payload)


def test_large_checkpoint_does_not_relax_ordinary_artifact_or_routing_input() -> None:
    oversized = {f"key-{index}": index for index in range(2200)}
    with pytest.raises((ValidationError, ValueError), match="structural"):
        Artifact(id=uuid4(), type="json", producer="main", content=oversized)
    saved = checkpoint(ledger_state())
    context = TaskContext(
        run_id=RUN_ID,
        tenant_id=TENANT_ID,
        mode=TaskMode.DISPATCH,
        request="fixture",
        checkpoint=saved,
    ).to_payload()
    context["routing_decision"] = oversized
    with pytest.raises(RuntimeContractError):
        TaskContext.from_payload(context)


def near_byte_limit_state(*, reserve: int = 0) -> dict[str, object]:
    records: dict[str, object] = {str(index): {"v": [0] * 1000} for index in range(100)}
    for index, length in enumerate((449631, 449631, 449631, 449630 - reserve)):
        records[f"pad{index}"] = {"text": "x" * length}
    return {"tools": records}


def byte_length(value: object) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def test_checkpoint_whole_envelope_obeys_actual_two_megabyte_bound() -> None:
    state = near_byte_limit_state()
    assert byte_length(state) == 1999900
    with pytest.raises((ValidationError, ValueError), match="size"):
        checkpoint(state)
    payload = {
        "id": str(uuid4()),
        "runtime_type": "crew",
        "runtime_version": "1",
        "run_id": str(RUN_ID),
        "tenant_id": str(TENANT_ID),
        "mode": "dispatch",
        "state": state,
        "state_sha256": "",
    }
    assert byte_length(payload) > 2000000
    with pytest.raises(RuntimeContractError):
        RuntimeCheckpoint.from_payload(payload)


@pytest.mark.parametrize("carrier", ["event", "context", "hybrid"])
def test_whole_typed_carrier_obeys_actual_two_megabyte_bound(carrier: str) -> None:
    saved = checkpoint(near_byte_limit_state(reserve=250))
    assert byte_length(saved.to_payload()) < 2000000
    if carrier == "event":
        payload = {
            "kind": "checkpoint.saved",
            "sequence": 1,
            "run_id": str(RUN_ID),
            "checkpoint": saved.to_payload(),
        }
        assert byte_length(payload) > 2000000
        with pytest.raises(RuntimeContractError):
            RunEvent.from_payload(payload)
        with pytest.raises((ValidationError, ValueError), match="size"):
            RunEvent(kind=EventKind.CHECKPOINT_SAVED, sequence=1, run_id=RUN_ID, checkpoint=saved)
    elif carrier == "context":
        payload = {
            "run_id": str(RUN_ID),
            "tenant_id": str(TENANT_ID),
            "mode": "dispatch",
            "request": "x" * 60000,
            "checkpoint": saved.to_payload(),
        }
        assert byte_length(payload) > 2000000
        with pytest.raises(RuntimeContractError):
            TaskContext.from_payload(payload)
        with pytest.raises((ValidationError, ValueError), match="size"):
            TaskContext(
                run_id=RUN_ID,
                tenant_id=TENANT_ID,
                mode=TaskMode.DISPATCH,
                request="x" * 60000,
                checkpoint=saved,
            )
    else:
        with pytest.raises((ValidationError, ValueError), match="size"):
            checkpoint({"child_checkpoint": saved.to_payload()}, runtime_type="hybrid")


@pytest.mark.parametrize("key", ["access_token", "refresh_token", "Access-Token"])
@pytest.mark.parametrize("location", ["ledger", "hybrid", "artifact"])
def test_standalone_credential_keys_cannot_enter_durable_state(key: str, location: str) -> None:
    with pytest.raises((ValidationError, ValueError), match="sensitive"):
        if location == "ledger":
            checkpoint({"tools": {"read": {key: "own-fixture-secret"}}})
        elif location == "hybrid":
            checkpoint({"metadata": {key: "own-fixture-secret"}}, runtime_type="hybrid")
        else:
            Artifact(id=uuid4(), type="json", producer="main", content={key: "own-fixture-secret"})


@pytest.mark.parametrize("limit", ["bytes", "nodes"])
@pytest.mark.parametrize("carrier", ["event", "context"])
@pytest.mark.parametrize("entry", ["constructor", "payload", "json"])
def test_validated_artifact_collections_have_per_record_capacity(
    limit: str, carrier: str, entry: str
) -> None:
    contents: list[dict[str, JsonValue]] = (
        [{"v": "\n" * 500000} for _ in range(3)]
        if limit == "bytes"
        else [{"v": tuple(range(2500))} for _ in range(2)]
    )
    artifacts = tuple(
        Artifact(id=uuid4(), type="json", producer="main", content=value) for value in contents
    )
    if carrier == "event":
        payload = {
            "kind": "runtime.completed",
            "sequence": 1,
            "run_id": str(RUN_ID),
            "inputs": [item.to_payload() for item in artifacts],
        }
    else:
        payload = {
            "run_id": str(RUN_ID),
            "tenant_id": str(TENANT_ID),
            "mode": "dispatch",
            "request": "fixture",
            "artifacts": [item.to_payload() for item in artifacts],
        }
    if limit == "bytes":
        assert byte_length(payload) > 2000000
    if carrier == "event":
        if entry == "constructor":
            event = RunEvent(
                kind=EventKind.RUNTIME_COMPLETED, sequence=1, run_id=RUN_ID, inputs=artifacts
            )
        elif entry == "payload":
            event = RunEvent.from_payload(payload)
        else:
            event = RunEvent.model_validate_json(json.dumps(payload), strict=True)
        assert event.inputs == artifacts
    else:
        if entry == "constructor":
            context = TaskContext(
                run_id=RUN_ID,
                tenant_id=TENANT_ID,
                mode=TaskMode.DISPATCH,
                request="fixture",
                artifacts=artifacts,
            )
        elif entry == "payload":
            context = TaskContext.from_payload(payload)
        else:
            context = TaskContext.model_validate_json(json.dumps(payload), strict=True)
        assert context.artifacts == artifacts
        assert context.validated_internal_clone() == context


@pytest.mark.parametrize("carrier", ["event", "context"])
def test_artifact_collection_cannot_lend_capacity_to_untyped_metadata(carrier: str) -> None:
    artifacts = [
        Artifact(id=uuid4(), type="text", producer="main", content={"text": "fixture"}).to_payload()
        for _ in range(64)
    ]
    bad = {f"key-{index}": index for index in range(2200)}
    if carrier == "event":
        payload = {
            "kind": "runtime.completed",
            "sequence": 1,
            "run_id": str(RUN_ID),
            "inputs": artifacts,
            "payload": bad,
        }
        with pytest.raises(RuntimeContractError):
            RunEvent.from_payload(payload)
    else:
        payload = {
            "run_id": str(RUN_ID),
            "tenant_id": str(TENANT_ID),
            "mode": "dispatch",
            "request": "fixture",
            "artifacts": artifacts,
            "routing_decision": bad,
        }
        with pytest.raises(RuntimeContractError):
            TaskContext.from_payload(payload)


def test_ordinary_artifact_whole_envelope_obeys_actual_byte_bound() -> None:
    content = {f"p{index}": "x" * 499965 for index in range(4)}
    assert byte_length(content) < 2000000
    with pytest.raises((ValidationError, ValueError), match="size"):
        Artifact(id=uuid4(), type="json", producer="main", content=content)


def exact_size_artifact() -> Artifact:
    parts = {f"p{index}": "" for index in range(4)}
    prototype = Artifact(id=uuid4(), type="json", producer="main", content=parts)
    remaining = 2000000 - byte_length(prototype.to_payload())
    for key in parts:
        size = min(500000, remaining)
        parts[key] = "x" * size
        remaining -= size
    assert remaining == 0
    result = Artifact(id=prototype.id, type="json", producer="main", content=parts)
    assert byte_length(result.to_payload()) == 2000000
    return result


@pytest.mark.parametrize("entry", ["constructor", "payload", "json"])
def test_artifact_collection_budget_includes_array_separators(entry: str) -> None:
    routing = {f"p{index}": "" for index in range(6)}
    prototype = TaskContext(
        run_id=RUN_ID,
        tenant_id=TENANT_ID,
        mode=TaskMode.DISPATCH,
        request="fixture",
        routing_decision=routing,
    )
    remaining = 2000000 - byte_length(prototype.to_payload())
    for key in routing:
        size = min(500000, remaining)
        routing[key] = "x" * size
        remaining -= size
    assert remaining == 0
    residual = TaskContext(
        run_id=RUN_ID,
        tenant_id=TENANT_ID,
        mode=TaskMode.DISPATCH,
        request="fixture",
        routing_decision=routing,
    )
    assert byte_length(residual.to_payload()) == 2000000
    artifacts = (exact_size_artifact(), exact_size_artifact())
    payload = residual.to_payload()
    payload["artifacts"] = [item.to_payload() for item in artifacts]
    assert byte_length(payload) == 6000001
    if entry == "constructor":
        result = TaskContext(
            run_id=RUN_ID,
            tenant_id=TENANT_ID,
            mode=TaskMode.DISPATCH,
            request="fixture",
            routing_decision=routing,
            artifacts=artifacts,
        )
    elif entry == "payload":
        result = TaskContext.from_payload(payload)
    else:
        result = TaskContext.model_validate_json(json.dumps(payload), strict=True)
    assert result.artifacts == artifacts
    assert byte_length(result.to_payload()) == 6000001
