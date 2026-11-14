"""Focused real-consumer regressions; no Root, network, provider or paid run."""
from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import NoReturn, cast
from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

import pytest
from sqlalchemy import update

from agent_hub.capabilities.python_resolution_diagnostics import resolution_timeout_payload
from agent_hub.capabilities.python_resolution_preparation import PythonResolutionPreparation
from agent_hub.db.models import RunRow
from agent_hub.domain.runs import RunStatus, TaskMode
from agent_hub.runs.repository import RunConflict, RunRecord, RunRepository
from agent_hub.runs.service import RunService, TaskQueue
from agent_hub.runtime.contracts import EventKind, JsonValue, RunEvent, TaskContext
from agent_hub.runtime.registry import RuntimeRegistry
from agent_hub.skills.sandbox.python_resolution_protocol import (
    ResolutionPlanning,
    ResolutionStatus,
    resolution_key,
)
from tests.unit.runs.test_crew_report_publication_red_oct29 import sql_repository
from tests.unit.runs.test_project_input_lifecycle import (
    ObservingRuntime,
    original_project_repository,
)
from tests.unit.skills.sandbox.test_resolution_lease_witness import request


def test_completed_progress_never_crosses_resolution_keys() -> None:
    async def scenario() -> None:
        first = request()
        current = AsyncMock(side_effect=AssertionError("authority must remain in original guarded method"))
        client = PythonResolutionPreparation(current=current, approved=None)
        client._original = first.claim
        other_environment = {**first.environment, "python_full_version": "3.12.15", "implementation_version": "3.12.15"}
        key_a = resolution_key(first.claim, first.needs, first.environment)
        key_b = resolution_key(first.claim, first.needs, other_environment)
        assert key_a != key_b
        guarded = AsyncMock(return_value=ResolutionStatus(state="resolved", key=key_a,
                                                         descriptor={"validated-by-original-guard": True}))
        with patch.object(client, "_send_guarded", guarded):
            await client._send("resolve_python_inputs", needs=first.needs,
                               environment=first.environment, planning=first.claim.planning)
            for environment, expected in [(other_environment, "unobserved"),
                                          (first.environment, "freeze_pending")]:
                error = TimeoutError("PRIVATE credential/path")
                guarded.side_effect = error
                with pytest.raises(TimeoutError) as caught:
                    await client._send("freeze_python_resolution", needs=first.needs,
                                       environment=environment, planning=first.claim.planning)
                assert caught.value is error
                payload = resolution_timeout_payload(error)
                assert payload["resolution_progress"] == expected
                assert payload["resolution_progress_evidence"] == (
                    "none" if expected == "unobserved" else "validated_root_status")
        assert guarded.await_count == 3 and current.await_count == 0
    asyncio.run(scenario())


