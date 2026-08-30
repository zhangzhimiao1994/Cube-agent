from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass, replace
from enum import StrEnum

from agent_hub.skills.manifest import SkillManifest
from agent_hub.skills.scanner import SkillScanner, SkillScanReport


class SkillStatus(StrEnum):
    QUARANTINED = "quarantined"
    SCANNED = "scanned"
    APPROVED = "approved"
    ACTIVE = "active"
    DISABLED = "disabled"
    REVOKED = "revoked"


class SkillNotFound(KeyError):
    pass


class SkillNotApproved(RuntimeError):
    pass


class InvalidSkillTransition(RuntimeError):
    pass


class SkillContentChanged(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class SkillApprovalRecord:
    content_sha256: str
    scanner_version: str
    reviewer: str
    diff_summary: str
    requested_capabilities: tuple[str, ...]
    dependency_lock_hash: str


@dataclass(frozen=True, slots=True)
class SkillRecord:
    id: str
    package_version_id: str
    content_sha256: str
    package_size: int
    status: SkillStatus
    manifest: SkillManifest | None = None
    scan_report: SkillScanReport | None = None
    approval: SkillApprovalRecord | None = None


class InMemorySkillService:
    def __init__(self, scanner: SkillScanner | None = None) -> None:
        self._scanner = scanner or SkillScanner()
        self._records: dict[str, SkillRecord] = {}
        self._archives: dict[str, bytes] = {}
        self._lock = asyncio.Lock()

    async def upload(self, archive_bytes: bytes) -> SkillRecord:
        content_sha256 = hashlib.sha256(archive_bytes).hexdigest()
        record = SkillRecord(
            id=f"skill_{content_sha256[:24]}",
            package_version_id=f"pkg_{content_sha256}",
            content_sha256=content_sha256,
            package_size=len(archive_bytes),
            status=SkillStatus.QUARANTINED,
        )
        async with self._lock:
            existing = self._record_by_content_sha256(content_sha256)
            if existing is not None:
                return existing
            self._records[record.id] = record
            self._archives[record.id] = bytes(archive_bytes)
        return record

    async def get(self, skill_id: str) -> SkillRecord:
        async with self._lock:
            return self._record(skill_id)

    async def scan(self, skill_id: str) -> SkillRecord:
        async with self._lock:
            record = self._record(skill_id)
            if record.status != SkillStatus.QUARANTINED:
                raise InvalidSkillTransition("only quarantined skills can be scanned")
            archive_bytes = self._archives[skill_id]
            report = self._scanner.scan(archive_bytes)
            scanned = replace(
                record,
                status=SkillStatus.SCANNED,
                manifest=report.inspection.manifest,
                scan_report=report,
                content_sha256=report.content_sha256,
                package_version_id=f"pkg_{report.content_sha256}",
            )
            self._records[skill_id] = scanned
            return scanned

    async def approve(self, skill_id: str, *, reviewer: str, diff_summary: str) -> SkillRecord:
        _validate_audit_text(reviewer, field_name="reviewer")
        _validate_audit_text(diff_summary, field_name="diff_summary")
        async with self._lock:
            record = self._record(skill_id)
            if record.status != SkillStatus.SCANNED:
                raise InvalidSkillTransition("only scanned skills can be approved")
            if record.scan_report is None:
                raise InvalidSkillTransition("skill has not been scanned")
            _assert_current_content(record, self._archives[skill_id])
            approval = SkillApprovalRecord(
                content_sha256=record.content_sha256,
                scanner_version=record.scan_report.scanner_version,
                reviewer=reviewer,
                diff_summary=diff_summary,
                requested_capabilities=record.scan_report.inspection.requested_capabilities,
                dependency_lock_hash=record.scan_report.inspection.dependency_lock_hash,
            )
            approved = replace(record, status=SkillStatus.APPROVED, approval=approval)
            self._records[skill_id] = approved
            return approved

    async def activate(self, skill_id: str) -> SkillRecord:
        async with self._lock:
            record = self._record(skill_id)
            if record.status != SkillStatus.APPROVED:
                if record.status in {SkillStatus.QUARANTINED, SkillStatus.SCANNED}:
                    raise SkillNotApproved("skill must be approved before activation")
                raise InvalidSkillTransition("only approved skills can be activated")
            if record.approval is None:
                raise SkillNotApproved("skill must be approved before activation")
            _assert_current_content(record, self._archives[skill_id])
            if record.approval.content_sha256 != record.content_sha256:
                raise SkillContentChanged("approved skill content no longer matches approval")
            active = replace(record, status=SkillStatus.ACTIVE)
            self._records[skill_id] = active
            return active

    async def disable(self, skill_id: str) -> SkillRecord:
        async with self._lock:
            record = self._record(skill_id)
            if record.status != SkillStatus.ACTIVE:
                raise InvalidSkillTransition("only active skills can be disabled")
            disabled = replace(record, status=SkillStatus.DISABLED)
            self._records[skill_id] = disabled
            return disabled

    async def revoke(self, skill_id: str) -> SkillRecord:
        async with self._lock:
            record = self._record(skill_id)
            if record.status != SkillStatus.ACTIVE:
                raise InvalidSkillTransition("only active skills can be revoked")
            revoked = replace(record, status=SkillStatus.REVOKED)
            self._records[skill_id] = revoked
            return revoked

    async def replace_archive_for_test(self, skill_id: str, archive_bytes: bytes) -> None:
        async with self._lock:
            self._record(skill_id)
            self._archives[skill_id] = bytes(archive_bytes)

    def _record(self, skill_id: str) -> SkillRecord:
        try:
            return self._records[skill_id]
        except KeyError as exc:
            raise SkillNotFound(skill_id) from exc

    def _record_by_content_sha256(self, content_sha256: str) -> SkillRecord | None:
        for record in self._records.values():
            if record.content_sha256 == content_sha256:
                return record
        return None


def _assert_current_content(record: SkillRecord, archive_bytes: bytes) -> None:
    current_sha256 = hashlib.sha256(archive_bytes).hexdigest()
    if current_sha256 != record.content_sha256:
        raise SkillContentChanged("skill package bytes changed after scan")


def _validate_audit_text(value: str, *, field_name: str) -> None:
    if value != value.strip() or not value or any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        raise ValueError(f"{field_name} must be non-empty unpadded printable text")
