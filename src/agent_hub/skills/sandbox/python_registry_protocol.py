"""Non-authorizing Python resolution values and original-deadline resource accounting."""

from __future__ import annotations

import hashlib
import json
import math
import re
import time
from collections.abc import Mapping
from dataclasses import asdict, dataclass, fields
from uuid import UUID

from packaging.requirements import InvalidRequirement, Requirement

from agent_hub.capabilities.python_dependency_closure import PythonWheelMetadata
from agent_hub.skills.sandbox.python_input_fetch import WheelDownload


class ResolutionError(ValueError):
    """Finite diagnostics only; callers must not promote these values to authority."""


def need(value: object, code: str = "resolution_invalid") -> None:
    if not value:
        raise ResolutionError(code)


def digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("ascii")
    ).hexdigest()


def require_sha(value: str) -> None:
    need(type(value) is str and re.fullmatch("[a-f0-9]{64}", value))


def requirement(text: str) -> Requirement:
    need(
        type(text) is str
        and 0 < len(text) <= 1024
        and text.count("(") <= 32
        and all(32 <= ord(c) <= 126 for c in text)
    )
    try:
        parsed = Requirement(text)
        need(parsed.url is None, "resolution_source_unsupported")
        return parsed
    except InvalidRequirement:
        raise ResolutionError("resolution_requirement_invalid") from None


@dataclass(frozen=True, slots=True)
class DependencyNeed:
    requirement: str
    evidence_sha256: str

    def __post_init__(self) -> None:
        parsed = requirement(self.requirement)
        need(parsed.marker is None, "resolution_root_marker_unsupported")
        require_sha(self.evidence_sha256)


@dataclass(frozen=True, slots=True)
class ResolutionScope:
    tenant_id: UUID
    run_id: UUID
    user_id: UUID
    actor: str
    project_id: str
    session_id: str
    request_sha256: str
    source_sha256: str
    catalog_sha256: str

    def __post_init__(self) -> None:
        need(all(type(v) is UUID for v in (self.tenant_id, self.run_id, self.user_id)))
        for value in (self.actor, self.project_id, self.session_id):
            need(
                type(value) is str
                and 0 < len(value) <= 256
                and all(32 <= ord(c) <= 126 for c in value)
            )
        for value in (self.request_sha256, self.source_sha256, self.catalog_sha256):
            require_sha(value)

    def to_payload(self) -> dict[str, str]:
        return {field.name: str(getattr(self, field.name)) for field in fields(self)}


@dataclass(frozen=True, slots=True)
class ResolutionLimits:
    """Trusted caller ceilings, not model options. Counts below these remain adaptive."""

    max_packages: int = 128
    max_candidates: int = 1000
    max_edges: int = 4096
    max_requests: int = 256
    max_response_bytes: int = 1024**2
    max_metadata_bytes: int = 8 * 1024**2
    max_wheel_bytes: int = 64 * 1024**2

    def __post_init__(self) -> None:
        ceilings = (128, 1000, 4096, 4096, 2 * 1024**2, 32 * 1024**2, 64 * 1024**2)
        for field, ceiling in zip(fields(self), ceilings, strict=True):
            value = getattr(self, field.name)
            need(type(value) is int and 0 < value <= ceiling, "resolution_limits_invalid")


class ResolutionBudget:
    """One ledger across root/child work; soft milestones never require interaction."""

    def __init__(self, limits: ResolutionLimits, deadline: float) -> None:
        need(type(limits) is ResolutionLimits)
        limits.__post_init__()
        need(type(deadline) in {int, float} and math.isfinite(deadline))
        self._limits = limits
        self._deadline = deadline
        self._used: dict[str, int] = {}
        self._milestones: dict[str, int] = {}

    @property
    def limits(self) -> ResolutionLimits:
        return self._limits

    @property
    def deadline(self) -> float:
        return self._deadline

    @property
    def usage(self) -> tuple[tuple[str, int], ...]:
        return tuple(sorted(self._used.items()))

    @property
    def milestones(self) -> tuple[tuple[str, int], ...]:
        return tuple(sorted(self._milestones.items()))

    def check(self) -> None:
        need(time.monotonic() < self.deadline, "resolution_deadline")

    def consume(self, resource: str, amount: int = 1) -> None:
        self.check()
        need(type(amount) is int and amount >= 0)
        maximum = getattr(self.limits, "max_" + resource, None)
        need(type(maximum) is int, "resolution_resource_invalid")
        assert isinstance(maximum, int)
        used = self._used.get(resource, 0) + amount
        need(used <= maximum, "resolution_" + resource + "_limit")
        self._used[resource] = used
        self._milestones[resource] = self.soft_limit(resource, demand=used)

    def soft_limit(self, resource: str, *, demand: int) -> int:
        """Recompute milestones from actual demand, usage and remaining time; no grant."""
        self.check()
        maximum = getattr(self.limits, "max_" + resource, None)
        need(type(maximum) is int and type(demand) is int and demand >= 0)
        assert isinstance(maximum, int)
        initial = 4 if resource == "candidates" else 16
        remaining = max(1, int(self.deadline - time.monotonic()))
        return int(min(maximum, max(initial, demand, self._used.get(resource, 0) + remaining)))


