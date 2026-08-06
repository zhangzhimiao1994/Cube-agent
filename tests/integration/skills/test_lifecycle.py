from __future__ import annotations

import hashlib
import io
import zipfile

import pytest

from agent_hub.skills.service import (
    InMemorySkillService,
    InvalidSkillTransition,
    SkillContentChanged,
    SkillNotApproved,
    SkillRecord,
    SkillStatus,
)


async def test_uploaded_skill_cannot_run_before_approval() -> None:
    service = InMemorySkillService()
    skill = await service.upload(valid_skill_zip())

    assert skill.status == SkillStatus.QUARANTINED
    with pytest.raises(SkillNotApproved):
        await service.activate(skill.id)


async def test_skill_lifecycle_requires_scan_approval_before_activation() -> None:
    service = InMemorySkillService()
    uploaded = await service.upload(valid_skill_zip(requirements="pydantic==2.10.0\n"))

    scanned = await service.scan(uploaded.id)
    assert scanned.status == SkillStatus.SCANNED
    assert scanned.manifest is not None
    assert scanned.manifest.name == "demo_skill"

    approved = await service.approve(scanned.id, reviewer="admin", diff_summary="initial review")
    assert approved.status == SkillStatus.APPROVED
    assert approved.approval is not None
    assert approved.approval.content_sha256 == scanned.content_sha256
    assert approved.approval.scanner_version == "skill-static-scanner/1"
    assert approved.approval.reviewer == "admin"
    assert approved.approval.diff_summary == "initial review"
    assert approved.approval.dependency_lock_hash == hashlib.sha256(b"pydantic==2.10.0\n").hexdigest()

    active = await service.activate(approved.id)
    assert active.status == SkillStatus.ACTIVE


async def test_disabled_and_revoked_skills_are_terminal() -> None:
    service = InMemorySkillService()
    active = await activate_valid_skill(service)

    disabled = await service.disable(active.id)
    assert disabled.status == SkillStatus.DISABLED
    with pytest.raises(InvalidSkillTransition):
        await service.activate(disabled.id)

    another_active = await activate_valid_skill(service)
    revoked = await service.revoke(another_active.id)
    assert revoked.status == SkillStatus.REVOKED
    with pytest.raises(InvalidSkillTransition):
        await service.activate(revoked.id)


async def test_byte_change_creates_new_version_requiring_approval() -> None:
    service = InMemorySkillService()
    first = await service.upload(valid_skill_zip(entry_body=b"print('one')\n"))
    second = await service.upload(valid_skill_zip(entry_body=b"print('two')\n"))

    assert first.id != second.id
    assert first.package_version_id != second.package_version_id
    assert first.status == SkillStatus.QUARANTINED
    assert second.status == SkillStatus.QUARANTINED
    with pytest.raises(SkillNotApproved):
        await service.activate(second.id)


async def test_content_mutation_after_scan_invalidates_approval() -> None:
    service = InMemorySkillService()
    uploaded = await service.upload(valid_skill_zip(entry_body=b"print('before')\n"))
    scanned = await service.scan(uploaded.id)

    await service.replace_archive_for_test(scanned.id, valid_skill_zip(entry_body=b"print('after')\n"))

    with pytest.raises(SkillContentChanged):
        await service.approve(scanned.id, reviewer="admin", diff_summary="tampered")


@pytest.mark.parametrize("reviewer,diff_summary", [("", "ok"), (" admin", "ok"), ("admin", ""), ("admin", "ok\n")])
async def test_approval_audit_fields_must_be_printable_unpadded(
    reviewer: str,
    diff_summary: str,
) -> None:
    service = InMemorySkillService()
    uploaded = await service.upload(valid_skill_zip())
    scanned = await service.scan(uploaded.id)

    with pytest.raises(ValueError, match="must be non-empty"):
        await service.approve(scanned.id, reviewer=reviewer, diff_summary=diff_summary)


async def activate_valid_skill(service: InMemorySkillService) -> SkillRecord:
    uploaded = await service.upload(valid_skill_zip())
    scanned = await service.scan(uploaded.id)
    approved = await service.approve(scanned.id, reviewer="admin", diff_summary="ok")
    return await service.activate(approved.id)


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
