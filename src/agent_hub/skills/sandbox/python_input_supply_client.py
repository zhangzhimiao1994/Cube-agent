"""Explicit v5 transport. A supply reply is neither a grant nor an install receipt."""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path

from agent_hub.skills.sandbox.broker import (
    _MAX_REQUEST_BYTES,
    _MAX_RESPONSE_BYTES,
    BROKER_SOCKET_PATH,
    _read_frame,
    _write_frame,
)
from agent_hub.skills.sandbox.project import _systemd_supported, _uninterrupted
from agent_hub.skills.sandbox.python_input_supply_protocol import (
    PythonInputSupplyReceipt,
    PythonInputSupplyRequest,
    PythonInputSupplyResponse,
    PythonInputSupplyStatus,
)
from agent_hub.skills.sandbox.python_project_client import PythonProjectSystemdSandbox
from agent_hub.skills.sandbox.python_project_protocol import PythonProjectProbeInvocation
from agent_hub.subprocess_cleanup import ProcessCleanupIncomplete


def _validated(request: PythonInputSupplyRequest) -> PythonInputSupplyRequest:
    try:
        if type(request) is not PythonInputSupplyRequest:
            raise ValueError
        return PythonInputSupplyRequest.model_validate_json(
            request.model_dump_json(warnings="error"), strict=True)
    except (ValueError, TypeError):
        raise ValueError("input_supply_request_invalid") from None


async def request_input_supply_broker(
    request: PythonInputSupplyRequest, *, socket_path: Path = BROKER_SOCKET_PATH,
) -> PythonInputSupplyResponse:
    request = _validated(request)
    body = request.model_dump_json().encode("utf-8")
    if not 0 < len(body) <= _MAX_REQUEST_BYTES:
        raise ValueError("input_supply_request_invalid")
    connect = getattr(asyncio, "open_unix_connection", None)
    if connect is None:
        raise OSError("input_supply_unsupported")
    reader, writer = await connect(str(socket_path))
    try:
        await _write_frame(writer, body)
        try:
            response = PythonInputSupplyResponse.model_validate_json(
                await _read_frame(reader, _MAX_RESPONSE_BYTES), strict=True)
            response.require_for(request)
            return response
        except (ValueError, TypeError, asyncio.IncompleteReadError):
            raise ValueError("input_supply_request_invalid") from None
    finally:
        writer.close()
        await writer.wait_closed()


class PythonInputSupplySandbox:
    def __init__(self, *, socket_path: Path = BROKER_SOCKET_PATH) -> None:
        self.socket_path = socket_path

    async def plan(self, invocation: PythonProjectProbeInvocation) -> PythonInputSupplyResponse:
        return await self._exchange(PythonInputSupplyRequest(
            action="plan_python_project_inputs", invocation=invocation))

    async def supply(
        self, invocation: PythonProjectProbeInvocation, *, expected_plan_sha256: str,
    ) -> PythonInputSupplyReceipt:
        response = await self._exchange(PythonInputSupplyRequest(
            action="supply_python_project_inputs", invocation=invocation,
            expected_plan_sha256=expected_plan_sha256))
        assert response.receipt is not None
        return response.receipt

    async def inspect(
        self, invocation: PythonProjectProbeInvocation, *, expected_plan_sha256: str,
        supply_execution_id: str,
    ) -> PythonInputSupplyStatus:
        response = await self._exchange(PythonInputSupplyRequest(
            action="inspect_python_project_input_supply", invocation=invocation,
            expected_plan_sha256=expected_plan_sha256, supply_execution_id=supply_execution_id))
        assert response.status is not None
        return response.status

    async def _exchange(self, request: PythonInputSupplyRequest) -> PythonInputSupplyResponse:
        request = _validated(request)
        if not _systemd_supported():
            raise OSError("input_supply_unsupported")
        pending = asyncio.create_task(request_input_supply_broker(request, socket_path=self.socket_path))
        try:
            async with asyncio.timeout(request.invocation.timeout_seconds + 10):
                response = await asyncio.shield(pending)
        except (asyncio.CancelledError, TimeoutError):
            await _uninterrupted(self._drain(request.invocation.execution_id, pending))
            raise
        if not response.ok:
            if response.error == "input_supply_cleanup_incomplete":
                raise ProcessCleanupIncomplete(response.error)
            raise ValueError(response.error or "input_supply_unavailable")
        return response

    async def _drain(self, execution_id: str, pending: asyncio.Task[PythonInputSupplyResponse]) -> None:
        try:
            await self.terminate(execution_id)
        finally:
            if not pending.done():
                pending.cancel()
            with contextlib.suppress(asyncio.CancelledError, OSError, ValueError):
                await pending

    async def terminate(self, execution_id: str) -> None:
        await PythonProjectSystemdSandbox(socket_path=self.socket_path).terminate(execution_id)
