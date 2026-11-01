import asyncio
from pathlib import Path
from typing import Never
from uuid import UUID

import pytest

from agent_hub.skills.sandbox.project import OriginalProjectReference
from agent_hub.skills.sandbox.python_input_supply_client import PythonInputSupplySandbox
from agent_hub.skills.sandbox.python_input_supply_protocol import PythonInputSupplyRequest
from agent_hub.skills.sandbox.python_project_protocol import PythonProjectProbeInvocation


def invocation() -> PythonProjectProbeInvocation:
    return PythonProjectProbeInvocation(execution_id="supply_" + "1" * 32,
        tenant_id=UUID(int=1), run_id=UUID(int=2), project_id="project", session_id="session",
        sandbox_profile="workspace_write", timeout_seconds=1,
        original_reference=OriginalProjectReference(schema_version=1, contract_sha256="1" * 64,
                            input_source_sha256="2" * 64, file_count=6))


@pytest.mark.asyncio
async def test_invalid_constructed_request_never_contacts_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    client = PythonInputSupplySandbox()
    calls: list[int] = []

    async def exchange(request: PythonInputSupplyRequest, *, socket_path: Path) -> Never:
        calls.append(1)
        raise AssertionError

    monkeypatch.setattr("agent_hub.skills.sandbox.python_input_supply_client.request_input_supply_broker", exchange)
    request = PythonInputSupplyRequest.model_construct(action="supply_python_project_inputs", invocation=invocation())
    with pytest.raises(ValueError, match="input_supply_request_invalid"):
        await client._exchange(request)
    assert calls == []


@pytest.mark.asyncio
async def test_cancel_drains_same_execution_and_never_resends(monkeypatch: pytest.MonkeyPatch) -> None:
    client = PythonInputSupplySandbox()
    entered = asyncio.Event()
    calls: list[str] = []
    stops: list[str] = []

    async def exchange(request: PythonInputSupplyRequest, *, socket_path: Path) -> None:
        calls.append(request.invocation.execution_id)
        entered.set()
        await asyncio.Event().wait()

    async def terminate(execution_id: str) -> None:
        stops.append(execution_id)

    monkeypatch.setattr("agent_hub.skills.sandbox.python_input_supply_client.request_input_supply_broker", exchange)
    monkeypatch.setattr("agent_hub.skills.sandbox.python_input_supply_client._systemd_supported", lambda: True)
    monkeypatch.setattr(client, "terminate", terminate)
    task = asyncio.create_task(client.plan(invocation()))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert calls == stops == [invocation().execution_id]