def test_root_main_inspection_unknown_has_exact_finite_phase(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    import sys

    from agent_hub.skills.sandbox import broker, project_contract_broker
    from agent_hub.skills.sandbox import python_metadata_preparation_worker as metadata
    from agent_hub.skills.sandbox import python_resolution_worker as worker

    flags = {name: getattr(sys.flags, name) for name in dir(sys.flags) if not name.startswith('_')}
    flags.update(isolated=True, optimize=0)
    monkeypatch.setattr(sys, 'flags', SimpleNamespace(**flags))
    monkeypatch.setattr(sys, 'dont_write_bytecode', True)
    first = request().model_copy(update={"action": "inspect_python_resolution"})
    stage = tmp_path / "stage"
    monkeypatch.setattr(metadata, "_native", lambda: None)
    monkeypatch.setattr(metadata, "_stage_identity", lambda *args: None)
    monkeypatch.setattr(worker, "read_root_configuration", lambda: SimpleNamespace(stage_root=tmp_path))
    monkeypatch.setattr(worker, "read_supply_configuration", lambda: object())
    monkeypatch.setattr(worker, "_root", lambda *args, **kwargs: None)
    monkeypatch.setattr(project_contract_broker, "_read_control",
                        lambda *args: first.model_dump_json().encode())
    monkeypatch.setattr(broker, "load_broker_policy", lambda: None)
    execute = AsyncMock(side_effect=TimeoutError("PRIVATE DNS/path/credentials"))
    monkeypatch.setattr(worker, "execute_resolution", execute)
    monkeypatch.setattr(sys, "argv", ["readonly-fake-root-boundary", "--stage", str(stage)])
    assert worker.main() == 2
    captured = capsys.readouterr()
    result = ResolutionStatus.model_validate_json(captured.out, strict=True)
    assert result.state == "unknown" and result.key == first.key
    assert result.diagnostic is not None
    assert result.diagnostic.phase == "inspection"
    assert result.diagnostic.code == "resolution_timeout" and result.diagnostic.timed_out is True
    assert execute.await_count == 1 and "PRIVATE" not in captured.out


def test_actual_service_and_sql_repository_persist_finite_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        repository = original_project_repository()
        record = repository._record()
        context = TaskContext(run_id=record.id, tenant_id=record.tenant_id, request=record.request,
                              mode=cast(TaskMode, record.mode), actor_id=record.actor_id,
                              actor_role=record.actor_role,
                              routing_decision=cast(dict[str, JsonValue], record.routing_decision or {}),
                              timeout_seconds=25.)
        model_calls: list[str] = []
        seen: list[BaseException | None] = []
        error = TimeoutError("PRIVATE password/path/DNS")
        client = PythonResolutionPreparation(current=AsyncMock(), approved=None,
                                            original_deadline=datetime.now(UTC)-timedelta(seconds=1))
        guarded = AsyncMock(side_effect=error)
        async with sql_repository(context) as (sql, factory, _engine):
            async def fail(run_id: UUID, *, reason: str, worker_id: str | None = None,
                           worker_lease_token: UUID | None = None, resolution_failure: object = None,
                           resolution_timeout: BaseException | None = None) -> RunRecord:
                assert run_id == repository.run_id and worker_id == repository.row.worker_id
                assert worker_lease_token == repository.row.worker_lease_token and worker_lease_token is not None
                async with factory.begin() as session:
                    await session.execute(update(RunRow).where(RunRow.id == run_id).values(
                        worker_id=worker_id, worker_lease_token=worker_lease_token))
                extras = {} if resolution_timeout is None else {"resolution_timeout": resolution_timeout}
                with pytest.raises(RunConflict, match="worker execution ownership changed"):
                    await sql.fail_run(run_id, reason=reason, worker_id="wrong-worker",
                                       worker_lease_token=uuid4(), **extras)
                assert await sql.raw_events(record.tenant_id, run_id) == ()
                result = await sql.fail_run(run_id, reason=reason, worker_id=worker_id,
                                           worker_lease_token=worker_lease_token, **extras)
                seen.append(resolution_timeout)
                repository.row.status = result.status.value
                return repository._record()

            async def prepare(task: TaskContext) -> NoReturn:
                with patch.object(client, "_send_guarded", guarded):
                    await client._send("freeze_python_resolution", needs=(), environment={},
                                       planning=cast(ResolutionPlanning, None))
                raise AssertionError("timeout must stop the actual preparation")

            monkeypatch.setattr(repository, "fail_run", fail)
            service = RunService(cast(RunRepository, repository),
                runtime_registry=RuntimeRegistry((ObservingRuntime(TaskMode.DISPATCH, model_calls),)),
                router=None, task_queue=cast(TaskQueue, object()), project_input_preparer=prepare)
            result = await service.execute(repository.run_id)
            assert result.status is RunStatus.FAILED and guarded.await_count == 1 and model_calls == []
            assert repository.row.worker_id is None and repository.row.worker_lease_token is None
            events = await sql.raw_events(record.tenant_id, record.id)
            assert len(events) == 1
            payload = events[0].payload
            assert payload["resolution_phase"] == "freeze"
            assert payload["resolution_progress"] == "unobserved"
            assert payload["resolution_progress_evidence"] == "none"
            assert payload["original_deadline_expired"] is True
            assert payload["error_code"] == "project_verification.resolution.resolution_deadline"
            assert payload["retryable"] is False and "PRIVATE" not in json.dumps(events[0].to_payload())
            assert seen == [error]
            for invalid in [{**payload, "resolution_phase": "PRIVATE"},
                            {key: value for key, value in payload.items() if key != "resolution_progress_evidence"},
                            {**payload, "original_deadline_expired": 1},
                            {**payload, "private_exception": "PRIVATE"}]:
                with pytest.raises(ValueError):
                    RunEvent(kind=EventKind.RUNTIME_FAILED, sequence=2, run_id=record.id,
                             reason="runtime_failed", payload=invalid)
            with pytest.raises(ValueError, match="resolution_timeout_diagnostic_invalid"):
                await sql.fail_run(record.id, reason="cannot admit a forged observation",
                                   resolution_timeout=TimeoutError("PRIVATE"))
            before = events
            await sql.fail_run(record.id, reason="cannot replace terminal diagnostics")
            assert await sql.raw_events(record.tenant_id, record.id) == before
    asyncio.run(scenario())
