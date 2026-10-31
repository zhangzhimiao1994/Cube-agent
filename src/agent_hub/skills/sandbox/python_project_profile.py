"""v6 configuration discovery, not measurement, availability or install authority.

The broker authenticates its peer BEFORE dispatch. This action needs no workspace
or original contract. Only public configuration fields cross the socket; a later
Root probe must still measure the interpreter, wheels and project controls.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import socket
import struct
import time
from pathlib import Path
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from agent_hub.capabilities.python_project_inputs import PythonInterpreterIdentity
from agent_hub.capabilities.python_project_wheels import PythonWheelBinding
from agent_hub.skills.sandbox.project import ProjectInvocation, _systemd_supported, _uninterrupted

_MAX_REQUEST_BYTES = 16 * 1024
_MAX_RESPONSE_BYTES = 1024**2
_REQUEST_SECONDS = 10.0
_CLOSE_SECONDS = 2.0
_REQUEST_INVALID = "python_project_profile_request_invalid"
_RESPONSE_INVALID = "python_project_profile_response_invalid"
type ProfileError = Literal[
    "python_project_profile_unavailable", "python_project_profile_unsupported",
    "python_project_runtime_missing", "python_project_runtime_untrusted",
    "input_supply_configuration_missing", "input_supply_configuration_untrusted",
]
_UNAVAILABLE: ProfileError = "python_project_profile_unavailable"
Digest = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
ScopedName = Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9_-]*$")]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class _Envelope(_Strict):
    version: Literal[6] = 6
    action: Literal["inspect_python_project_profile"] = "inspect_python_project_profile"

    @field_validator("version", mode="before")
    @classmethod
    def integer_version(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError(_REQUEST_INVALID)
        return value


class PythonProjectProfileRequest(_Envelope):
    invocation: ProjectInvocation
    expected_source_sha256: Digest | None = None

    @field_validator("invocation", mode="before")
    @classmethod
    def base_invocation_only(cls, value: object) -> object:
        if isinstance(value, ProjectInvocation) and type(value) is not ProjectInvocation:
            raise ValueError(_REQUEST_INVALID)
        return value

    @model_validator(mode="after")
    def no_install_authority(self) -> Self:
        if self.invocation.install_allowed is not False:
            raise ValueError(_REQUEST_INVALID)
        return self


class PythonProjectProfileScope(_Strict):
    tenant_id: UUID
    run_id: UUID
    project_id: ScopedName
    session_id: ScopedName
    execution_id: str = Field(min_length=1, max_length=128, pattern=r"^[a-z0-9][a-z0-9_-]*$")

    @classmethod
    def for_invocation(cls, invocation: ProjectInvocation) -> Self:
        return cls.model_validate({name: getattr(invocation, name) for name in (
            "tenant_id", "run_id", "project_id", "session_id", "execution_id")}, strict=True)


class PythonProjectProfileInterpreter(_Strict):
    implementation: str
    version: str
    abi: str
    platform: str
    binary_sha256: Digest
    base_runtime_sha256: Digest

    @model_validator(mode="after")
    def declared_supported_identity(self) -> Self:
        PythonInterpreterIdentity.from_payload(self.model_dump())
        return self


class PythonProjectProfileWheel(_Strict):
    name: str = Field(min_length=1, max_length=255)
    filename: str = Field(min_length=1, max_length=255)
    version: str = Field(min_length=1, max_length=255)
    sha256: Digest
    size_bytes: int = Field(gt=0, le=64 * 1024**2)

    @model_validator(mode="after")
    def declared_universal_wheel(self) -> Self:
        PythonWheelBinding(self.filename, self.name, self.version, self.sha256)
        return self


class PythonProjectProfile(_Strict):
    status: Literal["configured_not_probed"] = "configured_not_probed"
    interpreter: PythonProjectProfileInterpreter
    catalog: tuple[PythonProjectProfileWheel, ...] = Field(min_length=1, max_length=1000)
    # RootSupplyConfig.digest binds the entire private policy, not just this public projection.
    catalog_sha256: Digest
    root_config_sha256: Digest | None = None
    source_sha256: Digest | None = None
    target_environment: dict[str, str] | None = None

    @model_validator(mode="after")
    def complete_sorted_catalog(self) -> Self:
        keys = tuple((entry.name, entry.version) for entry in self.catalog)
        if keys != tuple(sorted(set(keys))) or len({e.filename for e in self.catalog}) != len(keys):
            raise ValueError(_RESPONSE_INVALID)
        if (self.source_sha256 is None) != (self.target_environment is None):
            raise ValueError(_RESPONSE_INVALID)
        if self.target_environment is not None:
            from agent_hub.skills.sandbox.python_registry_protocol import target_environment

            target_environment(self.target_environment)
        return self


class PythonProjectProfileResponse(_Envelope):
    scope: PythonProjectProfileScope
    ok: bool
    error: ProfileError | None = None
    profile: PythonProjectProfile | None = None

    @model_validator(mode="after")
    def exclusive_outcome(self) -> Self:
        if (self.ok and (self.error is not None or self.profile is None)
                or not self.ok and (self.error is None or self.profile is not None)):
            raise ValueError(_RESPONSE_INVALID)
        return self

    def require_for(self, request: PythonProjectProfileRequest) -> None:
        if self.scope != PythonProjectProfileScope.for_invocation(request.invocation):
            raise ValueError(_RESPONSE_INVALID)
        if request.expected_source_sha256 is not None and self.ok and (
            self.profile is None or self.profile.source_sha256 != request.expected_source_sha256
            or self.profile.target_environment is None
        ):
            raise ValueError(_RESPONSE_INVALID)


def _object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _constant(value: str) -> object:
    raise ValueError("nonfinite JSON")


def _check_json(payload: bytes, limit: int) -> None:
    if type(payload) is not bytes or not 0 < len(payload) <= limit:
        raise ValueError("JSON size")
    value = json.loads(payload, object_pairs_hook=_object, parse_constant=_constant)
    if type(value) is not dict:
        raise ValueError("JSON object required")


def _parse_request(payload: bytes) -> PythonProjectProfileRequest:
    try:
        _check_json(payload, _MAX_REQUEST_BYTES)
        return PythonProjectProfileRequest.model_validate_json(payload, strict=True)
    except (ValueError, TypeError, RecursionError):
        raise ValueError(_REQUEST_INVALID) from None


def _validated_request(request: PythonProjectProfileRequest) -> PythonProjectProfileRequest:
    try:
        if type(request) is not PythonProjectProfileRequest:
            raise ValueError
        return _parse_request(request.model_dump_json(warnings="error").encode("utf-8"))
    except (ValueError, TypeError):
        raise ValueError(_REQUEST_INVALID) from None


def _read_profile(
    *, invocation: ProjectInvocation | None = None, expected_source_sha256: str | None = None,
) -> tuple[PythonProjectProfile | None, ProfileError | None]:
    from agent_hub.skills.sandbox.python_input_supply_catalog import (
        InputSupplyError,
        read_supply_configuration,
    )
    from agent_hub.skills.sandbox.python_project_backend import (
        PythonProjectBackendError,
        read_root_configuration,
    )

    codes: dict[str, ProfileError] = {
        "python_project_runtime_missing": "python_project_runtime_missing",
        "python_project_runtime_untrusted": "python_project_runtime_untrusted",
        "input_supply_configuration_missing": "input_supply_configuration_missing",
        "input_supply_configuration_untrusted": "input_supply_configuration_untrusted",
    }
    try:
        root = read_root_configuration()
        supply = read_supply_configuration()
    except (PythonProjectBackendError, InputSupplyError) as error:
        return None, codes.get(error.code, _UNAVAILABLE)
    from agent_hub.skills.sandbox.python_metadata_plan import _digest

    environment = None
    if expected_source_sha256 is not None:
        if type(invocation) is not ProjectInvocation:
            raise ValueError(_REQUEST_INVALID)
        from agent_hub.skills.sandbox.python_dependency_inspection import _target_environment
        from agent_hub.skills.sandbox.python_resolution_overlay import effective_supply

        supply = effective_supply(supply, invocation, source_sha256=expected_source_sha256,
                                  deadline=time.monotonic() + min(invocation.timeout_seconds, _REQUEST_SECONDS))
        environment = _target_environment(root)
    return PythonProjectProfile(
        interpreter=PythonProjectProfileInterpreter.model_validate(root.interpreter.to_payload(), strict=True),
        catalog=tuple(sorted((PythonProjectProfileWheel(name=e.name, filename=e.filename,
            version=e.version, sha256=e.sha256, size_bytes=e.size_bytes) for e in supply.catalog),
            key=lambda e: (e.name, e.version))),
        catalog_sha256=supply.digest,
        root_config_sha256=_digest(root.to_payload()),
        source_sha256=expected_source_sha256, target_environment=environment,
    ), None


def dispatch_profile_request(payload: bytes) -> bytes:
    """Authenticated broker entry; return unframed v6 JSON, no filesystem mutation.

    Invalid input raises a finite ValueError before any Root configuration read.
    Valid requests always receive a scope-bound success or typed unavailable.
    """
    request = _parse_request(payload)
    scope = PythonProjectProfileScope.for_invocation(request.invocation)
    try:
        profile, error = (_read_profile() if request.expected_source_sha256 is None else
            _read_profile(invocation=request.invocation, expected_source_sha256=request.expected_source_sha256))
        response = PythonProjectProfileResponse(scope=scope, ok=profile is not None,
                                                profile=profile, error=error)
        raw = response.model_dump_json().encode("utf-8")
        if len(raw) > _MAX_RESPONSE_BYTES:
            raise ValueError("profile size")
        return raw
    except Exception:  # noqa: BLE001 - paths, credentials and arbitrary exception text stay private.
        return PythonProjectProfileResponse(scope=scope, ok=False, error=_UNAVAILABLE).model_dump_json().encode()


def _parse_response(payload: bytes, request: PythonProjectProfileRequest) -> PythonProjectProfileResponse:
    try:
        _check_json(payload, _MAX_RESPONSE_BYTES)
        response = PythonProjectProfileResponse.model_validate_json(payload, strict=True)
        response.require_for(request)
        return response
    except (ValueError, TypeError, RecursionError):
        raise ValueError(_RESPONSE_INVALID) from None


async def _close_socket(writer: asyncio.StreamWriter) -> None:
    writer.close()
    # No process/unit was started: close the transport and bound its handshake wait.
    with contextlib.suppress(OSError, TimeoutError):
        async with asyncio.timeout(_CLOSE_SECONDS):
            await writer.wait_closed()


def _root_peer(writer: asyncio.StreamWriter) -> None:
    peer = writer.get_extra_info('socket')
    option = getattr(socket, 'SO_PEERCRED', None)
    if peer is None or type(option) is not int:
        raise OSError(_UNAVAILABLE)
    try:
        pid, uid, gid = struct.unpack('3i', peer.getsockopt(socket.SOL_SOCKET, option, 12))
    except (OSError, ValueError, TypeError, struct.error):
        raise OSError(_UNAVAILABLE) from None
    if pid <= 0 or uid != 0 or gid != 0:
        raise OSError(_UNAVAILABLE)


async def request_python_project_profile_broker(
    request: PythonProjectProfileRequest, *, socket_path: Path | None = None,
) -> PythonProjectProfileResponse:
    # Lazy import allows broker.py to import the v6 DTOs without a framing import cycle.
    from agent_hub.skills.sandbox.broker import (
        BROKER_SOCKET_PATH,
        BrokerRequestError,
        _read_frame,
        _write_frame,
    )

    request = _validated_request(request)
    connect = getattr(asyncio, "open_unix_connection", None)
    if connect is None:
        raise OSError("python_project_profile_unsupported")
    try:
        reader, writer = await connect((socket_path or BROKER_SOCKET_PATH).as_posix())
    except OSError:
        raise OSError(_UNAVAILABLE) from None
    try:
        _root_peer(writer)
        await _write_frame(writer, request.model_dump_json().encode("utf-8"))
        return _parse_response(await _read_frame(reader, _MAX_RESPONSE_BYTES), request)
    except (BrokerRequestError, asyncio.IncompleteReadError, ValueError, TypeError):
        raise ValueError(_RESPONSE_INVALID) from None
    except OSError:
        raise OSError(_UNAVAILABLE) from None
    finally:
        await _uninterrupted(_close_socket(writer))


async def _cancel_and_drain(pending: asyncio.Task[PythonProjectProfileResponse]) -> None:
    if not pending.done():
        pending.cancel()
    with contextlib.suppress(asyncio.CancelledError, OSError, ValueError):
        await pending


class PythonProjectProfileSandbox:
    def __init__(self, *, socket_path: Path | None = None) -> None:
        self.socket_path = socket_path

    async def inspect(self, invocation: ProjectInvocation, *,
                      expected_source_sha256: str | None = None) -> PythonProjectProfileResponse:
        try:
            request = _validated_request(PythonProjectProfileRequest(
                invocation=invocation, expected_source_sha256=expected_source_sha256))
        except (ValueError, TypeError):
            raise ValueError(_REQUEST_INVALID) from None
        if not _systemd_supported():
            raise OSError("python_project_profile_unsupported")
        pending = asyncio.create_task(request_python_project_profile_broker(request, socket_path=self.socket_path))
        try:
            async with asyncio.timeout(min(invocation.timeout_seconds, _REQUEST_SECONDS)):
                response = await asyncio.shield(pending)
        except (asyncio.CancelledError, TimeoutError):
            await _uninterrupted(_cancel_and_drain(pending))
            raise
        # Revalidate even a substituted transport: no copied/constructed DTO is trusted.
        try:
            if type(response) is not PythonProjectProfileResponse:
                raise ValueError
            return _parse_response(response.model_dump_json(warnings="error").encode(), request)
        except (ValueError, TypeError):
            raise ValueError(_RESPONSE_INVALID) from None
