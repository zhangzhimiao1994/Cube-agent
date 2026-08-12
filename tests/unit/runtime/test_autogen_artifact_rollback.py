from __future__ import annotations

import asyncio
from decimal import Decimal
from uuid import UUID, uuid4

import pytest

from agent_hub.domain.runs import TaskMode
from agent_hub.models.gateway import GatewayCompletion
from agent_hub.models.types import ModelRequest, ModelResponse, TokenUsage
from agent_hub.runtime.artifacts import (
    ArtifactReference,
    ArtifactRepositoryError,
    InMemoryArtifactRepository,
)
from agent_hub.runtime.autogen.adapter import (
    AutoGenDiscussionRuntime,
    DiscussionParticipant,
    DiscussionPlan,
    RuntimeExecutionError,
    _can_complete_with_partial_discussion,
)
from agent_hub.runtime.contracts import Artifact, TaskContext

TENANT_ID = UUID("00000000-0000-4000-8000-000000000041")
RUN_ID = UUID("00000000-0000-4000-8000-000000000042")


class UnusedGateway:
    async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
        return GatewayCompletion(
            response=ModelResponse(text="unused", usage=TokenUsage(1, 1, 2)),
            deployment_id="primary",
            logical_model=request.logical_model,
            provider_id="deepseek",
            provider_model="deepseek-v4-flash",
            cost_usd=Decimal(0),
        )


def _runtime(repository: InMemoryArtifactRepository) -> AutoGenDiscussionRuntime:
    return AutoGenDiscussionRuntime(
        UnusedGateway(),
        DiscussionPlan(
            participants=(
                DiscussionParticipant(
                    id="analyst", role="Analyst", goal="Analyze", logical_model="general"
                ),
                DiscussionParticipant(
                    id="critic", role="Critic", goal="Critique", logical_model="general"
                ),
            ),
            selector_model="general",
        ),
        artifact_repository=repository,
    )


def _context() -> TaskContext:
    return TaskContext(
        run_id=RUN_ID,
        tenant_id=TENANT_ID,
        mode=TaskMode.DISCUSS,
        request="Compare options.",
    )


def _artifact() -> Artifact:
    return Artifact(id=uuid4(), type="text", producer="analyst", content={"text": "safe"})


def test_partial_discussion_can_complete_after_late_model_gateway_failure() -> None:
    artifact = _artifact()

    assert _can_complete_with_partial_discussion(
        (artifact,), "model gateway failed: model transport failed"
    )
    assert not _can_complete_with_partial_discussion(
        (), "model gateway failed: model transport failed"
    )
    assert not _can_complete_with_partial_discussion((artifact,), "discussion_failed")


async def test_autogen_abort_artifact_write_returns_repository_result() -> None:
    repository = InMemoryArtifactRepository()
    runtime = _runtime(repository)
    artifact = _artifact()
    reference = ArtifactReference(id=artifact.id, sha256=artifact.content_sha256)
    first_owner = uuid4()
    second_owner = uuid4()

    await repository.put(TENANT_ID, RUN_ID, artifact, write_id=first_owner)
    await repository.put(TENANT_ID, RUN_ID, artifact, write_id=second_owner)
    runtime._pending_artifact_writes[first_owner] = reference

    assert await runtime._abort_artifact_write(_context(), first_owner) is False


async def test_autogen_store_artifact_preserves_original_error_when_rollback_fails() -> None:
    class FailingRollbackRepository(InMemoryArtifactRepository):
        async def put(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            del args, kwargs
            raise ArtifactRepositoryError("artifact repository capacity exceeded")

        async def abort_write(self, *args, **kwargs) -> bool:  # type: ignore[no-untyped-def]
            del args, kwargs
            return False

    runtime = _runtime(FailingRollbackRepository())
    runtime._cancel_event = asyncio.Event()

    with pytest.raises(RuntimeExecutionError) as caught:
        await runtime._store_artifact(_context(), _artifact())

    assert (
        str(caught.value)
        == "artifact rollback failed after artifact repository capacity exceeded"
    )
