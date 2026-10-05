from __future__ import annotations

import copy
import io
import json
import zipfile
from pathlib import Path
from typing import Any, cast

import pytest

from agent_hub.harness.project_scale_runner import ProjectScaleCaseResult
from tests.unit.test_real_user_four_scale_acceptance_script import (
    PublicArtifactClient,
    _passing_evidence,
    _pending_automated_report,
    _real_device_evidence,
    _scope_evidence,
    load_script,
    public_artifact_evidence,
    validated_workspace_manifest,
    workspace_zip,
)


def test_public_download_without_validated_identity_cannot_pass() -> None:
    result = load_script().verify_public_workspace_artifacts(
        PublicArtifactClient(workspace_zip()),
        project_id="project-small",
        workspace_session_id="conv-small",
    )
    assert result["ok"] is False


def test_case_without_validated_identity_cannot_receive_core_credit() -> None:
    result = ProjectScaleCaseResult(
        case_id="small:auto",
        run_id="run-small",
        status="completed",
        observed_mode="direct",
        effective_scale="small",
        artifact_origin="tool_workspace_write",
        workspace_bundle_source="public_workspace_api",
        evidence=_passing_evidence(),
    )
    report = load_script().build_case_report(
        scale="small",
        project={},
        conversation={},
        result=result,
        model_scope_evidence=_scope_evidence("run-small", "deepseek-backup"),
        public_artifacts={"ok": True, "source": "public_workspace_api"},
        dynamic_web_preview={"counted_as_passed": True},
    )
    assert report["core_acceptance_ok"] is False
    assert report["build_and_test"]["status"] == "failed"


def test_legacy_report_without_validated_identity_cannot_resume_as_complete() -> None:
    case = _pending_automated_report()["cases"][0]
    case["run"].pop("validated_workspace_manifest", None)
    case["public_artifacts"].pop("workspace_manifest", None)
    case["public_artifacts"].pop("validated_bundle_matches", None)
    assert (
        load_script()._has_complete_core_evidence(
            case,
            safe_execution_id="matrix-123",
            case_key="auto-small",
        )
        is False
    )


@pytest.mark.parametrize("compression", [zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED])
def test_repacked_zip_binds_by_content_not_archive_bytes(compression: int) -> None:
    source = workspace_zip()
    target = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(source)) as before, zipfile.ZipFile(target, "w") as after:
        for item in reversed(before.infolist()):
            rewritten = zipfile.ZipInfo(item.filename, date_time=(2001, 2, 3, 4, 5, 6))
            rewritten.compress_type = compression
            after.writestr(rewritten, before.read(item))
    assert target.getvalue() != source
    result = load_script().verify_public_workspace_artifacts(
        PublicArtifactClient(target.getvalue()),
        project_id="project-small",
        workspace_session_id="conv-small",
        validated_workspace_manifest=json.loads(json.dumps(validated_workspace_manifest())),
    )
    assert result["ok"] is True
    assert result["validated_bundle_matches"] is True
    assert result["workspace_manifest"] == public_artifact_evidence()["workspace_manifest"]


@pytest.mark.parametrize("change", ["missing", "extra", "size", "digest"])
def test_consistent_public_downloads_cannot_substitute_different_validated_input(
    change: str,
) -> None:
    manifest = validated_workspace_manifest()
    size, digest = manifest["README.md"]
    if change == "missing":
        del manifest["README.md"]
    elif change == "extra":
        manifest["deleted.json"] = (size, digest)
    elif change == "size":
        manifest["README.md"] = (size + 1, digest)
    else:
        manifest["README.md"] = (size, "0" * 64)
    result = load_script().verify_public_workspace_artifacts(
        PublicArtifactClient(workspace_zip()),
        project_id="project-small",
        workspace_session_id="conv-small",
        validated_workspace_manifest=manifest,
    )
    assert result["metadata_matches_zip"] is True
    assert result["zip_crc_ok"] is True
    assert result["ok"] is False
    assert result["validated_bundle_matches"] is False
    assert "public ZIP differs from the workspace that passed validation" in result["errors"]


