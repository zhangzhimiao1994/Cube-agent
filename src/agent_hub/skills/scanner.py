from __future__ import annotations

from dataclasses import dataclass

from agent_hub.skills.package import SkillPackageInspection, SkillPackageInspector

SCANNER_VERSION = "skill-static-scanner/1"


@dataclass(frozen=True, slots=True)
class SkillScanReport:
    scanner_version: str
    content_sha256: str
    inspection: SkillPackageInspection
    findings: tuple[str, ...] = ()


class SkillScanner:
    def __init__(self, inspector: SkillPackageInspector | None = None) -> None:
        self._inspector = inspector or SkillPackageInspector()

    def scan(self, archive_bytes: bytes) -> SkillScanReport:
        inspection = self._inspector.inspect(archive_bytes)
        return SkillScanReport(
            scanner_version=SCANNER_VERSION,
            content_sha256=inspection.content_sha256,
            inspection=inspection,
        )

