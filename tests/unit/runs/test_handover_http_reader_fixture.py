"""The authenticated projection fixture must construct without executable work."""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from agent_hub.runs.repository import RunRepository
from agent_hub.runs.service import RunService


def test_public_http_fixture_constructs_real_service_without_execution() -> None:
    from tests.integration.plugins.test_handover_public_http import _projection_run_service

    service = _projection_run_service(RunRepository(async_sessionmaker()))
    assert isinstance(service, RunService)


@pytest.mark.asyncio
async def test_public_http_fixture_queue_rejects_execution() -> None:
    from uuid import uuid4

    from tests.integration.plugins.test_handover_public_http import _NoTasks

    with pytest.raises(AssertionError, match="never enqueue"):
        await _NoTasks().enqueue_run(uuid4(), idempotency_key="fixture-only")


@pytest.mark.asyncio
async def test_public_http_fixture_runtime_rejects_every_execution_entry() -> None:
    from uuid import uuid4

    from agent_hub.domain.runs import TaskMode
    from agent_hub.runtime.contracts import RuntimeCheckpoint, TaskContext
    from tests.integration.plugins.test_handover_public_http import _NoExecution

    runtime = _NoExecution()
    context = TaskContext(run_id=uuid4(), tenant_id=uuid4(), mode=TaskMode.DISPATCH,
                          request="fixture only")
    checkpoint = RuntimeCheckpoint(
        id=uuid4(), runtime_type="crew", runtime_version="11", run_id=context.run_id,
        tenant_id=context.tenant_id, mode=context.mode, state={},
    )
    with pytest.raises(AssertionError, match="never execute"):
        runtime.run(context)
    with pytest.raises(AssertionError, match="never execute"):
        await runtime.save_checkpoint()
    with pytest.raises(AssertionError, match="never execute"):
        await runtime.restore_checkpoint(checkpoint)
    with pytest.raises(AssertionError, match="never execute"):
        await runtime.cancel()


@pytest.mark.parametrize("requested", [False, True])
def test_public_http_fixture_mirrors_obey_runtime_event_contract(requested: bool) -> None:
    from uuid import uuid4

    from agent_hub.runtime.contracts import EventKind, RunEvent
    from tests.integration.plugins.test_handover_public_http import _approval_mirror

    kind = EventKind.APPROVAL_REQUESTED if requested else EventKind.APPROVAL_RESOLVED
    event = _approval_mirror(uuid4(), "fixture-approval", kind)
    assert event.kind is kind
    assert RunEvent.from_payload(event.to_payload()) == event