@pytest.mark.parametrize(
    "manifest",
    [
        None,
        {},
        [],
        {"README.md": [True, "0" * 64]},
        {"README.md": [-1, "0" * 64]},
        {"README.md": [8, "A" * 64]},
        {"README.md": [8, "0" * 63]},
        {"README.md": [8, "0" * 64, "extra"]},
        {"README.md": "invalid"},
        {"../README.md": [8, "0" * 64]},
        {"/README.md": [8, "0" * 64]},
        {"./README.md": [8, "0" * 64]},
        {"src\\app.py": [8, "0" * 64]},
    ],
)
def test_missing_or_malformed_identity_is_not_invented_from_public_zip(manifest: object) -> None:
    result = load_script().verify_public_workspace_artifacts(
        PublicArtifactClient(workspace_zip()),
        project_id="project-small",
        workspace_session_id="conv-small",
        validated_workspace_manifest=manifest,
    )
    assert result["ok"] is False
    assert result["validated_bundle_matches"] is False
    assert "validated workspace manifest is missing or invalid" in result["errors"]


class DuplicateListingClient(PublicArtifactClient):
    def request_json(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, object] | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, object] | list[object]:
        result = cast(
            dict[str, Any],
            super().request_json(
                method,
                path,
                body=body,
                idempotency_key=idempotency_key,
            ),
        )
        result["items"].append(copy.deepcopy(result["items"][0]))
        return result


def test_duplicate_public_listing_does_not_receive_positive_binding() -> None:
    result = load_script().verify_public_workspace_artifacts(
        DuplicateListingClient(workspace_zip()),
        project_id="project-small",
        workspace_session_id="conv-small",
        validated_workspace_manifest=validated_workspace_manifest(),
    )
    assert result["ok"] is False
    assert any("duplicate" in error for error in result["errors"])


@pytest.mark.parametrize(
    "change",
    [
        "missing_run",
        "missing_public",
        "different_digest",
        "different_size",
        "extra_file",
        "invalid_path",
        "false_flag",
        "count",
        "boolean_count",
        "summary_flag",
        "build_flag",
    ],
)
def test_resume_and_finalizer_recompute_identity_instead_of_trusting_success_flags(
    change: str, tmp_path: Path,
) -> None:
    module = load_script()
    report = _pending_automated_report()
    case = report["cases"][0]
    assert (
        module._has_complete_core_evidence(
            case,
            safe_execution_id="matrix-123",
            case_key="auto-small",
        )
        is True
    )
    public, run = case["public_artifacts"], case["run"]
    if change == "missing_run":
        run.pop("validated_workspace_manifest")
    elif change == "missing_public":
        public.pop("workspace_manifest")
    elif change == "different_digest":
        public["workspace_manifest"]["README.md"][1] = "0" * 64
    elif change == "different_size":
        run["validated_workspace_manifest"]["README.md"][0] += 1
    elif change == "extra_file":
        public["workspace_manifest"]["deleted.json"] = [8, "0" * 64]
    elif change == "invalid_path":
        public["workspace_manifest"]["../README.md"] = public["workspace_manifest"].pop("README.md")
    elif change == "false_flag":
        public["validated_bundle_matches"] = False
    elif change == "count":
        public["downloaded_file_count"] += 1
    elif change == "boolean_count":
        public["zip_member_count"] = True
    elif change == "summary_flag":
        case["validated_bundle_matches"] = False
    else:
        case["build_and_test"]["validated_bundle_matches"] = False
    before = copy.deepcopy(report)
    assert (
        module._has_complete_core_evidence(
            case,
            safe_execution_id="matrix-123",
            case_key="auto-small",
        )
        is False
    )
    device = _real_device_evidence("matrix-123", evidence_root=tmp_path)
    with pytest.raises(ValueError, match="core"):
        module.finalize_real_device_acceptance(report, device, evidence_root=tmp_path)
    assert report == before
