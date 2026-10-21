"""Explicit v7 SDK transport; cancellation drains the same shared broker job."""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path

from agent_hub.skills.sandbox.broker import (
    BROKER_SOCKET_PATH,
    BrokerRequestError,
    _read_frame,
    _write_frame,
)
from agent_hub.skills.sandbox.project import (
    ProjectTerminateRequest,
    _systemd_supported,
    _uninterrupted,
    request_project_broker,
)
from agent_hub.skills.sandbox.toolchain_catalog import ToolchainSupplyPlan
from agent_hub.skills.sandbox.toolchain_protocol import (
    TOOLCHAIN_TERMINATION_SECONDS,
    ToolchainInvocation,
    ToolchainReference,
    ToolchainRequest,
    ToolchainResponse,
    ToolchainSupplyReceipt,
    ToolchainSupplyStatus,
    parse_toolchain_request,
    parse_toolchain_response,
)
from agent_hub.subprocess_cleanup import ProcessCleanupIncomplete


async def request_toolchain_broker(
    request: ToolchainRequest,
    *,
    socket_path: Path = BROKER_SOCKET_PATH,
) -> ToolchainResponse:
    request = parse_toolchain_request(request.model_dump_json().encode())
    connect = getattr(asyncio, "open_unix_connection", None)
    if connect is None:
        raise OSError("toolchain_unsupported")
    reader, writer = await connect(str(socket_path))
    try:
        await _write_frame(writer, request.model_dump_json().encode())
        response = parse_toolchain_response(await _read_frame(reader, 512 * 1024))
        response.require_for(request)
        return response
    except (ValueError, BrokerRequestError, asyncio.IncompleteReadError):
        raise ValueError("toolchain_request_invalid") from None
    finally:
        writer.close()
        await writer.wait_closed()


class ToolchainSandbox:
    def __init__(self, *, socket_path: Path = BROKER_SOCKET_PATH) -> None:
        self.socket_path = socket_path

    async def plan(self, invocation: ToolchainInvocation) -> ToolchainResponse:
        return await self._exchange(
            ToolchainRequest(action="plan_project_toolchain", invocation=invocation)
        )

    async def supply(
        self, invocation: ToolchainInvocation, *, plan: ToolchainSupplyPlan
    ) -> ToolchainSupplyReceipt:
        response = await self._exchange(
            ToolchainRequest(action="supply_project_toolchain", invocation=invocation, plan=plan)
        )
        assert response.receipt is not None
        return response.receipt

    async def inspect(
        self,
        invocation: ToolchainInvocation,
        *,
        plan: ToolchainSupplyPlan,
        supply_execution_id: str,
    ) -> ToolchainSupplyStatus:
        response = await self._exchange(
            ToolchainRequest(
                action="inspect_project_toolchain_supply",
                invocation=invocation,
                plan=plan,
                supply_execution_id=supply_execution_id,
            )
        )
        assert response.status is not None
        return response.status

    async def profile(
        self,
        invocation: ToolchainInvocation,
        *,
        plan: ToolchainSupplyPlan,
        expected_environment: ToolchainReference,
    ) -> ToolchainReference:
        response = await self._exchange(
            ToolchainRequest(
                action="inspect_project_toolchain",
                invocation=invocation,
                plan=plan,
                expected_environment=expected_environment,
            )
        )
        assert response.environment is not None
        return response.environment

    async def _exchange(self, request: ToolchainRequest) -> ToolchainResponse:
        request = parse_toolchain_request(request.model_dump_json().encode())
        if not _systemd_supported():
            raise OSError("toolchain_unsupported")
        pending = asyncio.create_task(
            request_toolchain_broker(request, socket_path=self.socket_path)
        )
        try:
            async with asyncio.timeout(
                request.invocation.timeout_seconds + TOOLCHAIN_TERMINATION_SECONDS
            ):
                response = await asyncio.shield(pending)
        except (asyncio.CancelledError, TimeoutError):
            await _uninterrupted(self._drain(request.invocation.execution_id, pending))
            raise
        if not response.ok:
            if response.error == "toolchain_cleanup_incomplete":
                raise ProcessCleanupIncomplete(response.error)
            raise ValueError(response.error or "toolchain_unavailable")
        return response

    async def _drain(self, execution_id: str, pending: asyncio.Task[ToolchainResponse]) -> None:
        try:
            async with asyncio.timeout(TOOLCHAIN_TERMINATION_SECONDS + 2):
                response = await request_project_broker(
                    ProjectTerminateRequest(execution_id=execution_id), socket_path=self.socket_path
                )
                if not response.ok:
                    raise ProcessCleanupIncomplete("toolchain_cleanup_incomplete")
        finally:
            if not pending.done():
                pending.cancel()
            with contextlib.suppress(asyncio.CancelledError, OSError, ValueError):
                await pending
