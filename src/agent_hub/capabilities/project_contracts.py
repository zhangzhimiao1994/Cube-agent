"""Immutable project-input preservation contracts; never installation authority."""

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import PureWindowsPath
from types import MappingProxyType
from typing import Literal, Self, cast
from uuid import UUID

_SHA = re.compile(r"[a-f0-9]{64}")
_SCOPE = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
_SEGMENT = re.compile(r"[A-Za-z0-9_.-]{1,255}")
_DELIVERY_MANIFEST = "DELIVERY_MANIFEST.json"
_CONTROL_NAMES = frozenset(name.lower() for name in (
    "package.json", "npm-shrinkwrap.json", "package-lock.json", "pnpm-lock.yaml", "yarn.lock",
    "bun.lock", "bun.lockb", "pyproject.toml", "requirements.txt", "requirements.lock",
    "uv.lock", "poetry.lock", "Pipfile", "Pipfile.lock", "setup.py", "setup.cfg", "pytest.ini",
    "tox.ini", "conftest.py", "AGENTS.md", _DELIVERY_MANIFEST,
))
_CONTRACT_FIELDS = frozenset({
    "schema_version", "tenant_id", "run_id", "project_id", "session_id", "input_source_sha256",
    "editable_paths", "protected_files", "protected_sha256",
})
_ERROR = "invalid project check contract"
# Preserve the old worst-case JSON path envelope (128 paths of 1024 ASCII bytes).
# Count is a soft convenience threshold; actual normalized metadata remains bounded.
EDITABLE_PATHS_SOFT_LIMIT = 128
EDITABLE_PATHS_MAX_BYTES = EDITABLE_PATHS_SOFT_LIMIT * (1024 + 3) + 1
EDITABLE_PATHS_MAX_COUNT = min(100_000, (EDITABLE_PATHS_MAX_BYTES - 1) // 4)


def _need(condition: object) -> None:
    if not condition:
        raise ValueError(_ERROR)


def _mapping(value: object) -> dict[str, object]:
    _need(isinstance(value, Mapping))
    mapping = cast(Mapping[object, object], value)
    _need(len(mapping) <= 100_000)
    result: dict[str, object] = {}
    for key, item in mapping.items():
        _need(type(key) is str and key not in result)
        result[cast(str, key)] = item
        _need(len(result) <= 100_000)
    _need(len(result) == len(mapping))
    return result


def _path(value: object) -> str:
    _need(type(value) is str)
    path = cast(str, value)
    _need(0 < len(path) <= 1024)
    for part in path.split("/"):
        _need(_SEGMENT.fullmatch(part) is not None and part not in {".", ".."}
              and not part.endswith(".") and not PureWindowsPath(part).is_reserved())
    return path


def _sha(value: object) -> str:
    if type(value) is not str or _SHA.fullmatch(value) is None:
        raise ValueError(_ERROR)
    return value


def _scope(value: object) -> str:
    if type(value) is not str or _SCOPE.fullmatch(value) is None:
        raise ValueError(_ERROR)
    return value


def _spellings(paths: Sequence[str]) -> None:
    spellings: dict[str, str] = {}
    for path in paths:
        parts = path.split("/")
        for end in range(1, len(parts) + 1):
            prefix = "/".join(parts[:end])
            lower = prefix.lower()
            _need(lower not in spellings or spellings[lower] == prefix)
            spellings[lower] = prefix


def _tree(paths: Sequence[str]) -> None:
    _spellings(paths)
    members = set(paths)
    _need(len(members) == len(paths))
    for path in paths:
        parts = path.split("/")
        _need(not any("/".join(parts[:end]) in members for end in range(1, len(parts))))


def _control(path: str) -> bool:
    return any(part.lower() in _CONTROL_NAMES for part in path.split("/"))


def _editables(value: object, *, allow_list: bool = False) -> tuple[str, ...]:
    _need(type(value) is tuple or (allow_list and type(value) is list))
    paths = cast(tuple[object, ...] | list[object], value)
    _need(1 <= len(paths) <= EDITABLE_PATHS_MAX_COUNT)
    raw_bytes = 1
    for path in paths:
        _need(type(path) is str)
        raw_bytes += len(cast(str, path)) + 3
        _need(raw_bytes <= EDITABLE_PATHS_MAX_BYTES)
    result = tuple(_path(path) for path in paths)
    _need(not any(_control(path) for path in result))
    _tree(result)
    return tuple(sorted(result))


def _files(value: object) -> dict[str, str]:
    files = {_path(path): _sha(digest) for path, digest in _mapping(value).items()}
    _tree(tuple(files))
    return dict(sorted(files.items()))


def _editable(path: str, scopes: tuple[str, ...] | frozenset[str]) -> bool:
    if _control(path):
        return False
    if isinstance(scopes, frozenset):
        parts = path.split('/')
        return any('/'.join(parts[:end]) in scopes for end in range(1, len(parts) + 1))
    return any(path == scope or path.startswith(scope + "/") for scope in scopes)


def _layout(scopes: tuple[str, ...], files: Mapping[str, str]) -> None:
    _spellings((*scopes, *files))
    for scope in scopes:
        parts = scope.split("/")
        _need(not any("/".join(parts[:end]) in files for end in range(1, len(parts))))


def _digest(payload: Mapping[str, object]) -> str:
    raw = json.dumps(dict(payload), sort_keys=True, separators=(",", ":"), ensure_ascii=True,
                     allow_nan=False).encode("ascii")
    return hashlib.sha256(raw).hexdigest()


def _binding(
    tenant_id: UUID, run_id: UUID, project_id: str, session_id: str, input_source_sha256: str,
    editable_paths: tuple[str, ...], protected_files: Mapping[str, str],
) -> dict[str, object]:
    return {
        "schema_version": 1, "tenant_id": str(tenant_id), "run_id": str(run_id),
        "project_id": project_id, "session_id": session_id,
        "input_source_sha256": input_source_sha256, "editable_paths": list(editable_paths),
        "protected_files": dict(protected_files),
    }


def _uuid(value: object) -> UUID:
    _need(type(value) is str)
    try:
        parsed = UUID(cast(str, value))
    except ValueError:
        raise ValueError(_ERROR) from None
    _need(str(parsed) == value)
    return parsed


@dataclass(frozen=True, slots=True)
class ManagedProjectInput:
    """User-API-selected edit scopes; never model-supplied execution permission."""

    editable_paths: tuple[str, ...]
    schema_version: Literal[1] = 1
    kind: Literal["managed_workspace"] = "managed_workspace"

    def __post_init__(self) -> None:
        _need(type(self.schema_version) is int and self.schema_version == 1)
        _need(type(self.kind) is str and self.kind == "managed_workspace")
        object.__setattr__(self, "editable_paths", _editables(self.editable_paths))

    def to_payload(self) -> dict[str, object]:
        return {"schema_version": 1, "kind": "managed_workspace",
                "editable_paths": list(self.editable_paths)}

    @classmethod
    def from_payload(cls, value: object) -> Self:
        payload = _mapping(value)
        _need(not payload.keys() - {"schema_version", "kind", "editable_paths"})
        version, kind = payload.get("schema_version", 1), payload.get("kind", "managed_workspace")
        _need(type(version) is int and version == 1)
        _need(type(kind) is str and kind == "managed_workspace")
        return cls(editable_paths=_editables(payload.get("editable_paths"), allow_list=True))


@dataclass(frozen=True, slots=True)
class ProjectCheckContract:
    """Bound original-file preservation data, not provenance or a passed check receipt."""

    tenant_id: UUID
    run_id: UUID
    project_id: str
    session_id: str
    input_source_sha256: str
    editable_paths: tuple[str, ...]
    protected_files: Mapping[str, str]
    protected_sha256: str
    schema_version: Literal[1] = 1
    _editable_scope_index: frozenset[str] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        _need(type(self.schema_version) is int and self.schema_version == 1)
        _need(type(self.tenant_id) is UUID and type(self.run_id) is UUID)
        _scope(self.project_id)
        _scope(self.session_id)
        _sha(self.input_source_sha256)
        scopes = _editables(self.editable_paths)
        protected = _files(self.protected_files)
        _layout(scopes, protected)
        index = frozenset(scopes)
        _need(all(path != _DELIVERY_MANIFEST and not _editable(path, index) for path in protected))
        binding = _binding(self.tenant_id, self.run_id, self.project_id, self.session_id,
                           self.input_source_sha256, scopes, protected)
        _need(_sha(self.protected_sha256) == _digest(binding))
        object.__setattr__(self, "editable_paths", scopes)
        object.__setattr__(self, "_editable_scope_index", index)
        object.__setattr__(self, "protected_files", MappingProxyType(protected))

    def is_editable(self, relative_path: object) -> bool:
        try:
            return _editable(_path(relative_path), self._editable_scope_index)
        except ValueError:
            return False

    def require_current(self, source_files: Mapping[str, str]) -> None:
        current = _files(source_files)
        _layout(self.editable_paths, current)
        _spellings((*current, *self.protected_files))
        _need(all(current.get(path) == digest for path, digest in self.protected_files.items()))
        _need(all(path in self.protected_files or path == _DELIVERY_MANIFEST
                  or _editable(path, self._editable_scope_index) for path in current))

    def to_payload(self) -> dict[str, object]:
        return {
            **_binding(self.tenant_id, self.run_id, self.project_id, self.session_id,
                       self.input_source_sha256, self.editable_paths, self.protected_files),
            "protected_sha256": self.protected_sha256,
        }

    @classmethod
    def from_payload(cls, value: object) -> Self:
        payload = _mapping(value)
        _need(payload.keys() == _CONTRACT_FIELDS)
        _need(type(payload["schema_version"]) is int and payload["schema_version"] == 1)
        return cls(
            tenant_id=_uuid(payload["tenant_id"]), run_id=_uuid(payload["run_id"]),
            project_id=_scope(payload["project_id"]), session_id=_scope(payload["session_id"]),
            input_source_sha256=_sha(payload["input_source_sha256"]),
            editable_paths=_editables(payload["editable_paths"], allow_list=True),
            protected_files=_files(payload["protected_files"]),
            protected_sha256=_sha(payload["protected_sha256"]),
        )


def build_project_check_contract(
    tenant_id: UUID, run_id: UUID, project_id: str, session_id: str, input_source_sha256: str,
    editable_paths: tuple[str, ...], source_files: Mapping[str, str],
) -> ProjectCheckContract:
    """Bind a trusted snapshot's per-file hashes, including its executable-mode tags."""
    scopes, files = _editables(editable_paths), _files(source_files)
    _layout(scopes, files)
    _need(_sha(input_source_sha256) == _digest(files))
    index = frozenset(scopes)
    protected = {path: digest for path, digest in files.items()
                 if path != _DELIVERY_MANIFEST and not _editable(path, index)}
    binding = _binding(tenant_id, run_id, project_id, session_id, input_source_sha256, scopes, protected)
    return ProjectCheckContract(
        tenant_id=tenant_id, run_id=run_id, project_id=project_id, session_id=session_id,
        input_source_sha256=input_source_sha256, editable_paths=scopes,
        protected_files=protected, protected_sha256=_digest(binding),
    )
