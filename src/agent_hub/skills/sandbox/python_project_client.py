"""Explicit Python v4 client over existing broker frames; no routing or grants.

Run retains both the execution result and optional root environment reference.
Probe returns measured inputs; inspect returns only the exact expected reference.
None of these replies establishes sandbox availability or an authorization lease.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import sys
from pathlib import Path
from typing import BinaryIO, cast

from agent_hub.skills.sandbox.broker import (
    _MAX_REQUEST_BYTES,
    _MAX_RESPONSE_BYTES,
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
from agent_hub.skills.sandbox.python_project_protocol import (
    PythonEnvironmentReference,
    PythonProductDescriptor,
    PythonProjectBrokerRequest,
    PythonProjectBrokerResponse,
    PythonProjectInputsObservation,
    PythonProjectInspectEnvironmentRequest,
    PythonProjectInvocation,
    PythonProjectProbeInvocation,
    PythonProjectProbeRequest,
    PythonProjectReleaseEnvironmentRequest,
)
from agent_hub.subprocess_cleanup import ProcessCleanupIncomplete

type _PythonRequest = (
    PythonProjectBrokerRequest | PythonProjectProbeRequest | PythonProjectInspectEnvironmentRequest
    | PythonProjectReleaseEnvironmentRequest
)
_REQUEST_GRACE_SECONDS = 10.0
_CLEANUP_SECONDS = 10.0


def _validated_request(request: _PythonRequest) -> _PythonRequest:
    try:
        if type(request) not in {
            PythonProjectBrokerRequest,
            PythonProjectProbeRequest,
            PythonProjectInspectEnvironmentRequest,
            PythonProjectReleaseEnvironmentRequest,
        }:
            raise ValueError("invalid request type")
        # Frozen models can still be constructed/copied without validation. Reparse
        # all nested input before any transport, using the actual request class.
        return type(request).model_validate_json(
            request.model_dump_json(warnings="error"), strict=True
        )
    except (ValueError, TypeError):
        raise ValueError("python_project_request_invalid") from None


async def request_python_project_broker(
    request: _PythonRequest,
    *,
    socket_path: Path = BROKER_SOCKET_PATH,
) -> PythonProjectBrokerResponse:
    """Reuse broker framing, but never parse a v4 reply as a Node response."""
    validated = _validated_request(request)
    body = validated.model_dump_json().encode("utf-8")
    if not 0 < len(body) <= _MAX_REQUEST_BYTES:
        raise ValueError("python_project_request_invalid")
    connector = getattr(asyncio, "open_unix_connection", None)
    if connector is None:
        raise OSError("python_project_systemd_linux_unsupported")
    reader, writer = await connector(socket_path.as_posix())
    spool: BinaryIO | None = None
    delivered: PythonProjectBrokerResponse | None = None
    try:
        await _write_frame(writer, body)
        try:
            first = await _read_frame(reader, _MAX_RESPONSE_BYTES)
            envelope = json.loads(first)
            product: PythonProductDescriptor | None = None
            if isinstance(envelope, dict) and "python_product_stream" in envelope:
                if (not isinstance(validated, PythonProjectBrokerRequest)
                        or validated.product_transfer_version != 1
                        or set(envelope) != {"python_product_stream", "product"}
                        or type(envelope["python_product_stream"]) is not int
                        or envelope["python_product_stream"] != 1):
                    raise ValueError("unsolicited Python product")
                product = PythonProductDescriptor.model_validate(envelope["product"], strict=True)
                from agent_hub.skills.sandbox.python_product_transport import receive_product

                spool = await receive_product(reader, product, validated.invocation)
                first = await _read_frame(reader, _MAX_RESPONSE_BYTES)
            response = PythonProjectBrokerResponse.model_validate_json(first, strict=True)
            response.require_for(validated)
            if not response.ok and response.error == "python_project_cleanup_incomplete":
                raise ProcessCleanupIncomplete(response.error)
            if (isinstance(validated, PythonProjectBrokerRequest)
                    and validated.product_transfer_version == 1 and response.ok
                    and response.result is not None and product is None):
                report = json.loads(response.result.stdout)
                if isinstance(report, dict) and report.get("status") == "passed":
                    raise ValueError("Python product transfer missing")
            if response.product != product:
                raise ValueError("Python product transfer missing")
            if product is not None:
                from agent_hub.skills.sandbox.python_product_transport import require_product_report

                require_product_report(response, product)
                response._product_stream = spool
                spool = None
            delivered = response
            return response
        except (ValueError, TypeError, BrokerRequestError, asyncio.IncompleteReadError):
            raise ValueError("python_project_response_invalid") from None
    finally:
        primary_error = sys.exception()
        if spool is not None:
            spool.close()
        try:
            writer.close()
            await writer.wait_closed()
        except BaseException:
            # A failed close overrides the return, so ownership never reaches
            # the service. Close a validated but undelivered spool here.
            if delivered is not None and delivered._product_stream is not None:
                delivered._product_stream.close()
                delivered._product_stream = None
            if primary_error is not None:
                raise primary_error
            raise


class PythonProjectSystemdSandbox:
    def __init__(self, settings: object | None = None, *, socket_path: Path | None = None) -> None:
        self.socket_path = socket_path or cast(
            Path,
            getattr(settings, "socket_path", BROKER_SOCKET_PATH),
        )

    async def run(self, invocation: PythonProjectInvocation) -> PythonProjectBrokerResponse:
        """Return execution and reference separately; stdout is opaque project data."""
        return await self._exchange(PythonProjectBrokerRequest(
            invocation=invocation, product_transfer_version=1))

    async def probe(
        self, invocation: PythonProjectProbeInvocation
    ) -> PythonProjectInputsObservation:
        response = await self._exchange(PythonProjectProbeRequest(invocation=invocation))
        assert response.observation is not None
        return response.observation

    async def inspect_environment(
        self,
        invocation: PythonProjectInvocation,
        *,
        expected_environment: PythonEnvironmentReference,
    ) -> PythonEnvironmentReference:
        response = await self._exchange(
            PythonProjectInspectEnvironmentRequest(
                invocation=invocation,
                expected_environment=expected_environment,
            )
        )
        assert response.environment_reference is not None
        return response.environment_reference

    async def release_completed(
        self,
        invocation: PythonProjectInvocation,
        *,
        expected_environment: PythonEnvironmentReference,
    ) -> PythonEnvironmentReference:
        """Trusted service lifecycle only; deliberately absent from model tool schemas."""
        response = await self._exchange(PythonProjectReleaseEnvironmentRequest(
            invocation=invocation, expected_environment=expected_environment
        ))
        assert response.released_environment is not None
        return response.released_environment

    async def _exchange(self, request: _PythonRequest) -> PythonProjectBrokerResponse:
        if not _systemd_supported():
            raise OSError("python_project_systemd_linux_unsupported")
        request = _validated_request(request)
        pending = asyncio.create_task(
            request_python_project_broker(request, socket_path=self.socket_path)
        )
        try:
            async with asyncio.timeout(request.invocation.timeout_seconds + _REQUEST_GRACE_SECONDS):
                response = await asyncio.shield(pending)
        except (asyncio.CancelledError, TimeoutError):
            await _uninterrupted(
                self._terminate_and_drain(request.invocation.execution_id, pending)
            )
            raise
        if not response.ok:
            if response.error == "python_project_cleanup_incomplete":
                raise ProcessCleanupIncomplete(response.error)
            raise OSError(response.error or "python_project_execution_unavailable")
        return response

    async def _terminate_and_drain(
        self,
        execution_id: str,
        pending: asyncio.Task[PythonProjectBrokerResponse],
    ) -> None:
        try:
            await self.terminate(execution_id)
        finally:
            if not pending.done():
                pending.cancel()
            # Retrieve even a late successful reply, but never turn interruption
            # into success. Wait for the transport's finally/close to finish.
            with contextlib.suppress(asyncio.CancelledError, OSError, ValueError):
                late = await pending
                if late._product_stream is not None:
                    late._product_stream.close()

    async def terminate(self, execution_id: str) -> None:
        if not _systemd_supported():
            raise OSError("python_project_systemd_linux_unsupported")
        request = ProjectTerminateRequest(execution_id=execution_id)
        try:
            async with asyncio.timeout(_CLEANUP_SECONDS):
                response = await request_project_broker(request, socket_path=self.socket_path)
            if not response.ok or response.result is not None or response.error is not None:
                raise ProcessCleanupIncomplete("python_project_cleanup_incomplete")
        except (OSError, ValueError, TimeoutError):
            raise ProcessCleanupIncomplete("python_project_cleanup_incomplete") from None
