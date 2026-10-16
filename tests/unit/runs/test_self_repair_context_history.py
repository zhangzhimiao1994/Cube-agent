"""Keep approved prompt contexts across bounded repair decisions, never tokens."""
from copy import deepcopy
from uuid import uuid4

import pytest

from agent_hub.domain.runs import RunStatus
from agent_hub.runs.repository import RunConflict
from agent_hub.runs.self_repair import repair_context_from_proposal
from tests.unit.runs.test_recovery_guard import _AcceptSelfRepairRepository


def proposal(repository: _AcceptSelfRepairRepository, attempt: int, sequence: int) -> dict[str, object]:
    return {"kind": "self_repair", "source_run_id": str(repository.run_id),
            "source_event_sequence": sequence, "attempt": attempt, "max_attempts": 3,
            "instruction": f"Approved repair {attempt}", "requires_approval": True,
            "automatic_execution": False, "failure_kind": "step_failure"}


async def accept(repository: _AcceptSelfRepairRepository, attempt: int) -> dict[str, object]:
    previous = dict(repository.row.routing_decision or {})
    repository.row.status = RunStatus.FAILED.value
    repository.latest_event_sequence = attempt * 3
    repository.row.routing_decision = {
        **previous, "approval_kind": "self_repair", "decision_token": f"token-{attempt}",
        "repair_proposal": proposal(repository, attempt, attempt * 3 - 1),
    }
    result = await repository.accept_self_repair_and_enqueue(
        tenant_id=repository.tenant_id, run_id=repository.run_id,
        decision_token=f"token-{attempt}", version=repository.row.version,
    )
    assert result.routing_decision is not None
    return result.routing_decision


@pytest.mark.asyncio
async def test_three_approved_decisions_preserve_prior_contexts_without_secrets() -> None:
    repository = _AcceptSelfRepairRepository(latest_event_sequence=3)
    first = await accept(repository, 1)
    frozen_first = deepcopy(first)
    assert "self_repair_context_history" not in first
    second = await accept(repository, 2)
    assert second["self_repair_context_history"] == [first["self_repair_context"]]
    third = await accept(repository, 3)
    assert third["self_repair_context_history"] == [first["self_repair_context"], second["self_repair_context"]]
    assert third["self_repair_context"] == repair_context_from_proposal(proposal(repository, 3, 8))
    assert first == frozen_first
    assert "decision_token" not in str(third["self_repair_context_history"])
    assert "token-" not in str(third["self_repair_context_history"])


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ("foreign", "sequence", "unbounded", "extra", "attempt", "schema_bool", "approval_int"))
async def test_invalid_previous_context_never_enters_history(change: str) -> None:
    repository = _AcceptSelfRepairRepository(latest_event_sequence=3)
    first = await accept(repository, 1)
    context = first["self_repair_context"]
    assert isinstance(context, dict)
    corrupted = dict(context)
    if change == "foreign":
        corrupted["source_run_id"] = str(uuid4())
    elif change == "sequence":
        corrupted["source_event_sequence"] = 10_000
    elif change == "unbounded":
        corrupted["instruction"] = "x" * 50_000
    elif change == "extra":
        corrupted["decision_token"] = "private"
    elif change == "schema_bool":
        corrupted["schema_version"] = True
    elif change == "approval_int":
        corrupted["requires_approval"] = 1
    else:
        corrupted["attempt"] = True
    repository.row.routing_decision = {**first, "self_repair_context": corrupted}
    with pytest.raises(RunConflict, match="self repair context history is invalid"):
        await accept(repository, 2)
    assert repository.row.status == RunStatus.FAILED.value
    assert repository.row.version == 8


@pytest.mark.asyncio
async def test_foreign_or_oversized_retained_history_is_rejected() -> None:
    repository = _AcceptSelfRepairRepository(latest_event_sequence=3)
    await accept(repository, 1)
    second = await accept(repository, 2)
    for history in ([second["self_repair_context"]] * 3, [{"source_run_id": str(uuid4())}]):
        repository.row.routing_decision = {**second, "self_repair_context_history": history}
        with pytest.raises(RunConflict, match="self repair context history is invalid"):
            await accept(repository, 3)
        assert repository.row.status == RunStatus.FAILED.value
        assert repository.row.version == 9
