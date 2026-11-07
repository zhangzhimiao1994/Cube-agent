"""Stopped original owners may retain a proved suggestion, never repair authority."""

from __future__ import annotations

import copy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pytest

from agent_hub.capabilities.python_project_lifecycle import (
    _historical_nonblocking_repair_suggestion,
)
from agent_hub.db.models import RunEventRow, RuntimeArtifactRow
from agent_hub.domain.runs import RunStatus, TaskMode
from agent_hub.runs.repository import _event_with_failure_diagnostic
from agent_hub.runs.self_repair import SelfRepairPolicy, classify_terminal_run
from agent_hub.runtime.contracts import EventKind, RunEvent
from tests.unit.capabilities.test_stopped_premodel_retirement_lifecycle_nov07 import run, setup


def suggestion_owner(defect: str | None = None) -> tuple[Any, Any, list[str]]:
    hook, session, calls = setup(defect)
    failure = RunEvent(
        kind=EventKind.RUNTIME_FAILED,
        sequence=5,
        run_id=session.row.id,
        reason="structured output invalid",
    )
    decision = classify_terminal_run(
        status=RunStatus.FAILED,
        mode=TaskMode.DISPATCH,
        routing_decision={},
        events=(failure,),
        policy=SelfRepairPolicy(),
    )
    assert decision is not None
    proposal = decision.to_proposal(run_id=session.row.id)
    assert proposal is not None and proposal["pauses_run"] is False
    session.row.routing_decision.update(
        approval_kind="self_repair",
        decision_token="decision-" + "1" * 64,
        repair_proposal=proposal,
    )
    session.events = [
        SimpleNamespace(
            id=UUID(int=sequence),
            tenant_id=session.row.tenant_id,
            run_id=session.row.id,
            sequence=sequence,
            kind=kind,
            payload=event.to_payload(),
        )
        for sequence, kind, event in (
            (5, "runtime.failed", failure),
            (6, "repair.classified", decision.to_event(run_id=session.row.id, sequence=6)),
        )
    ]
    original_scalar, original_scalars = session.scalar, session.scalars
    session.mutate_during_classification = False

    async def scalar(query: Any) -> Any:
        if query.column_descriptions[0]["entity"] is RunEventRow:
            assert session.open
            session.queries.append(query)
            params = query.compile().params
            if "sequence_1" in params:
                return next(
                    (event for event in session.events if event.sequence == params["sequence_1"]),
                    None,
                )
            kinds = params["kind_1"]
            return next((event.id for event in session.events if event.kind not in kinds), None)
        return await original_scalar(query)

    async def scalars(query: Any) -> Any:
        if query.column_descriptions[0]["entity"] is RunEventRow:
            assert session.open
            session.queries.append(query)
            kinds = query.compile().params.get("kind_1")
            if kinds is not None:
                if session.mutate_during_classification:
                    session.row.routing_decision["concurrent_change"] = "changed"
                values = [event for event in session.events if event.kind in kinds]
                return SimpleNamespace(all=lambda: list(reversed(values)))
            return SimpleNamespace(all=lambda: list(session.events))
        return await original_scalars(query)

    session.scalar, session.scalars = scalar, scalars
    return hook, session, calls


def select_owner(hook: Any, session: Any) -> Any:
    async def select() -> Any:
        async with session, session.begin():
            return await hook._stopped_pre_model_owner(
                session, session.row.tenant_id, session.row.id,
                datetime.now(UTC) - timedelta(days=1),
            )

    return run(select())


def test_executing_original_with_nonblocking_suggestion_passes_locked_owner_selector() -> None:
    hook, session, calls = suggestion_owner()
    before = copy.deepcopy(vars(session.row)), copy.deepcopy(session.events)
    selected = select_owner(hook, session)
    assert selected is not None and selected[1] == session.item
    assert calls == ["metadata"]
    assert (vars(session.row), session.events) == before
    assert session.queries[0]._for_update_arg is not None and not session.open


def test_proved_suggestion_keeps_original_archive_intent_and_commit_fences() -> None:
    hook, session, calls = suggestion_owner()
    before = copy.deepcopy(vars(session.row)), copy.deepcopy(session.events)
    assert run(hook.maintain_capacity()) == 1
    assert [call for call in calls if call != "metadata"] == [
        "inspect", "archive", "readback", "intent", "intent-readback", "commit",
    ]
    assert (vars(session.row), session.events) == before
    assert session.row.routing_decision["project_input_preparation"]["phase"] == "executing"


