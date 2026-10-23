"""Explicit v8 Go socket jobs; never stage, build, execute, or grant authority.

Run preserves opaque diagnostics; inspection returns only the bound SDK identity.
The Go diagnostic wire budget is independent of the legacy source projection.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import NoReturn

from agent_hub.skills.sandbox.broker import (
    BROKER_SOCKET_PATH,
    BrokerRequestError,
    _read_frame,
    _write_frame,
)
from agent_hub.skills.sandbox.go_project_protocol import (
    GO_PROJECT_DIAGNOSTIC_MAX_BYTES,
    GoProjectAction,
    GoProjectInvocation,
    GoProjectRequest,
    GoProjectResponse,
    go_project_response_limit,
    parse_go_project_request,
    parse_go_project_response,
)
from agent_hub.skills.sandbox.project import (
    ProjectTerminateRequest,
    _systemd_supported,
    _uninterrupted,
)
from agent_hub.skills.sandbox.toolchain_protocol import ToolchainReference
from agent_hub.subprocess_cleanup import ProcessCleanupIncomplete

_REQUEST_GRACE_SECONDS = 22.0
_CLEANUP_SECONDS = 20.0
_TRANSPORT_SECONDS = 2.0
_TERMINATION_ACK_MAX_BYTES = 4096


def _retrieve_task_exception[T](task: asyncio.Task[T]) -> None:
    if not task.cancelled():
        task.exception()


async def _bounded_cleanup[T](task: asyncio.Task[T], seconds: float) -> T:
    # Unlike wait_for/timeout, wait does not wait indefinitely for a child that
    # suppresses cancellation. A detached child is always an explicit failure.
    try:
        done, _ = await asyncio.wait({task}, timeout=max(0.0, seconds))
    except asyncio.CancelledError:
        task.cancel()
        task.add_done_callback(_retrieve_task_exception)
        raise
    if task not in done:
        task.cancel()
        task.add_done_callback(_retrieve_task_exception)
        raise ProcessCleanupIncomplete('go_project_cleanup_incomplete')
    return task.result()


async def _close_writer(writer: asyncio.StreamWriter) -> None:
    writer.close()
    try:
        await _bounded_cleanup(asyncio.create_task(writer.wait_closed()), _TRANSPORT_SECONDS)
    except (ProcessCleanupIncomplete, asyncio.CancelledError):
        try:
            transport = getattr(writer, 'transport', None)
            if transport is not None:
                transport.abort()
        finally:
            raise ProcessCleanupIncomplete('go_project_cleanup_incomplete') from None


def _validated_request(request: GoProjectRequest) -> GoProjectRequest:
    try:
        if type(request) is not GoProjectRequest or type(request.invocation) is not GoProjectInvocation:
            raise ValueError
        # Validate Python values first: JSON alone can silently repair a forged
        # nested UUID string into a scope UUID. Neither scope nor SDK is coerced.
        checked = GoProjectRequest.model_validate(
            request.model_dump(warnings='error'), strict=True,
        )
        return parse_go_project_request(checked.model_dump_json(warnings='error').encode('utf-8'))
    except (ValueError, TypeError, RecursionError, OverflowError):
        raise ValueError('go_project_request_invalid') from None


def _request(action: GoProjectAction, invocation: GoProjectInvocation) -> GoProjectRequest:
    try:
        if type(invocation) is not GoProjectInvocation:
            raise ValueError
        checked = GoProjectInvocation.model_validate(
            invocation.model_dump(warnings='error'), strict=True,
        )
        return _validated_request(GoProjectRequest(action=action, invocation=checked))
    except (ValueError, TypeError, RecursionError, OverflowError):
        raise ValueError('go_project_request_invalid') from None


def _ack_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('go_project_cleanup_incomplete')
        result[key] = value
    return result


def _ack_constant(value: str) -> NoReturn:
    raise ValueError('go_project_cleanup_incomplete')


async def _request_go_project_termination(request: ProjectTerminateRequest, *, socket_path: Path) -> None:
    connect = getattr(asyncio, 'open_unix_connection', None)
    if connect is None:
        raise OSError('go_project_unsupported')
    reader, writer = await connect(socket_path.as_posix())
    try:
        await _write_frame(writer, request.model_dump_json().encode('utf-8'))
        raw = await _read_frame(reader, _TERMINATION_ACK_MAX_BYTES)
        payload = json.loads(raw.decode('utf-8'), object_pairs_hook=_ack_pairs,
                             parse_constant=_ack_constant)
        if (type(payload) is not dict or set(payload) - {'ok', 'result', 'error'}
                or payload.get('ok') is not True
                or payload.get('result') is not None or payload.get('error') is not None):
            raise ValueError('go_project_cleanup_incomplete')
    finally:
        await _uninterrupted(_close_writer(writer))


async def request_go_project_broker(
    request: GoProjectRequest,
    *,
    socket_path: Path = BROKER_SOCKET_PATH,
    diagnostic_limit_bytes: int = GO_PROJECT_DIAGNOSTIC_MAX_BYTES,
    _submitted: asyncio.Event | None = None,
) -> GoProjectResponse:
    """Exchange one v8 frame and bind every outcome to the exact request."""
    request = _validated_request(request)
    limit = go_project_response_limit(diagnostic_limit_bytes=diagnostic_limit_bytes)
    connect = getattr(asyncio, 'open_unix_connection', None)
    if not _systemd_supported() or connect is None:
        raise OSError('go_project_unsupported')
    try:
        reader, writer = await connect(socket_path.as_posix())
        try:
            body = request.model_dump_json().encode('utf-8')
            # A failed write/drain can still leave a complete frame at Root.
            if _submitted is not None:
                _submitted.set()
            await _write_frame(writer, body)
            response = parse_go_project_response(
                await _read_frame(reader, limit), diagnostic_limit_bytes=diagnostic_limit_bytes,
            )
            response.require_for(request)
            return response
        finally:
            await _uninterrupted(_close_writer(writer))
    except (ValueError, TypeError, BrokerRequestError, asyncio.IncompleteReadError,
            RecursionError, OverflowError):
        raise ValueError('go_project_request_invalid') from None
    except OSError:
        raise OSError('go_project_execution_unavailable') from None


class GoProjectSystemdSandbox:
    def __init__(
        self, *, socket_path: Path = BROKER_SOCKET_PATH,
        diagnostic_limit_bytes: int = GO_PROJECT_DIAGNOSTIC_MAX_BYTES,
    ) -> None:
        go_project_response_limit(diagnostic_limit_bytes=diagnostic_limit_bytes)
        self.socket_path = socket_path
        self.diagnostic_limit_bytes = diagnostic_limit_bytes

    async def run(self, invocation: GoProjectInvocation) -> GoProjectResponse:
        return await self._exchange(_request('run_go_project', invocation))

    async def inspect_environment(self, invocation: GoProjectInvocation) -> ToolchainReference:
        response = await self._exchange(_request('inspect_go_project_environment', invocation))
        assert response.environment is not None
        return response.environment

    async def _exchange(self, request: GoProjectRequest) -> GoProjectResponse:
        request = _validated_request(request)
        if not _systemd_supported():
            raise OSError('go_project_unsupported')
        submitted = asyncio.Event()
        pending = asyncio.create_task(request_go_project_broker(
            request, socket_path=self.socket_path, diagnostic_limit_bytes=self.diagnostic_limit_bytes,
            _submitted=submitted,
        ))
        try:
            async with asyncio.timeout(request.invocation.timeout_seconds + _REQUEST_GRACE_SECONDS):
                response = await asyncio.shield(pending)
        except (asyncio.CancelledError, TimeoutError):
            await _uninterrupted(self._terminate_and_drain(request.invocation.execution_id, pending))
            raise
        except Exception as failure:
            if submitted.is_set():
                await _uninterrupted(self._terminate_and_drain(
                    request.invocation.execution_id, pending, _known_failure=failure,
                ))
            raise
        if not response.ok:
            if response.error == 'go_project_cleanup_incomplete':
                raise ProcessCleanupIncomplete(response.error)
            raise OSError(response.error or 'go_project_execution_unavailable')
        return response

    async def _terminate_and_drain(
        self, execution_id: str, pending: asyncio.Task[GoProjectResponse],
        *, _known_failure: Exception | None = None,
    ) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + _CLEANUP_SECONDS + _TRANSPORT_SECONDS
        try:
            await self.terminate(execution_id)
        finally:
            if not pending.done():
                pending.cancel()
            try:
                response = await _bounded_cleanup(pending, deadline - loop.time())
                if not response.ok:
                    raise ProcessCleanupIncomplete('go_project_cleanup_incomplete')
            except asyncio.CancelledError:
                pass
            except Exception as failure:  # noqa: BLE001 - unknown outcomes cannot establish cleanup
                # Only the exact failure already being propagated by _exchange
                # is accounted for by a successful termination ACK.
                if failure is not _known_failure:
                    raise ProcessCleanupIncomplete('go_project_cleanup_incomplete') from None

    async def terminate(self, execution_id: str) -> None:
        if not _systemd_supported():
            raise OSError('go_project_unsupported')
        await _uninterrupted(self._terminate(execution_id))

    async def _terminate(self, execution_id: str) -> None:
        try:
            request = ProjectTerminateRequest(execution_id=execution_id)
            pending = asyncio.create_task(_request_go_project_termination(
                request, socket_path=self.socket_path,
            ))
            await _bounded_cleanup(pending, _CLEANUP_SECONDS + _TRANSPORT_SECONDS)
        except (Exception, asyncio.CancelledError):  # noqa: BLE001 - fixed cleanup error boundary
            raise ProcessCleanupIncomplete('go_project_cleanup_incomplete') from None
