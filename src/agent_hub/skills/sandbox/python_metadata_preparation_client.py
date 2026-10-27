"""Authenticated v10 metadata preparation SDK; receipts never grant installation."""

from __future__ import annotations

import asyncio
import contextlib
import math
import socket
import struct
from pathlib import Path
from typing import Literal, get_args

from agent_hub.skills.sandbox.project import ProjectInvocation, _systemd_supported, _uninterrupted
from agent_hub.skills.sandbox.python_dependency_protocol import _check_json
from agent_hub.skills.sandbox.python_metadata_plan_client import request_python_metadata_plan_broker
from agent_hub.skills.sandbox.python_metadata_plan_protocol import (
    MetadataPlanningError,
    PythonMetadataDraft,
    PythonMetadataPlan,
    PythonMetadataPlanningRequest,
    PythonMetadataPlanningResponse,
    PythonMetadataScope,
)
from agent_hub.skills.sandbox.python_metadata_preparation_protocol import (
    MetadataPreparationAction,
    MetadataPreparationError,
    PythonMetadataPreparationReceipt,
    PythonMetadataPreparationRequest,
    PythonMetadataPreparationResponse,
    PythonMetadataPreparationStatus,
    _fields,
    _plan_fields,
)
from agent_hub.skills.sandbox.python_project_profile import (
    ProfileError,
    PythonProjectProfile,
    PythonProjectProfileInterpreter,
    PythonProjectProfileRequest,
    PythonProjectProfileResponse,
    PythonProjectProfileSandbox,
    PythonProjectProfileScope,
    PythonProjectProfileWheel,
)

_REQUEST_BYTES = 16 * 1024
_RESPONSE_BYTES = 16 * 1024
_UNAVAILABLE = "metadata_prepare_unavailable"
_INVALID_REQUEST = "metadata_prepare_request_invalid"
_INVALID_RESPONSE = "metadata_prepare_response_invalid"
_TIMEOUT = "metadata_prepare_timeout"
_Phase = Literal["profile", "plan", "prepare", "inspect"]
_LOCAL_CODES = frozenset({_INVALID_REQUEST, _INVALID_RESPONSE, _UNAVAILABLE, _TIMEOUT})
_PHASE_CODES = {
    "profile": _LOCAL_CODES | frozenset(get_args(ProfileError.__value__)),
    "plan": _LOCAL_CODES | frozenset(get_args(MetadataPlanningError.__value__)),
    "prepare": _LOCAL_CODES | frozenset(get_args(MetadataPreparationError.__value__)),
    "inspect": _LOCAL_CODES | frozenset(get_args(MetadataPreparationError.__value__)),
}
PUBLIC_FAILURE_CODES = frozenset(
    f"python_metadata.{phase}.{code}"
    for phase, codes in _PHASE_CODES.items() for code in codes
)


class PythonMetadataPreparationFailure(RuntimeError):
    """Finite SDK failure data, never evidence of Root provenance or retry authority."""

    def __init__(self, *, phase: str, code: str) -> None:
        if (type(self) is not PythonMetadataPreparationFailure
                or type(phase) is not str or type(code) is not str
                or f"python_metadata.{phase}.{code}" not in PUBLIC_FAILURE_CODES):
            raise ValueError("metadata_prepare_failure_invalid")
        self.phase = phase
        self.code = code
        super().__init__(self.public_code)

    @property
    def public_code(self) -> str:
        return f"python_metadata.{self.phase}.{self.code}"


def parse_metadata_preparation_request(payload: bytes) -> PythonMetadataPreparationRequest:
    try:
        _check_json(payload, _REQUEST_BYTES)
        return PythonMetadataPreparationRequest.model_validate_json(payload, strict=True)
    except (ValueError, TypeError, RecursionError):
        raise ValueError(_INVALID_REQUEST) from None


def _validated_request(request: PythonMetadataPreparationRequest) -> PythonMetadataPreparationRequest:
    try:
        if type(request) is not PythonMetadataPreparationRequest:
            raise ValueError
        # The DTO recursively checks original Python fields before any serializer
        # can erase copied extras or convert bool into an integer representation.
        checked = PythonMetadataPreparationRequest.model_validate(request, strict=True)
        return parse_metadata_preparation_request(checked.model_dump_json(warnings="error").encode())
    except (ValueError, TypeError, AttributeError, RecursionError):
        raise ValueError(_INVALID_REQUEST) from None


def _response(
    payload: bytes, request: PythonMetadataPreparationRequest,
) -> PythonMetadataPreparationResponse:
    try:
        _check_json(payload, _RESPONSE_BYTES)
        response = PythonMetadataPreparationResponse.model_validate_json(payload, strict=True)
        response.require_for(request)
        return response
    except (ValueError, TypeError, AttributeError, RecursionError):
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


async def request_python_metadata_preparation_broker(
    request: PythonMetadataPreparationRequest, *, socket_path: Path | None = None,
) -> PythonMetadataPreparationResponse:
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
        raise TimeoutError("metadata_prepare_timeout") from None
    except (BrokerRequestError, asyncio.IncompleteReadError, ValueError, TypeError, RecursionError):
        raise ValueError(_INVALID_RESPONSE) from None
    except (OSError, NotImplementedError, struct.error):
        raise OSError(_UNAVAILABLE) from None
    finally:
        if writer is not None:
            await _uninterrupted(_close(writer))


