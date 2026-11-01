"""Actual AutoGen/RunRepository publication boundaries with offline model receipts."""
from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, cast
from uuid import uuid4

import pytest
from sqlalchemy import event as sql_event
from sqlalchemy.engine import Connection, ExecutionContext

from agent_hub.db.models import RunCheckpointRow
from agent_hub.runs.repository import _public_event_payload
from agent_hub.runs.service import _delivery_publication_batches
from agent_hub.runtime.autogen.adapter import AutoGenDiscussionRuntime, _DiscussionControl
from agent_hub.runtime.contracts import EventKind, RunEvent, RuntimeCheckpoint, TaskContext
from agent_hub.runtime.defaults import ConfigBackedDiscussionRuntime, _PlannedRuntime
from agent_hub.runtime.engineering_admission import EngineeringAdmissionRuntime
from tests.unit.runs.project_auto_repair_typed_fixture import mapping
from tests.unit.runs.snapshot_approval_types_fixture import closable, number
from tests.unit.runs.test_crew_report_publication_red_oct29 import persist_group, sql_repository
from tests.unit.runtime.autogen.test_engineering_completion import (
    Capabilities,
    Gateway,
    context,
    plan,
)

REPORT = "OWNED_DISCUSS_NATIVE_REPORT: The verified archive is attached."
OWNER = "delivery_main_agent"


async def stream(events: tuple[RunEvent, ...]) -> AsyncIterator[RunEvent]:
    for event in events:
        yield event


@dataclass
class NativeCase:
    task: TaskContext
    runtime: AutoGenDiscussionRuntime
    backend: Capabilities
    gateway: Gateway
    events: tuple[RunEvent, ...]

    @property
    def report_index(self) -> int:
        return next(i for i, event in enumerate(self.events)
                    if event.artifact is not None and event.artifact.producer == OWNER)

    @property
    def main_checkpoint(self) -> RuntimeCheckpoint:
        checkpoint = self.events[self.report_index + 1].checkpoint
        assert checkpoint is not None and checkpoint.state["terminal"] is False
        return checkpoint


async def make_case() -> NativeCase:
    gateway = Gateway(["[COMPLETE] premature", "workspace.write_text", "written",
                       "project.verify", "checked", "workspace.bundle", REPORT])
    backend = Capabilities()
    runtime = AutoGenDiscussionRuntime(gateway, plan(), capability_gateway=backend)
    task = context()
    task = task.model_copy(update={"routing_decision": {
        **task.routing_decision, "engineering_final_report_version": 1,
    }})
    events = tuple([event async for event in runtime.run(task)])
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert backend.calls == ["workspace.write_text", "project.verify", "workspace.bundle"]
    return NativeCase(task, runtime, backend, gateway, events)


@pytest.fixture
async def native_case() -> NativeCase:
    return await make_case()


def changed_checkpoint(checkpoint: RuntimeCheckpoint, changes: dict[str, Any]) -> RuntimeCheckpoint:
    return RuntimeCheckpoint(id=checkpoint.id, runtime_type=checkpoint.runtime_type,
        runtime_version=checkpoint.runtime_version, run_id=checkpoint.run_id,
        tenant_id=checkpoint.tenant_id, mode=checkpoint.mode,
        state={**checkpoint.state, **changes})


@pytest.mark.parametrize("changes", [{"next_sequence": 0}, {"remaining_timeout_seconds": 3601.0}])
async def test_native_invalid_main_checkpoint_is_rejected_before_publication(
    native_case: NativeCase, changes: dict[str, Any],
) -> None:
    case = native_case
    valid = case.main_checkpoint
    case.runtime._validate_checkpoint(valid, case.task)
    invalid = changed_checkpoint(valid, changes)
    assert invalid.state_sha256 == invalid.recompute_state_sha256()
    with pytest.raises(RuntimeError, match="checkpoint"):
        case.runtime._validate_checkpoint(invalid, case.task)
    events = tuple(event.model_copy(update={"checkpoint": invalid})
                   if event.checkpoint == valid else event for event in case.events)
    committed: list[RunEvent] = []
    with pytest.raises(RuntimeError, match="publication.*checkpoint"):
        async for group in _delivery_publication_batches(stream(events), case.task, runtime=case.runtime):
            committed.extend(group)
    assert not any(event.artifact is not None and event.artifact.producer == OWNER
                   or event.kind is EventKind.MESSAGE_CREATED and event.actor == OWNER
                   for event in committed)


@pytest.mark.parametrize("field,value", [("turns", -1), ("soft_limit", 0),
                                        ("hard_limit", 1_000_000), ("extension_size", 0)])
