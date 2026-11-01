"""Exact registry-test module surfaces and mutable synthetic metadata shapes."""
from typing import Protocol, TypedDict, TypeGuard

from agent_hub.skills.sandbox.python_registry_protocol import (
    DependencyNeed,
    RegistryProvenance,
    ResolutionBudget,
    ResolutionLimits,
    ResolutionScope,
)
from agent_hub.skills.sandbox.python_registry_read import RegistryReader


class ProtocolAPI(Protocol):
    DependencyNeed: type[DependencyNeed]
    RegistryProvenance: type[RegistryProvenance]
    ResolutionBudget: type[ResolutionBudget]
    ResolutionLimits: type[ResolutionLimits]
    ResolutionScope: type[ResolutionScope]


class ReaderAPI(Protocol):
    RegistryReader: type[RegistryReader]


class WheelEntry(TypedDict):
    filename: str
    url: str
    digests: dict[str, str]
    size: int
    packagetype: str
    requires_python: str
    yanked: bool


class LimitOverrides(TypedDict, total=False):
    max_packages: int
    max_candidates: int
    max_edges: int
    max_requests: int
    max_response_bytes: int
    max_metadata_bytes: int
    max_wheel_bytes: int


def _is_object(value: object) -> TypeGuard[dict[str, object]]:
    return isinstance(value, dict) and all(isinstance(key, str) for key in value)


def _is_array(value: object) -> TypeGuard[list[object]]:
    return isinstance(value, list)


def object_map(value: object) -> dict[str, object]:
    """Narrow a fixture's existing dictionary without copying its mutable identity."""
    assert _is_object(value), "synthetic registry object required"
    return value


def object_array(value: object) -> list[object]:
    assert _is_array(value), "synthetic registry array required"
    return value
