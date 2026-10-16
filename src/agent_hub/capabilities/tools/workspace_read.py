from __future__ import annotations

import os
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agent_hub.runtime.contracts import JsonValue


def read_tool_description(name: str) -> str | None:
    if name in {"workspace.read", "workspace_read"}:
        return (
            "Read one authorized workspace file by required path. Use the relative file path "
            "returned by workspace.list or workspace.write_text, without prepending workspace/current. "
            "This tool does not accept query or search conversations."
        )
    if name == "read_context":
        return (
            "Read available runtime context using optional query (or text). This does not search "
            "workspace file contents. An explicit path instead reads that authorized workspace file."
        )
    return None


def read_tool_input_schema(name: str) -> Mapping[str, JsonValue] | None:
    path: Mapping[str, JsonValue] = {
        "type": "string", "minLength": 1,
        "description": "Relative file path within the authorized run/project scope, not a search query.",
    }
    if name in {"workspace.read", "workspace_read"}:
        return {"type": "object", "additionalProperties": False, "required": ("path",),
                "properties": {"path": path}}
    if name == "read_context":
        query: Mapping[str, JsonValue] = {"type": "string", "minLength": 1, "pattern": r"\S"}
        return {"type": "object", "additionalProperties": False,
                "properties": {"query": {}, "text": {}, "path": path},
                # Match the executor: path wins; otherwise non-null query wins over text.
                "anyOf": (
                    {"required": ("path",)},
                    {"not": {"required": ("path",)}, "required": ("query",),
                     "properties": {"query": query}},
                    {"not": {"required": ("path",)}, "properties": {
                        "query": {"type": "null"},
                        "text": {"type": ("string", "null"), "minLength": 1, "pattern": r"\S"},
                    }},
                )}
    return None


class UnsafePath(ValueError):
    """Workspace path escapes the configured root."""


@dataclass(frozen=True, slots=True)
class WorkspaceReadResult:
    relative_path: str
    text: str
    truncated: bool


class WorkspaceReader:
    def __init__(self, root: Path, *, max_bytes: int = 65_536) -> None:
        self._root = root.resolve(strict=True)
        self._max_bytes = max_bytes

    def read(self, path: str) -> WorkspaceReadResult:
        if _is_unsafe_input_path(path):
            raise UnsafePath("workspace path is unsafe")
        candidate = (self._root / path.replace("\\", "/")).resolve(strict=True)
        try:
            candidate.relative_to(self._root)
        except ValueError:
            raise UnsafePath("workspace path is unsafe") from None
        if not candidate.is_file():
            raise UnsafePath("workspace path is unsafe")
        expected_stat = candidate.lstat()
        if stat.S_ISLNK(expected_stat.st_mode):
            raise UnsafePath("workspace path is unsafe")
        with candidate.open("rb") as handle:
            opened_stat = os.fstat(handle.fileno())
            if _stat_identity(opened_stat) != _stat_identity(expected_stat):
                raise UnsafePath("workspace path is unsafe")
            data = handle.read(self._max_bytes + 1)
        truncated = len(data) > self._max_bytes
        bounded = data[: self._max_bytes]
        return WorkspaceReadResult(
            relative_path=candidate.relative_to(self._root).as_posix(),
            text=bounded.decode("utf-8", errors="replace"),
            truncated=truncated,
        )


def _is_unsafe_input_path(path: str) -> bool:
    if type(path) is not str or not path or "\x00" in path:
        return True
    normalized = path.replace("\\", "/")
    if normalized.startswith("/"):
        return True
    if PureWindowsPath(path).is_absolute():
        return True
    return any(part == ".." for part in normalized.split("/"))


def _stat_identity(value: os.stat_result) -> tuple[int, int, int]:
    return (value.st_dev, value.st_ino, value.st_size)
