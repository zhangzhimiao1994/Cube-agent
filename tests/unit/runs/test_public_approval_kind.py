from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from typing import cast
from uuid import uuid4

import pytest

from agent_hub.api.routers.runs import RunSummaryResponse
from agent_hub.domain.runs import RunStatus, TaskMode
from agent_hub.runs.repository import RunRecord, RunRepository
from agent_hub.runs.service import RunService, RunSummary
from agent_hub.runtime.defaults import UnavailableRuntime
from agent_hub.runtime.registry import RuntimeRegistry
from tests.unit.runs.test_conversation_mode import PreviewMutationRepository, RecordingQueue


def _service(
    approval_kind: object,
    status: RunStatus = RunStatus.WAITING_APPROVAL,
) -> tuple[RunService, RunRecord]:
    repository = PreviewMutationRepository(
        tenant_id=uuid4(), actor_id=uuid4(), run_id=uuid4(),
    )
    repository.record = replace(
        repository.record,
        status=status,
        version=5,
        routing_decision={
            "approval_kind": approval_kind,
            "approval_id": "approval-current",
            "decision_token": "decision-current",
            "repair_proposal": {"private_payload": "hidden-repair-data"},
            "raw_private_routing": "hidden-routing-data",
        },
    )
    return RunService(
        cast(RunRepository, repository),
        runtime_registry=RuntimeRegistry((UnavailableRuntime(TaskMode.DIRECT),)),
        router=None,
        task_queue=RecordingQueue(),
    ), repository.record


@pytest.mark.parametrize(
    "kind",
    [
        "capability_tool",
        "self_repair",
        "temporary_agent_creation",
        "evolution_creation",
        "schedule_creation",
        "openclaw_operation",
        "project_preflight",
    ],
)
async def test_waiting_summary_and_dto_expose_known_approval_kind(kind: str) -> None:
    service, record = _service(kind)

    summary = await service.get(record.tenant_id, record.id, actor_id=record.actor_id)
    response = RunSummaryResponse.from_summary(summary)

    assert summary.approval_kind == kind
    assert response.model_dump(mode="json")["approval_kind"] == kind
    expected_id = "approval-current" if kind == "capability_tool" else None
    assert summary.approval_id == response.approval_id == expected_id
    assert summary.version == response.version == 5
    assert summary.decision_token == response.decision_token == "decision-current"
    assert summary.id == response.id == record.id
    assert summary.tenant_id == response.tenant_id == record.tenant_id
    serialized = response.model_dump_json()
    assert "hidden-repair-data" not in serialized
    assert "hidden-routing-data" not in serialized
    assert "routing_decision" not in serialized
    assert record.routing_decision is not None
    assert record.routing_decision["approval_kind"] == kind


@pytest.mark.parametrize(
    "kind",
    [
        None,
        "",
        "future_private_kind",
        "SELF_REPAIR",
        " self_repair ",
        1,
        True,
        ["self_repair"],
        {"kind": "self_repair"},
    ],
)
async def test_waiting_summary_does_not_export_unknown_approval_kind(kind: object) -> None:
    service, record = _service(kind)

    summary = await service.get(record.tenant_id, record.id, actor_id=record.actor_id)
    response = RunSummaryResponse.from_summary(summary)

    assert summary.approval_kind is None
    assert response.model_dump(mode="json")["approval_kind"] is None
    assert summary.approval_id is None
    assert summary.version == 5


@pytest.mark.parametrize("kind", ["self_repair", "capability_tool"])
@pytest.mark.parametrize(
    "status", [status for status in RunStatus if status is not RunStatus.WAITING_APPROVAL],
)
async def test_nonwaiting_summary_hides_stale_approval_kind(
    kind: str, status: RunStatus,
) -> None:
    service, record = _service(kind, status)

    summary = await service.get(record.tenant_id, record.id, actor_id=record.actor_id)
    response = RunSummaryResponse.from_summary(summary)

    assert summary.approval_kind is None
    assert response.approval_kind is None
    assert summary.approval_id is None
    assert summary.version == response.version == 5


def test_legacy_summary_and_dto_default_approval_kind_to_none() -> None:
    summary = RunSummary(
        id=uuid4(),
        tenant_id=uuid4(),
        status=RunStatus.WAITING_APPROVAL,
        mode=TaskMode.DISPATCH,
        version=5,
        request="run an approved capability",
        completed_step_ids=(),
        artifact_ids=(),
        usage_cost_usd=Decimal(0),
        approval_id="approval-current",
    )

    response = RunSummaryResponse.from_summary(summary)
    legacy_payload = response.model_dump(mode="json", exclude={"approval_kind"})

    assert summary.approval_kind is None
    assert response.approval_kind is None
    assert RunSummaryResponse.model_validate(legacy_payload).approval_kind is None
    assert response.approval_id == "approval-current"
    assert response.version == 5
