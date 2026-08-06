from __future__ import annotations

import asyncio
from uuid import UUID, uuid4

import pytest

from agent_hub.improvements.service import (
    EvaluationCase,
    ImprovementDraftInput,
    ImprovementService,
    ImprovementStatus,
    PermissionDenied,
    ProposalConflict,
    ProposedDiff,
)

TENANT_A = UUID("11111111-1111-4111-8111-111111111111")
TENANT_B = UUID("22222222-2222-4222-8222-222222222222")
ADMIN = UUID("33333333-3333-4333-8333-333333333333")
USER = UUID("44444444-4444-4444-8444-444444444444")


class RecordingPublisher:
    def __init__(self) -> None:
        self.published: list[tuple[UUID, UUID, UUID]] = []

    async def publish(self, proposal_id: UUID, *, tenant_id: UUID, admin_id: UUID) -> str:
        self.published.append((proposal_id, tenant_id, admin_id))
        return "42"


class BlockingPublisher:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.calls: list[UUID] = []

    async def publish(self, proposal_id: UUID, *, tenant_id: UUID, admin_id: UUID) -> str:
        del tenant_id, admin_id
        self.calls.append(proposal_id)
        self.started.set()
        await self.release.wait()
        return "43"


def evaluation_case() -> EvaluationCase:
    return EvaluationCase(
        name="keeps_policy",
        input="summarize safely",
        expected="summary without secrets",
    )


async def test_improvement_is_only_a_draft() -> None:
    publisher = RecordingPublisher()
    service = ImprovementService(admin_publisher=publisher)

    proposal = await service.propose_prompt_change(
        tenant_id=TENANT_A,
        created_by=USER,
        source_run_id=uuid4(),
        target_id="dispatch_prompt",
        title="Improve dispatch prompt",
        rationale="The run missed a reviewer step.",
        diff={"before": "old prompt", "after": "new prompt"},
        evaluation_cases=(evaluation_case(),),
    )

    assert proposal.status is ImprovementStatus.DRAFT
    assert proposal.published_config_version is None
    assert publisher.published == []


async def test_non_admin_cannot_publish_improvement_draft() -> None:
    publisher = RecordingPublisher()
    service = ImprovementService(admin_publisher=publisher)
    proposal = await service.propose_prompt_change(
        tenant_id=TENANT_A,
        created_by=USER,
        source_run_id=uuid4(),
        target_id="dispatch_prompt",
        title="Improve dispatch prompt",
        rationale="The run missed a reviewer step.",
        diff={"before": "old prompt", "after": "new prompt"},
        evaluation_cases=(evaluation_case(),),
    )

    with pytest.raises(PermissionDenied):
        await service.publish(
            tenant_id=TENANT_A,
            proposal_id=proposal.id,
            actor_id=USER,
            actor_is_admin=False,
        )

    stored = await service.get(tenant_id=TENANT_A, proposal_id=proposal.id)
    assert stored.status is ImprovementStatus.DRAFT
    assert publisher.published == []


async def test_publish_requires_explicit_admin_proof() -> None:
    publisher = RecordingPublisher()
    service = ImprovementService(admin_publisher=publisher)
    proposal = await service.propose_prompt_change(
        tenant_id=TENANT_A,
        created_by=USER,
        source_run_id=uuid4(),
        target_id="dispatch_prompt",
        title="Improve dispatch prompt",
        rationale="The run missed a reviewer step.",
        diff={"before": "old prompt", "after": "new prompt"},
        evaluation_cases=(evaluation_case(),),
    )

    with pytest.raises(PermissionDenied, match="administrator proof"):
        await service.publish(
            tenant_id=TENANT_A,
            proposal_id=proposal.id,
            actor_id=ADMIN,
        )

    assert publisher.published == []