def persisted_suggestion_owner() -> tuple[Any, Any, list[str]]:
    hook, session, calls = suggestion_owner()
    original = RunEvent.from_payload(session.events[0].payload)
    # The actual repository producer enriches a copy before strict validation
    # and storage. Classification used the untouched runtime event above.
    persisted = RunEvent.model_validate(_event_with_failure_diagnostic(original))
    session.events[0].payload = persisted.to_payload()
    assert not original.payload and persisted.payload
    return hook, session, calls


def test_exact_repository_producer_enrichment_proves_original_suggestion_without_mutation() -> None:
    hook, session, calls = persisted_suggestion_owner()
    before = copy.deepcopy(vars(session.row)), copy.deepcopy(session.events)
    selected = select_owner(hook, session)
    assert selected is not None and selected[1] == session.item
    assert calls == ["metadata"]
    assert (vars(session.row), session.events) == before


def test_repository_producer_projection_is_not_enabled_for_existing_observed_classifier() -> None:
    _hook, session, _calls = persisted_suggestion_owner()
    session.row.routing_decision["project_input_preparation"]["phase"] = "observed"
    before = copy.deepcopy(vars(session.row)), copy.deepcopy(session.events)

    async def check() -> bool:
        async with session, session.begin():
            return await _historical_nonblocking_repair_suggestion(
                session, session.row, session.row.routing_decision,
            )

    assert run(check()) is False
    assert (vars(session.row), session.events) == before


@pytest.mark.parametrize("defect", [
    "diagnostic", "error-code", "extra-payload", "reason", "event-fingerprint",
    "proposal-fingerprint", "both-fingerprints", "category",
])
def test_repository_producer_projection_never_admits_tampered_history(defect: str) -> None:
    hook, session, calls = persisted_suggestion_owner()
    failure = session.events[0].payload
    classified = session.events[1].payload["payload"]
    proposal = session.row.routing_decision["repair_proposal"]
    if defect == "diagnostic":
        failure["payload"]["error_summary"] = "different diagnostic"
    elif defect == "error-code":
        failure["payload"]["error_code"] = "runtime.unrelated"
    elif defect == "extra-payload":
        # A valid diagnostic field is still not an actual reason-only producer output.
        failure["payload"]["status_code"] = 418
    elif defect == "reason":
        failure["reason"] += " replaced"
    elif defect == "event-fingerprint":
        classified["fingerprint"] = "2" * 64
    elif defect == "proposal-fingerprint":
        proposal["fingerprint"] = "2" * 64
    elif defect == "both-fingerprints":
        classified["fingerprint"] = proposal["fingerprint"] = "2" * 64
    else:
        classified["failure_category"] = proposal["failure_category"] = "runtime_failure"
    before = copy.deepcopy(vars(session.row)), copy.deepcopy(session.events)
    assert select_owner(hook, session) is None
    assert calls == ["metadata"]
    assert (vars(session.row), session.events) == before


