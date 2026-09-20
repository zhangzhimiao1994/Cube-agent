"""Strict public metadata projection for runtime instruction evidence."""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import datetime
from uuid import UUID

CONTEXT_EVENT_KINDS = frozenset({"context.loaded", "context.injected"})
_HASH = re.compile(r"[a-f0-9]{64}")
_ID = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
_MODEL = re.compile(r"[a-z0-9][a-z0-9_-]{0,127}")
_SUMMARIES = {
    "context.loaded": "project guidance loading recorded",
    "context.injected": "project guidance submitted to model gateway",
}


def _uuid(value: object) -> str | None:
    if type(value) is not str:
        return None
    try:
        return str(UUID(value))
    except ValueError:
        return None


def _source(value: object, *, injected: bool) -> dict[str, object] | None:
    if not isinstance(value, Mapping) or value.get("path") not in ("AGENTS.md", "SKILL.md"):
        return None
    result: dict[str, object] = {"path": value["path"]}
    hash_keys = ("injected_sha256",) if injected else (
        "read_sha256", "file_sha256", "content_sha256",
    )
    count_keys = ("injected_bytes",) if injected else ("read_bytes", "content_bytes")
    for key in hash_keys:
        item = value.get(key)
        if item is None or (type(item) is str and _HASH.fullmatch(item)):
            result[key] = item
    for key in count_keys:
        item = value.get(key)
        limit = 8193 if key == "read_bytes" else 8192
        if type(item) is int and 0 <= item <= limit:
            result[key] = item
    if type(value.get("truncated")) is bool:
        result["truncated"] = value["truncated"]
    if not injected:
        if value.get("kind") == "project_guidance":
            result["kind"] = "project_guidance"
        if value.get("status") in ("loaded", "unavailable", "invalid_utf8"):
            result["status"] = value["status"]
        for key in ("project_id", "session_id"):
            item = value.get(key)
            if item is None or (type(item) is str and _ID.fullmatch(item)):
                result[key] = item
    return result


def context_event_projection(event: Mapping[str, object]) -> dict[str, object]:
    kind = event.get("kind")
    if type(kind) is not str or kind not in CONTEXT_EVENT_KINDS:
        raise ValueError("not a context evidence event")
    injected = kind == "context.injected"
    result: dict[str, object] = {
        "kind": kind, "message": kind, "summary": _SUMMARIES[kind],
        "actor": "direct" if injected else "context_loader",
    }
    sequence = event.get("sequence")
    result["sequence"] = sequence if type(sequence) is int and 0 < sequence < 2**63 else 1
    run_id = _uuid(event.get("run_id"))
    if run_id is not None:
        result["run_id"] = run_id
    created_at = event.get("created_at")
    if isinstance(created_at, datetime):
        result["created_at"] = created_at
    elif type(created_at) is str:
        try:
            result["created_at"] = datetime.fromisoformat(created_at)
        except ValueError:
            pass
    payload = event.get("payload")
    metadata: dict[str, object] = {}
    result["payload"] = metadata
    if not isinstance(payload, Mapping):
        return result
    if type(payload.get("schema_version")) is int and payload["schema_version"] == 1:
        metadata["schema_version"] = 1
    for key in ("load_id", "tenant_id", "run_id"):
        identity = _uuid(payload.get(key))
        if identity is not None:
            metadata[key] = identity
    if injected:
        if payload.get("boundary") == "model_gateway":
            metadata["boundary"] = "model_gateway"
        model = payload.get("logical_model")
        if type(model) is str and _MODEL.fullmatch(model):
            metadata["logical_model"] = model
        digest = payload.get("request_sha256")
        if type(digest) is str and _HASH.fullmatch(digest):
            metadata["request_sha256"] = digest
    elif payload.get("source") == "session_root":
        metadata["source"] = "session_root"
    sources = payload.get("sources")
    if isinstance(sources, list | tuple):
        metadata["sources"] = [
            projected for item in sources[:2]
            if (projected := _source(item, injected=injected)) is not None
        ]
    return result