async def test_admin_publish_requires_explicit_publisher_and_records_version() -> None:
    publisher = RecordingPublisher()
    service = ImprovementService(admin_publisher=publisher)
    proposal = await service.propose_prompt_change(
        tenant_id=TENANT_A,
        created_by=USER,
        source_run_id=uuid4(),
        target_id="dispatch_prompt",
        title="Improve dispatch prompt",
        rationale="The run missed a reviewer step.",
        diff={"before": "old prompt", "after": "new prompt"},
        evaluation_cases=(evaluation_case(),),
    )

    published = await service.publish(
        tenant_id=TENANT_A,
        proposal_id=proposal.id,
        actor_id=ADMIN,
        actor_is_admin=True,
    )

    assert published.status is ImprovementStatus.PUBLISHED
    assert published.approved_by == ADMIN
    assert published.published_config_version == "42"
    assert publisher.published == [(proposal.id, TENANT_A, ADMIN)]


async def test_concurrent_publish_reserves_before_external_side_effect() -> None:
    publisher = BlockingPublisher()
    service = ImprovementService(admin_publisher=publisher)
    proposal = await service.propose_prompt_change(
        tenant_id=TENANT_A,
        created_by=USER,
        source_run_id=uuid4(),
        target_id="dispatch_prompt",
        title="Improve dispatch prompt",
        rationale="The run missed a reviewer step.",
        diff={"before": "old prompt", "after": "new prompt"},
        evaluation_cases=(evaluation_case(),),
    )

    first = asyncio.create_task(
        service.publish(
            tenant_id=TENANT_A,
            proposal_id=proposal.id,
            actor_id=ADMIN,
            actor_is_admin=True,
        )
    )
    await publisher.started.wait()
    with pytest.raises(ProposalConflict):
        await service.publish(
            tenant_id=TENANT_A,
            proposal_id=proposal.id,
            actor_id=ADMIN,
            actor_is_admin=True,
        )
    publisher.release.set()
    published = await first

    assert published.published_config_version == "43"
    assert publisher.calls == [proposal.id]


async def test_create_draft_supports_workflow_and_skill_diffs() -> None:
    service = ImprovementService()
    proposal = await service.create_draft(
        ImprovementDraftInput(
            tenant_id=TENANT_A,
            created_by=USER,
            source_run_id=uuid4(),
            rationale="The run found workflow and skill improvements.",
            proposed_diffs=(
                ProposedDiff(
                    target_type="workflow",
                    target_name="daily_report",
                    diff="--- old\n+++ new\n@@ workflow",
                ),
                ProposedDiff(
                    target_type="skill",
                    target_name="safe_reader",
                    diff="--- old\n+++ new\n@@ skill",
                ),
            ),
            evaluation_cases=(evaluation_case(),),
            idempotency_key="workflow-skill-draft",
        )
    )

    assert proposal.status is ImprovementStatus.DRAFT
    assert [item.target_type for item in proposal.proposed_diffs] == ["workflow", "skill"]
    assert proposal.published_config_version is None


async def test_improvement_proposals_are_tenant_isolated() -> None:
    service = ImprovementService()
    proposal = await service.propose_prompt_change(
        tenant_id=TENANT_A,
        created_by=USER,
        source_run_id=uuid4(),
        target_id="dispatch_prompt",
        title="Improve dispatch prompt",
        rationale="The run missed a reviewer step.",
        diff={"before": "old prompt", "after": "new prompt"},
        evaluation_cases=(evaluation_case(),),
    )

    assert await service.list_proposals(tenant_id=TENANT_B) == ()
    with pytest.raises(KeyError):
        await service.get(tenant_id=TENANT_B, proposal_id=proposal.id)


def test_improvement_requires_evaluation_cases() -> None:
    with pytest.raises(ValueError, match="evaluation cases"):
        ImprovementDraftInput(
            tenant_id=TENANT_A,
            created_by=USER,
            source_run_id=uuid4(),
            rationale="The run missed a reviewer step.",
            proposed_diffs=(
                ProposedDiff(
                    target_type="prompt",
                    target_name="dispatch_prompt",
                    diff="new prompt",
                ),
            ),
            evaluation_cases=(),
            idempotency_key="missing-evals",
        )


def test_proposed_diff_rejects_unsupported_target_type_from_untyped_callers() -> None:
    with pytest.raises(ValueError, match="target type"):
        ProposedDiff(
            target_type="database",  # type: ignore[arg-type]
            target_name="unsafe_target",
            diff="--- old\n+++ new",
        )
