"""Strict advisory stack observations; never environment or execution authority."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import PureWindowsPath
from typing import Literal, cast, get_args

ProjectStack = Literal["node", "python", "go", "java", "rust", "dotnet", "cmake"]
GoDeclarationReason = Literal[
    "go_manifest_missing", "go_declaration_invalid", "go_declaration_limit",
    "go_workspace_unsupported", "go_multiple_modules_unsupported", "go_replace_unsupported",
    "go_toolchain_unsupported", "go_cgo_unsupported", "go_native_source_unsupported",
    "go_external_modules_unsupported", "go_directive_unsupported",
]
StackReason = Literal[
    "stack_not_detected", "mixed_stack_unsupported", "recipe_not_inspected",
    "stack_execution_unsupported", "execution_not_available",
] | GoDeclarationReason
_STACKS = frozenset(get_args(ProjectStack))
_REASONS = frozenset(get_args(GoDeclarationReason)) | frozenset({
    "stack_not_detected", "mixed_stack_unsupported", "recipe_not_inspected",
    "stack_execution_unsupported", "execution_not_available",
})


class ProjectStackContractError(ValueError):
    """Invalid advisory input, without disclosing supplied project content."""

    def __init__(self, code: str = "project_stack_contract_invalid") -> None:
        self.code = code
        super().__init__(code)


def project_relative_path(value: object) -> str:
    if type(value) is not str or not 0 < len(value) <= 1024:
        raise ProjectStackContractError()
    for part in value.split("/"):
        if (not 0 < len(part) <= 255 or part in {".", ".."}
                or any(ord(char) < 32 or 0xD800 <= ord(char) <= 0xDFFF
                       or char in '\\:*?"<>|' for char in part)
                or part.endswith((".", " ")) or PureWindowsPath(part).is_reserved()):
            raise ProjectStackContractError()
    return value


def validate_project_files(
    files: object, *, max_files: int = 100_000, max_total_bytes: int = 2 * 1024 * 1024,
) -> dict[str, bytes]:
    """Copy already-read bytes; the caller owns completeness, scope and no-link reads."""
    if (type(max_files) is not int or max_files <= 0
            or type(max_total_bytes) is not int or max_total_bytes <= 0):
        raise ProjectStackContractError("project_stack_input_limit")
    if not isinstance(files, Mapping):
        raise ProjectStackContractError()
    if len(files) > max_files:
        raise ProjectStackContractError("project_stack_input_limit")
    result: dict[str, bytes] = {}
    spellings: dict[str, str] = {}
    size = 0
    for key, data in files.items():
        path = project_relative_path(key)
        if type(data) is not bytes or path in result:
            raise ProjectStackContractError()
        size += len(data)
        if size > max_total_bytes or len(result) >= max_files:
            raise ProjectStackContractError("project_stack_input_limit")
        parts = path.split("/")
        for end in range(1, len(parts) + 1):
            prefix = "/".join(parts[:end])
            folded = prefix.lower()
            if folded in spellings and spellings[folded] != prefix:
                raise ProjectStackContractError()
            spellings[folded] = prefix
        result[path] = data
    if len(result) != len(files):
        raise ProjectStackContractError()
    for path in result:
        parts = path.split("/")
        if any("/".join(parts[:end]) in result for end in range(1, len(parts))):
            raise ProjectStackContractError()
    return result


@dataclass(frozen=True, slots=True)
class StackObservation:
    detected_stacks: tuple[ProjectStack, ...]
    stack: ProjectStack | None
    reason: StackReason
    declaration_supported: bool = False
    recipe_supported: Literal[False] = False
    execution_supported: Literal[False] = False
    environment_ready: Literal[False] = False
    execution_proved: Literal[False] = False

    def __post_init__(self) -> None:
        stacks = self.detected_stacks
        if (type(stacks) is not tuple
                or not all(type(item) is str and item in _STACKS for item in stacks)
                or stacks != tuple(sorted(set(stacks)))
                or (self.stack is not None and type(self.stack) is not str)
                or self.stack != (stacks[0] if len(stacks) == 1 else None)
                or type(self.reason) is not str or self.reason not in _REASONS
                or type(self.declaration_supported) is not bool
                or any(value is not False for value in (
                    self.recipe_supported, self.execution_supported,
                    self.environment_ready, self.execution_proved,
                ))):
            raise ProjectStackContractError()
        if ((not stacks and self.reason != "stack_not_detected")
                or (len(stacks) > 1 and self.reason != "mixed_stack_unsupported")
                or (len(stacks) == 1 and self.reason in {
                    "stack_not_detected", "mixed_stack_unsupported",
                })
                or (self.declaration_supported != (self.reason == "execution_not_available"))
                or (self.declaration_supported and self.stack != "go")
                or (self.reason.startswith("go_") and self.stack != "go")):
            raise ProjectStackContractError()

    @property
    def detected(self) -> bool:
        return bool(self.detected_stacks)

    def to_payload(self) -> dict[str, object]:
        return {
            "stack": self.stack, "detected_stacks": list(self.detected_stacks),
            "detected": self.detected, "reason": self.reason,
            "declaration_supported": self.declaration_supported,
            "recipe_supported": False, "execution_supported": False,
            "environment_ready": False, "execution_proved": False,
        }


def require_project_stack(value: object) -> ProjectStack:
    if type(value) is not str or value not in _STACKS:
        raise ProjectStackContractError()
    return cast(ProjectStack, value)
