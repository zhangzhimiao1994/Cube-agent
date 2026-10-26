"""v8 read-only dependency closure observations, without execution authority."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import math
from pathlib import Path
from typing import Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    JsonValue,
    field_validator,
    model_validator,
)

from agent_hub.skills.sandbox.project import ProjectInvocation, _systemd_supported, _uninterrupted
from agent_hub.skills.sandbox.python_project_profile import Digest, PythonProjectProfileScope

_MAX_REQUEST_BYTES = 16 * 1024
_MAX_RESPONSE_BYTES = 32 * 1024
_CLOSE_SECONDS = 2.0
_REQUEST_INVALID = "python_dependency_request_invalid"
_RESPONSE_INVALID = "python_dependency_response_invalid"
type PythonDependencyClosureError = Literal[
    "python_dependency_unavailable",
    "python_dependency_metadata_unavailable",
    "python_dependency_metadata_untrusted",
    "python_dependency_catalog_changed",
    "python_dependency_draft_changed",
    "python_dependency_declaration_invalid",
]
_UNAVAILABLE: PythonDependencyClosureError = "python_dependency_unavailable"
_MARKER_KEYS = frozenset({
    "implementation_name", "implementation_version", "os_name", "platform_machine",
    "platform_release", "platform_system", "platform_version",
    "platform_python_implementation", "python_full_version", "python_version", "sys_platform",
})


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class _Envelope(_Strict):
    version: Literal[8] = 8
    action: Literal["inspect_python_dependency_closure"] = "inspect_python_dependency_closure"

    @field_validator("version", mode="before")
    @classmethod
    def integer_version(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError(_REQUEST_INVALID)
        return value


class PythonDependencyClosureRequest(_Envelope):
    invocation: ProjectInvocation
    expected_source_sha256: Digest
    expected_catalog_sha256: Digest

    @field_validator("invocation", mode="before")
    @classmethod
    def base_invocation_only(cls, value: object) -> object:
        if isinstance(value, ProjectInvocation):
            if type(value) is not ProjectInvocation:
                raise ValueError(_REQUEST_INVALID)
            # Frozen models can still be forged with model_copy/model_construct.
            return ProjectInvocation.model_validate(
                value.model_dump(warnings="error"), strict=True,
            )
        return value

    @model_validator(mode="after")
    def read_only_authority(self) -> Self:
        if (type(self.invocation) is not ProjectInvocation
                or self.invocation.install_allowed is not False
                or self.invocation.sandbox_profile != "restricted"):
            raise ValueError(_REQUEST_INVALID)
        return self


class PythonDependencyClosureObservation(_Strict):
    source_sha256: Digest
    manifest_sha256: Digest
    lock_sha256: Digest
    catalog_sha256: Digest
    metadata_index_sha256: Digest
    target_environment_sha256: Digest
    target_environment: dict[str, str]
    closure: dict[str, JsonValue]

    @model_validator(mode="after")
    def validated_observation(self) -> Self:
        from agent_hub.capabilities.python_dependency_closure import PythonDependencyClosure

        if set(self.target_environment) != _MARKER_KEYS:
            raise ValueError(_RESPONSE_INVALID)
        canonical = json.dumps(self.target_environment, sort_keys=True,
                               separators=(",", ":"), ensure_ascii=True).encode("ascii")
        if hashlib.sha256(canonical).hexdigest() != self.target_environment_sha256:
            raise ValueError(_RESPONSE_INVALID)
        PythonDependencyClosure.from_payload(self.closure)
        return self


class PythonDependencyClosureResponse(_Envelope):
    scope: PythonProjectProfileScope
    ok: bool
    error: PythonDependencyClosureError | None = None
    observation: PythonDependencyClosureObservation | None = None

    @model_validator(mode="after")
    def exclusive_outcome(self) -> Self:
        if (self.ok and (self.error is not None or self.observation is None)
                or not self.ok and (self.error is None or self.observation is not None)):
            raise ValueError(_RESPONSE_INVALID)
        return self

    def require_for(self, request: PythonDependencyClosureRequest) -> None:
        request = _validated_request(request)
        if self.scope != PythonProjectProfileScope.for_invocation(request.invocation):
            raise ValueError(_RESPONSE_INVALID)
        if self.observation is not None and (
            self.observation.source_sha256 != request.expected_source_sha256
            or self.observation.catalog_sha256 != request.expected_catalog_sha256
        ):
            raise ValueError(_RESPONSE_INVALID)


def _object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _constant(value: str) -> object:
    raise ValueError("nonfinite JSON")


def _float(value: str) -> float:
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("nonfinite JSON")
    return result


def _check_json(payload: bytes, limit: int) -> None:
    if type(payload) is not bytes or not 0 < len(payload) <= limit:
        raise ValueError("JSON size")
    value = json.loads(payload, object_pairs_hook=_object,
                       parse_constant=_constant, parse_float=_float)
    if type(value) is not dict:
        raise ValueError("JSON object required")


def _parse_request(payload: bytes) -> PythonDependencyClosureRequest:
    try:
        _check_json(payload, _MAX_REQUEST_BYTES)
        return PythonDependencyClosureRequest.model_validate_json(payload, strict=True)
    except (ValueError, TypeError, RecursionError):
        raise ValueError(_REQUEST_INVALID) from None


def _validated_request(request: PythonDependencyClosureRequest) -> PythonDependencyClosureRequest:
    try:
        if (type(request) is not PythonDependencyClosureRequest
                or type(request.invocation) is not ProjectInvocation):
            raise ValueError
        # Integer serializers can turn bool into 0/1; validate Python values first.
        request = PythonDependencyClosureRequest.model_validate(
            request.model_dump(warnings="error"), strict=True,
        )
        return _parse_request(request.model_dump_json(warnings="error").encode("utf-8"))
    except (ValueError, TypeError, RecursionError):
        raise ValueError(_REQUEST_INVALID) from None


def _parse_response(
    payload: bytes, request: PythonDependencyClosureRequest,
) -> PythonDependencyClosureResponse:
    try:
        _check_json(payload, _MAX_RESPONSE_BYTES)
        response = PythonDependencyClosureResponse.model_validate_json(payload, strict=True)
        response.require_for(request)
        return response
    except (ValueError, TypeError, RecursionError):
        raise ValueError(_RESPONSE_INVALID) from None


async def _close_socket(writer: asyncio.StreamWriter) -> None:
    with contextlib.suppress(OSError, TimeoutError):
        writer.close()
        async with asyncio.timeout(_CLOSE_SECONDS):
            await writer.wait_closed()


async def request_python_dependency_closure_broker(
    request: PythonDependencyClosureRequest, *, socket_path: Path | None = None,
) -> PythonDependencyClosureResponse:
    request = _validated_request(request)
    connect = getattr(asyncio, "open_unix_connection", None)
    if not _systemd_supported() or connect is None:
        raise OSError(_UNAVAILABLE)

    # Root owns dispatch; keep framing imports lazy to permit its DTO imports.
    from agent_hub.skills.sandbox.broker import (
        BROKER_SOCKET_PATH,
        BrokerRequestError,
        _read_frame,
        _write_frame,
    )

    writer: asyncio.StreamWriter | None = None
    try:
        async with asyncio.timeout(request.invocation.timeout_seconds + 1):
            reader, writer = await connect((socket_path or BROKER_SOCKET_PATH).as_posix())
            await _write_frame(writer, request.model_dump_json().encode("utf-8"))
            return _parse_response(await _read_frame(reader, _MAX_RESPONSE_BYTES), request)
    except TimeoutError:
        raise TimeoutError(_UNAVAILABLE) from None
    except (BrokerRequestError, asyncio.IncompleteReadError, ValueError, TypeError, RecursionError):
        raise ValueError(_RESPONSE_INVALID) from None
    except (OSError, NotImplementedError):
        raise OSError(_UNAVAILABLE) from None
    finally:
        if writer is not None:
            await _uninterrupted(_close_socket(writer))


async def _cancel_and_drain(pending: asyncio.Task[PythonDependencyClosureResponse]) -> None:
    if not pending.done():
        pending.cancel()
    with contextlib.suppress(asyncio.CancelledError, OSError, ValueError):
        await pending


class PythonDependencyClosureSandbox:
    def __init__(self, *, socket_path: Path | None = None) -> None:
        self.socket_path = socket_path

    async def inspect(
        self, invocation: ProjectInvocation, *,
        expected_source_sha256: str, expected_catalog_sha256: str,
    ) -> PythonDependencyClosureObservation:
        try:
            request = _validated_request(PythonDependencyClosureRequest(invocation=invocation,
                expected_source_sha256=expected_source_sha256,
                expected_catalog_sha256=expected_catalog_sha256))
        except (ValueError, TypeError, RecursionError):
            raise ValueError(_REQUEST_INVALID) from None
        if not _systemd_supported():
            raise OSError(_UNAVAILABLE)
        pending = asyncio.create_task(request_python_dependency_closure_broker(
            request, socket_path=self.socket_path))
        try:
            async with asyncio.timeout(request.invocation.timeout_seconds + 1):
                response = await asyncio.shield(pending)
        except (asyncio.CancelledError, TimeoutError):
            await _uninterrupted(_cancel_and_drain(pending))
            raise
        try:
            if type(response) is not PythonDependencyClosureResponse:
                raise ValueError
            response = _parse_response(response.model_dump_json(warnings="error").encode(), request)
        except (ValueError, TypeError, RecursionError):
            raise ValueError(_RESPONSE_INVALID) from None
        if not response.ok:
            raise ValueError(response.error or _UNAVAILABLE)
        assert response.observation is not None
        return response.observation
