"""Authenticated, readonly v9 metadata planning transport; never an install grant."""

from __future__ import annotations

import asyncio
import contextlib
import socket
import struct
from pathlib import Path

from agent_hub.skills.sandbox.project import _systemd_supported, _uninterrupted
from agent_hub.skills.sandbox.python_dependency_protocol import _check_json
from agent_hub.skills.sandbox.python_metadata_plan_protocol import (
    PythonMetadataPlanningRequest,
    PythonMetadataPlanningResponse,
)

_REQUEST_BYTES = 16 * 1024
_RESPONSE_BYTES = 16 * 1024
_UNAVAILABLE = "metadata_plan_unavailable"
_INVALID_REQUEST = "metadata_plan_request_invalid"
_INVALID_RESPONSE = "metadata_plan_response_invalid"


def parse_metadata_planning_request(payload: bytes) -> PythonMetadataPlanningRequest:
    try:
        _check_json(payload, _REQUEST_BYTES)
        return PythonMetadataPlanningRequest.model_validate_json(payload, strict=True)
    except (ValueError, TypeError, RecursionError):
        raise ValueError(_INVALID_REQUEST) from None


def _validated_request(request: PythonMetadataPlanningRequest) -> PythonMetadataPlanningRequest:
    try:
        if type(request) is not PythonMetadataPlanningRequest:
            raise ValueError
        checked = PythonMetadataPlanningRequest.model_validate(request, strict=True)
        return parse_metadata_planning_request(checked.model_dump_json(warnings="error").encode())
    except (ValueError, TypeError, AttributeError, RecursionError):
        raise ValueError(_INVALID_REQUEST) from None


def _response(payload: bytes, request: PythonMetadataPlanningRequest) -> PythonMetadataPlanningResponse:
    try:
        _check_json(payload, _RESPONSE_BYTES)
        response = PythonMetadataPlanningResponse.model_validate_json(payload, strict=True)
        response.require_for(request)
        return response
    except (ValueError, TypeError, RecursionError):
        raise ValueError(_INVALID_RESPONSE) from None


def _root_peer(writer: asyncio.StreamWriter) -> None:
    transport_socket = writer.get_extra_info("socket")
    option = getattr(socket, "SO_PEERCRED", None)
    if transport_socket is None or type(option) is not int:
        raise OSError(_UNAVAILABLE)
    pid, uid, gid = struct.unpack("3i", transport_socket.getsockopt(socket.SOL_SOCKET, option, 12))
    if pid <= 0 or uid != 0 or gid != 0:
        raise OSError(_UNAVAILABLE)


async def _close(writer: asyncio.StreamWriter) -> None:
    with contextlib.suppress(OSError, TimeoutError):
        writer.close()
        async with asyncio.timeout(2):
            await writer.wait_closed()


async def request_python_metadata_plan_broker(
    request: PythonMetadataPlanningRequest, *, socket_path: Path | None = None,
) -> PythonMetadataPlanningResponse:
    request = _validated_request(request)
    connect = getattr(asyncio, "open_unix_connection", None)
    if not _systemd_supported() or connect is None:
        raise OSError(_UNAVAILABLE)
    from agent_hub.skills.sandbox.broker import (
        BROKER_SOCKET_PATH,
        BrokerRequestError,
        _read_frame,
        _write_frame,
    )

    writer: asyncio.StreamWriter | None = None
    try:
        async with asyncio.timeout(request.invocation.timeout_seconds):
            reader, writer = await connect((socket_path or BROKER_SOCKET_PATH).as_posix())
            _root_peer(writer)
            await _write_frame(writer, request.model_dump_json(warnings="error").encode())
            return _response(await _read_frame(reader, _RESPONSE_BYTES), request)
    except TimeoutError:
        raise TimeoutError("metadata_plan_timeout") from None
    except (BrokerRequestError, asyncio.IncompleteReadError, ValueError, TypeError, RecursionError):
        raise ValueError(_INVALID_RESPONSE) from None
    except (OSError, NotImplementedError, struct.error):
        raise OSError(_UNAVAILABLE) from None
    finally:
        if writer is not None:
            await _uninterrupted(_close(writer))
