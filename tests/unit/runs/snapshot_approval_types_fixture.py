"""Owned typed seams for snapshot, approval and publication tests only."""
from __future__ import annotations

from collections.abc import AsyncGenerator, AsyncIterator, Mapping
from datetime import datetime
from typing import Protocol
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from agent_hub.capabilities.python_project_lifecycle import PythonProjectTerminalHook
from agent_hub.db.models import RunRow
from agent_hub.domain.runs import TaskMode
from agent_hub.runs.repository import RunRecord
from agent_hub.runs.service import RunService, SubmittedRun
from agent_hub.runtime.artifacts import ArtifactRepository
from agent_hub.runtime.contracts import JsonValue, RunEvent, RuntimeCheckpoint
from tests.unit.runs.automatic_conversation_input_typed_fixture import SelectionRepository
from tests.unit.runs.delivery_context_revision_typed_fixture import DeliveryRecord
from tests.unit.runs.project_auto_repair_typed_fixture import mutable


def json_state(value: object) -> Mapping[str, JsonValue]:
    from agent_hub.runtime.contracts import _freeze_object
    return _freeze_object(value, name='snapshot approval fixture')


def number(value: object) -> int | float:
    assert isinstance(value, (int, float)) and not isinstance(value, bool)
    return value


def mutable_list(value: object) -> list[object]:
    assert isinstance(value, list)
    return value


def closable(stream: AsyncIterator[RunEvent]) -> AsyncGenerator[RunEvent, None]:
    assert isinstance(stream, AsyncGenerator)
    return stream


def record(row: DeliveryRecord | RunRecord) -> RunRecord:
    if isinstance(row, RunRecord):
        return row
    # The historical delivery DTO has all scope/lease fields but no database
    # version. This synthetic generation-one record adds that nominal field;
    # every existing field, including the mutable routing object, is forwarded.
    return RunRecord(id=row.id, tenant_id=row.tenant_id, actor_id=row.actor_id,
        actor_role=row.actor_role, request=row.request, mode=row.mode, status=row.status,
        version=1, created_at=row.created_at, routing_decision=row.routing_decision,
        worker_id=row.worker_id, worker_lease_token=row.worker_lease_token,
        worker_lease_expires_at=row.worker_lease_expires_at, worker_heartbeat_at=row.worker_heartbeat_at)


class CheckpointRepository(SelectionRepository):
    """Recovered checkpoint behavior from bd5aec3ae653d164769535019aaf351c18fb27e9.

    Same super claim, nonterminal assertion, real empty checkpoint and mode as
    the committed _CheckpointRepository. Only nominal row/session types differ.
    """
    async def claim_for_execution(self, session: AsyncSession, run_id: UUID, *,
        allow_running_recovery: bool, worker_id: str | None = None,
        worker_lease_token: UUID | None = None,
        worker_lease_expires_at: datetime | None = None,
    ) -> tuple[RunRow, RuntimeCheckpoint | None] | RunRecord:
        claimed = await super().claim_for_execution(session, run_id,
            allow_running_recovery=allow_running_recovery, worker_id=worker_id,
            worker_lease_token=worker_lease_token, worker_lease_expires_at=worker_lease_expires_at)
        assert not isinstance(claimed, RunRecord)
        row, _ = claimed
        assert row.mode is not None
        return row, RuntimeCheckpoint(id=uuid4(), runtime_type='test', runtime_version='1',
            run_id=run_id, tenant_id=row.tenant_id, mode=TaskMode(row.mode), state={})


async def untrusted_reference_query(repository: ArtifactRepository, query: dict[str, object]) -> object:
    """Wrong-type query values must reach the real validator without coercion."""
    method: object = repository.find_references
    assert callable(method)
    result: object = await method(**query)
    return result


async def untrusted_channel_submit(service: RunService, **kwargs: object) -> SubmittedRun:
    """The one forged channel_context test intentionally violates str values."""
    method: object = service.submit
    assert callable(method)
    result: object = await method(**kwargs)
    assert isinstance(result, SubmittedRun)
    return result


def inject_field(target: object, name: str, value: object) -> None:
    # Preserve wrong runtime types and the real ORM descriptor's behavior.
    setattr(target, name, value)


class BoundMaintenanceHook(PythonProjectTerminalHook):
    def __init__(self, callback: object) -> None:
        self.callback = callback

    async def maintain_capacity(self) -> int:
        raise AssertionError('metadata-only hook identity test must not run maintenance')


class RawApproval:
    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload


class LegacyInputMethod(Protocol):
    async def __call__(self, service: RunService, *, tenant_id: UUID, run_id: UUID,
        current_request: str, routing_decision: Mapping[str, object],
        runtime_token_budget: int, checkpoint: RuntimeCheckpoint | None,
    ) -> tuple[object, ...]: ...


def state(value: object) -> dict[str, object]:
    return mutable(value)
