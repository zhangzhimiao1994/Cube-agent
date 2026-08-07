from __future__ import annotations

from contextvars import ContextVar
from uuid import uuid4

_trace_id: ContextVar[str | None] = ContextVar("agent_hub_trace_id", default=None)


def current_trace_id() -> str:
    trace_id = _trace_id.get()
    if trace_id is None:
        trace_id = uuid4().hex
        _trace_id.set(trace_id)
    return trace_id


def set_trace_id(trace_id: str) -> None:
    _trace_id.set(trace_id)


__all__ = ["current_trace_id", "set_trace_id"]
