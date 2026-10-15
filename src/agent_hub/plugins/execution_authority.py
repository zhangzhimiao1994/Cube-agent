from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True, slots=True)
class PackageExecutionMember:
    path: str
    size: int
    sha256: str


@dataclass(frozen=True, slots=True)
class GeneratedExecutionAuthority:
    tenant_id: UUID
    plugin_id: str
    registration_id: UUID
    archive_sha256: str
    report_id: str
    report_sha256: str
    members: tuple[PackageExecutionMember, ...]
