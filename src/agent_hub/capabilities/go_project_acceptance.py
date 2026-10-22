"""Versioned Go acceptance data, without IO, execution or approval authority.

Named tests are declarations only. Root must match them against its authenticated
AST inventory and reconstruct this manifest from the sealed original bytes.
Constructors validate shape; callers allocate and enforce resource limits.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from typing import Literal, Self, cast

from agent_hub.capabilities.go_project_business import GoBusinessLimits, GoBusinessSuite
from agent_hub.capabilities.project_contracts import _control, _path, _spellings, _tree
from agent_hub.capabilities.project_stack_registry import is_stack_control_path
from agent_hub.skills.sandbox.go_project_inventory import _path as _package_path
from agent_hub.skills.sandbox.go_project_inventory import _test_name

_ERROR = "go_acceptance_invalid"
_MANIFEST = "ACCEPTANCE.json"
_JSON = json.JSONEncoder(sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def _need(value: object) -> None:
    if not value:
        raise ValueError(_ERROR)


def _object(value: object, keys: set[str]) -> dict[str, object]:
    _need(type(value) is dict)
    item = cast(dict[str, object], value)
    _need(set(item) == keys)
    return item


def _array(value: object) -> tuple[object, ...]:
    _need(type(value) is list and bool(value))
    return tuple(cast(list[object], value))


def _unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        _need(key not in result)
        result[key] = value
    return result


def _nonfinite(value: str) -> object:
    raise ValueError(_ERROR)


def _require_json_budget(value: object, maximum: int) -> None:
    remaining = maximum
    try:
        for chunk in _JSON.iterencode(value):
            remaining -= len(chunk)
            _need(remaining >= 0)
    except (ValueError, TypeError, OverflowError, RecursionError):
        raise ValueError(_ERROR) from None


def _protected(path: str) -> bool:
    return (_control(path) or is_stack_control_path(path)
            or any(part.casefold() == _MANIFEST.casefold() for part in path.split("/")))


def _under(path: str, scope: str) -> bool:
    return path == scope or path.startswith(scope + "/")


def _test_id(value: object) -> str:
    _need(type(value) is str)
    text = cast(str, value)
    package, separator, name = text.partition("::")
    _need(separator == "::")
    _package_path(package)
    _test_name(name)
    return text


@dataclass(frozen=True, slots=True)
class GoAcceptanceCriterion:
    id: str
    description: str
    tests: tuple[str, ...]

    def __post_init__(self) -> None:
        _need(type(self.id) is str
              and re.fullmatch(r"AC-(?:[0-9]{3}|[1-9][0-9]{3,})", self.id) is not None
              and self.id != "AC-000")
        _need(type(self.description) is str and bool(self.description)
              and self.description.strip() == self.description
              and unicodedata.normalize("NFC", self.description) == self.description
              and not any(unicodedata.category(char).startswith("C") for char in self.description))
        _need(type(self.tests) is tuple and bool(self.tests))
        names = tuple(_test_id(name) for name in self.tests)
        _need(len(set(names)) == len(names))
        object.__setattr__(self, "tests", tuple(sorted(names)))

    def to_payload(self) -> dict[str, object]:
        return {"id": self.id, "description": self.description, "tests": list(self.tests)}

    @classmethod
    def from_payload(cls, value: object) -> Self:
        item = _object(value, {"id", "description", "tests"})
        return cls(cast(str, item["id"]), cast(str, item["description"]),
                   cast(tuple[str, ...], _array(item["tests"])))


@dataclass(frozen=True, slots=True)
class GoAcceptanceManifest:
    editable_paths: tuple[str, ...]
    test_files: tuple[str, ...]
    criteria: tuple[GoAcceptanceCriterion, ...]
    business: GoBusinessSuite
    schema_version: Literal[2] = 2
    stack: Literal["go"] = "go"

    def __post_init__(self) -> None:
        _need(type(self.schema_version) is int and self.schema_version == 2
              and type(self.stack) is str and self.stack == "go"
              and type(self.editable_paths) is tuple and bool(self.editable_paths)
              and type(self.test_files) is tuple and bool(self.test_files)
              and type(self.criteria) is tuple and bool(self.criteria)
              and type(self.business) is GoBusinessSuite)
        edits = tuple(_path(path) for path in self.editable_paths)
        tests = tuple(_path(path) for path in self.test_files)
        _tree(edits)
        _tree(tests)
        _spellings((_MANIFEST, *edits, *tests))
        _need(not any(_protected(path) for path in edits))
        for path in tests:
            parent = path.rpartition("/")[0]
            _need(path.endswith("_test.go") and not _control(path)
                  and (not parent or not _protected(parent)))
        _need(not any(_under(test, edit) or _under(edit, test) for test in tests for edit in edits))
        _need(all(type(row) is GoAcceptanceCriterion for row in self.criteria))
        ids = tuple(row.id for row in self.criteria)
        names = tuple(name for row in self.criteria for name in row.tests)
        _need(len(set(ids)) == len(ids) and len(set(names)) == len(names))
        # Revalidate the suite without reordering cases or changing its digest.
        suite = GoBusinessSuite.from_payload(self.business.to_payload())
        _need(set(suite.criterion_ids) == set(ids))
        object.__setattr__(self, "editable_paths", tuple(sorted(edits)))
        object.__setattr__(self, "test_files", tuple(sorted(tests)))
        object.__setattr__(self, "criteria", tuple(sorted(self.criteria, key=lambda row: row.id)))
        object.__setattr__(self, "business", suite)

    def to_payload(self) -> dict[str, object]:
        return {"schema_version": self.schema_version, "stack": self.stack,
                "editable_paths": list(self.editable_paths), "test_files": list(self.test_files),
                "criteria": [row.to_payload() for row in self.criteria],
                "business": self.business.to_payload()}

    @property
    def sha256(self) -> str:
        digest = hashlib.sha256()
        for chunk in _JSON.iterencode(self.to_payload()):
            digest.update(chunk.encode("ascii"))
        return digest.hexdigest()

    @property
    def test_ids(self) -> tuple[str, ...]:
        return tuple(sorted(name for row in self.criteria for name in row.tests))

    def is_editable(self, path: object) -> bool:
        """Preservation boundary only; this never grants write authority."""
        try:
            normalized = _path(path)
            return not _protected(normalized) and any(
                _under(normalized, scope) for scope in self.editable_paths)
        except (ValueError, TypeError):
            return False

    def require_limits(self, limits: GoBusinessLimits) -> None:
        """The caller's JSON allocation covers the full canonical manifest."""
        _need(type(limits) is GoBusinessLimits)
        self.business.require_limits(limits)
        _require_json_budget(self.to_payload(), limits.max_json_bytes)

    @classmethod
    def from_payload(cls, value: object, *, limits: GoBusinessLimits | None = None) -> Self:
        item = _object(value, {"schema_version", "stack", "editable_paths", "test_files",
                               "criteria", "business"})
        if limits is not None:
            _need(type(limits) is GoBusinessLimits)
            _require_json_budget(item, limits.max_json_bytes)
        result = cls(
            editable_paths=cast(tuple[str, ...], _array(item["editable_paths"])),
            test_files=cast(tuple[str, ...], _array(item["test_files"])),
            criteria=tuple(GoAcceptanceCriterion.from_payload(row) for row in _array(item["criteria"])),
            business=GoBusinessSuite.from_payload(item["business"], limits=limits),
            schema_version=cast(Literal[2], item["schema_version"]),
            stack=cast(Literal["go"], item["stack"]),
        )
        if limits is not None:
            result.require_limits(limits)
        return result

    @classmethod
    def from_json(cls, raw: bytes | str, *, limits: GoBusinessLimits) -> Self:
        try:
            _need(type(limits) is GoBusinessLimits and type(raw) in {bytes, str}
                  and 0 < len(raw) <= limits.max_json_bytes)
            text = raw.decode("utf-8") if type(raw) is bytes else cast(str, raw)
            _need(len(text.encode("utf-8")) <= limits.max_json_bytes)
            return cls.from_payload(json.loads(text, object_pairs_hook=_unique,
                                               parse_constant=_nonfinite), limits=limits)
        except (ValueError, TypeError, KeyError, OverflowError, RecursionError):
            raise ValueError(_ERROR) from None


def parse_go_acceptance_manifest(raw: bytes, *, limits: GoBusinessLimits) -> GoAcceptanceManifest:
    """Parse original bytes only; callers authenticate their source independently."""
    _need(type(raw) is bytes)
    return GoAcceptanceManifest.from_json(raw, limits=limits)
