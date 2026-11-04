"""Automatic project recovery guides a full repair scope, not a single command."""

from copy import deepcopy
from uuid import uuid4

import pytest

from agent_hub.capabilities.generated_project_draft import GeneratedDraftRejection
from agent_hub.runtime import self_repair_context as repair_context
from agent_hub.runtime.contracts import Artifact
from agent_hub.runtime.self_repair_context import self_repair_context_text
from tests.unit.runs.project_auto_repair_typed_fixture import json_map
from tests.unit.runs.reviewed_report_revision_typed_fixture import read_map, string_value


def automatic_route() -> dict[str, object]:
    return {
        "source": "self_repair",
        "self_repair_accepted": True,
        "engineering_auto_repair_version": 1,
        "self_repair_context": {
            "source": "self_repair", "source_run_id": "run_1",
            "failure_kind": "tool_failure", "attempt": 1, "max_attempts": 1,
            "automatic_execution": True, "requires_approval": False,
        },
    }


def test_automatic_scope_requires_diagnosis_alternatives_and_verified_delivery() -> None:
    route = automatic_route()
    before = deepcopy(route)
    text = self_repair_context_text(route)
    assert "approved bounded self-repair attempt" not in text
    assert "stop after this attempt" not in text
    assert "Run one bounded repair attempt, then stop." not in text
    assert "Diagnose the observed failure" in text
    assert "different evidence-based approach" in text
    assert "verify the acceptance criteria and deliver" in text
    assert "original permissions and remaining hard budgets" in text
    assert "Do not replay UNKNOWN outcomes" in text
    assert "replan instead of repeating an unchanged failing action" in text
    assert "specific action needs additional authority" in text
    assert "no safe executable alternative remains" in text
    assert route == before


@pytest.mark.parametrize("key,value", [
    ("source", "task"), ("self_repair_accepted", False),
    ("engineering_auto_repair_version", None), ("engineering_auto_repair_version", True),
])
def test_legacy_or_unaccepted_route_does_not_claim_automatic_scope(key: str, value: object) -> None:
    route = automatic_route()
    route[key] = value
    assert "stop after this attempt" in self_repair_context_text(route)


@pytest.mark.parametrize("key,value", [
    ("automatic_execution", False), ("requires_approval", True),
    ("failure_kind", "model_credential_unavailable"), ("failure_kind", {}),
])
def test_pending_approval_does_not_receive_automatic_recovery_guidance(key: str, value: object) -> None:
    route = automatic_route()
    repair = route["self_repair_context"]
    assert isinstance(repair, dict)
    repair[key] = value
    assert "stop after this attempt" in self_repair_context_text(route)


def failure_artifact() -> Artifact:
    return Artifact(id=uuid4(), type="tool_result", producer="implementer", content={
        "tool_name": "project.verify", "arguments_sha256": "a" * 64,
        "result": {"status": "failed", "untrusted": True, "execution_diagnostic": {
            "backend": "node", "exit_code": 1, "timed_out": False,
            "stdout": "", "stderr": "Cannot find module required-for-build",
        }},
    })


def test_bound_failure_projection_keeps_diagnosis_as_untrusted_data() -> None:
    artifact = failure_artifact()
    text = repair_context.project_repair_failure_text(
        artifact, expected_artifact_id=str(artifact.id),
        expected_artifact_sha256=artifact.content_sha256,
        validated_feedback=json_map(read_map(artifact.content["result"])),
    )
    assert text.startswith("UNTRUSTED_PROJECT_REPAIR_FEEDBACK_JSON=")
    assert "Cannot find module required-for-build" in text
    assert str(artifact.id) in text and artifact.content_sha256 in text
    assert '"exit_code":1' in text and '"untrusted":true' in text


@pytest.mark.parametrize("defect", ["artifact_id", "hash", "changed_feedback", "success"])
def test_failure_projection_does_not_forge_evidence(defect: str) -> None:
    artifact = failure_artifact()
    feedback = dict(read_map(artifact.content["result"]))
    expected_id, expected_hash = str(artifact.id), artifact.content_sha256
    if defect == "artifact_id":
        expected_id = str(uuid4())
    elif defect == "hash":
        expected_hash = "b" * 64
    elif defect == "changed_feedback":
        feedback["execution_diagnostic"] = {}
    else:
        feedback["status"] = "succeeded"
    with pytest.raises(ValueError, match="repair failure evidence"):
        repair_context.project_repair_failure_text(artifact,
            expected_artifact_id=expected_id, expected_artifact_sha256=expected_hash,
            validated_feedback=json_map(feedback))


@pytest.mark.parametrize('pins,package,version', [
    (False, 'click', '8.1.8'), (True, 'click', '8.1.8'), (True, 'flask', '3.1.2'),
])
def test_draft_projection_preserves_actual_issue_and_observation_not_private_identity(
    pins: bool, package: str, version: str,
) -> None:
    from dataclasses import replace

    draft_value = GeneratedDraftRejection(tenant_id=str(uuid4()), user_id=str(uuid4()), run_id=str(uuid4()),
        project_id="private-project", session_id="private-session", request_sha256="a" * 64,
        proposer_actor="implementer", observation_sha256="c" * 64,
        reason="generated_acceptance_manifest_required", issues=("manifest_missing",))
    if pins:
        draft_value = replace(draft_value, schema_version=3, reason='generated_acceptance_python_declaration_invalid',
                        issues=('python_lock_hashes_required',), dependency_pins=({
                            'name': package, 'version': version, 'filename': f'{package}-{version}-py3-none-any.whl',
                            'sha256': 'd' * 64, 'size_bytes': 100},))
    draft = draft_value.to_payload()
    artifact = Artifact(id=uuid4(), type="tool_result", producer="implementer", content={
        "tool_name": "project.verify", "result": {
            "status": "failed", "untrusted": True, "generated_draft_rejection": json_map(draft),
        },
    })
    text = repair_context.project_repair_failure_text(artifact,
        expected_artifact_id=str(artifact.id), expected_artifact_sha256=artifact.content_sha256,
        validated_feedback=json_map(read_map(artifact.content["result"])))
    assert ("python_lock_hashes_required" if pins else "manifest_missing") in text
    if pins:
        assert '"dependency_pins"' in text and 'd' * 64 in text
        assert f'{package}-{version}-py3-none-any.whl' in text
    assert "c" * 64 in text and '"execution_started":false' in text
    assert string_value(draft["user_id"]) not in text and "private-project" not in text


def test_diagnostic_projection_redacts_sensitive_stream_text_without_mutating_receipt() -> None:
    artifact = Artifact(id=uuid4(), type="tool_result", producer="implementer", content={
        "tool_name": "project.verify", "result": {"status": "failed", "untrusted": True,
            "execution_diagnostic": {"stderr": "Authorization: Bearer PRIVATE", "exit_code": 1}},
    })
    original = artifact.content_sha256
    text = repair_context.project_repair_failure_text(artifact,
        expected_artifact_id=str(artifact.id), expected_artifact_sha256=original,
        validated_feedback=json_map(read_map(artifact.content["result"])))
    assert "PRIVATE" not in text and "Bearer" not in text
    assert '"diagnostic_projection_redacted":true' in text
    assert artifact.recompute_content_sha256() == original
