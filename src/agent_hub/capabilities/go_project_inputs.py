"""Original-only Go recipe data, separate from SDK supply and execution grants.

Bind controls from a trusted captured snapshot, never from model arguments.
The Root executor must independently reconstruct this binding, freshly measure
the mounted SDK and enforce live approval before launching any project code.
An accepted payload proves none of those observations or native execution.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import cast
from uuid import UUID

from agent_hub.capabilities.go_project_recipe import (
    GoProjectDeclaration,
    parse_go_project_declaration,
    validate_go_source_paths,
)
from agent_hub.capabilities.project_baselines import ProjectBaselineReference, StoredProjectBaseline
from agent_hub.capabilities.project_contracts import ProjectCheckContract, _files
from agent_hub.capabilities.project_stack_registry import detect_stacks, is_stack_control_path
from agent_hub.capabilities.scoped_read import _read_no_links
from agent_hub.capabilities.types import CapabilityRequest
from agent_hub.skills.sandbox.toolchain_catalog import ToolchainPlanScope
from agent_hub.skills.sandbox.toolchain_protocol import ToolchainReference

_ERROR = "go_project_inputs_invalid"
_PROTECTED = "go_project_protected_input_changed"
_RESOURCE = "workspace/current/project-verification/go/"
_KIND = "go_stdlib_cli_v1"


def _need(value: object, code: str = _ERROR) -> None:
    if not value:
        raise ValueError(code)


def _encoded(value: object) -> bytes:
    return json.dumps(
        value,
        default=dict,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii")


def _digest(value: object) -> str:
    return hashlib.sha256(_encoded(value)).hexdigest()


def _version(value: str) -> tuple[int, ...]:
    parts = tuple(int(part) for part in value.split("."))
    return parts + (0,) * (3 - len(parts))


def _policy() -> dict[str, object]:
    return {
        "dependency_policy": "stdlib_only",
        "external_dependencies": [],
        "build_target": ".",
        "test_target": "./...",
    }


@dataclass(frozen=True, slots=True)
class GoProjectInputs:
    scope: ToolchainPlanScope
    contract: ProjectCheckContract
    original_reference: ProjectBaselineReference
    source_files: Mapping[str, str]
    declaration: GoProjectDeclaration
    toolchain: ToolchainReference
    source_limit_bytes: int
    file_limit: int

    def __post_init__(self) -> None:
        _need(
            type(self.scope) is ToolchainPlanScope
            and type(self.contract) is ProjectCheckContract
            and type(self.original_reference) is ProjectBaselineReference
            and type(self.declaration) is GoProjectDeclaration
            and type(self.toolchain) is ToolchainReference
        )
        scope = ToolchainPlanScope.from_json(self.scope.to_json())
        contract = ProjectCheckContract.from_payload(self.contract.to_payload())
        reference = ProjectBaselineReference.from_payload(self.original_reference.to_payload())
        declaration = GoProjectDeclaration.from_payload(self.declaration.to_payload())
        sdk = ToolchainReference.model_validate_json(self.toolchain.model_dump_json(), strict=True)
        files = _files(self.source_files)
        validate_go_source_paths(tuple(files))
        _need(
            type(self.source_limit_bytes) is int
            and 1 <= self.source_limit_bytes <= 500 * 1024**2
            and type(self.file_limit) is int
            and 1 <= self.file_limit <= 100_000
            and len(files) <= self.file_limit
        )
        _need(
            (scope.tenant_id, scope.run_id, scope.project_id, scope.session_id, scope.source_sha256)
            == (
                contract.tenant_id,
                contract.run_id,
                contract.project_id,
                contract.session_id,
                contract.input_source_sha256,
            )
            and scope.source_sha256 == reference.input_source_sha256
            and scope.contract_sha256 == reference.contract_sha256
            and scope.actor_id == "main_agent"
            and scope.actor_role in {"operator", "admin", "super_admin"}
        )
        _need(
            reference.file_count == len(files) and _digest(files) == reference.input_source_sha256
        )
        # A baseline reference hashes the whole envelope, not protected_sha256.
        _need(
            _digest({"schema_version": 1, "contract": contract.to_payload(), "source_files": files})
            == reference.contract_sha256
        )
        contract.require_current(files)
        _need(detect_stacks(tuple(files)) == ("go",))
        controls = {path for path in files if is_stack_control_path(path)}
        _need(
            "go.mod" in controls and any(path.endswith("_test.go") for path in controls),
            "go_project_tests_missing",
        )
        _need(
            all(contract.protected_files.get(path) == files[path] for path in controls), _PROTECTED
        )
        _need(set(declaration.control_paths) <= controls)
        _need(
            _version(sdk.version) >= _version(declaration.go_version)
            and declaration.toolchain in {None, "default", "go" + sdk.version}
        )
        object.__setattr__(self, "source_files", MappingProxyType(files))

    def require_current(self, source_files: Mapping[str, str]) -> None:
        """Candidate file hashes are measured by the caller; they grant no access."""
        current = _files(source_files)
        validate_go_source_paths(tuple(current))
        _need(len(current) <= self.file_limit)
        self.contract.require_current(current)
        original_controls = {
            p: value for p, value in self.source_files.items() if is_stack_control_path(p)
        }
        controls = {p: value for p, value in current.items() if is_stack_control_path(p)}
        _need(controls == original_controls, _PROTECTED)
        _need(detect_stacks(tuple(current)) == ("go",))

    def _binding(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "kind": _KIND,
            "scope": self.scope.to_payload(),
            "contract": self.contract.to_payload(),
            "original_reference": self.original_reference.to_payload(),
            "source_files": dict(self.source_files),
            "declaration": self.declaration.to_payload(),
            "toolchain": self.toolchain.model_dump(mode="json"),
            "source_limit_bytes": self.source_limit_bytes,
            "file_limit": self.file_limit,
            **_policy(),
        }

    @property
    def sha256(self) -> str:
        return _digest(self._binding())

    @property
    def environment_input_sha256(self) -> str:
        return _digest(
            {
                "schema_version": 1,
                "kind": _KIND,
                "toolchain": self.toolchain.model_dump(mode="json"),
                **_policy(),
            }
        )

    def to_payload(self) -> dict[str, object]:
        return self._binding() | {
            "recipe_sha256": self.sha256,
            "environment_input_sha256": self.environment_input_sha256,
        }

    @classmethod
    def from_payload(cls, value: object) -> GoProjectInputs:
        try:
            _need(type(value) is dict)
            item = cast(dict[str, object], value)
            _need(
                set(item)
                == {
                    "schema_version",
                    "kind",
                    "scope",
                    "contract",
                    "original_reference",
                    "source_files",
                    "declaration",
                    "toolchain",
                    "source_limit_bytes",
                    "file_limit",
                    "recipe_sha256",
                    "environment_input_sha256",
                    *_policy(),
                }
                and type(item["schema_version"]) is int
                and item["schema_version"] == 1
                and item["kind"] == _KIND
            )
            result = cls(
                scope=ToolchainPlanScope.from_payload(item["scope"]),
                contract=ProjectCheckContract.from_payload(item["contract"]),
                original_reference=ProjectBaselineReference.from_payload(
                    item["original_reference"]
                ),
                source_files=_files(item["source_files"]),
                declaration=GoProjectDeclaration.from_payload(item["declaration"]),
                toolchain=ToolchainReference.model_validate_json(
                    _encoded(item["toolchain"]), strict=True
                ),
                source_limit_bytes=cast(int, item["source_limit_bytes"]),
                file_limit=cast(int, item["file_limit"]),
            )
            _need(_encoded(result.to_payload()) == _encoded(item))
            return result
        except (ValueError, TypeError, KeyError, OverflowError, RecursionError):
            raise ValueError(_ERROR) from None

    def _approval_arguments(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "kind": _KIND,
            "recipe_sha256": self.sha256,
            "environment_input_sha256": self.environment_input_sha256,
            "scope": self.scope.to_payload(),
            "original_reference": self.original_reference.to_payload(),
            "original_check_contract": self.contract.protected_sha256,
            "toolchain_sha256": _digest(self.toolchain.model_dump(mode="json")),
        }

    def approval_request(
        self, *, user_id: UUID, agent_id: str, idempotency_key: str
    ) -> CapabilityRequest:
        _need(
            type(user_id) is UUID
            and user_id == self.scope.user_id
            and agent_id == self.scope.actor_id
        )
        return CapabilityRequest(
            tenant_id=self.scope.tenant_id,
            user_id=user_id,
            run_id=self.scope.run_id,
            agent_id=agent_id,
            capability="command",
            operation="run",
            resource=_RESOURCE + self.sha256,
            arguments=self._approval_arguments(),
            idempotency_key=idempotency_key,
        )

    def matches_approval_request(self, request: CapabilityRequest) -> bool:
        try:
            arguments = _encoded(request.arguments)
        except (ValueError, TypeError, OverflowError, RecursionError):
            return False
        return (
            request.tenant_id,
            request.user_id,
            request.run_id,
            request.agent_id,
            request.capability,
            request.operation,
            request.resource,
        ) == (
            self.scope.tenant_id,
            self.scope.user_id,
            self.scope.run_id,
            self.scope.actor_id,
            "command",
            "run",
            _RESOURCE + self.sha256,
        ) and arguments == _encoded(self._approval_arguments())


def bind_go_project_inputs(
    *,
    stored: StoredProjectBaseline,
    scope: ToolchainPlanScope,
    toolchain: ToolchainReference,
    source_limit_bytes: int,
    file_limit: int,
) -> GoProjectInputs:
    """Reread every recorded original file; Root still must rederive before exec.

    stored must come from the trusted complete original capture/staging reader,
    not a model or arbitrary path/map. Missing/added source and SDK provenance
    are checked independently by that boundary and the eventual native runner.
    """
    _need(type(stored) is StoredProjectBaseline and stored.generated_metadata is None)
    _need(
        type(source_limit_bytes) is int
        and 1 <= source_limit_bytes <= 500 * 1024**2
        and type(file_limit) is int
        and 1 <= file_limit <= 100_000
        and len(stored.source_files) <= file_limit
    )
    raw: dict[str, bytes] = {}
    remaining = source_limit_bytes
    for name, digest in stored.source_files.items():
        data = _read_no_links(stored.snapshot, tuple(name.split("/")), max_bytes=remaining)
        remaining -= len(data)
        _need(
            remaining >= 0
            and digest in {hashlib.sha256(tag + data).hexdigest() for tag in (b"f\0", b"x\0")}
        )
        raw[name] = data
    declaration = parse_go_project_declaration(
        raw, max_source_bytes=source_limit_bytes, max_files=file_limit
    )
    return GoProjectInputs(
        scope=scope,
        contract=stored.contract,
        original_reference=stored.reference,
        source_files=stored.source_files,
        declaration=declaration,
        toolchain=toolchain,
        source_limit_bytes=source_limit_bytes,
        file_limit=file_limit,
    )