def _planning_inputs(
    invocation: ProjectInvocation, scope: PythonMetadataScope, draft: PythonMetadataDraft,
) -> tuple[ProjectInvocation, PythonMetadataScope, PythonMetadataDraft]:
    try:
        if (type(invocation) is not ProjectInvocation or type(scope) is not PythonMetadataScope
                or type(draft) is not PythonMetadataDraft):
            raise ValueError
        invocation = ProjectInvocation.model_validate(vars(invocation), strict=True)
        scope = PythonMetadataScope.model_validate(vars(scope), strict=True)
        draft = PythonMetadataDraft.model_validate(vars(draft), strict=True)
        if invocation.sandbox_profile != "restricted" or invocation.install_allowed is not False:
            raise ValueError
        scope.require_invocation(invocation)
        return invocation, scope, draft
    except (ValueError, TypeError, AttributeError, RecursionError):
        raise ValueError(_INVALID_REQUEST) from None


def _profile_response(
    value: PythonProjectProfileResponse, invocation: ProjectInvocation,
) -> PythonProjectProfileResponse:
    try:
        if type(value) is not PythonProjectProfileResponse:
            raise ValueError
        fields = dict(vars(value)) | (value.model_extra or {})
        fields["scope"] = _fields(fields.get("scope"), PythonProjectProfileScope)
        profile = _fields(fields.get("profile"), PythonProjectProfile)
        if isinstance(profile, dict):
            profile = profile.copy()
            profile["interpreter"] = _fields(profile.get("interpreter"), PythonProjectProfileInterpreter)
            catalog = profile.get("catalog")
            if isinstance(catalog, tuple):
                profile["catalog"] = tuple(_fields(item, PythonProjectProfileWheel) for item in catalog)
        fields["profile"] = profile
        checked = PythonProjectProfileResponse.model_validate(fields, strict=True)
        checked.require_for(PythonProjectProfileRequest(invocation=invocation))
        return checked
    except (ValueError, TypeError, AttributeError, RecursionError):
        raise ValueError(_INVALID_RESPONSE) from None


def _planning_response(
    value: PythonMetadataPlanningResponse, request: PythonMetadataPlanningRequest,
) -> PythonMetadataPlanningResponse:
    try:
        if type(value) is not PythonMetadataPlanningResponse:
            raise ValueError
        fields = dict(vars(value)) | (value.model_extra or {})
        fields["scope"] = _fields(fields.get("scope"), PythonProjectProfileScope)
        if fields.get("plan") is not None:
            fields["plan"] = _plan_fields(fields["plan"])
        checked = PythonMetadataPlanningResponse.model_validate(fields, strict=True)
        checked.require_for(request)
        return checked
    except (ValueError, TypeError, AttributeError, RecursionError):
        raise ValueError(_INVALID_RESPONSE) from None


def _preparation_request(
    action: MetadataPreparationAction, invocation: ProjectInvocation, plan: PythonMetadataPlan,
    execution_id: str | None = None,
) -> PythonMetadataPreparationRequest:
    try:
        if type(plan) is not PythonMetadataPlan:
            raise ValueError
        plan = PythonMetadataPlan.model_validate(_plan_fields(plan), strict=True)
        if plan.mode == "ready":
            raise ValueError
        invocation, scope, draft = _planning_inputs(invocation, plan.scope, plan.draft)
        planning = PythonMetadataPlanningRequest(invocation=invocation, scope=scope, draft=draft,
            expected_root_config_sha256=plan.root_config_sha256,
            expected_catalog_sha256=plan.catalog_sha256)
        return PythonMetadataPreparationRequest(action=action, planning=planning,
            expected_plan_sha256=plan.sha256, preparation_execution_id=execution_id)
    except (ValueError, TypeError, AttributeError, RecursionError):
        raise ValueError(_INVALID_REQUEST) from None


