"""Bounded proposal eligibility over an already validated causal tool ledger.

This is not authorization or progress credit. Eligible calls still traverse the
normal scope, schema, approval, idempotency and uncertain-outcome checks.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from pathlib import PurePosixPath

from agent_hub.models.types import ToolCall
from agent_hub.runtime.contracts import Artifact, JsonValue
from agent_hub.runtime.crew.workspace_maintenance_delivery import maintenance_delivery_ready


def _path(value: object) -> str | None:
    if (type(value) is not str or not value or value == "." or "\\" in value or ":" in value
            or any(ord(ch) < 32 or ord(ch) == 127 for ch in value)
            or PurePosixPath(value).is_absolute() or ".." in PurePosixPath(value).parts
            or str(PurePosixPath(value)) != value):
        return None
    return value


def _metadata(value: object) -> tuple[str, str, int] | None:
    if not isinstance(value, Mapping):
        return None
    path = _path(value.get("path"))
    digest = value.get("content_sha256", value.get("sha256"))
    size = value.get("content_bytes", value.get("size_bytes"))
    if (path is None or type(digest) is not str or re.fullmatch("[a-f0-9]{64}", digest) is None
            or type(size) is not int or size < 0
            or any(key in value and value[key] != digest for key in ("sha256", "content_sha256"))
            or any(key in value and (type(value[key]) is not int or value[key] != size)
                   for key in ("size_bytes", "content_bytes"))):
        return None
    return path, digest, size


def _verified_result(
    row: Mapping[str, JsonValue], artifact: Artifact | None, *, name: str, actor: str,
) -> Mapping[str, JsonValue] | None:
    if (row.get("status") != "succeeded" or artifact is None or artifact.type != "tool_result"
            or artifact.producer != actor or artifact.content.get("agent_id") != actor
            or artifact.content.get("tool_name") != name or row.get("name") != name
            or str(artifact.id) != row.get("artifact_id")
            or artifact.content_sha256 != row.get("sha256")
            or artifact.content_sha256 != artifact.recompute_content_sha256()
            or artifact.content.get("arguments_sha256") != row.get("arguments_sha256")
            or not isinstance(row.get("trigger_model_artifact_id"), str)
            or artifact.source_ids != (row["trigger_model_artifact_id"],)):
        return None
    result = artifact.content.get("result")
    return result if isinstance(result, Mapping) else None


def productive_workspace_proposal(
    *, calls: Sequence[ToolCall], planned_tools: Sequence[str], step_id: str, actor: str,
    attempt: int, round_index: int, states: Mapping[str, Mapping[str, JsonValue]],
    artifacts: Mapping[str, Artifact],
) -> bool:
    """Prove changed writes or missing fresh delivery, never speculative reads.

    Current/future slots must not participate: a resumed waiting/succeeded write
    has already changed the live workspace but must retain its original grant.
    """
    if (not calls or type(attempt) is not int or attempt < 0
            or type(round_index) is not int or round_index < 0
            or any(call.name not in planned_tools or call.name not in {
                "workspace.write_text", "workspace.bundle",
            } for call in calls)):
        return False
    prefix: list[tuple[tuple[int, int, int], str, Mapping[str, JsonValue]]] = []
    seen: set[tuple[int, int, int]] = set()
    for key, row in states.items():
        name = row.get("name")
        if row.get("step_id") != step_id or name not in {
            "workspace.read", "workspace_read", "workspace.list", "workspace.write_text", "workspace.bundle",
        }:
            continue
        coordinates = tuple(row.get(field) for field in ("attempt", "round", "tool_index"))
        if any(type(value) is not int or value < 0 for value in coordinates):
            return False
        prior_attempt, prior_round, index = coordinates
        assert isinstance(prior_attempt, int) and isinstance(prior_round, int) and isinstance(index, int)
        if (prior_attempt, prior_round) >= (attempt, round_index):
            continue
        position = prior_attempt, prior_round, index
        if position in seen or row.get("status") != "succeeded":
            return False
        seen.add(position)
        assert isinstance(name, str)
        result = _verified_result(row, artifacts.get(key), name=name, actor=actor)
        if result is None:
            return False
        prefix.append((position, name, result))
    known: dict[str, tuple[str, int]] = {}
    present_paths: set[str] = set()
    complete_listing = False
    wrote = False
    for _, name, result in sorted(prefix, key=lambda item: item[0]):
        if name in {"workspace.read", "workspace_read"}:
            path, text = _path(result.get("path")), result.get("text")
            if path is not None:
                if path not in present_paths:
                    complete_listing = False
                present_paths.add(path)
                if type(text) is str and result.get("truncated") is False:
                    raw = text.encode("utf-8")
                    known[path] = hashlib.sha256(raw).hexdigest(), len(raw)
                else:
                    known.pop(path, None)
        else:
            entries = result.get("workspace_files")
            if name == "workspace.list":
                # The builtin lists the entire scoped store, without pagination.
                # A missing/partial result cannot certify absence of a path.
                complete_listing = (
                    isinstance(entries, tuple | list)
                    and result.get("truncated", False) is False
                    and result.get("has_more", False) is False
                    and result.get("next_cursor") is None
                    and result.get("complete", True) is True
                )
            if name == "workspace.write_text":
                wrote = True
                if not isinstance(entries, tuple | list):
                    entries = (result.get("file", result),)
                if len(entries) != 1:
                    return False
            if isinstance(entries, tuple | list):
                metadata = [_metadata(value) for value in entries]
                if any(item is None for item in metadata):
                    return False
                paths: set[str] = set()
                for item in metadata:
                    assert item is not None
                    path, digest, size = item
                    if path in paths:
                        return False
                    paths.add(path)
                    known[path] = digest, size
                    present_paths.add(path)
                if name == "workspace.bundle" or (name == "workspace.list" and complete_listing):
                    known = {path: known[path] for path in paths}
                    present_paths = paths
    bundled = maintenance_delivery_ready(
        step_id=step_id, actor=actor, attempt=attempt, round_index=round_index,
        states=states, artifacts=artifacts,
    )
    written_paths: set[str] = set()
    for index, call in enumerate(calls):
        args = call.arguments
        if call.name == "workspace.write_text":
            path, content = _path(args.get("path")), args.get("content")
            if (set(args) != {"path", "content"} or path is None or type(content) is not str
                    or not content.strip() or path in written_paths
                    or (path not in known and (not complete_listing or path in present_paths))):
                return False
            raw = content.encode("utf-8")
            candidate = hashlib.sha256(raw).hexdigest(), len(raw)
            if candidate == known.get(path):
                return False
            written_paths.add(path)
            known[path] = candidate
            present_paths.add(path)
            wrote, bundled = True, False
        else:
            if (index != len(calls) - 1 or not wrote or bundled
                    or set(args) - {"presentation", "title", "filename"}
                    or args.get("presentation", "final_attachment") != "final_attachment"):
                return False
            for field in ("title", "filename"):
                if field in args:
                    value = args[field]
                    if (type(value) is not str or not value.strip() or len(value) > 200
                            or any(ord(ch) < 32 or ord(ch) == 127 for ch in value)):
                        return False
            filename = args.get("filename")
            if filename is not None and (type(filename) is not str or _path(filename) is None or "/" in filename):
                return False
    return True


def productive_workspace_success(
    *, calls: Sequence[ToolCall], planned_tools: Sequence[str], step_id: str, actor: str,
    attempt: int, round_index: int, states: Mapping[str, Mapping[str, JsonValue]],
    artifacts: Mapping[str, Artifact],
) -> bool:
    """Release a spent opportunity only for exact successful proposal receipts.

    This is replay-derived phase state, not a new progress/deadline/token credit.
    The proposal is re-proven from the prefix excluding all its own cached slots.
    """
    if not productive_workspace_proposal(
        calls=calls, planned_tools=planned_tools, step_id=step_id, actor=actor,
        attempt=attempt, round_index=round_index, states=states, artifacts=artifacts,
    ):
        return False
    current: dict[int, tuple[Mapping[str, JsonValue], Artifact | None]] = {}
    for key, row in states.items():
        if (row.get("step_id") != step_id or row.get("attempt") != attempt
                or row.get("round") != round_index):
            continue
        index = row.get("tool_index")
        if (type(row.get("attempt")) is not int or type(row.get("round")) is not int
                or type(index) is not int or index < 0 or index in current):
            return False
        current[index] = row, artifacts.get(key)
    if set(current) != set(range(len(calls))):
        return False
    triggers: set[str] = set()
    for index, call in enumerate(calls):
        row, artifact = current[index]
        result = _verified_result(row, artifact, name=call.name, actor=actor)
        if result is None:
            return False
        # Eligibility restricts arguments to flat builtin string fields.
        arguments_sha256 = hashlib.sha256(json.dumps(
            dict(call.arguments), sort_keys=True, ensure_ascii=False, allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")).hexdigest()
        if row.get("arguments_sha256") != arguments_sha256:
            return False
        trigger = row.get("trigger_model_artifact_id")
        assert isinstance(trigger, str)
        triggers.add(trigger)
        if call.name == "workspace.write_text":
            entries = result.get("workspace_files")
            if not isinstance(entries, tuple | list):
                entries = (result.get("file", result),)
            if len(entries) != 1:
                return False
            content = call.arguments["content"]
            assert isinstance(content, str)
            raw = content.encode("utf-8")
            if _metadata(entries[0]) != (call.arguments["path"], hashlib.sha256(raw).hexdigest(), len(raw)):
                return False
        elif not maintenance_delivery_ready(
            step_id=step_id, actor=actor, attempt=attempt, round_index=round_index + 1,
            states=states, artifacts=artifacts,
        ):
            return False
    return len(triggers) == 1
