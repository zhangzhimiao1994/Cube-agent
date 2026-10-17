"""Causal completion proof for an already authorized maintenance tool ledger."""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import PurePosixPath
from uuid import UUID

from agent_hub.runtime.contracts import Artifact, JsonValue


def _file_metadata(value: object) -> tuple[str, str, int] | None:
    if not isinstance(value, Mapping):
        return None
    path = value.get("path")
    digest = value.get("content_sha256", value.get("sha256"))
    size = value.get("content_bytes", value.get("size_bytes"))
    if (
        type(path) is not str or not path or "\\" in path
        or PurePosixPath(path).is_absolute() or ".." in PurePosixPath(path).parts
        or str(PurePosixPath(path)) != path
        or type(digest) is not str or re.fullmatch("[a-f0-9]{64}", digest) is None
        or type(size) is not int or size < 0
    ):
        return None
    return path, digest, size


def maintenance_delivery_ready(
    *, step_id: str, actor: str, attempt: int, round_index: int,
    states: Mapping[str, Mapping[str, JsonValue]], artifacts: Mapping[str, Artifact],
) -> bool:
    """Inspect only the causal prefix of a previously validated runtime ledger."""
    if type(attempt) is not int or attempt < 0 or type(round_index) is not int or round_index < 0:
        return False
    writes: dict[str, tuple[tuple[int, int, int], tuple[str, str, int]]] = {}
    bundles: list[tuple[tuple[int, int, int], Mapping[str, JsonValue]]] = []
    positions: set[tuple[int, int, int]] = set()
    for key, row in states.items():
        name = row.get("name")
        if row.get("step_id") != step_id or name not in {"workspace.write_text", "workspace.bundle"}:
            continue
        coordinates = tuple(row.get(field) for field in ("attempt", "round", "tool_index"))
        if any(type(value) is not int or value < 0 for value in coordinates):
            return False
        prior_attempt, prior_round, index = coordinates
        assert isinstance(prior_attempt, int) and isinstance(prior_round, int) and isinstance(index, int)
        if prior_attempt > attempt or (prior_attempt == attempt and prior_round >= round_index):
            continue
        position = prior_attempt, prior_round, index
        if position in positions or row.get("status") != "succeeded":
            return False
        positions.add(position)
        artifact = artifacts.get(key)
        if (
            artifact is None or artifact.type != "tool_result" or artifact.producer != actor
            or artifact.content.get("agent_id") != actor or len(artifact.source_ids) != 1
            or artifact.content.get("tool_name") != name
            or artifact.content.get("arguments_sha256") != row.get("arguments_sha256")
            or str(artifact.id) != row.get("artifact_id")
            or artifact.content_sha256 != row.get("sha256")
            or artifact.content_sha256 != artifact.recompute_content_sha256()
        ):
            return False
        result = artifact.content.get("result")
        if not isinstance(result, Mapping):
            return False
        if name == "workspace.write_text":
            metadata = result.get("workspace_files")
            if isinstance(metadata, tuple | list):
                if len(metadata) != 1:
                    return False
                item = _file_metadata(metadata[0])
            else:
                item = _file_metadata(result)
            if item is None:
                return False
            previous = writes.get(item[0])
            if previous is None or previous[0] < position:
                writes[item[0]] = position, item
        else:
            bundles.append((position, result))
    if not writes:
        return False
    latest_write = max(position for position, _ in writes.values())
    expected = {path: item for path, (_, item) in writes.items()}
    for position, result in bundles:
        if position <= latest_write or result.get("presentation") != "final_attachment":
            continue
        artifact_id, file = result.get("artifact_id"), result.get("file")
        try:
            if type(artifact_id) is not str or str(UUID(artifact_id)) != artifact_id:
                continue
        except ValueError:
            continue
        if not isinstance(file, Mapping) or type(file.get("filename")) is not str:
            continue
        manifest = result.get("workspace_files")
        if not isinstance(manifest, tuple | list):
            continue
        actual: dict[str, tuple[str, str, int]] = {}
        for value in manifest:
            entry = _file_metadata(value)
            if entry is None or entry[0] in actual:
                break
            actual[entry[0]] = entry
        else:
            if all(actual.get(path) == item for path, item in expected.items()):
                return True
    return False
