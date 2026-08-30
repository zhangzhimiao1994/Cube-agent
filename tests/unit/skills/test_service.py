from __future__ import annotations

import hashlib
import io
import zipfile

from agent_hub.skills.service import InMemorySkillService, SkillStatus


async def test_duplicate_skill_archive_upload_returns_existing_record_without_resetting_status() -> None:
    service = InMemorySkillService()
    archive = valid_skill_zip()
    uploaded = await service.upload(archive)
    scanned = await service.scan(uploaded.id)

    duplicate = await service.upload(archive)

    assert duplicate.id == uploaded.id
    assert duplicate.package_version_id == uploaded.package_version_id
    assert duplicate.status == SkillStatus.SCANNED
    assert duplicate.scan_report == scanned.scan_report


async def test_changed_skill_archive_creates_new_version_requiring_review() -> None:
    service = InMemorySkillService()

    first = await service.upload(valid_skill_zip(entry_body=b"print('one')\n"))
    second = await service.upload(valid_skill_zip(entry_body=b"print('two')\n"))

    assert first.id != second.id
    assert first.package_version_id != second.package_version_id
    assert first.status == SkillStatus.QUARANTINED
    assert second.status == SkillStatus.QUARANTINED


def valid_skill_zip(*, requirements: str = "", entry_body: bytes = b"print('ok')\n") -> bytes:
    dependency_hash = hashlib.sha256(requirements.encode()).hexdigest()
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "skill.yaml",
            f"""name: demo_skill
version: 1.0.0
entry_point: main.py
compatible_runtime: python3.12
declared_tools:
  - calculator.evaluate
network_policy:
  mode: none
  allow_hosts:
    []
writable_paths:
  - tmp/output
env_secret_refs:
  - deepseek_api_key
dependency_lock_hash: "{dependency_hash}"
""",
        )
        archive.writestr("main.py", entry_body)
        if requirements:
            archive.writestr("requirements.txt", requirements.encode())
    return buffer.getvalue()