async def test_restore_invalid_discussion_control_cannot_publish(
    native_case: NativeCase, field: str, value: int,
) -> None:
    case = native_case
    checkpoint = case.main_checkpoint
    invalid = changed_checkpoint(checkpoint, {"discussion_control": {
        **mapping(checkpoint.state["discussion_control"]), field: value,
    }})
    # The state gate alone permits this Mapping; actual restoration does not.
    case.runtime._validate_checkpoint(invalid, case.task)
    with pytest.raises(RuntimeError, match="checkpoint"):
        _DiscussionControl.from_state(invalid.state["discussion_control"], case.runtime._plan,
                                      case.task, case.runtime._restored_usage(invalid))
    events = tuple(event.model_copy(update={"checkpoint": invalid})
                   if event.checkpoint == checkpoint else event for event in case.events)
    public: list[RunEvent] = []
    with pytest.raises(RuntimeError, match="publication.*checkpoint"):
        async for group in _delivery_publication_batches(stream(events), case.task, runtime=case.runtime):
            public.extend(group)
    assert not any(event.artifact is not None and event.artifact.producer == OWNER for event in public)


async def test_valid_nonterminal_report_and_completed_remain_public(native_case: NativeCase) -> None:
    case = native_case
    groups = [group async for group in _delivery_publication_batches(
        stream(case.events), case.task, runtime=case.runtime,
    )]
    main = next(group for group in groups if any(
        event.artifact is not None and event.artifact.producer == OWNER for event in group))
    assert [event.kind for event in main] == [
        EventKind.MESSAGE_CREATED, EventKind.ARTIFACT_CREATED, EventKind.CHECKPOINT_SAVED,
    ]
    assert main[-1].checkpoint == case.main_checkpoint
    assert [event for group in groups for event in group][-1].kind is EventKind.RUNTIME_COMPLETED


async def test_legacy_discuss_does_not_require_publication_validator() -> None:
    gateway = Gateway(["workspace.write_text", "written", "project.verify", "checked",
                       "workspace.bundle", "[COMPLETE] delivered"])
    runtime = AutoGenDiscussionRuntime(gateway, plan(), capability_gateway=Capabilities())
    legacy = context()
    events = tuple([event async for event in runtime.run(legacy)])
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    groups = [group async for group in _delivery_publication_batches(stream(events), legacy)]
    assert groups == [(event,) for event in events]


async def test_actual_configured_chain_validates_only_its_live_execution() -> None:
    gateway = Gateway(["workspace.write_text", "written", "project.verify", "checked",
                       "workspace.bundle", REPORT])
    backend = Capabilities()
    native = AutoGenDiscussionRuntime(gateway, plan(), capability_gateway=backend)
    child = EngineeringAdmissionRuntime(_PlannedRuntime(
        native,
        mode=native.mode, main_agent_model="shared", roles=(), steps=(),
    ), None)

    class Configured(ConfigBackedDiscussionRuntime):
        builds = 0

        async def _runtime_for(self, task: TaskContext) -> EngineeringAdmissionRuntime:
            self.builds += 1
            return child

    configured = Configured(config_service=cast(Any, object()), secret_service=cast(Any, object()),
                            capacity_factory=cast(Any, object()), transport=cast(Any, object()))
    task = context()
    task = task.model_copy(update={"execution_token": uuid4(), "routing_decision": {
        **task.routing_decision, "engineering_final_report_version": 1,
    }})
    saved: RuntimeCheckpoint | None = None
    seen_report = False
    iterator = closable(configured.run(task))
    try:
        async for event in iterator:
            if event.artifact is not None and event.artifact.producer == OWNER:
                seen_report = True
            if not seen_report or event.checkpoint is None:
                continue
            saved = event.checkpoint
            assert saved.state["terminal"] is False
            request_count = len(gateway.requests)
            configured.validate_publication_checkpoint(saved, task)
            for identity in ({"execution_token": None}, {"execution_token": uuid4()},
                             {"run_id": uuid4()}, {"tenant_id": uuid4()}):
                with pytest.raises(RuntimeError):
                    configured.validate_publication_checkpoint(saved, task.model_copy(update=identity))
            foreign = saved.model_copy(update={"tenant_id": uuid4()})
            with pytest.raises(RuntimeError):
                configured.validate_publication_checkpoint(
                    foreign, task.model_copy(update={"tenant_id": foreign.tenant_id}),
                )
            assert len(gateway.requests) == request_count
            break
    finally:
        await iterator.aclose()
    assert saved is not None and configured.builds == 1
    with pytest.raises(RuntimeError):
        configured.validate_publication_checkpoint(saved, task)


