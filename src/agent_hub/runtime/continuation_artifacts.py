"""Bounded JSON storage for caller-reviewed, persistable continuation data only.

This is neither a privacy channel nor an authorization credential. Callers MUST
review data before saving: never pass native_assistant / NativeAssistantMessage
payloads, private native reasoning, raw_prompt, or credentials, including strings
or encodings hiding those values. Key checks are defense in depth, not approval
to persist opaque data. Decoding grants no authority and executes nothing.

Use the existing runtime ArtifactRepository: PostgreSQL stores its rows in
agent_hub_runtime_artifacts, separate from the public agent_hub_run_artifacts
queried by RunRepository.artifacts. This codec emits no ARTIFACT_CREATED events
and never publishes its segments. That isolation does not relax the native
message prohibition in models/native_messages.py.

Canonical JSON is sorted, compact, finite-number, unescaped-Unicode UTF-8.
At most 64 MiB / 1024 segments of 64 KiB are supported, with nesting bounded at
128. Base64 and envelopes consume repository quota too; the existing repository
limits can reject values below the codec ceiling and are never increased here.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import math
import re
from collections.abc import Mapping
from types import MappingProxyType
from typing import cast
from uuid import UUID, uuid4

from agent_hub.runtime.artifacts import (
    ArtifactReference,
    ArtifactRepository,
    ArtifactRepositoryError,
)
from agent_hub.runtime.contracts import Artifact, JsonValue, _contains_sensitive_key

_SEGMENT_BYTES = 64 * 1024
_MAX_BYTES = 64 * 1024 * 1024
_MAX_SEGMENTS = 1024
_MAX_DEPTH = 128
_SHA256 = re.compile(r"[0-9a-f]{64}")
_TYPE = "continuation_json_segment"
_PRODUCER = "continuation_codec"
_CHECKPOINT_FIELDS = frozenset({"version", "content_sha256", "total_bytes", "segments"})
_BODY_FIELDS = frozenset({
    "tenant_id", "run_id", "content_sha256", "total_bytes",
    "segment_index", "segment_count", "data",
})
_FORBIDDEN_KEYS = frozenset({
    "native_assistant", "native_assistant_message", "reasoning_content", "credentials",
})


def _scope_id(value: object) -> UUID:
    if type(value) is UUID:
        return value
    if type(value) is not str or len(value) != 36:
        raise ValueError
    parsed = UUID(value)
    if str(parsed) != value:
        raise ValueError
    return parsed


def _digest(value: object) -> str:
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        raise ValueError
    return value


def _json_data(value: object) -> object:
    nodes = 0
    ancestors: set[int] = set()

    def visit(item: object, depth: int) -> object:
        nonlocal nodes
        nodes += 1
        if depth > _MAX_DEPTH or nodes > _MAX_BYTES:
            raise ValueError
        if item is None or type(item) in (bool, int):
            return item
        if type(item) is float:
            if not math.isfinite(item):
                raise ValueError
            return item
        if type(item) is str:
            if len(item) > _MAX_BYTES or len(item.encode("utf-8")) > _MAX_BYTES:
                raise ValueError
            return item
        if not isinstance(item, Mapping | list | tuple) or id(item) in ancestors:
            raise ValueError
        ancestors.add(id(item))
        try:
            if isinstance(item, Mapping):
                result: dict[str, object] = {}
                for key, nested in item.items():
                    if type(key) is not str:
                        raise ValueError
                    visit(key, depth + 1)
                    normalized = re.sub(r"[^a-z0-9]+", "_", key.casefold()).strip("_")
                    if normalized in _FORBIDDEN_KEYS or _contains_sensitive_key({key: None}):
                        raise ValueError
                    result[key] = visit(nested, depth + 1)
                return result
            return [visit(nested, depth + 1) for nested in item]
        finally:
            ancestors.remove(id(item))

    return visit(value, 0)


def _encode(value: object) -> bytes:
    normalized = _json_data(value)
    encoder = json.JSONEncoder(
        ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"),
    )
    raw = bytearray()
    for text in encoder.iterencode(normalized):
        encoded = text.encode("utf-8")
        if len(raw) + len(encoded) > _MAX_BYTES:
            raise ValueError
        raw.extend(encoded)
    return bytes(raw)


def _freeze(value: object) -> JsonValue:
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze(nested) for key, nested in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(nested) for nested in value)
    return cast(JsonValue, value)


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _reject_constant(value: str) -> object:
    raise ValueError


async def _abort_writes(
    repository: ArtifactRepository,
    tenant_id: UUID,
    run_id: UUID,
    writes: list[tuple[ArtifactReference, UUID]],
) -> tuple[bool, bool]:
    async def cleanup() -> bool:
        succeeded = True
        for reference, write_id in reversed(writes):
            try:
                # False can mean another owner/permanent publication retained it.
                await repository.abort_write(tenant_id, run_id, reference, write_id=write_id)
            except (Exception, asyncio.CancelledError):  # noqa: BLE001 - attempt every owned abort
                succeeded = False
        return succeeded

    task = asyncio.create_task(cleanup())
    cancelled = False
    # Keep a strong task reference and await completion even under repeated cancel().
    while True:
        try:
            return await asyncio.shield(task), cancelled
        except asyncio.CancelledError:
            cancelled = True


async def save_continuation(
    repository: ArtifactRepository,
    *,
    tenant_id: UUID,
    run_id: UUID,
    value: JsonValue,
) -> dict[str, JsonValue]:
    """Save reviewed persistable data, never native/private reasoning or credentials.

    The returned checkpoint contains references only. Partial writes are aborted
    using this call's write IDs, including writes whose reservation outcome is
    uncertain. Repository limits and cancellation remain effective.
    """
    writes: list[tuple[ArtifactReference, UUID]] = []
    cancelled = False
    try:
        tenant_id, run_id = _scope_id(tenant_id), _scope_id(run_id)
        raw = _encode(value)
        total_bytes = len(raw)
        segment_count = (total_bytes + _SEGMENT_BYTES - 1) // _SEGMENT_BYTES
        if not 1 <= segment_count <= _MAX_SEGMENTS:
            raise ValueError
        content_sha256 = hashlib.sha256(raw).hexdigest()
        references: list[JsonValue] = []
        for index in range(segment_count):
            data = raw[index * _SEGMENT_BYTES:(index + 1) * _SEGMENT_BYTES]
            artifact = Artifact(
                id=uuid4(), type=_TYPE, producer=_PRODUCER,
                content={
                    "tenant_id": str(tenant_id), "run_id": str(run_id),
                    "content_sha256": content_sha256, "total_bytes": total_bytes,
                    "segment_index": index, "segment_count": segment_count,
                    "data": base64.b64encode(data).decode("ascii"),
                },
            )
            reference = ArtifactReference(artifact.id, artifact.content_sha256)
            write_id = uuid4()
            # Record before awaiting: reserve_write can commit then fail/cancel.
            writes.append((reference, write_id))
            await repository.reserve_write(tenant_id, run_id, reference, write_id=write_id)
            await repository.put(tenant_id, run_id, artifact, write_id=write_id)
            references.append(cast(JsonValue, reference.to_payload()))
        return {
            "version": 1, "content_sha256": content_sha256,
            "total_bytes": total_bytes, "segments": cast(JsonValue, references),
        }
    except asyncio.CancelledError:
        cancelled = True
    except Exception:  # noqa: BLE001, S110 - discard private source errors without logging
        pass

    cleanup_succeeded = True
    if writes:
        cleanup_succeeded, cleanup_cancelled = await _abort_writes(
            repository, tenant_id, run_id, writes,
        )
        cancelled |= cleanup_cancelled
    # Raise outside exception handlers: source errors must not survive as context.
    if cancelled:
        raise asyncio.CancelledError from None
    if not cleanup_succeeded:
        raise ArtifactRepositoryError("continuation write cleanup failed") from None
    raise ArtifactRepositoryError("continuation save failed") from None


def _checkpoint(
    reference: Mapping[str, JsonValue],
) -> tuple[str, int, tuple[ArtifactReference, ...]]:
    if not isinstance(reference, Mapping) or set(reference) != _CHECKPOINT_FIELDS:
        raise ValueError
    if type(reference["version"]) is not int or reference["version"] != 1:
        raise ValueError
    content_sha256 = _digest(reference["content_sha256"])
    total_bytes = reference["total_bytes"]
    segments = reference["segments"]
    if type(total_bytes) is not int or not 1 <= total_bytes <= _MAX_BYTES:
        raise ValueError
    count = (total_bytes + _SEGMENT_BYTES - 1) // _SEGMENT_BYTES
    if (
        not isinstance(segments, list | tuple)
        or not 1 <= len(segments) <= _MAX_SEGMENTS
        or len(segments) != count
    ):
        raise ValueError
    references: list[ArtifactReference] = []
    seen: set[UUID] = set()
    for item in segments:
        if not isinstance(item, Mapping) or set(item) != {"id", "sha256"}:
            raise ValueError
        artifact_id = item["id"]
        if type(artifact_id) is not str or len(artifact_id) != 36:
            raise ValueError
        parsed_id = UUID(artifact_id)
        if str(parsed_id) != artifact_id or parsed_id in seen:
            raise ValueError
        seen.add(parsed_id)
        references.append(ArtifactReference(parsed_id, _digest(item["sha256"])))
    return content_sha256, total_bytes, tuple(references)


def _segment(
    artifact: Artifact,
    reference: ArtifactReference,
    *,
    tenant_id: UUID,
    run_id: UUID,
    content_sha256: str,
    total_bytes: int,
    index: int,
    count: int,
) -> bytes:
    if (
        type(artifact) is not Artifact or type(artifact.id) is not UUID
        or artifact.id != reference.id or artifact.content_sha256 != reference.sha256
        or type(artifact.version) is not int or artifact.version != 1
        or artifact.type != _TYPE or artifact.producer != _PRODUCER
        or artifact.source_ids != () or artifact.provenance is not None
    ):
        raise ValueError
    body = artifact.content
    if not isinstance(body, Mapping) or set(body) != _BODY_FIELDS:
        raise ValueError
    if (
        body["tenant_id"] != str(tenant_id) or body["run_id"] != str(run_id)
        or body["content_sha256"] != content_sha256
    ):
        raise ValueError
    for field, expected in (
        ("total_bytes", total_bytes), ("segment_index", index), ("segment_count", count),
    ):
        if type(body[field]) is not int or body[field] != expected:
            raise ValueError
    expected_bytes = min(_SEGMENT_BYTES, total_bytes - index * _SEGMENT_BYTES)
    data = body["data"]
    if type(data) is not str or len(data) != 4 * ((expected_bytes + 2) // 3):
        raise ValueError
    decoded = base64.b64decode(data, validate=True)
    if len(decoded) != expected_bytes or base64.b64encode(decoded).decode("ascii") != data:
        raise ValueError
    if artifact.recompute_content_sha256() != reference.sha256:
        raise ValueError
    return decoded


async def load_continuation(
    repository: ArtifactRepository,
    *,
    tenant_id: UUID,
    run_id: UUID,
    reference: Mapping[str, JsonValue],
) -> JsonValue:
    """Validate and load data only; references/hashes confer no execution authority."""
    try:
        tenant_id, run_id = _scope_id(tenant_id), _scope_id(run_id)
        content_sha256, total_bytes, references = _checkpoint(reference)
        artifacts = await repository.get_many(tenant_id, run_id, references)
        if type(artifacts) is not tuple or len(artifacts) != len(references):
            raise ValueError
        pieces = [
            _segment(
                artifact, ref, tenant_id=tenant_id, run_id=run_id,
                content_sha256=content_sha256, total_bytes=total_bytes,
                index=index, count=len(references),
            )
            for index, (artifact, ref) in enumerate(zip(artifacts, references, strict=True))
        ]
        raw = b"".join(pieces)
        if len(raw) != total_bytes or hashlib.sha256(raw).hexdigest() != content_sha256:
            raise ValueError
        value = json.loads(
            raw.decode("utf-8"), object_pairs_hook=_unique_object, parse_constant=_reject_constant,
        )
        if _encode(value) != raw:
            raise ValueError
        return _freeze(value)
    except Exception:  # noqa: BLE001, S110 - never retain private parse/repository errors
        pass
    raise ArtifactRepositoryError("continuation is invalid or unavailable") from None


__all__ = ["load_continuation", "save_continuation"]