@pytest.mark.parametrize("defect", [
    "pending", "foreign-proposal", "foreign-event", "foreign-source", "fingerprint",
    "bad-token", "missing-token", "wrong-kind", "pause", "automatic", "no-approval",
    "phase-other", "prep-extra", "prep-schema", "generated-null", "unknown",
    "child", "intent", "missing-event", "duplicate-event", "event-extra",
    "route-mutation",
])
def test_unproved_suggestion_or_active_owner_remains_ineligible(defect: str) -> None:
    hook, session, calls = suggestion_owner()
    route = session.row.routing_decision
    proposal = route["repair_proposal"]
    if defect == "pending":
        session.block_table = "agent_hub_run_approvals"
    elif defect == "foreign-proposal":
        proposal["source_run_id"] = str(UUID(int=99))
    elif defect == "foreign-event":
        session.events[1].tenant_id = UUID(int=99)
    elif defect == "foreign-source":
        session.events[0].payload["run_id"] = str(UUID(int=99))
    elif defect == "fingerprint":
        proposal["fingerprint"] = "0" * 64
    elif defect == "bad-token":
        route["decision_token"] = "malformed"
    elif defect == "missing-token":
        route["decision_token"] = None
    elif defect == "wrong-kind":
        route["approval_kind"] = "project_preflight"
    elif defect == "pause":
        proposal["pauses_run"] = True
    elif defect == "automatic":
        proposal["automatic_execution"] = True
    elif defect == "no-approval":
        proposal["requires_approval"] = False
    elif defect == "phase-other":
        route["project_input_preparation"]["phase"] = "observed"
    elif defect == "prep-extra":
        route["project_input_preparation"]["extra"] = "safe"
    elif defect == "prep-schema":
        route["project_input_preparation"]["schema_version"] = True
    elif defect == "generated-null":
        route["generated_project_preparation"] = None
    elif defect == "unknown":
        route["other_native_state"] = "UNKNOWN"
    elif defect == "child":
        session.child = UUID(int=99)
    elif defect == "intent":
        intent = hook._stopped_intent_id(session.item)
        session.artifacts[intent] = RuntimeArtifactRow(
            id=intent, tenant_id=session.row.tenant_id, run_id=session.row.id,
        )
    elif defect == "missing-event":
        session.events.pop()
    elif defect == "duplicate-event":
        session.events.append(copy.deepcopy(session.events[1]))
    elif defect == "event-extra":
        session.events[1].payload["payload"]["execution_authorized"] = True
    else:
        session.mutate_during_classification = True
    before = copy.deepcopy(vars(session.row)), copy.deepcopy(session.events)
    assert select_owner(hook, session) is None
    assert not set(calls) - {"metadata"} and not session.open
    if defect == "route-mutation":
        before[0]["routing_decision"]["concurrent_change"] = "changed"
    assert (vars(session.row), session.events) == before


def test_existing_observed_helper_does_not_implicitly_accept_executing_preparation() -> None:
    _hook, session, _calls = suggestion_owner()

    async def check() -> bool:
        async with session, session.begin():
            return await _historical_nonblocking_repair_suggestion(
                session, session.row, session.row.routing_decision,
            )

    assert run(check()) is False


@pytest.mark.parametrize("flag", [1, "true"])
def test_stopped_flag_rejects_truthy_non_boolean_values(flag: Any) -> None:
    _hook, session, _calls = suggestion_owner()
    before = copy.deepcopy(vars(session.row)), copy.deepcopy(session.events)

    async def check() -> bool:
        async with session, session.begin():
            return await _historical_nonblocking_repair_suggestion(
                session, session.row, session.row.routing_decision, stopped_pre_model=flag,
            )

    assert run(check()) is False
    assert (vars(session.row), session.events) == before


@pytest.mark.parametrize(("defect", "accepted"), [
    (None, True), ("captured", False), ("observed", False), ("UNKNOWN", False),
    ("extra", False), ("bool-schema", False), ("generated-contract", False),
    ("generated-preparation", False), ("other-unknown", False),
])
def test_explicit_stopped_flag_only_classifies_exact_original_executing_preparation(
    defect: str | None, accepted: bool,
) -> None:
    _hook, session, _calls = suggestion_owner()
    route = session.row.routing_decision
    preparation = route["project_input_preparation"]
    if defect in {"captured", "observed", "UNKNOWN"}:
        preparation["phase"] = defect
    elif defect == "extra":
        preparation["extra"] = "safe"
    elif defect == "bool-schema":
        preparation["schema_version"] = True
    elif defect == "generated-contract":
        route["generated_project_contract_version"] = None
    elif defect == "generated-preparation":
        route["generated_project_preparation"] = None
    elif defect == "other-unknown":
        preparation["python_recipe"]["retained_state"] = "UNKNOWN"
    before = copy.deepcopy(vars(session.row)), copy.deepcopy(session.events)

    async def check() -> bool:
        async with session, session.begin():
            return await _historical_nonblocking_repair_suggestion(
                session, session.row, route, stopped_pre_model=True,
            )

    assert run(check()) is accepted
    assert (vars(session.row), session.events) == before


@pytest.mark.parametrize("defect", ["route-race", "child-race", "event-race", "commit-unknown"])
def test_proved_suggestion_cannot_bypass_existing_recheck_or_unknown_fence(defect: str) -> None:
    hook, _session, calls = suggestion_owner(defect)
    assert run(hook.maintain_capacity()) == 0
    assert calls.count("inspect") == 1
    assert calls.count("commit") == int(defect == "commit-unknown")
    assert "normal-release" not in calls and "empty-failed" not in calls
    if defect == "commit-unknown":
        before = list(calls)
        hook._capacity_cursor = None
        assert run(hook.maintain_capacity()) == 0
        assert calls == before
