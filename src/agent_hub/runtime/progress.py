"""Semantic budget credits, independent of causal artifact identities."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping

from agent_hub.runtime.contracts import Artifact


def _plain(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, tuple | list):
        return [_plain(item) for item in value]
    return value


def _files(value: object) -> list[dict[str, object]]:
    if not isinstance(value, tuple | list):
        return []
    rows = [
        {k: item[k] for k in ("path", "sha256", "size_bytes", "content_sha256", "content_bytes") if k in item}
        for item in value if isinstance(item, Mapping) and isinstance(item.get("path"), str)
    ]
    return sorted(rows, key=lambda row: str(row["path"]))


def workspace_progress_key(name: str, arguments_sha256: str, result: object) -> str | None:
    """Hash observed information, not execution IDs or tool-supplied credit flags."""
    del arguments_sha256  # Builtins return canonical paths; ignored arguments cannot earn credit.
    if not isinstance(result, Mapping):
        return None
    if name in {"workspace.read", "workspace_read"} or (
        name == "read_context" and "path" in result
    ):
        if not isinstance(result.get("path"), str) or not isinstance(result.get("text"), str):
            return None
        name = "workspace.read"
        value: object = {k: result.get(k) for k in ("path", "text", "truncated")}
    elif name == "workspace.list":
        value = _files(result.get("workspace_files"))
    elif name == "workspace.write_text":
        rows = _files(result.get("workspace_files"))
        if not rows:
            rows = _files((result.get("file", result),))
        if not rows:
            return None
        value = rows
    elif name == "workspace.bundle":
        value = _files(result.get("workspace_files"))
        if not value:
            file = result.get("file")
            value = ({k: file[k] for k in ("filename", "sha256", "size_bytes") if k in file}
                     if isinstance(file, Mapping) else {})
    elif name == "read_context":
        value = {k: result.get(k) for k in ("query", "matches", "truncated")}
    else:
        return None
    raw = json.dumps([name, _plain(value)], sort_keys=True, ensure_ascii=False,
                     allow_nan=False, separators=(",", ":")).encode()
    return "workspace:" + hashlib.sha256(raw).hexdigest()


def artifact_progress_key(artifact: Artifact) -> str | None:
    if artifact.type in {"model_attempt", "model_response"}:
        return None
    if artifact.type == "tool_result":
        if not isinstance(artifact.content.get("result"), Mapping):
            return None
        name = artifact.content.get("tool_name")
        if isinstance(name, str) and (name.startswith("workspace.") or name in {"workspace_read", "read_context"}):
            return workspace_progress_key(name, str(artifact.content.get("arguments_sha256", "")),
                                          artifact.content.get("result"))
    return "artifact:" + str(artifact.id)


def count_artifact_progress(artifacts: Iterable[Artifact]) -> int:
    return len({key for artifact in artifacts if (key := artifact_progress_key(artifact)) is not None})