class PythonMetadataPreparationSandbox:
    def __init__(self, *, socket_path: Path | None = None) -> None:
        self.socket_path = socket_path

    async def plan(
        self, invocation: ProjectInvocation, scope: PythonMetadataScope, draft: PythonMetadataDraft,
    ) -> PythonMetadataPlan:
        try:
            invocation, scope, draft = _planning_inputs(invocation, scope, draft)
        except (ValueError, TypeError, AttributeError, RecursionError):
            raise PythonMetadataPreparationFailure(phase="plan", code=_INVALID_REQUEST) from None
        loop = asyncio.get_running_loop()
        deadline = loop.time() + invocation.timeout_seconds
        phase: _Phase = "profile"

        def remaining() -> ProjectInvocation:
            seconds = deadline - loop.time()
            if seconds <= 0:
                raise TimeoutError
            return invocation.model_copy(update={
                "timeout_seconds": min(invocation.timeout_seconds, math.ceil(seconds)),
            })

        try:
            async with asyncio.timeout_at(deadline):
                for attempt in range(2):
                    phase = "profile"
                    profile_invocation = remaining()
                    discovered = await PythonProjectProfileSandbox(socket_path=self.socket_path).inspect(
                        profile_invocation,
                    )
                    profile = _profile_response(discovered, profile_invocation)
                    if not profile.ok:
                        # Only a fully revalidated, scope-bound negative response
                        # qualifies. Transport exception text is never consulted.
                        if attempt == 0 and profile.error == "python_project_profile_unavailable":
                            continue
                        raise PythonMetadataPreparationFailure(phase=phase, code=profile.error or _UNAVAILABLE)
                    if profile.profile is None or profile.profile.root_config_sha256 is None:
                        raise PythonMetadataPreparationFailure(phase=phase, code=_UNAVAILABLE)
                    phase = "plan"
                    request = PythonMetadataPlanningRequest(invocation=remaining(), scope=scope, draft=draft,
                        expected_root_config_sha256=profile.profile.root_config_sha256,
                        expected_catalog_sha256=profile.profile.catalog_sha256)
                    result = _planning_response(await request_python_metadata_plan_broker(
                        request, socket_path=self.socket_path,
                    ), request)
                    if not result.ok:
                        if attempt == 0 and result.error in {"metadata_plan_unavailable", "metadata_plan_timeout"}:
                            continue
                        raise PythonMetadataPreparationFailure(phase=phase, code=result.error or _UNAVAILABLE)
                    if result.plan is None:
                        raise PythonMetadataPreparationFailure(phase=phase, code=_INVALID_RESPONSE)
                    return result.plan
        except PythonMetadataPreparationFailure:
            raise
        except TimeoutError:
            raise PythonMetadataPreparationFailure(phase=phase, code=_TIMEOUT) from None
        except (OSError, NotImplementedError):
            raise PythonMetadataPreparationFailure(phase=phase, code=_UNAVAILABLE) from None
        except (ValueError, TypeError, AttributeError, RecursionError):
            raise PythonMetadataPreparationFailure(phase=phase, code=_INVALID_RESPONSE) from None
        except Exception:  # noqa: BLE001 - unexpected transport text is never public SDK data.
            raise PythonMetadataPreparationFailure(phase=phase, code=_UNAVAILABLE) from None
        raise PythonMetadataPreparationFailure(phase=phase, code=_UNAVAILABLE)

    async def prepare(
        self, invocation: ProjectInvocation, plan: PythonMetadataPlan,
    ) -> PythonMetadataPreparationReceipt:
        try:
            request = _preparation_request("prepare_python_dependency_metadata", invocation, plan)
        except (ValueError, TypeError, AttributeError, RecursionError):
            raise PythonMetadataPreparationFailure(phase="prepare", code=_INVALID_REQUEST) from None
        response = await self._exchange(request, phase="prepare")
        if response.receipt is None:
            raise PythonMetadataPreparationFailure(phase="prepare", code=_INVALID_RESPONSE)
        return response.receipt

    async def inspect(
        self, invocation: ProjectInvocation, plan: PythonMetadataPlan, execution_id: str,
    ) -> PythonMetadataPreparationStatus:
        try:
            request = _preparation_request("inspect_python_dependency_metadata_preparation",
                invocation, plan, execution_id)
        except (ValueError, TypeError, AttributeError, RecursionError):
            raise PythonMetadataPreparationFailure(phase="inspect", code=_INVALID_REQUEST) from None
        response = await self._exchange(request, phase="inspect")
        if response.status is None:
            raise PythonMetadataPreparationFailure(phase="inspect", code=_INVALID_RESPONSE)
        return response.status

    async def _exchange(
        self, request: PythonMetadataPreparationRequest, *, phase: _Phase,
    ) -> PythonMetadataPreparationResponse:
        try:
            request = _validated_request(request)
        except (ValueError, TypeError, AttributeError, RecursionError):
            raise PythonMetadataPreparationFailure(phase=phase, code=_INVALID_REQUEST) from None
        try:
            async with asyncio.timeout(request.invocation.timeout_seconds):
                response = await request_python_metadata_preparation_broker(
                    request, socket_path=self.socket_path,
                )
                if type(response) is not PythonMetadataPreparationResponse:
                    raise ValueError
                response = PythonMetadataPreparationResponse.model_validate(response, strict=True)
                response.require_for(request)
        except TimeoutError:
            raise PythonMetadataPreparationFailure(phase=phase, code=_TIMEOUT) from None
        except (OSError, NotImplementedError):
            raise PythonMetadataPreparationFailure(phase=phase, code=_UNAVAILABLE) from None
        except (ValueError, TypeError, AttributeError, RecursionError):
            raise PythonMetadataPreparationFailure(phase=phase, code=_INVALID_RESPONSE) from None
        except Exception:  # noqa: BLE001 - preserve cancellation, sanitize other transport failures.
            raise PythonMetadataPreparationFailure(phase=phase, code=_UNAVAILABLE) from None
        if not response.ok:
            raise PythonMetadataPreparationFailure(phase=phase, code=response.error or _UNAVAILABLE)
        return response
