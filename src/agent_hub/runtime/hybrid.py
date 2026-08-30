"""Framework-neutral composition of dispatch, discussion, and synthesis runtimes."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, cast
from uuid import UUID, uuid4

from agent_hub.domain.runs import TaskMode
from agent_hub.runtime.artifacts import (
    ArtifactReference,
    ArtifactRepository,
    ArtifactRepositoryError,
    InMemoryArtifactRepository,
)
from agent_hub.runtime.contracts import (
    Artifact,
    EventKind,
    RunEvent,
    RuntimeCheckpoint,
    TaskContext,
)
from agent_hub.runtime.failure_reason import safe_runtime_failure_reason

_RUNTIME_TYPE = "hybrid"
_RUNTIME_VERSION = "1"


class RuntimeExecutionError(RuntimeError):
    """Stable composite runtime failure."""


class RuntimeBusy(RuntimeExecutionError):
    """One HybridRuntime instance is single-flight."""


class ChildRuntime(Protocol):
    mode: TaskMode

    def run(self, context: TaskContext) -> AsyncIterator[RunEvent]: ...

    async def save_checkpoint(self) -> RuntimeCheckpoint: ...

    async def restore_checkpoint(self, checkpoint: RuntimeCheckpoint) -> None: ...

    async def cancel(self) -> None: ...


class HybridUpgrade(StrEnum):
    DISPATCH_TO_HYBRID = "dispatch_to_hybrid"
    DIRECT_TO_DISPATCH = "direct_to_dispatch"
    DISCUSS_DISPATCH_DISCUSS = "discuss_dispatch_discuss"


@dataclass(frozen=True, slots=True)
class HybridPlan:
    upgrade: HybridUpgrade = HybridUpgrade.DISPATCH_TO_HYBRID

    def __post_init__(self) -> None:
        if type(self.upgrade) is not HybridUpgrade:
            raise ValueError("hybrid upgrade is invalid")

    @property
    def digest(self) -> str:
        return hashlib.sha256(
            json.dumps({"upgrade": self.upgrade.value}, sort_keys=True).encode()
        ).hexdigest()


class HybridRuntime:
    """Pass only validated Artifacts across otherwise isolated runtime contexts."""

    mode = TaskMode.HYBRID

    def __init__(
        self,
        dispatch: ChildRuntime,
        discussion: ChildRuntime,
        synthesizer: ChildRuntime,
        *,
        plan: HybridPlan | None = None,
        artifact_repository: ArtifactRepository | None = None,
    ) -> None:
        if (
            dispatch.mode is not TaskMode.DISPATCH
            or discussion.mode is not TaskMode.DISCUSS
            or synthesizer.mode is not TaskMode.DIRECT
        ):
            raise ValueError("hybrid child runtime modes are invalid")
        self._dispatch = dispatch
        self._discussion = discussion
        self._synthesizer = synthesizer
        self._plan = plan or HybridPlan()
        self._repository = artifact_repository or InMemoryArtifactRepository()
        self._active_task: asyncio.Task[object] | None = None
        self._active_child: ChildRuntime | None = None
        self._last_checkpoint: RuntimeCheckpoint | None = None
        self._restored: RuntimeCheckpoint | None = None

    def run(self, context: TaskContext) -> AsyncIterator[RunEvent]:
        if self._active_task is not None:
            raise RuntimeBusy("runtime is busy")
        if type(context) is not TaskContext or context.mode is not self.mode:
            raise RuntimeExecutionError("runtime context is invalid")
        return self._run(context)

    async def _run(self, context: TaskContext) -> AsyncIterator[RunEvent]:
        task = asyncio.current_task()
        if task is None:
            raise RuntimeExecutionError("runtime task is unavailable")
        self._active_task = cast(asyncio.Task[object], task)
        sequence = 1
        artifacts = list(context.artifacts)
        known = {artifact.id for artifact in artifacts}
        try:
            restored = self._restored
            if restored is not None:
                self._validate_checkpoint(restored, context)
                if context.checkpoint is None or context.checkpoint.id != restored.id:
                    raise RuntimeExecutionError("runtime checkpoint mismatch")
                restored_artifacts = await self._hydrate_checkpoint(restored, context)
                for artifact in restored_artifacts:
                    if artifact.id not in known:
                        artifacts.append(artifact)
                        known.add(artifact.id)
                sequence = cast(int, restored.state["next_sequence"])
                if restored.state["terminal"] is True:
                    yield RunEvent(
                        kind=EventKind.RUNTIME_COMPLETED,
                        sequence=cast(int, restored.state["next_sequence"]),
                        run_id=context.run_id,
                        reason=cast(str, restored.state["reason"]),
                    )
                    return
                next_stage = cast(int, restored.state["next_stage"])
            elif context.checkpoint is not None:
                raise RuntimeExecutionError("runtime checkpoint was not restored")
            else:
                next_stage = 0

            for artifact in artifacts:
                await self._repository.put(context.tenant_id, context.run_id, artifact)

            stages = self._stages()
            if (
                restored is None
                and self._plan.upgrade is HybridUpgrade.DISPATCH_TO_HYBRID
                and artifacts
            ):
                next_stage = 1

            for stage_index in range(next_stage, len(stages)):
                child, mode, is_discussion = stages[stage_index]
                child_events = (
                    self._run_discussion(context, tuple(artifacts), sequence)
                    if is_discussion
                    else self._run_child(child, context, mode, tuple(artifacts), sequence)
                )
                async for event in child_events:
                    sequence = event.sequence + 1
                    if event.artifact is not None and event.artifact.id not in known:
                        await self._repository.put(
                            context.tenant_id, context.run_id, event.artifact
                        )
                        artifacts.append(event.artifact)
                        known.add(event.artifact.id)
                    yield event
                stage_checkpoint = self._checkpoint(
                    context,
                    artifacts=tuple(artifacts),
                    next_sequence=sequence + 1,
                    next_stage=stage_index + 1,
                    terminal=False,
                    reason=None,
                )
                self._last_checkpoint = stage_checkpoint
                yield RunEvent(
                    kind=EventKind.CHECKPOINT_SAVED,
                    sequence=sequence,
                    run_id=context.run_id,
                    checkpoint=stage_checkpoint,
                )
                sequence += 1
            checkpoint = self._checkpoint(
                context,
                artifacts=tuple(artifacts),
                next_sequence=sequence + 1,
                next_stage=len(stages),
                terminal=True,
                reason="explicit_completion",
            )
            self._last_checkpoint = checkpoint
            yield RunEvent(
                kind=EventKind.CHECKPOINT_SAVED,
                sequence=sequence,
                run_id=context.run_id,
                checkpoint=checkpoint,
            )
            sequence += 1
            yield RunEvent(
                kind=EventKind.RUNTIME_COMPLETED,
                sequence=sequence,
                run_id=context.run_id,
                reason="explicit_completion",
            )
        except asyncio.CancelledError:
            yield RunEvent(
                kind=EventKind.RUNTIME_CANCELLED,
                sequence=sequence,
                run_id=context.run_id,
            )
            raise
        except (ArtifactRepositoryError, RuntimeExecutionError, ValueError, TypeError) as error:
            failure_reason = _safe_failure_reason(error, fallback="hybrid_failed")
            partial_reason = _partial_hybrid_completion_reason(artifacts, failure_reason)
            if partial_reason is not None:
                checkpoint = self._checkpoint(
                    context,
                    artifacts=tuple(artifacts),
                    next_sequence=sequence + 1,
                    next_stage=len(self._stages()),
                    terminal=True,
                    reason=partial_reason,
                )
                self._last_checkpoint = checkpoint
                yield RunEvent(
                    kind=EventKind.CHECKPOINT_SAVED,
                    sequence=sequence,
                    run_id=context.run_id,
                    checkpoint=checkpoint,
                )
                sequence += 1
                yield RunEvent(
                    kind=EventKind.RUNTIME_COMPLETED,
                    sequence=sequence,
                    run_id=context.run_id,
                    reason=partial_reason,
                )
                return
            yield RunEvent(
                kind=EventKind.RUNTIME_FAILED,
                sequence=sequence,
                run_id=context.run_id,
                reason=failure_reason,
            )
        finally:
            self._active_child = None
            self._active_task = None

    def _stages(self) -> tuple[tuple[ChildRuntime, TaskMode, bool], ...]:
        if self._plan.upgrade is HybridUpgrade.DISCUSS_DISPATCH_DISCUSS:
            return (
                (self._discussion, TaskMode.DISCUSS, True),
                (self._dispatch, TaskMode.DISPATCH, False),
                (self._discussion, TaskMode.DISCUSS, True),
                (self._synthesizer, TaskMode.DIRECT, False),
            )
        return (
            (self._dispatch, TaskMode.DISPATCH, False),
            (self._discussion, TaskMode.DISCUSS, True),
            (self._synthesizer, TaskMode.DIRECT, False),
        )

    async def _run_discussion(
        self,
        parent: TaskContext,
        artifacts: tuple[Artifact, ...],
        sequence: int,
    ) -> AsyncIterator[RunEvent]:
        handoff_artifacts = _discussion_handoff_artifacts(artifacts)
        participants = getattr(self._discussion, "participant_ids", ("main", "reviewer"))
        if not isinstance(participants, tuple) or not 2 <= len(participants) <= 8:
            raise RuntimeExecutionError("discussion participants are invalid")
        yield RunEvent(
            kind=EventKind.DISCUSSION_STARTED,
            sequence=sequence,
            run_id=parent.run_id,
            actor=participants[0],
            session_id=str(parent.run_id),
            participants=participants,
            inputs=handoff_artifacts,
        )
        async for event in self._run_child(
            self._discussion, parent, TaskMode.DISCUSS, handoff_artifacts, sequence + 1
        ):
            # The composite owns the normalized discussion.started event.
            if event.kind is EventKind.DISCUSSION_STARTED:
                continue
            yield event

    async def _run_child(
        self,
        child: ChildRuntime,
        parent: TaskContext,
        mode: TaskMode,
        artifacts: tuple[Artifact, ...],
        sequence: int,
    ) -> AsyncIterator[RunEvent]:
        if len(artifacts) > 64:
            raise RuntimeExecutionError("hybrid artifact handoff exceeds limit")
        child_context = TaskContext(
            run_id=parent.run_id,
            tenant_id=parent.tenant_id,
            actor_id=parent.actor_id,
            actor_role=parent.actor_role,
            mode=mode,
            request=parent.request,
            artifacts=artifacts,
            routing_decision=parent.routing_decision,
            timeout_seconds=parent.timeout_seconds,
            token_budget=parent.token_budget,
        )
        self._active_child = child
        terminal_seen = False
        child_failure_reason: str | None = None
        try:
            async for item in child.run(child_context):
                if item.kind in {EventKind.STEP_FAILED, EventKind.TOOL_FAILED} and item.reason:
                    child_failure_reason = item.reason
                if item.kind is EventKind.RUNTIME_FAILED:
                    reason = item.reason or child_failure_reason or "runtime failed"
                    raise RuntimeExecutionError(f"hybrid {mode.value} failed: {reason}")
                if item.kind is EventKind.RUNTIME_CANCELLED:
                    raise asyncio.CancelledError
                if item.kind is EventKind.RUNTIME_COMPLETED:
                    terminal_seen = True
                    continue
                if item.kind is EventKind.CHECKPOINT_SAVED:
                    continue
                if (
                    item.kind is EventKind.ARTIFACT_CREATED
                    and item.artifact is not None
                    or _is_forwardable_child_event(item)
                ):
                    yield _renumber_child_event(item, sequence, parent.run_id, inputs=artifacts)
                    sequence += 1
        except RuntimeExecutionError:
            raise
        except Exception as error:  # noqa: BLE001 - child runtime boundary is normalized.
            raise RuntimeExecutionError(
                f"hybrid {mode.value} failed: {_safe_failure_reason(error, fallback='runtime failed')}"
            ) from None
        self._active_child = None
        if not terminal_seen:
            raise RuntimeExecutionError("hybrid child ended without terminal")

    def _checkpoint(
        self,
        context: TaskContext,
        *,
        artifacts: tuple[Artifact, ...],
        next_sequence: int,
        next_stage: int,
        terminal: bool,
        reason: str | None,
    ) -> RuntimeCheckpoint:
        return RuntimeCheckpoint(
            id=uuid4(),
            runtime_type=_RUNTIME_TYPE,
            runtime_version=_RUNTIME_VERSION,
            run_id=context.run_id,
            tenant_id=context.tenant_id,
            mode=self.mode,
            state={
                "plan_digest": self._plan.digest,
                "artifact_registry": {
                    str(artifact.id): artifact.content_sha256 for artifact in artifacts
                },
                "next_sequence": next_sequence,
                "next_stage": next_stage,
                "terminal": terminal,
                "reason": reason,
            },
        )

    def _validate_checkpoint(self, checkpoint: RuntimeCheckpoint, context: TaskContext) -> None:
        if (
            checkpoint.runtime_type != _RUNTIME_TYPE
            or checkpoint.runtime_version != _RUNTIME_VERSION
            or checkpoint.mode is not self.mode
            or checkpoint.run_id != context.run_id
            or checkpoint.tenant_id != context.tenant_id
            or checkpoint.state_sha256 != checkpoint.recompute_state_sha256()
            or checkpoint.state.get("plan_digest") != self._plan.digest
        ):
            raise RuntimeExecutionError("runtime checkpoint is incompatible")
        state = checkpoint.state
        registry = state.get("artifact_registry")
        if (
            set(state)
            != {
                "plan_digest",
                "artifact_registry",
                "next_sequence",
                "next_stage",
                "terminal",
                "reason",
            }
            or not isinstance(registry, Mapping)
            or len(registry) > 64
            or type(state.get("next_sequence")) is not int
            or cast(int, state["next_sequence"]) < 1
            or type(state.get("next_stage")) is not int
            or not 0 <= cast(int, state["next_stage"]) <= len(self._stages())
            or type(state.get("terminal")) is not bool
            or (state.get("reason") is not None and type(state["reason"]) is not str)
        ):
            raise RuntimeExecutionError("runtime checkpoint is incompatible")
        try:
            for artifact_id, sha256 in registry.items():
                if type(artifact_id) is not str or type(sha256) is not str:
                    raise ValueError
                ArtifactReference(id=UUID(artifact_id), sha256=sha256)
        except (TypeError, ValueError):
            raise RuntimeExecutionError("runtime checkpoint is incompatible") from None

    async def _hydrate_checkpoint(
        self, checkpoint: RuntimeCheckpoint, context: TaskContext
    ) -> tuple[Artifact, ...]:
        registry = cast(Mapping[str, str], checkpoint.state["artifact_registry"])
        references = tuple(
            ArtifactReference(id=UUID(artifact_id), sha256=sha256)
            for artifact_id, sha256 in registry.items()
        )
        try:
            artifacts = await self._repository.get_many(
                context.tenant_id, context.run_id, references
            )
        except ArtifactRepositoryError:
            raise RuntimeExecutionError("hybrid checkpoint artifacts are unavailable") from None
        if any(
            artifact.content_sha256 != registry[str(artifact.id)]
            or artifact.recompute_content_sha256() != artifact.content_sha256
            for artifact in artifacts
        ):
            raise RuntimeExecutionError("hybrid checkpoint artifacts are invalid")
        return artifacts

    async def save_checkpoint(self) -> RuntimeCheckpoint:
        if self._last_checkpoint is None:
            raise RuntimeExecutionError("runtime has no checkpoint")
        return self._last_checkpoint

    async def restore_checkpoint(self, checkpoint: RuntimeCheckpoint) -> None:
        if self._active_task is not None or type(checkpoint) is not RuntimeCheckpoint:
            raise RuntimeExecutionError("runtime checkpoint is incompatible")
        validated = RuntimeCheckpoint.from_payload(checkpoint.to_payload())
        if (
            validated.runtime_type != _RUNTIME_TYPE
            or validated.runtime_version != _RUNTIME_VERSION
            or validated.mode is not self.mode
        ):
            raise RuntimeExecutionError("runtime checkpoint is incompatible")
        self._restored = validated
        self._last_checkpoint = validated

    async def cancel(self) -> None:
        child = self._active_child
        if child is not None:
            await child.cancel()
        task = self._active_task
        if task is not None and task is not asyncio.current_task():
            task.cancel()


def _safe_failure_reason(error: Exception, *, fallback: str) -> str:
    return safe_runtime_failure_reason(error, fallback=fallback)


def _is_forwardable_child_event(event: RunEvent) -> bool:
    return event.kind in {
        EventKind.STEP_STARTED,
        EventKind.STEP_COMPLETED,
        EventKind.STEP_FAILED,
        EventKind.STEP_RETRYING,
        EventKind.MODEL_STARTED,
        EventKind.MESSAGE_CREATED,
        EventKind.REVIEW_COMPLETED,
        EventKind.TOOL_STARTED,
        EventKind.TOOL_COMPLETED,
        EventKind.TOOL_FAILED,
    }


def _renumber_child_event(
    event: RunEvent,
    sequence: int,
    run_id: UUID,
    *,
    inputs: tuple[Artifact, ...],
) -> RunEvent:
    updates: dict[str, object] = {"sequence": sequence, "run_id": run_id}
    if event.kind is EventKind.MESSAGE_CREATED:
        updates["session_id"] = str(run_id)
        if not event.inputs:
            updates["inputs"] = inputs
    return event.model_copy(update=updates)


def _discussion_handoff_artifacts(artifacts: tuple[Artifact, ...]) -> tuple[Artifact, ...]:
    """Keep discussion inputs compact and user-readable.

    Dispatch runtimes often emit a raw model_response followed by a text artifact
    whose source_ids point at that raw model_response. Passing both into the
    discussion stage doubles prompt size without adding information and can make
    real provider calls time out. Keep the text wrapper and drop the wrapped raw
    model_response.
    """

    wrapped_source_ids = {
        source_id for artifact in artifacts for source_id in artifact.source_ids
    }
    return tuple(
        artifact
        for artifact in artifacts
        if not (artifact.type == "model_response" and str(artifact.id) in wrapped_source_ids)
    )


def _partial_hybrid_completion_reason(
    artifacts: list[Artifact],
    failure_reason: str,
) -> str | None:
    if not artifacts:
        return None
    if failure_reason.startswith("hybrid discuss failed: model gateway failed"):
        return "partial_hybrid_after_discussion_failure"
    if failure_reason.startswith("hybrid direct failed: model gateway failed"):
        return "partial_hybrid_after_synthesis_failure"
    return None


__all__ = ["HybridPlan", "HybridRuntime", "HybridUpgrade", "RuntimeExecutionError"]
