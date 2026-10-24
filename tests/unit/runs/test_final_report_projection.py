"""Only explicit top-level delivery reports receive final-answer presentation."""

from copy import deepcopy
from typing import Any
from uuid import UUID, uuid4

import pytest

from agent_hub.api.routers.admin import _admin_run_artifact
from agent_hub.runs.repository import _public_artifact_payload
from agent_hub.runtime.contracts import Artifact


def final_report() -> dict[str, Any]:
    return {
        "id": str(uuid4()),
        "type": "text",
        "producer": "delivery_main_agent",
        "content": {
            "text": "  ## Delivery\n\nImplemented the requested changes. Checks passed.  ",
            "presentation": "final_answer",
            "engineering_report": {
                "version": 1,
                "run_id": str(uuid4()),
                "tenant_id": str(uuid4()),
                "owner": "delivery_main_agent",
                "logical_model": "offline-model",
                "model_position": 3,
                "model_request_hash": "a" * 64,
                "model_artifact_id": str(uuid4()),
                "model_artifact_sha256": "b" * 64,
                "bundle_position": 2,
                "bundle_artifact_id": str(uuid4()),
                "bundle_artifact_sha256": "c" * 64,
                "tool_prefix_length": 2,
                "tool_prefix_sha256": "d" * 64,
                "private_evidence": {"api_key": "private-token-must-stay-private"},
            },
        },
    }


def test_explicit_final_report_projects_safe_text_without_private_metadata() -> None:
    artifact = final_report()
    before = deepcopy(artifact)

    projected = _admin_run_artifact(artifact, run_id=uuid4())

    assert projected.presentation == "final_answer"
    assert projected.kind == "text"
    assert projected.title == "delivery_main_agent"
    assert projected.text == artifact["content"]["text"].strip()
    public = projected.model_dump_json()
    for private in ("engineering_report", "private-token", "model_request_hash", "tool_prefix"):
        assert private not in public
    assert artifact == before


def test_public_storage_sanitizer_preserves_report_presentation_without_exposing_bindings() -> None:
    payload = final_report()
    payload["content"]["engineering_report"].pop("private_evidence")
    artifact = Artifact(
        id=UUID(payload["id"]), type=payload["type"], producer=payload["producer"],
        content=payload["content"],
    )
    public = _public_artifact_payload(artifact.to_payload())

    projected = _admin_run_artifact(public)

    assert projected.presentation == "final_answer"
    assert projected.text == payload["content"]["text"].strip()
    assert "engineering_report" not in projected.model_dump_json()
    assert "private-token" not in projected.model_dump_json()


@pytest.mark.parametrize("text", [None, "", " \n\t ", 42, {"text": "Nested report"},
                                  "password=private-token"])
def test_empty_invalid_or_sensitive_final_text_is_not_promoted(text: object) -> None:
    artifact = final_report()
    artifact["content"]["text"] = text

    projected = _admin_run_artifact(artifact)

    assert projected.text is None
    assert projected.presentation != "final_answer"


@pytest.mark.parametrize("metadata", [None, {}, [], "report", {"version": True},
                                      {"version": "1"}, {"version": 2},
                                      {"version": 1},
                                      {"version": 1, "owner": "main"}])
def test_final_report_requires_basic_metadata_schema(metadata: object) -> None:
    artifact = final_report()
    artifact["content"]["engineering_report"] = metadata
    assert _admin_run_artifact(artifact).presentation != "final_answer"


@pytest.mark.parametrize("kind,producer", [
    ("tool_result", "delivery_main_agent"),
    ("model_result", "delivery_main_agent"),
    ("text", "main"),
    ("text", "reviewer"),
])
def test_other_artifact_types_and_producers_cannot_claim_final_answer(
    kind: str, producer: str,
) -> None:
    artifact = final_report()
    artifact.update(type=kind, producer=producer)
    assert _admin_run_artifact(artifact).presentation != "final_answer"


@pytest.mark.parametrize("container", ["result", "file", "metadata", "result_file",
                                       "result_metadata", "workspace_delivery"])
def test_nested_final_answer_markers_are_never_promoted(container: str) -> None:
    artifact = final_report()
    report = artifact["content"]
    if container.startswith("result_"):
        content = {"result": {container.removeprefix("result_"): report}}
    else:
        content = {container: report}
    artifact["content"] = content

    projected = _admin_run_artifact(artifact)

    assert projected.presentation != "final_answer"
    assert projected.text is None


def test_ordinary_chat_and_legacy_presentations_remain_compatible() -> None:
    artifact = final_report()
    artifact["producer"] = "main"
    artifact["content"] = {"text": "An ordinary chat reply."}
    assert _admin_run_artifact(artifact).text == "An ordinary chat reply."
    assert _admin_run_artifact(artifact).presentation is None
    for presentation in ("final_attachment", "step_detail", "diagnostic", "internal"):
        artifact["content"]["presentation"] = presentation
        assert _admin_run_artifact(artifact).presentation == presentation