async def test_real_repository_rolls_back_report_and_recovers_without_replay(native_case: NativeCase) -> None:
    case = native_case
    report = case.events[case.report_index].artifact
    assert report is not None
    async with sql_repository(case.task) as (repository, factory, engine):
        groups = [group async for group in _delivery_publication_batches(
            stream(case.events), case.task, runtime=case.runtime,
        )]

        class SaveFailed(RuntimeError):
            pass

        def fail_checkpoint(connection: Connection, cursor: object, statement: str, parameters: object, execution_context: ExecutionContext, many: bool) -> None:
            if statement.startswith("INSERT INTO " + RunCheckpointRow.__tablename__):
                raise SaveFailed("owned checkpoint insertion failure")

        for group in groups:
            if any(event.artifact == report for event in group):
                sql_event.listen(engine.sync_engine, "before_cursor_execute", fail_checkpoint)
                try:
                    with pytest.raises(SaveFailed):
                        await persist_group(repository, case.task, group)
                finally:
                    sql_event.remove(engine.sync_engine, "before_cursor_execute", fail_checkpoint)
                break
            await persist_group(repository, case.task, group)
        raw = await repository.raw_events(case.task.tenant_id, case.task.run_id)
        assert not any(REPORT in str(_public_event_payload(event.to_payload())) for event in raw)
        async with factory() as session:
            checkpoint = await repository.latest_checkpoint(session, tenant_id=case.task.tenant_id,
                                                            run_id=case.task.run_id)
        assert checkpoint is not None
        gateway = Gateway([])
        cold = AutoGenDiscussionRuntime(gateway, plan(), capability_gateway=case.backend,
                                       artifact_repository=case.runtime._repository)
        await cold.restore_checkpoint(checkpoint)
        task = case.task.model_copy(update={"checkpoint": checkpoint})
        async for group in _delivery_publication_batches(cold.run(task), task, runtime=cold,
                                                       recovery_events=raw):
            await persist_group(repository, task, group)
        recovered = await repository.raw_events(task.tenant_id, task.run_id)
        assert sum(event.artifact == report for event in recovered) == 1
        assert recovered[-1].kind is EventKind.RUNTIME_COMPLETED
        assert not gateway.requests and len(case.backend.calls) == 3


@pytest.mark.parametrize("boundary", ["main", "terminal"])
async def test_committed_main_cold_recovery_does_not_duplicate_public_report(
    native_case: NativeCase, boundary: str,
) -> None:
    case = native_case
    report = case.events[case.report_index].artifact
    assert report is not None
    expected = case.main_checkpoint if boundary == "main" else next(
        event.checkpoint for event in case.events
        if event.checkpoint is not None and event.checkpoint.state["terminal"] is True)
    async with sql_repository(case.task) as (repository, factory, _):
        async for group in _delivery_publication_batches(stream(case.events), case.task, runtime=case.runtime):
            await persist_group(repository, case.task, group)
            if any(event.checkpoint == expected for event in group):
                break
        raw = await repository.raw_events(case.task.tenant_id, case.task.run_id)
        async with factory() as session:
            checkpoint = await repository.latest_checkpoint(session, tenant_id=case.task.tenant_id,
                                                            run_id=case.task.run_id)
        assert checkpoint == expected
        gateway = Gateway([])
        cold = AutoGenDiscussionRuntime(gateway, plan(), capability_gateway=case.backend,
                                       artifact_repository=case.runtime._repository)
        await cold.restore_checkpoint(checkpoint)
        task = case.task.model_copy(update={"checkpoint": checkpoint})
        async for group in _delivery_publication_batches(cold.run(task), task, runtime=cold,
                                                       recovery_events=raw):
            await persist_group(repository, task, group)
        final = await repository.raw_events(task.tenant_id, task.run_id)
        assert sum(event.artifact == report for event in final) == 1
        assert sum(event.kind is EventKind.RUNTIME_COMPLETED for event in final) == 1
        assert [event.sequence for event in final] == list(range(1, len(final) + 1))
        assert not gateway.requests and len(case.backend.calls) == 3


@pytest.mark.parametrize("defect", ["missing", "scope", "changed"])
async def test_recovery_dedup_requires_exact_current_run_checkpoint_prefix(
    native_case: NativeCase, defect: str,
) -> None:
    case = native_case
    checkpoint = case.main_checkpoint
    task = case.task.model_copy(update={"checkpoint": checkpoint})
    prefix = case.events[:case.report_index + 2]
    if defect == "missing":
        prefix = prefix[:-1]
    elif defect == "scope":
        prefix = (prefix[0].model_copy(update={"run_id": uuid4()}), *prefix[1:])
    else:
        changed = changed_checkpoint(checkpoint, {"next_sequence": number(checkpoint.state["next_sequence"]) + 1})
        prefix = (*prefix[:-1], prefix[-1].model_copy(update={"checkpoint": changed}))
    with pytest.raises(RuntimeError, match="publication recovery"):
        _ = [group async for group in _delivery_publication_batches(
            stream(case.events), task, runtime=case.runtime, recovery_events=prefix,
        )]