@dataclass(frozen=True, slots=True)
class RegistryProvenance:
    url: str
    sha256: str

    def __post_init__(self) -> None:
        require_sha(self.sha256)
        need(
            type(self.url) is str
            and re.fullmatch(
                r"https://pypi[.]org/(?:simple/[a-z0-9-]+/|pypi/[a-z0-9-]+/[A-Za-z0-9.!+_-]+/json)",
                self.url,
            )
        )


@dataclass(frozen=True, slots=True)
class RegistryCandidate:
    download: WheelDownload
    provenance: RegistryProvenance
    index_provenance: RegistryProvenance

    def __post_init__(self) -> None:
        need(
            type(self.download) is WheelDownload
            and type(self.provenance) is RegistryProvenance
            and type(self.index_provenance) is RegistryProvenance
        )
        self.download.__post_init__()
        self.provenance.__post_init__()
        self.index_provenance.__post_init__()
        need(
            self.provenance.url
            == f"https://pypi.org/pypi/{self.download.name}/{self.download.version}/json"
            and self.index_provenance.url == f"https://pypi.org/simple/{self.download.name}/",
            "registry_provenance_mismatch",
        )


@dataclass(frozen=True, slots=True)
class ResolvedWheel:
    candidate: RegistryCandidate
    metadata: PythonWheelMetadata

    def __post_init__(self) -> None:
        need(
            type(self.candidate) is RegistryCandidate and type(self.metadata) is PythonWheelMetadata
        )
        self.metadata.__post_init__()
        wheel = self.metadata.wheel
        entry = self.candidate.download
        need(
            (wheel.name, wheel.version, wheel.filename, wheel.sha256)
            == (entry.name, entry.version, entry.filename, entry.sha256)
        )


@dataclass(frozen=True, slots=True)
class ResolutionDescriptor:
    scope: ResolutionScope
    needs: tuple[DependencyNeed, ...]
    environment: tuple[tuple[str, str], ...]
    wheels: tuple[ResolvedWheel, ...]
    edges: tuple[tuple[str, str], ...]

    def __post_init__(self) -> None:
        need(
            type(self.scope) is ResolutionScope
            and type(self.needs) is tuple
            and self.needs
            and all(type(n) is DependencyNeed for n in self.needs)
        )
        self.scope.__post_init__()
        for item in self.needs:
            item.__post_init__()
        need(type(self.wheels) is tuple and all(type(w) is ResolvedWheel for w in self.wheels))
        names = tuple(w.candidate.download.name for w in self.wheels)
        need(names == tuple(sorted(set(names))))
        for resolved in self.wheels:
            resolved.__post_init__()
        need(
            type(self.environment) is tuple
            and self.environment == tuple(sorted(self.environment))
            and len(dict(self.environment)) == len(self.environment)
        )
        target_environment(dict(self.environment))
        need(
            type(self.edges) is tuple
            and self.edges == tuple(sorted(set(self.edges)))
            and all(a in names and b in names for a, b in self.edges)
        )

    def to_payload(self) -> dict[str, object]:
        return {
            "scope": self.scope.to_payload(),
            "needs": [asdict(n) for n in self.needs],
            "environment": dict(self.environment),
            "wheels": [
                {"candidate": asdict(w.candidate), "metadata": w.metadata.to_payload()}
                for w in self.wheels
            ],
            "edges": [list(edge) for edge in self.edges],
        }

    @property
    def sha256(self) -> str:
        self.__post_init__()
        return digest(self.to_payload())


def target_environment(value: Mapping[str, str]) -> dict[str, str]:
    keys = {
        "implementation_name",
        "implementation_version",
        "os_name",
        "platform_machine",
        "platform_release",
        "platform_system",
        "platform_version",
        "platform_python_implementation",
        "python_full_version",
        "python_version",
        "sys_platform",
    }
    need(
        set(value) == keys
        and all(
            type(v) is str and 0 < len(v) <= 1024 and all(32 <= ord(c) <= 126 for c in v)
            for v in value.values()
        ),
        "resolution_environment_invalid",
    )
    need(
        value["implementation_name"] == "cpython"
        and value["python_version"] == "3.12"
        and value["sys_platform"] == "linux"
        and value["os_name"] == "posix"
        and value["platform_system"] == "Linux"
        and value["platform_python_implementation"] == "CPython"
        and value["platform_machine"] in {"x86_64", "aarch64"}
        and re.fullmatch(r"3[.]12[.][0-9]{1,5}", value["python_full_version"])
        and value["implementation_version"] == value["python_full_version"],
        "resolution_environment_unsupported",
    )
    return dict(value)
