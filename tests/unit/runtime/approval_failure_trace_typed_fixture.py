"""Checked DTO and generator boundaries for approval/failure-trace tests only.

The history factory still runs its real service/metadata assembly with the same
offline executor. No native or provider execution is added by this adapter.
"""
from collections.abc import AsyncGenerator, AsyncIterator, Mapping
from pathlib import Path
from typing import Never
from uuid import UUID

from agent_hub.capabilities.python_project_service import PythonOriginalContext
from agent_hub.harness.types import HarnessToolCallRequest, HarnessToolCallResult
from agent_hub.runtime.contracts import JsonValue, RunEvent, TaskContext
from tests.unit.runs.project_auto_repair_typed_fixture import json_map, string
from tests.unit.runtime.test_python_project_history import history_case as original_history_case


class HarnessOnlyExecution:
    """Satisfy CapabilityGateway while forbidding execution outside the harness."""

    async def execute(self, *, tenant_id: UUID, run_id: UUID, actor: str, name: str,
                      arguments: Mapping[str, JsonValue], idempotency_key: str) -> Never:
        raise AssertionError('approval fixture must execute through its existing harness')


def approval_result(request: HarnessToolCallRequest, approval_id: str,
                    overrides: Mapping[str, JsonValue]) -> HarnessToolCallResult:
    fields: dict[str, JsonValue] = {
        'call_id': request.call_id, 'tool_name': request.tool_name, 'status': 'failed',
        'failure_reason': 'capability requires approval', 'payload': {'approval_id': approval_id},
        **overrides,
    }
    assert set(fields) <= {'call_id', 'tool_name', 'status', 'failure_reason', 'payload', 'truncated'}
    status = fields['status']
    assert status in ('succeeded', 'failed')
    reason = fields['failure_reason']
    assert reason is None or isinstance(reason, str)
    truncated = fields.get('truncated', False)
    assert isinstance(truncated, bool)
    return HarnessToolCallResult(
        call_id=string(fields['call_id']), tool_name=string(fields['tool_name']),
        status='succeeded' if status == 'succeeded' else 'failed',
        failure_reason=reason, payload=json_map(fields['payload']), truncated=truncated,
    )


def event_stream(value: AsyncIterator[RunEvent]) -> AsyncGenerator[RunEvent, None]:
    # Keep the actual native generator: wrapping it would change close/ack timing.
    assert isinstance(value, AsyncGenerator)
    return value


async def history_case(tmp_path: Path, *, failure_index: int = 2) -> tuple[
    object, object, object, PythonOriginalContext, TaskContext,
]:
    verifier, repository, executor, original, context = await original_history_case(
        tmp_path, failure_index=failure_index,
    )
    assert isinstance(original, PythonOriginalContext)
    return verifier, repository, executor, original, context
