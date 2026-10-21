"""Pure GENERATED acceptance values, never original-input evidence or authority.

Trusted callers read file hashes/manifest bytes and enforce identity, permissions,
leases and once-only persistence. This module performs no filesystem IO, capture,
installation, test execution, approval or state transition.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import TYPE_CHECKING, Literal, Self, cast
from uuid import UUID

from agent_hub.capabilities.project_contracts import (
    ProjectCheckContract,
    _control,
    _files,
    _path,
    _scope,
    _sha,
    _spellings,
    _tree,
    build_project_check_contract,
)

if TYPE_CHECKING:
    from agent_hub.capabilities.project_baselines import ProjectBaselineReference

_MANIFEST = "ACCEPTANCE.json"
_MAX_BYTES = 128 * 1024
_ERROR = "generated project acceptance is invalid or changed"


def _need(value: object) -> None:
    if not value:
        raise ValueError(_ERROR)


def _encoded(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
                      allow_nan=False).encode("ascii")


def _digest(value: object) -> str:
    return hashlib.sha256(_encoded(value)).hexdigest()


def _object(value: object, keys: set[str]) -> dict[str, object]:
    _need(isinstance(value, Mapping) and set(value) == keys)
    return cast(dict[str, object], dict(cast(Mapping[str, object], value)))


def _array(value: object, maximum: int) -> tuple[object, ...]:
    _need(type(value) is list and 0 < len(cast(list[object], value)) <= maximum)
    return tuple(cast(list[object], value))


def _text(value: object, *, maximum: int, test_id: bool = False) -> str:
    _need(type(value) is str)
    text = cast(str, value)
    _need(0 < len(text) <= maximum and text.strip() == text
          and unicodedata.normalize("NFC", text) == text
          and not any(unicodedata.category(c).startswith("C") for c in text))
    if test_id:
        _need(not any(c.isspace() and c != " " for c in text) and "  " not in text)
    return text


def _protected_control(path: str) -> bool:
    return _control(path) or any(part.casefold() == _MANIFEST.casefold() for part in path.split("/"))


def _under(path: str, scope: str) -> bool:
    return path == scope or path.startswith(scope + "/")


@dataclass(frozen=True, slots=True)
class AcceptanceCriterion:
    id: str
    description: str
    tests: tuple[str, ...]

    def __post_init__(self) -> None:
        _need(type(self.id) is str and re.fullmatch(r"AC-[0-9]{3}", self.id) is not None
              and self.id != "AC-000")
        _text(self.description, maximum=2048)
        _need(type(self.tests) is tuple and 0 < len(self.tests) <= 1000)
        names = tuple(_text(item, maximum=512, test_id=True) for item in self.tests)
        _need(len({name.casefold() for name in names}) == len(names))
        object.__setattr__(self, "tests", tuple(sorted(names)))

    def to_payload(self) -> dict[str, object]:
        return {"id": self.id, "description": self.description, "tests": list(self.tests)}

    @classmethod
    def from_payload(cls, value: object) -> Self:
        item = _object(value, {"id", "description", "tests"})
        return cls(cast(str, item["id"]), cast(str, item["description"]),
                   cast(tuple[str, ...], _array(item["tests"], 1000)))


@dataclass(frozen=True, slots=True)
class AcceptanceManifest:
    stack: Literal["node", "python"]
    editable_paths: tuple[str, ...]
    test_files: tuple[str, ...]
    criteria: tuple[AcceptanceCriterion, ...]
    schema_version: Literal[1] = 1

    def __post_init__(self) -> None:
        _need(type(self.schema_version) is int and self.schema_version == 1
              and type(self.stack) is str and self.stack in {"node", "python"}
              and type(self.editable_paths) is tuple and 0 < len(self.editable_paths) <= 128
              and type(self.test_files) is tuple and 0 < len(self.test_files) <= 256
              and type(self.criteria) is tuple and 0 < len(self.criteria) <= 256)
        edits = tuple(_path(path) for path in self.editable_paths)
        tests = tuple(_path(path) for path in self.test_files)
        _need(not any(_protected_control(path) for path in (*edits, *tests)))
        _tree(edits)
        _tree(tests)
        _spellings((_MANIFEST, *edits, *tests))
        _need(not any(_under(test, edit) or _under(edit, test) for test in tests for edit in edits))
        for path in tests:
            if self.stack == "node":
                _need(path.endswith((".js", ".cjs", ".mjs")))
            else:
                parts = path.split("/")
                _need(len(parts) >= 2 and parts[0] == "tests"
                      and re.fullmatch(r"test[A-Za-z0-9_]*\.py", parts[-1]) is not None
                      and all(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", part) for part in parts[1:-1]))
        _need(all(type(item) is AcceptanceCriterion for item in self.criteria))
        ids = tuple(item.id for item in self.criteria)
        names = tuple(name for item in self.criteria for name in item.tests)
        _need(len(set(ids)) == len(ids) and len(names) <= 1000
              and len({name.casefold() for name in names}) == len(names))
        object.__setattr__(self, "editable_paths", tuple(sorted(edits)))
        object.__setattr__(self, "test_files", tuple(sorted(tests)))
        object.__setattr__(self, "criteria", tuple(sorted(self.criteria, key=lambda item: item.id)))
        _need(len(_encoded(self.to_payload())) <= _MAX_BYTES)

    @classmethod
    def from_payload(cls, value: object) -> Self:
        item = _object(value, {"schema_version", "stack", "editable_paths", "test_files", "criteria"})
        return cls(stack=cast(Literal["node", "python"], item["stack"]),
            editable_paths=cast(tuple[str, ...], _array(item["editable_paths"], 128)),
            test_files=cast(tuple[str, ...], _array(item["test_files"], 256)),
            criteria=tuple(AcceptanceCriterion.from_payload(row) for row in _array(item["criteria"], 256)),
            schema_version=cast(Literal[1], item["schema_version"]))

    def to_payload(self) -> dict[str, object]:
        return {"schema_version": 1, "stack": self.stack, "editable_paths": list(self.editable_paths),
                "test_files": list(self.test_files), "criteria": [item.to_payload() for item in self.criteria]}

    @property
    def sha256(self) -> str:
        return _digest(self.to_payload())

    @property
    def test_ids(self) -> tuple[str, ...]:
        return tuple(sorted(name for criterion in self.criteria for name in criterion.tests))

    def is_editable(self, path: object) -> bool:
        """A preservation boundary only; does not authorize the actor to write."""
        try:
            normalized = _path(path)
            return not _protected_control(normalized) and any(_under(normalized, scope) for scope in self.editable_paths)
        except (ValueError, TypeError):
            return False


def _unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        _need(key not in result)
        result[key] = value
    return result


def _nonfinite(value: str) -> object:
    raise ValueError(_ERROR)


def parse_acceptance_manifest(raw: bytes) -> AcceptanceManifest:
    try:
        _need(type(raw) is bytes and 0 < len(raw) <= _MAX_BYTES)
        return AcceptanceManifest.from_payload(json.loads(raw.decode("utf-8"),
            object_pairs_hook=_unique, parse_constant=_nonfinite))
    except (ValueError, TypeError, KeyError, RecursionError, OverflowError):
        raise ValueError(_ERROR) from None


@dataclass(frozen=True, slots=True)
class GeneratedAcceptanceEnvelope:
    tenant_id: UUID
    user_id: UUID
    run_id: UUID
    project_id: str
    session_id: str
    request_sha256: str
    proposer_actor: str
    manifest: AcceptanceManifest
    manifest_raw: bytes = field(repr=False)
    reference: ProjectBaselineReference
    source_files: Mapping[str, str]
    contract: ProjectCheckContract
    schema_version: Literal[1] = 1
    kind: Literal["generated_project_acceptance"] = "generated_project_acceptance"
    capture_phase: Literal["post_model"] = "post_model"
    purpose: Literal["generated_acceptance"] = "generated_acceptance"

    def __post_init__(self) -> None:
        from agent_hub.capabilities.project_baselines import ProjectBaselineReference

        _need(type(self.schema_version) is int and self.schema_version == 1
              and type(self.kind) is str and self.kind == "generated_project_acceptance"
              and type(self.capture_phase) is str and self.capture_phase == "post_model"
              and type(self.purpose) is str and self.purpose == "generated_acceptance"
              and type(self.proposer_actor) is str
              and re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,127}", self.proposer_actor) is not None
              and all(type(value) is UUID for value in (self.tenant_id, self.user_id, self.run_id))
              and type(self.manifest) is AcceptanceManifest and type(self.reference) is ProjectBaselineReference
              and type(self.contract) is ProjectCheckContract)
        _scope(self.project_id)
        _scope(self.session_id)
        _sha(self.request_sha256)
        ProjectBaselineReference.from_payload(self.reference.to_payload())
        _need(parse_acceptance_manifest(self.manifest_raw) == self.manifest)
        files = _files(self.source_files)
        _spellings((*files, *self.manifest.editable_paths, *self.manifest.test_files, _MANIFEST))
        _need(files.get(_MANIFEST) in {hashlib.sha256(tag + self.manifest_raw).hexdigest()
                                      for tag in (b"f\0", b"x\0")}
              and set(self.manifest.test_files) <= files.keys())
        required = {"package.json"} if self.manifest.stack == "node" else {"pyproject.toml", "requirements.lock"}
        _need(required <= files.keys())
        expected = build_project_check_contract(self.tenant_id, self.run_id, self.project_id,
            self.session_id, _digest(files), self.manifest.editable_paths, files)
        _need(self.contract.to_payload() == expected.to_payload()
              and self.reference.input_source_sha256 == _digest(files)
              and self.reference.file_count == len(files)
              and {_MANIFEST, *self.manifest.test_files, *required} <= self.contract.protected_files.keys())
        _need(all(path in self.contract.protected_files or path == "DELIVERY_MANIFEST.json"
                  or self.manifest.is_editable(path) for path in files))
        object.__setattr__(self, "source_files", MappingProxyType(files))

    @classmethod
    def bind(
        cls, *, tenant_id: UUID, user_id: UUID, run_id: UUID, project_id: str, session_id: str,
        request_sha256: str, proposer_actor: str, manifest_raw: bytes, reference: ProjectBaselineReference,
        source_files: Mapping[str, str], contract: ProjectCheckContract,
    ) -> Self:
        """Bind store-authenticated ref/observations, not workspace/install authority.

        The baseline store authenticates its private schema2 metadata separately;
        its contract digest never depends on this envelope's digest.
        """
        return cls(tenant_id=tenant_id, user_id=user_id, run_id=run_id, project_id=project_id,
            session_id=session_id, request_sha256=request_sha256, proposer_actor=proposer_actor,
            manifest=parse_acceptance_manifest(manifest_raw), manifest_raw=manifest_raw,
            reference=reference, source_files=source_files, contract=contract)

    def to_payload(self) -> dict[str, object]:
        return {"schema_version": 1, "kind": self.kind, "tenant_id": str(self.tenant_id),
                "user_id": str(self.user_id), "run_id": str(self.run_id), "project_id": self.project_id,
                "session_id": self.session_id, "request_sha256": self.request_sha256,
                "proposer_actor": self.proposer_actor, "capture_phase": self.capture_phase, "purpose": self.purpose,
                "manifest": self.manifest.to_payload(), "manifest_sha256": self.manifest.sha256,
                "manifest_raw_sha256": hashlib.sha256(self.manifest_raw).hexdigest(),
                "reference": self.reference.to_payload(), "source_files": dict(self.source_files),
                "contract": self.contract.to_payload()}

    @property
    def manifest_sha256(self) -> str:
        return self.manifest.sha256

    @property
    def sha256(self) -> str:
        return _digest(self.to_payload())

    @classmethod
    def from_payload(cls, value: object, *, manifest_raw: bytes) -> Self:
        from agent_hub.capabilities.project_baselines import ProjectBaselineReference

        try:
            item = _object(value, {"schema_version", "kind", "tenant_id", "user_id", "run_id", "project_id",
                "session_id", "request_sha256", "manifest", "manifest_sha256", "manifest_raw_sha256",
                "reference", "source_files", "contract", "proposer_actor", "capture_phase", "purpose"})
            identities: dict[str, UUID] = {}
            for key in ("tenant_id", "user_id", "run_id"):
                text = item[key]
                _need(type(text) is str)
                identities[key] = UUID(cast(str, text))
                _need(str(identities[key]) == text)
            stored_manifest = AcceptanceManifest.from_payload(item["manifest"])
            result = cls.bind(tenant_id=identities["tenant_id"], user_id=identities["user_id"],
                run_id=identities["run_id"], project_id=cast(str, item["project_id"]),
                session_id=cast(str, item["session_id"]), request_sha256=cast(str, item["request_sha256"]),
                proposer_actor=cast(str, item["proposer_actor"]),
                manifest_raw=manifest_raw, reference=ProjectBaselineReference.from_payload(item["reference"]),
                source_files=cast(Mapping[str, str], item["source_files"]),
                contract=ProjectCheckContract.from_payload(item["contract"]))
            _need(type(item["schema_version"]) is int and stored_manifest == result.manifest
                  and result.to_payload() == item)
            return result
        except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
            raise ValueError(_ERROR) from None

    def require_scope(
        self, *, tenant_id: UUID, user_id: UUID, run_id: UUID, project_id: str, session_id: str,
        request_sha256: str, proposer_actor: str,
    ) -> None:
        _need(all(type(value) is UUID for value in (tenant_id, user_id, run_id))
              and (self.tenant_id, self.user_id, self.run_id, self.project_id, self.session_id,
                   self.request_sha256, self.proposer_actor)
              == (tenant_id, user_id, run_id, project_id, session_id, request_sha256, proposer_actor))

    def require_current(self, source_files: Mapping[str, str]) -> None:
        current = _files(source_files)
        self.contract.require_current(current)
        _need(all(path in self.contract.protected_files or path == "DELIVERY_MANIFEST.json"
                  or self.manifest.is_editable(path) for path in current))
