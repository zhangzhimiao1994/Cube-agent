"""Initial-input recovery using a shared memory repository, not process durability."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, cast
from uuid import UUID, uuid4

import pytest

from agent_hub.models.gateway import GatewayCompletion
from agent_hub.models.types import ModelRequest
from agent_hub.runtime.artifacts import ArtifactReference, InMemoryArtifactRepository
from agent_hub.runtime.contracts import Artifact, EventKind, RuntimeCheckpoint
from agent_hub.runtime.crew.adapter import CrewDispatchRuntime, RuntimeExecutionError
from tests.unit.runtime.crew.test_adapter_failure_reason import (
    FastFactory,
    _context,
    _reviewed_step_plan,
)
from tests.unit.runtime.crew.test_structured_handoff_repair import RepairCaptureGateway


def history_inputs() -> tuple[Artifact, ...]:
    # Deliberately not UUID-sorted: input order is part of the model request.
    return tuple(
        Artifact(
            id=UUID(identifier),
            type="text",
            producer="conversation_history",
            content={
                "text": text,
                "conversation_id": "input-snapshot-conversation",
                "trust": "internal_conversation_summary",
                "context_policy": "full_history",
            },
        )
        for identifier, text in (
            ("00000000-0000-4000-8000-000000000092", "Original history: preserve customer A."),
            (
                "00000000-0000-4000-8000-000000000091",
                "Original history: retain the accepted scope.",
            ),
        )
    )


async def capture_partial(
    inputs: tuple[Artifact, ...],
) -> tuple[RuntimeCheckpoint, InMemoryArtifactRepository]:
    repository = InMemoryArtifactRepository()
    gateway = RepairCaptureGateway(
        ('{"summary":"actual history-grounded worker output"}', 17, False),
        ('{"verdict":"approve"}', 8, False),
        ("actual final answer", 11, False),
    )
    runtime = CrewDispatchRuntime(
        gateway,
        _reviewed_step_plan(),
        artifact_repository=repository,
        crew_factory=FastFactory(),
    )
    ctx = _context(artifacts=inputs)
    events = [event async for event in runtime.run(ctx)]
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert not any(
        event.artifact is not None and event.artifact.id in {artifact.id for artifact in inputs}
        for event in events
    )
    for event in events:
        serialized = json.dumps(event.to_payload(), ensure_ascii=False)
        assert all(str(artifact.content["text"]) not in serialized for artifact in inputs)
    assert len(gateway.requests) == 3
    checkpoint = next(
        event.checkpoint
        for event in events
        if event.checkpoint is not None
        and event.checkpoint.state["phase"] == "running"
        and event.checkpoint.state["usage"] == {"tokens": 17, "cost_usd": "0"}
    )
    assert checkpoint.state["completed"] == ()
    models = checkpoint.state["models"]
    assert isinstance(models, Mapping) and len(models) == 1
    succeeded = next(iter(models.values()))
    assert isinstance(succeeded, Mapping)
    assert succeeded["status"] == "succeeded" and succeeded["actor"] == "writer"
    reference = ArtifactReference(
        id=UUID(str(succeeded["artifact_id"])),
        sha256=str(succeeded["sha256"]),
    )
    (model_artifact,) = await repository.get_many(ctx.tenant_id, ctx.run_id, (reference,))
    assert model_artifact.source_ids == tuple(str(artifact.id) for artifact in inputs)
    return checkpoint, repository


async def resume_without_repeating_worker(
    checkpoint: RuntimeCheckpoint,
    repository: InMemoryArtifactRepository,
    fresh_inputs: tuple[Artifact, ...],
) -> None:
    gateway = RepairCaptureGateway(
        ('{"verdict":"approve"}', 8, False),
        ("actual final answer", 11, False),
    )
    runtime = CrewDispatchRuntime(
        gateway,
        _reviewed_step_plan(),
        artifact_repository=repository,
        crew_factory=FastFactory(),
    )
    await runtime.restore_checkpoint(checkpoint)
    events = [
        event
        async for event in runtime.run(_context(checkpoint=checkpoint, artifacts=fresh_inputs))
    ]
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert len(gateway.requests) == 2
    raw_refs = checkpoint.state["input_refs"]
    assert isinstance(raw_refs, tuple)
    references = tuple(
        ArtifactReference(id=UUID(str(reference["id"])), sha256=str(reference["sha256"]))
        for reference in raw_refs if isinstance(reference, Mapping)
    )
    ctx = _context()
    inputs = await repository.get_many(ctx.tenant_id, ctx.run_id, references)
    for event in events:
        serialized = json.dumps(event.to_payload(), ensure_ascii=False)
        assert all(str(artifact.content["text"]) not in serialized for artifact in inputs)
    assert [request.logical_model for request in gateway.requests] == ["review", "general"]
    assert gateway.requests[0].response_schema is not None
    assert gateway.requests[0].response_schema.name == "DispatchReviewVerdict"
    assert [
        event.payload["verdict"] for event in events if event.kind is EventKind.REVIEW_COMPLETED
    ] == ["approve"]
    restored = await runtime.save_checkpoint()
    assert restored.state["usage"] == {"tokens": 36, "cost_usd": "0"}
    old_models, new_models = checkpoint.state["models"], restored.state["models"]
    assert isinstance(old_models, Mapping) and isinstance(new_models, Mapping)
    for key, value in old_models.items():
        assert new_models[key] == value


@pytest.mark.parametrize(
    "fresh_history", ["missing", "new_ids", "changed_content", "same_ids_changed"]
)
async def test_partial_checkpoint_restores_original_inputs_in_fresh_context(
    fresh_history: str,
) -> None:
    original = history_inputs()
    checkpoint, repository = await capture_partial(original)
    rebuilt = (
        tuple(
            Artifact(
                id=artifact.id if fresh_history == "same_ids_changed" else uuid4(),
                type=artifact.type,
                producer=artifact.producer,
                content=(
                    artifact.content if fresh_history == "new_ids" else {"text": "New history"}
                ),
            )
            for artifact in reversed(original)
        )
        if fresh_history != "missing"
        else ()
    )
    if fresh_history != "same_ids_changed":
        assert not {artifact.id for artifact in rebuilt} & {artifact.id for artifact in original}
    await resume_without_repeating_worker(checkpoint, repository, rebuilt)


async def test_partial_checkpoint_records_ordered_exact_input_refs() -> None:
    inputs = history_inputs()
    checkpoint, _ = await capture_partial(inputs)
    assert checkpoint.state.get("input_refs") == tuple(
        {"id": str(artifact.id), "sha256": artifact.content_sha256} for artifact in inputs
    )
    assert checkpoint.runtime_version == "9"


async def test_partial_checkpoint_inputs_are_stored_in_private_repository() -> None:
    inputs = history_inputs()
    _, repository = await capture_partial(inputs)
    ctx = _context()
    references = tuple(
        ArtifactReference(id=artifact.id, sha256=artifact.content_sha256) for artifact in inputs
    )
    stored = await repository.get_many(ctx.tenant_id, ctx.run_id, references)
    assert tuple(artifact.to_payload() for artifact in stored) == tuple(
        artifact.to_payload() for artifact in inputs
    )


async def test_partial_checkpoint_without_inputs_recovers_without_repeating_worker() -> None:
    checkpoint, repository = await capture_partial(())
    assert checkpoint.state["input_refs"] == ()
    await resume_without_repeating_worker(checkpoint, repository, ())


async def test_partial_checkpoint_with_original_history_in_context_control() -> None:
    original = history_inputs()
    checkpoint, repository = await capture_partial(original)
    await resume_without_repeating_worker(checkpoint, repository, original)


@pytest.mark.parametrize("kind", ["model_response", "tool_result"])
async def test_external_typed_input_is_not_an_internal_ledger_artifact(kind: str) -> None:
    original = Artifact(
        id=uuid4(), type=kind, producer="writer", content={"text": f"PRIVATE_EXTERNAL_{kind}"},
    )
    checkpoint, repository = await capture_partial((original,))
    await resume_without_repeating_worker(checkpoint, repository, ())


@pytest.mark.parametrize("kind", ["model_response", "tool_result"])
@pytest.mark.parametrize("claimed_input", [False, True])
async def test_unaccounted_internal_artifact_cannot_be_added_or_disguised_as_input(
    kind: str, claimed_input: bool,
) -> None:
    original = history_inputs()
    checkpoint, repository = await capture_partial(original)
    unaccounted = Artifact(
        id=uuid4(), type=kind, producer="writer", content={"text": "unaccounted internal output"},
        source_ids=(str(original[0].id),),
    )
    ctx = _context()
    await repository.put(ctx.tenant_id, ctx.run_id, unaccounted)
    payload = cast(dict[str, Any], checkpoint.to_payload())
    payload["state"]["artifact_registry"][str(unaccounted.id)] = unaccounted.content_sha256
    if claimed_input:
        payload["state"]["input_refs"].append({
            "id": str(unaccounted.id), "sha256": unaccounted.content_sha256,
        })
    payload["state_sha256"] = ""
    damaged = RuntimeCheckpoint.from_payload(payload)
    gateway = RepairCaptureGateway()
    runtime = CrewDispatchRuntime(
        gateway, _reviewed_step_plan(), artifact_repository=repository, crew_factory=FastFactory(),
    )
    await runtime.restore_checkpoint(damaged)
    with pytest.raises(RuntimeExecutionError, match="^runtime checkpoint artifact graph is invalid$"):
        _ = [event async for event in runtime.run(_context(checkpoint=damaged))]
    assert gateway.requests == []


async def test_accounted_model_identity_cannot_also_be_claimed_as_an_input() -> None:
    checkpoint, repository = await capture_partial(history_inputs())
    payload = cast(dict[str, Any], checkpoint.to_payload())
    model = next(iter(payload["state"]["models"].values()))
    payload["state"]["input_refs"].append({"id": model["artifact_id"], "sha256": model["sha256"]})
    payload["state_sha256"] = ""
    damaged = RuntimeCheckpoint.from_payload(payload)
    gateway = RepairCaptureGateway()
    runtime = CrewDispatchRuntime(
        gateway, _reviewed_step_plan(), artifact_repository=repository, crew_factory=FastFactory(),
    )
    await runtime.restore_checkpoint(damaged)
    with pytest.raises(RuntimeExecutionError, match="^runtime checkpoint artifact graph is invalid$"):
        _ = [event async for event in runtime.run(_context(checkpoint=damaged))]
    assert gateway.requests == []


@pytest.mark.parametrize(
    "damage", ["missing", "duplicate", "oversized", "hash", "order", "unknown"]
)
async def test_invalid_input_refs_fail_before_any_submission(damage: str) -> None:
    original = history_inputs()
    checkpoint, repository = await capture_partial(original)
    payload = cast(dict[str, Any], checkpoint.to_payload())
    refs = [{"id": str(item.id), "sha256": item.content_sha256} for item in original]
    if damage == "duplicate":
        refs.append(dict(refs[0]))
    elif damage == "oversized":
        refs = [{"id": str(uuid4()), "sha256": original[0].content_sha256} for _ in range(65)]
        payload["state"]["artifact_registry"].update({ref["id"]: ref["sha256"] for ref in refs})
    elif damage == "hash":
        refs[0]["sha256"] = "0" * 64
    elif damage == "order":
        refs.reverse()
    elif damage == "unknown":
        refs[0]["id"] = str(uuid4())
    if damage == "missing":
        payload["state"].pop("input_refs", None)
    else:
        payload["state"]["input_refs"] = refs
    payload["state_sha256"] = ""
    damaged = RuntimeCheckpoint.from_payload(payload)
    gateway = RepairCaptureGateway()
    runtime = CrewDispatchRuntime(
        gateway,
        _reviewed_step_plan(),
        artifact_repository=repository,
        crew_factory=FastFactory(),
    )
    with pytest.raises(RuntimeExecutionError):
        await runtime.restore_checkpoint(damaged)
        assert damage == "order", (
            "invalid input refs were not rejected during checkpoint validation"
        )
        _ = [event async for event in runtime.run(_context(checkpoint=damaged, artifacts=original))]
    assert gateway.requests == []


async def test_inputs_are_privately_readable_before_first_model_submission() -> None:
    original = history_inputs()
    repository = InMemoryArtifactRepository()
    ctx = _context(artifacts=original)
    references = tuple(
        ArtifactReference(id=artifact.id, sha256=artifact.content_sha256) for artifact in original
    )

    class StorageCheckingGateway(RepairCaptureGateway):
        async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
            assert await repository.get_many(ctx.tenant_id, ctx.run_id, references) == original
            return await super().complete_with_context(request)

    gateway = StorageCheckingGateway(
        ('{"summary":"stored before call"}', 17, False),
        ('{"verdict":"approve"}', 8, False),
        ("final", 11, False),
    )
    runtime = CrewDispatchRuntime(
        gateway,
        _reviewed_step_plan(),
        artifact_repository=repository,
        crew_factory=FastFactory(),
    )
    events = [event async for event in runtime.run(ctx)]
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert len(gateway.requests) == 3


async def test_private_input_events_expose_refs_not_bodies_but_keep_worker_outputs() -> None:
    original = history_inputs()
    gateway = RepairCaptureGateway(
        ('{"summary":"visible worker output"}', 17, False),
        ('{"verdict":"approve"}', 8, False), ("visible final output", 11, False),
    )
    runtime = CrewDispatchRuntime(gateway, _reviewed_step_plan(), crew_factory=FastFactory())
    events = [event async for event in runtime.run(_context(artifacts=original))]
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    for artifact in original:
        marker = str(artifact.content["text"])
        assert marker in "\n".join(str(message.content) for message in gateway.requests[0].messages)
        for event in events:
            assert marker not in json.dumps(event.to_payload(), ensure_ascii=False)
    started = next(event for event in events
                   if event.kind is EventKind.STEP_STARTED and event.step_id == "draft")
    assert started.inputs == ()
    assert started.payload["input_refs"] == tuple(
        {"id": str(artifact.id), "sha256": artifact.content_sha256} for artifact in original
    )
    final_started = next(event for event in events
                         if event.kind is EventKind.STEP_STARTED and event.step_id == "final_response")
    assert any("visible worker output" in str(artifact.content) for artifact in final_started.inputs)
    assert any(event.artifact is not None and "visible worker output" in str(event.artifact.content)
               for event in events)


async def test_duplicate_initial_input_ids_fail_before_first_call() -> None:
    first = history_inputs()[0]
    gateway = RepairCaptureGateway()
    runtime = CrewDispatchRuntime(gateway, _reviewed_step_plan(), crew_factory=FastFactory())
    invalid = _context(artifacts=(first,)).model_copy(update={"artifacts": (first, first)})
    with pytest.raises(RuntimeExecutionError):
        _ = [event async for event in runtime.run(invalid)]
    assert gateway.requests == []


@pytest.mark.parametrize("scope", ["missing", "other_tenant", "other_run", "corrupt"])
async def test_input_body_must_come_from_exact_private_scope_not_context(scope: str) -> None:
    original = history_inputs()
    checkpoint, source = await capture_partial(original)
    repository = InMemoryArtifactRepository()
    ctx = _context()
    registry = checkpoint.state["artifact_registry"]
    assert isinstance(registry, Mapping)
    old_input_ids = {item.id for item in original}
    for identifier, digest in registry.items():
        reference = ArtifactReference(id=UUID(identifier), sha256=str(digest))
        if reference.id not in old_input_ids:
            (artifact,) = await source.get_many(ctx.tenant_id, ctx.run_id, (reference,))
            await repository.put(ctx.tenant_id, ctx.run_id, artifact)
    for artifact in original:
        if scope == "missing":
            continue
        tenant, run = ctx.tenant_id, ctx.run_id
        if scope == "other_tenant":
            tenant = uuid4()
        elif scope == "other_run":
            run = uuid4()
        else:
            artifact = Artifact(
                id=artifact.id,
                type=artifact.type,
                producer=artifact.producer,
                content={"text": "Corrupted stored history"},
            )
        await repository.put(tenant, run, artifact)
    gateway = RepairCaptureGateway()
    runtime = CrewDispatchRuntime(
        gateway,
        _reviewed_step_plan(),
        artifact_repository=repository,
        crew_factory=FastFactory(),
    )
    with pytest.raises(RuntimeExecutionError):
        await runtime.restore_checkpoint(checkpoint)
        _ = [
            event
            async for event in runtime.run(_context(checkpoint=checkpoint, artifacts=original))
        ]
    assert gateway.requests == []


async def test_v8_checkpoint_is_rejected_without_guessing_inputs() -> None:
    checkpoint, repository = await capture_partial(())
    payload = checkpoint.to_payload()
    payload["runtime_version"] = "8"
    old = RuntimeCheckpoint.from_payload(payload)
    gateway = RepairCaptureGateway()
    runtime = CrewDispatchRuntime(
        gateway,
        _reviewed_step_plan(),
        artifact_repository=repository,
        crew_factory=FastFactory(),
    )
    with pytest.raises(RuntimeExecutionError):
        await runtime.restore_checkpoint(old)
        _ = [event async for event in runtime.run(_context(checkpoint=old))]
    assert gateway.requests == []
