"""Private, scoped Root-frame capture before an approval releases the worker lease.

This carries continuity data, never permission or verification authority. The
reader must use the live worker; recovery must independently recheck Root state.
"""
from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING, NoReturn
from uuid import UUID

from agent_hub.capabilities.types import CapabilityRequest
from agent_hub.runtime.continuation_artifacts import _encode
from agent_hub.runtime.contracts import JsonValue, TaskContext, _freeze_object
from agent_hub.subprocess_cleanup import ProcessCleanupIncomplete

if TYPE_CHECKING:
    from agent_hub.harness.types import HarnessToolCallRequest

_ERROR = 'action approval capture is unavailable or changed'
_APPROVAL = re.compile(r'approval_[A-Za-z0-9_-]{1,100}')
type _Reader = Callable[[], Awaitable[Mapping[str, JsonValue]]]
type _Identity = tuple[UUID, UUID, UUID, str, str]


class ActionApprovalCaptureError(RuntimeError):
    """No matching, successful approval preparation capture is available."""


class Capture:
    __slots__ = ('_active', '_approval_id', '_failed', '_frame', '_identity', '_read')

    def __init__(self, identity: _Identity, read: _Reader) -> None:
        self._identity: _Identity | None = identity
        self._read: _Reader | None = read
        self._active = True
        self._approval_id: str | None = None
        self._frame: Mapping[str, JsonValue] | None = None
        self._failed = False

    def _reject(self) -> NoReturn:
        self._failed = True
        self._frame = None
        raise ActionApprovalCaptureError(_ERROR)

    def frame_for(self, approval_id: str) -> Mapping[str, JsonValue] | None:
        """Read a matching frozen frame after scope exit; None means no hook ran."""
        if (self._failed or type(approval_id) is not str or _APPROVAL.fullmatch(approval_id) is None
                or self._approval_id is not None and (
                    self._approval_id != approval_id or self._frame is None)):
            self._reject()
        return self._frame

    async def _before(self, request: CapabilityRequest, approval_id: str) -> None:
        if not self._active:
            self._reject()
        if (self._failed or type(request) is not CapabilityRequest
                or self._identity != (request.tenant_id, request.run_id, request.user_id,
                                      request.agent_id, request.idempotency_key)
                or type(approval_id) is not str or _APPROVAL.fullmatch(approval_id) is None
                or self._approval_id is not None and self._approval_id != approval_id):
            self._reject()
        if self._approval_id is not None:
            if self._frame is None:
                self._reject()
            return
        self._approval_id = approval_id
        read = self._read
        if read is None:
            self._reject()
        try:
            frame = await _read_frame(read)
            if not self._active or self._failed:
                self._reject()
            self._frame = frame
        except BaseException:
            self._failed = True
            self._frame = None
            raise

    def _revoke(self) -> None:
        self._active = False
        self._read = None
        self._identity = None
        if self._approval_id is not None and self._frame is None:
            self._failed = True


async def _read_frame(read: _Reader) -> Mapping[str, JsonValue]:
    try:
        frame = _freeze_object(await read(), name='action approval capture')
        # Reuse the continuation boundary: no native messages, reasoning or secrets.
        _encode(frame)
        _reject_private_index(frame)
        return frame
    except (asyncio.CancelledError, ProcessCleanupIncomplete):
        raise
    except Exception:  # noqa: BLE001, S110 - private failures must not be logged or chained.
        pass
    raise ActionApprovalCaptureError(_ERROR)


def _reject_private_index(value: JsonValue) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            normalized = re.sub(r'[^a-z0-9]+', '_', key.casefold()).strip('_')
            if normalized in {'execution_token', 'worker_lease_token', 'project_verification_receipt'}:
                raise ValueError(_ERROR)
            _reject_private_index(item)
    elif isinstance(value, tuple):
        for item in value:
            _reject_private_index(item)


_CAPTURE: ContextVar[Capture | None] = ContextVar('action_approval_capture', default=None)


@contextmanager
def capture_action_approval(
    context: TaskContext, action: HarnessToolCallRequest, read: _Reader,
) -> Iterator[Capture]:
    """Bind one real action invoke; scope exit also revokes inherited task access."""
    from agent_hub.harness.types import HarnessToolCallRequest

    if (type(context) is not TaskContext or type(action) is not HarnessToolCallRequest
            or type(context.actor_id) is not UUID or action.run_id != context.run_id
            or not callable(read)):
        raise ActionApprovalCaptureError(_ERROR)
    capture = Capture((context.tenant_id, context.run_id, context.actor_id,
                       action.actor, action.idempotency_key), read)
    token = _CAPTURE.set(capture)
    try:
        yield capture
    finally:
        capture._revoke()
        _CAPTURE.reset(token)


def action_approval_capture_active() -> bool:
    """Presence includes revoked inherited bindings so failed hooks cannot publish."""
    return _CAPTURE.get() is not None


async def before_action_approval(request: CapabilityRequest, approval_id: str) -> None:
    capture = _CAPTURE.get()
    if capture is not None:
        await capture._before(request, approval_id)
