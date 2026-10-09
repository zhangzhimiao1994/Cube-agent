"""Framework-neutral composition of dispatch, discussion, and synthesis runtimes."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from time import monotonic
from types import MappingProxyType
from typing import Protocol, cast
from uuid import UUID, uuid4

from agent_hub.domain.runs import TaskMode
from agent_hub.runtime.adaptive_budget import AdaptiveDeadline, deadline_from_routing
from agent_hub.runtime.artifacts import (
    ArtifactReference,
    ArtifactRepository,
    ArtifactRepositoryError,
    InMemoryArtifactRepository,
)
from agent_hub.runtime.contracts import (
    Artifact,
    EventKind,
    JsonValue,
    RunEvent,
    RuntimeCheckpoint,
    TaskContext,
)
from agent_hub.runtime.failure_reason import (
    runtime_failure_diagnostic_from_reason,
    safe_runtime_failure_reason,
)
from agent_hub.runtime.generated_file_recovery import (
    final_attachment_ready_text,
    final_attachment_result,
    final_attachment_text_conflicts,
)
from agent_hub.runtime.model_scope import validate_model_scope_artifact, validate_model_scope_parts
from agent_hub.runtime.streams import closing_runtime_events
from agent_hub.runtime.token_budget import (
    TokenBudgetExhausted,
    TokenBudgetSource,
    token_budget_events,
)

_RUNTIME_TYPE = "hybrid"
_RUNTIME_VERSION = "3"
_PREVIOUS_RUNTIME_VERSION = "2"
_LEGACY_RUNTIME_VERSION = "1"
_MAX_HANDOFF_ARTIFACTS = 64
_MAX_HANDOFF_ANCHORS = 8


class RuntimeExecutionError(RuntimeError):
    """Stable composite runtime failure."""


class RuntimeBusy(RuntimeExecutionError):
    """One HybridRuntime instance is single-flight."""


class _ChildModelScopeError(RuntimeExecutionError):
    """Scope integrity failures must not become partial delivery successes."""


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


@dataclass(slots=True)
class _ModelScopeBoundary:
    parts: list[Mapping[str, object]] = field(default_factory=list)
    incomplete: bool = False


@dataclass(slots=True)
class _StageBudget:
    token_limit: int
    timeout_limit: float
    token_source: TokenBudgetSource | None = None
    checkpoint_tokens: int = 0
    checkpoint_token_baseline: int = 0
    artifact_tokens: int = 0
    accounted_artifact_ids: set[UUID] = field(default_factory=set)
    model_scope: _ModelScopeBoundary = field(default_factory=_ModelScopeBoundary)

    @property
    def consumed_tokens(self) -> int:
        checkpoint_delta = max(
            0,
            self.checkpoint_tokens - self.checkpoint_token_baseline,
        )
        return max(checkpoint_delta, self.artifact_tokens)


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
        token_source = TokenBudgetSource.from_context(context)
        remaining_tokens = token_source.limit
        spent_tokens = 0
        remaining_timeout_seconds = context.timeout_seconds
        deadline: float | None = None
        adaptive_deadline: AdaptiveDeadline | None = None
        timeout_progress_units = 0
        restored_absolute_timeout_seconds: float | None = None
        last_child_progress_fingerprint: str | None = None
        restored_child_checkpoint: RuntimeCheckpoint | None = None
        model_scope = _ModelScopeBoundary()
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
                restored_tokens = restored.state.get("remaining_token_budget")
                restored_timeout = restored.state.get("remaining_timeout_seconds")
                restored_absolute_timeout = restored.state.get(
                    "remaining_absolute_timeout_seconds"
                )
                restored_progress_units = restored.state.get("timeout_progress_units")
                restored_progress_fingerprint = restored.state.get(
                    "last_child_progress_fingerprint"
                )
                if type(restored_tokens) is int:
                    remaining_tokens = restored_tokens
                if isinstance(restored_timeout, int | float) and not isinstance(
                    restored_timeout, bool
                ):
                    remaining_timeout_seconds = float(restored_timeout)
                if isinstance(restored_absolute_timeout, int | float) and not isinstance(
                    restored_absolute_timeout, bool
                ):
                    restored_absolute_timeout_seconds = float(restored_absolute_timeout)
                if type(restored_progress_units) is int and restored_progress_units >= 0:
                    timeout_progress_units = restored_progress_units
                if type(restored_progress_fingerprint) is str:
                    last_child_progress_fingerprint = restored_progress_fingerprint
                restored_child_checkpoint = _child_checkpoint_from_state(restored)
                if restored.runtime_version == _RUNTIME_VERSION:
                    restored_grant = cast(int, restored.state["token_budget_grant"])
                    spent_tokens = cast(int, restored.state["token_budget_spent"])
                elif (
                    next_stage == 0 and type(restored_tokens) is int
                    and restored_child_checkpoint is not None
                    and isinstance(restored_child_checkpoint.state.get("usage"), Mapping)
                    and any(
                        type(value) is int and 0 <= value <= 10_000_000
                        for key, value in cast(
                            Mapping[str, object], restored_child_checkpoint.state["usage"],
                        ).items() if key in {"tokens", "total_tokens"}
                    )
                ):
                    spent_tokens = _reported_tokens(restored_child_checkpoint.state)
                    restored_grant = restored_tokens + spent_tokens
                    if restored_grant > 10_000_000:
                        raise RuntimeExecutionError("runtime checkpoint is incompatible")
                else:
                    raise RuntimeExecutionError("runtime checkpoint token history is unavailable")
                # A larger resume context is not a new grant. Preserve historical
                # consumption, while subsequent live source changes still propagate.
                token_source = token_source.remaining(
                    spent=max(0, token_source.limit - restored_grant),
                )
            elif context.checkpoint is not None:
                raise RuntimeExecutionError("runtime checkpoint was not restored")
            else:
                next_stage = 0

            remaining_tokens = max(0, token_source.limit - spent_tokens)

            for artifact in artifacts:
                await self._repository.put(context.tenant_id, context.run_id, artifact)

            stages = self._stages()
            loop_time = monotonic()
            adaptive_deadline = deadline_from_routing(
                context.routing_decision,
                now=loop_time,
                initial_seconds=max(0.001, remaining_timeout_seconds),
                restored_absolute_seconds=restored_absolute_timeout_seconds,
                progress_units=timeout_progress_units,
            )
            deadline = adaptive_deadline.deadline
            if (
                restored is None
                and self._plan.upgrade is HybridUpgrade.DISPATCH_TO_HYBRID
                and artifacts
            ):
                next_stage = 1

            for stage_index in range(next_stage, len(stages)):
                child, mode, is_discussion = stages[stage_index]
                child_checkpoint = (
                    restored_child_checkpoint if stage_index == next_stage else None
                )
                stage_timeout = deadline - monotonic()
                remaining_tokens = max(0, token_source.limit - spent_tokens)
                if remaining_tokens < 1:
                    raise RuntimeExecutionError("hybrid token budget exhausted")
                if stage_timeout <= 0:
                    raise RuntimeExecutionError("hybrid timeout budget exhausted")
                handoff_artifacts = _bounded_handoff_artifacts(
                    _discussion_handoff_artifacts(tuple(artifacts))
                    if is_discussion
                    else tuple(artifacts)
                )
                stage_budget = _StageBudget(
                    token_limit=remaining_tokens,
                    timeout_limit=stage_timeout,
                    model_scope=model_scope,
                    checkpoint_tokens=(
                        _reported_tokens(child_checkpoint.state)
                        if child_checkpoint is not None
                        else 0
                    ),
                    checkpoint_token_baseline=(
                        _reported_tokens(child_checkpoint.state)
                        if child_checkpoint is not None
                        else 0
                    ),
                    accounted_artifact_ids=(
                        {artifact.id for artifact in handoff_artifacts}
                        if child_checkpoint is not None
                        else set()
                    ),
                )
                stage_budget.token_source = token_source.remaining(
                    spent=spent_tokens,
                    checkpoint_baseline=stage_budget.checkpoint_token_baseline,
                )
                child_events = (
                    self._run_discussion(
                        context,
                        handoff_artifacts,
                        sequence,
                        stage_budget,
                        child_checkpoint,
                        allow_gateway_failure=(
                            stage_index == 0
                            and self._plan.upgrade
                            is HybridUpgrade.DISCUSS_DISPATCH_DISCUSS
                        ),
                    )
                    if is_discussion
                    else self._run_child(
                        child,
                        context,
                        mode,
                        handoff_artifacts,
                        sequence,
                        stage_budget,
                        child_checkpoint,
                    )
                )
                async with closing_runtime_events(child_events) as events:
                    async for event in events:
                        remaining_tokens = max(0, token_source.limit - spent_tokens)
                        stage_budget.token_limit = remaining_tokens
                        sequence = event.sequence + 1
                        if event.kind is EventKind.CHECKPOINT_SAVED:
                            if event.checkpoint is None:
                                raise RuntimeExecutionError(
                                    "hybrid child checkpoint is unavailable"
                                )
                            if stage_budget.consumed_tokens > remaining_tokens:
                                raise RuntimeExecutionError(
                                    "hybrid child exceeded token budget"
                                )
                            child_progress_fingerprint = hashlib.sha256(
                                json.dumps(
                                    {
                                        "stage": stage_index,
                                        "state_sha256": event.checkpoint.state_sha256,
                                        "consumed_tokens": stage_budget.consumed_tokens,
                                    },
                                    sort_keys=True,
                                    separators=(",", ":"),
                                ).encode("utf-8")
                            ).hexdigest()
                            if child_progress_fingerprint != last_child_progress_fingerprint:
                                last_child_progress_fingerprint = child_progress_fingerprint
                                timeout_progress_units += 1
                                adaptive_deadline.observe(
                                    progress_units=timeout_progress_units,
                                    now=monotonic(),
                                )
                            deadline = adaptive_deadline.deadline
                            in_stage_checkpoint = self._checkpoint(
                                context,
                                artifacts=tuple(artifacts),
                                next_sequence=sequence,
                                next_stage=stage_index,
                                terminal=False,
                                reason=None,
                                remaining_tokens=(
                                    remaining_tokens - stage_budget.consumed_tokens
                                ),
                                token_budget_grant=token_source.limit,
                                token_budget_spent=spent_tokens + stage_budget.consumed_tokens,
                                remaining_timeout_seconds=max(0.0, deadline - monotonic()),
                                remaining_absolute_timeout_seconds=(
                                    adaptive_deadline.absolute_remaining(now=monotonic())
                                ),
                                timeout_progress_units=timeout_progress_units,
                                last_child_progress_fingerprint=(
                                    last_child_progress_fingerprint
                                ),
                                child_checkpoint=event.checkpoint,
                            )
                            self._last_checkpoint = in_stage_checkpoint
                            yield event.model_copy(
                                update={"checkpoint": in_stage_checkpoint}
                            )
                            continue
                        if event.artifact is not None and event.artifact.id not in known:
                            await self._repository.put(
                                context.tenant_id, context.run_id, event.artifact
                            )
                            artifacts.append(event.artifact)
                            known.add(event.artifact.id)
                        yield event
                remaining_tokens = max(0, token_source.limit - spent_tokens)
                if stage_budget.consumed_tokens > remaining_tokens:
                    raise RuntimeExecutionError("hybrid child exceeded token budget")
                spent_tokens += stage_budget.consumed_tokens
                remaining_tokens = max(0, token_source.limit - spent_tokens)
                timeout_progress_units += 1
                adaptive_deadline.observe(
                    progress_units=timeout_progress_units,
                    now=monotonic(),
                )
                deadline = adaptive_deadline.deadline
                remaining_timeout_seconds = max(0.0, deadline - monotonic())
                stage_checkpoint = self._checkpoint(
                    context,
                    artifacts=tuple(artifacts),
                    next_sequence=sequence + 1,
                    next_stage=stage_index + 1,
                    terminal=False,
                    reason=None,
                    remaining_tokens=remaining_tokens,
                    token_budget_grant=token_source.limit,
                    token_budget_spent=spent_tokens,
                    remaining_timeout_seconds=remaining_timeout_seconds,
                    remaining_absolute_timeout_seconds=(
                        adaptive_deadline.absolute_remaining(now=monotonic())
                    ),
                    timeout_progress_units=timeout_progress_units,
                    last_child_progress_fingerprint=last_child_progress_fingerprint,
                    child_checkpoint=None,
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
                remaining_tokens=remaining_tokens,
                token_budget_grant=token_source.limit,
                token_budget_spent=spent_tokens,
                remaining_timeout_seconds=max(0.0, deadline - monotonic()),
                remaining_absolute_timeout_seconds=(
                    adaptive_deadline.absolute_remaining(now=monotonic())
                ),
                timeout_progress_units=timeout_progress_units,
                last_child_progress_fingerprint=last_child_progress_fingerprint,
                child_checkpoint=None,
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
            partial_reason = (
                None if isinstance(error, _ChildModelScopeError)
                else _partial_hybrid_completion_reason(artifacts, failure_reason)
            )
            if partial_reason is not None:
                checkpoint = self._checkpoint(
                    context,
                    artifacts=tuple(artifacts),
                    next_sequence=sequence + 1,
                    next_stage=len(self._stages()),
                    terminal=True,
                    reason=partial_reason,
                    remaining_tokens=remaining_tokens,
                    token_budget_grant=token_source.limit,
                    token_budget_spent=spent_tokens,
                    remaining_timeout_seconds=(
                        max(0.0, deadline - monotonic())
                        if deadline is not None
                        else max(0.0, remaining_timeout_seconds)
                    ),
                    remaining_absolute_timeout_seconds=(
                        adaptive_deadline.absolute_remaining(now=monotonic())
                        if adaptive_deadline is not None
                        else max(0.0, remaining_timeout_seconds)
                    ),
                    timeout_progress_units=timeout_progress_units,
                    last_child_progress_fingerprint=last_child_progress_fingerprint,
                    child_checkpoint=None,
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
            closure_artifact = _empty_model_response_closure_artifact(context, failure_reason)
            if closure_artifact is not None:
                await self._repository.put(context.tenant_id, context.run_id, closure_artifact)
                artifacts.append(closure_artifact)
                yield RunEvent(
                    kind=EventKind.ARTIFACT_CREATED,
                    sequence=sequence,
                    run_id=context.run_id,
                    artifact=closure_artifact,
                )
                sequence += 1
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
        stage_budget: _StageBudget,
        checkpoint: RuntimeCheckpoint | None,
        *,
        allow_gateway_failure: bool = False,
    ) -> AsyncIterator[RunEvent]:
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
            inputs=artifacts,
        )
        try:
            async with closing_runtime_events(self._run_child(
                self._discussion,
                parent,
                TaskMode.DISCUSS,
                artifacts,
                sequence + 1,
                stage_budget,
                checkpoint,
            )) as events:
                async for event in events:
                    # The composite owns the normalized discussion.started event.
                    if event.kind is EventKind.DISCUSSION_STARTED:
                        continue
                    yield event
        except RuntimeExecutionError as error:
            reason = _safe_failure_reason(error, fallback="discussion_failed")
            if not (
                allow_gateway_failure
                and reason.startswith("hybrid discuss failed: model gateway failed")
            ):
                raise
            self._active_child = None

    async def _run_child(
        self,
        child: ChildRuntime,
        parent: TaskContext,
        mode: TaskMode,
        artifacts: tuple[Artifact, ...],
        sequence: int,
        stage_budget: _StageBudget,
        checkpoint: RuntimeCheckpoint | None,
    ) -> AsyncIterator[RunEvent]:
        child_context = TaskContext(
            run_id=parent.run_id,
            tenant_id=parent.tenant_id,
            actor_id=parent.actor_id,
            actor_role=parent.actor_role,
            mode=mode,
            request=parent.request,
            artifacts=artifacts,
            routing_decision=parent.routing_decision,
            timeout_seconds=stage_budget.timeout_limit,
            token_budget=min(
                10_000_000,
                stage_budget.token_limit + stage_budget.checkpoint_token_baseline,
            ),
            checkpoint=checkpoint,
        )
        self._active_child = child
        terminal_seen = False
        child_failure_reason: str | None = None
        model_starts: dict[UUID, RunEvent] = {}
        scope_artifacts: dict[str, RunEvent] = {}
        try:
            if checkpoint is not None:
                await child.restore_checkpoint(checkpoint)
            child_stream = (
                token_budget_events(
                    lambda: child.run(child_context), child_context, stage_budget.token_source,
                )
                if stage_budget.token_source is not None
                else child.run(child_context)
            )
            async with closing_runtime_events(child_stream) as events:
                async for item in events:
                    scope_artifact = _validate_child_model_scope_event(
                        item, parent.tenant_id, model_starts, scope_artifacts,
                    )
                    if scope_artifact is not None and item.kind is EventKind.ARTIFACT_CREATED:
                        stage_budget.model_scope.parts.append(validate_model_scope_artifact(
                            scope_artifact, str(item.run_id),
                        ))
                    elif item.kind == "model.scope_incomplete":
                        stage_budget.model_scope.incomplete = True
                    _record_stage_usage(stage_budget, item)
                    if item.kind in {EventKind.STEP_FAILED, EventKind.TOOL_FAILED} and item.reason:
                        child_failure_reason = item.reason
                    if item.kind is EventKind.RUNTIME_FAILED:
                        reason = item.reason or child_failure_reason or "runtime failed"
                        raise RuntimeExecutionError(f"hybrid {mode.value} failed: {reason}")
                    if item.kind is EventKind.RUNTIME_CANCELLED:
                        raise asyncio.CancelledError
                    if item.kind is EventKind.RUNTIME_COMPLETED:
                        _seal_child_model_scope(scope_artifacts, stage_budget.model_scope)
                        terminal_seen = True
                        continue
                    if item.kind is EventKind.CHECKPOINT_SAVED:
                        _seal_child_model_scope(scope_artifacts, stage_budget.model_scope)
                        yield _renumber_child_event(
                            item,
                            sequence,
                            parent.run_id,
                            inputs=artifacts,
                        )
                        sequence += 1
                        continue
                    if mode is TaskMode.DIRECT:
                        item = _reconcile_synthesis_event(item, artifacts)
                    if (
                        item.kind is EventKind.ARTIFACT_CREATED
                        and item.artifact is not None
                        or _is_forwardable_child_event(item)
                    ):
                        yield _renumber_child_event(
                            item, sequence, parent.run_id, inputs=artifacts,
                            scope_artifact=scope_artifact,
                        )
                        sequence += 1
        except TokenBudgetExhausted:
            raise RuntimeExecutionError("hybrid token budget exhausted") from None
        except RuntimeExecutionError:
            raise
        except Exception as error:  # noqa: BLE001 - child runtime boundary is normalized.
            raise RuntimeExecutionError(
                f"hybrid {mode.value} failed: {_safe_failure_reason(error, fallback='runtime failed')}"
            ) from None
        self._active_child = None
        _seal_child_model_scope(scope_artifacts, stage_budget.model_scope)
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
        remaining_tokens: int,
        token_budget_grant: int,
        token_budget_spent: int,
        remaining_timeout_seconds: float,
        remaining_absolute_timeout_seconds: float,
        timeout_progress_units: int,
        last_child_progress_fingerprint: str | None,
        child_checkpoint: RuntimeCheckpoint | None,
    ) -> RuntimeCheckpoint:
        checkpoint_artifacts = _bounded_handoff_artifacts(artifacts)
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
                    str(artifact.id): artifact.content_sha256
                    for artifact in checkpoint_artifacts
                },
                "next_sequence": next_sequence,
                "next_stage": next_stage,
                "terminal": terminal,
                "reason": reason,
                "remaining_token_budget": remaining_tokens,
                "token_budget_grant": token_budget_grant,
                "token_budget_spent": token_budget_spent,
                "remaining_timeout_seconds": remaining_timeout_seconds,
                "remaining_absolute_timeout_seconds": remaining_absolute_timeout_seconds,
                "timeout_progress_units": timeout_progress_units,
                "last_child_progress_fingerprint": last_child_progress_fingerprint,
                "child_checkpoint": (
                    None
                    if child_checkpoint is None
                    else cast(JsonValue, child_checkpoint.to_payload())
                ),
            },
        )

    def _validate_checkpoint(self, checkpoint: RuntimeCheckpoint, context: TaskContext) -> None:
        if (
            checkpoint.runtime_type != _RUNTIME_TYPE
            or checkpoint.runtime_version
            not in {_LEGACY_RUNTIME_VERSION, _PREVIOUS_RUNTIME_VERSION, _RUNTIME_VERSION}
            or checkpoint.mode is not self.mode
            or checkpoint.run_id != context.run_id
            or checkpoint.tenant_id != context.tenant_id
            or checkpoint.state_sha256 != checkpoint.recompute_state_sha256()
            or checkpoint.state.get("plan_digest") != self._plan.digest
        ):
            raise RuntimeExecutionError("runtime checkpoint is incompatible")
        state = checkpoint.state
        registry = state.get("artifact_registry")
        required_state = {
            "plan_digest",
            "artifact_registry",
            "next_sequence",
            "next_stage",
            "terminal",
            "reason",
        }
        budget_state = {"remaining_token_budget", "remaining_timeout_seconds"}
        adaptive_budget_state = {
            "remaining_absolute_timeout_seconds",
            "timeout_progress_units",
            "last_child_progress_fingerprint",
        }
        child_state = {"child_checkpoint"}
        token_history_state = {"token_budget_grant", "token_budget_spent"}
        has_budget_state = budget_state.issubset(state)
        allowed_state = (
            {frozenset(required_state), frozenset(required_state | budget_state)}
            if checkpoint.runtime_version == _LEGACY_RUNTIME_VERSION
            else {
                frozenset(required_state | budget_state | child_state),
                frozenset(
                    required_state
                    | budget_state
                    | adaptive_budget_state
                    | child_state
                )
            }
        )
        if checkpoint.runtime_version == _RUNTIME_VERSION:
            allowed_state = {
                frozenset(required_state | budget_state | child_state
                          | adaptive_budget_state | token_history_state),
            }
            grant = state.get("token_budget_grant")
            spent = state.get("token_budget_spent")
            if (
                type(grant) is not int or not 0 <= grant <= 10_000_000
                or type(spent) is not int or not 0 <= spent <= 10_000_000
                or state.get("remaining_token_budget") != max(0, grant - spent)
            ):
                raise RuntimeExecutionError("runtime checkpoint is incompatible")
        if (
            frozenset(state) not in allowed_state
            or not isinstance(registry, Mapping)
            or len(registry) > _MAX_HANDOFF_ARTIFACTS
            or type(state.get("next_sequence")) is not int
            or cast(int, state["next_sequence"]) < 1
            or type(state.get("next_stage")) is not int
            or not 0 <= cast(int, state["next_stage"]) <= len(self._stages())
            or type(state.get("terminal")) is not bool
            or (state.get("reason") is not None and type(state["reason"]) is not str)
            or (
                has_budget_state
                and (
                    type(state["remaining_token_budget"]) is not int
                    or not 0 <= state["remaining_token_budget"] <= 10_000_000
                    or isinstance(state["remaining_timeout_seconds"], bool)
                    or not isinstance(state["remaining_timeout_seconds"], int | float)
                    or not math.isfinite(float(state["remaining_timeout_seconds"]))
                    or not 0.0 <= float(state["remaining_timeout_seconds"]) <= 3600.0
                    or (
                        checkpoint.runtime_version != _LEGACY_RUNTIME_VERSION
                        and adaptive_budget_state.issubset(state)
                        and (
                            isinstance(
                                state["remaining_absolute_timeout_seconds"], bool
                            )
                            or not isinstance(
                                state["remaining_absolute_timeout_seconds"], int | float
                            )
                            or not math.isfinite(
                                float(state["remaining_absolute_timeout_seconds"])
                            )
                            or not 0.0
                            <= float(state["remaining_absolute_timeout_seconds"])
                            <= 3600.0
                            or type(state["timeout_progress_units"]) is not int
                            or state["timeout_progress_units"] < 0
                            or (
                                state["last_child_progress_fingerprint"] is not None
                                and (
                                    type(
                                        state["last_child_progress_fingerprint"]
                                    )
                                    is not str
                                    or len(state["last_child_progress_fingerprint"]) != 64
                                )
                            )
                        )
                    )
                )
            )
        ):
            raise RuntimeExecutionError("runtime checkpoint is incompatible")
        try:
            for artifact_id, sha256 in registry.items():
                if type(artifact_id) is not str or type(sha256) is not str:
                    raise ValueError
                ArtifactReference(id=UUID(artifact_id), sha256=sha256)
        except (TypeError, ValueError):
            raise RuntimeExecutionError("runtime checkpoint is incompatible") from None
        child_checkpoint = _child_checkpoint_from_state(checkpoint)
        next_stage = cast(int, state["next_stage"])
        if child_checkpoint is not None and (
            checkpoint.runtime_version == _LEGACY_RUNTIME_VERSION
            or state["terminal"] is True
            or next_stage >= len(self._stages())
            or child_checkpoint.run_id != context.run_id
            or child_checkpoint.tenant_id != context.tenant_id
            or child_checkpoint.mode is not self._stages()[next_stage][1]
        ):
            raise RuntimeExecutionError("runtime checkpoint is incompatible")

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
            or validated.runtime_version
            not in {_LEGACY_RUNTIME_VERSION, _PREVIOUS_RUNTIME_VERSION, _RUNTIME_VERSION}
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


def _child_checkpoint_from_state(
    checkpoint: RuntimeCheckpoint,
) -> RuntimeCheckpoint | None:
    payload = checkpoint.state.get("child_checkpoint")
    if payload is None:
        return None
    if not isinstance(payload, Mapping):
        raise RuntimeExecutionError("runtime checkpoint is incompatible")
    try:
        return RuntimeCheckpoint.from_payload(_mutable_json_value(payload))
    except (TypeError, ValueError):
        raise RuntimeExecutionError("runtime checkpoint is incompatible") from None


def _mutable_json_value(value: JsonValue) -> object:
    if isinstance(value, Mapping):
        return {key: _mutable_json_value(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_mutable_json_value(item) for item in value]
    return value


def _is_forwardable_child_event(event: RunEvent) -> bool:
    return event.kind in {
        "model.failure_receipt",
        "model.scope_incomplete",
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


def _validate_child_model_scope_event(
    event: RunEvent,
    tenant_id: UUID,
    starts: dict[UUID, RunEvent],
    artifacts: dict[str, RunEvent],
) -> Artifact | None:
    if event.kind is EventKind.MODEL_STARTED and event.actor == "main_agent":
        starts[event.run_id] = event
        return None
    artifact = event.artifact
    is_scope = artifact is not None and artifact.type == "model_attempt"
    if not is_scope and event.kind not in {"model.failure_receipt", "model.scope_incomplete"}:
        return None
    try:
        start = starts.get(event.run_id)
        if start is None or start.sequence >= event.sequence:
            raise ValueError
        if is_scope:
            assert artifact is not None
            # Verify the original envelope before adding the parent event association.
            validated = Artifact.from_payload(artifact.to_payload())
            content = validate_model_scope_artifact(validated, str(event.run_id))
            provenance = validated.provenance
            if provenance is None:
                raise ValueError
            calls = cast(list[Mapping[str, object]], content["calls"])
            attempts = tuple(dict.fromkeys(
                model for call in calls
                for model in cast(list[str], cast(Mapping[str, object], call["receipt"])[
                    "attempted_logical_models"
                ])
            ))
            if (
                event.actor != "main_agent" or content["tenant_id"] != str(tenant_id)
                or start.payload.get("logical_model") != content["requested_logical_model"]
                or any(event.payload.get(key) != value for key, value in (
                    ("artifact_id", str(artifact.id)),
                    ("requested_logical_model", content["requested_logical_model"]),
                    ("logical_model", provenance.logical_model),
                    ("attempted_logical_models", attempts),
                    ("deployment", provenance.deployment_id),
                    ("provider", provenance.provider_id),
                    ("upstream_model", provenance.provider_model),
                ))
                or str(artifact.id) in artifacts
            ):
                raise ValueError
            artifacts[str(artifact.id)] = event
            return artifact
        if event.actor is not None or event.payload.get("actor") != "main_agent":
            raise ValueError
        if event.kind == "model.scope_incomplete":
            if event.payload.get("logical_model") != start.payload.get("logical_model"):
                raise ValueError
            return None
        linked = artifacts.pop(cast(str, event.payload.get("artifact_id")), None)
        if (
            linked is None or linked.run_id != event.run_id or linked.sequence >= event.sequence
            or any(event.payload.get(key) != linked.payload.get(key) for key in (
                "artifact_id", "logical_model", "requested_logical_model",
                "attempted_logical_models", "deployment", "provider", "upstream_model",
            ))
        ):
            raise ValueError
        return linked.artifact
    except Exception:  # noqa: BLE001 - do not disclose malformed scope evidence.
        raise _ChildModelScopeError("hybrid child model scope evidence is invalid") from None


def _seal_child_model_scope(
    pending: Mapping[str, RunEvent], boundary: _ModelScopeBoundary,
) -> None:
    try:
        if pending or boundary.incomplete:
            raise ValueError
        if boundary.parts:
            validate_model_scope_parts(tuple(boundary.parts))
    except Exception:  # noqa: BLE001 - do not expose untrusted scope metadata.
        raise _ChildModelScopeError("hybrid child model scope evidence is invalid") from None


def _renumber_child_event(
    event: RunEvent,
    sequence: int,
    run_id: UUID,
    *,
    inputs: tuple[Artifact, ...],
    scope_artifact: Artifact | None = None,
) -> RunEvent:
    updates: dict[str, object] = {"sequence": sequence, "run_id": run_id}
    if scope_artifact is not None or event.kind in {
        EventKind.MODEL_STARTED, "model.scope_incomplete",
    }:
        origin: dict[str, JsonValue] = {
            "schema_version": 1, "source": "hybrid_runtime", "run_id": str(event.run_id),
            "sequence": event.sequence, "parent_run_id": str(run_id),
        }
        if scope_artifact is not None:
            origin.update(artifact_id=str(scope_artifact.id),
                          content_sha256=scope_artifact.content_sha256)
        if "model_scope_origin" in event.payload:
            raise _ChildModelScopeError("hybrid child model scope evidence is invalid")
        updates["payload"] = MappingProxyType({
            **event.payload, "model_scope_origin": MappingProxyType(origin),
        })
    if event.kind is EventKind.MESSAGE_CREATED:
        updates["session_id"] = str(run_id)
        if not event.inputs:
            updates["inputs"] = inputs
    return event.model_copy(update=updates)


def _reconcile_synthesis_event(
    event: RunEvent,
    prior_artifacts: tuple[Artifact, ...],
) -> RunEvent:
    artifact = event.artifact
    if event.kind is not EventKind.ARTIFACT_CREATED or artifact is None:
        return event
    if artifact.type != "text":
        return event
    text = artifact.content.get("text")
    if type(text) is not str or not final_attachment_text_conflicts(text):
        return event
    result = final_attachment_result(prior_artifacts)
    if result is None:
        return event
    content = dict(artifact.content)
    content["text"] = final_attachment_ready_text(result)
    reconciled = Artifact(
        id=artifact.id,
        version=artifact.version,
        type=artifact.type,
        producer=artifact.producer,
        content=content,
        source_ids=artifact.source_ids,
        provenance=artifact.provenance,
    )
    updates: dict[str, object] = {"artifact": reconciled}
    payload = event.payload
    if "output" in payload:
        next_payload = dict(payload)
        next_payload["output"] = reconciled.content["text"]
        updates["payload"] = next_payload
    return event.model_copy(update=updates)


def _record_stage_usage(stage_budget: _StageBudget, event: RunEvent) -> None:
    artifact = event.artifact
    if artifact is not None and artifact.id not in stage_budget.accounted_artifact_ids:
        stage_budget.accounted_artifact_ids.add(artifact.id)
        stage_budget.artifact_tokens += _reported_tokens(artifact.content)
    checkpoint = event.checkpoint
    if checkpoint is not None:
        stage_budget.checkpoint_tokens = max(
            stage_budget.checkpoint_tokens,
            _reported_tokens(checkpoint.state),
        )


def _reported_tokens(payload: Mapping[str, object]) -> int:
    raw_usage = payload.get("usage")
    if not isinstance(raw_usage, Mapping):
        return 0
    total_tokens = raw_usage.get("total_tokens")
    if type(total_tokens) is int and total_tokens >= 0:
        return total_tokens
    tokens = raw_usage.get("tokens")
    if type(tokens) is int and tokens >= 0:
        return tokens
    prompt_tokens = raw_usage.get("prompt_tokens")
    completion_tokens = raw_usage.get("completion_tokens")
    if (
        type(prompt_tokens) is int
        and type(completion_tokens) is int
        and prompt_tokens >= 0
        and completion_tokens >= 0
    ):
        return prompt_tokens + completion_tokens
    return 0


def _bounded_handoff_artifacts(artifacts: tuple[Artifact, ...]) -> tuple[Artifact, ...]:
    if len(artifacts) <= _MAX_HANDOFF_ARTIFACTS:
        return artifacts
    anchors = artifacts[:_MAX_HANDOFF_ANCHORS]
    anchor_ids = {artifact.id for artifact in anchors}
    latest_count = _MAX_HANDOFF_ARTIFACTS - len(anchors)
    latest = tuple(
        artifact
        for artifact in artifacts[-latest_count:]
        if artifact.id not in anchor_ids
    )
    if len(latest) < latest_count:
        latest_ids = {artifact.id for artifact in latest}
        fill = tuple(
            artifact
            for artifact in artifacts[_MAX_HANDOFF_ANCHORS : -latest_count]
            if artifact.id not in anchor_ids and artifact.id not in latest_ids
        )[-(latest_count - len(latest)) :]
        latest = fill + latest
    return anchors + latest


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
    delivery_artifacts = [artifact for artifact in artifacts if artifact.type != "model_attempt"]
    if not delivery_artifacts:
        return None
    if final_attachment_result(delivery_artifacts) is not None:
        return "partial_hybrid_after_final_attachment"
    if failure_reason.startswith("hybrid discuss failed: model gateway failed"):
        return "partial_hybrid_after_discussion_failure"
    if failure_reason.startswith("hybrid direct failed: model gateway failed"):
        return "partial_hybrid_after_synthesis_failure"
    return None


def _empty_model_response_closure_artifact(
    context: TaskContext,
    failure_reason: str,
) -> Artifact | None:
    diagnostic = runtime_failure_diagnostic_from_reason(failure_reason)
    if diagnostic.get("error_code") != "model.empty_response":
        return None
    text = (
        "Harness 已保留中断前状态，但模型返回了空内容，当前没有可交付产物。\n\n"
        "已完成的处置：识别为空响应、要求压缩输入和历史上下文、建议拆分提示并标记模型 fallback。"
        "请在自修复流程中用更小任务重试；如果仍为空，检查对应模型配置和服务商可用性。"
    )
    return Artifact(
        id=uuid4(),
        type="text",
        producer="harness_failure_closure",
        content={
            "text": text,
            "error_code": "model.empty_response",
            "recovery_layers": (
                "input_compaction",
                "prompt_split",
                "model_fallback_marked",
                "failed_closure",
            ),
        },
    )


__all__ = ["HybridPlan", "HybridRuntime", "HybridUpgrade", "RuntimeExecutionError"]
