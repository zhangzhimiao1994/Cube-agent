from collections.abc import Mapping

from agent_hub.runs.repository import _public_artifact_payload, _public_event_payload


def test_public_event_payload_redacts_sensitive_values_under_safe_keys() -> None:
    payload = _public_event_payload(
        {
            "safe_summary": "failed after Bearer sk-private-token reached provider",
            "nested": {
                "safe_hint": "Authorization: Bearer sk-private-token",
                "ok": "retry with model fallback",
            },
            "items": ["private-token value", {"note": "normal"}],
            "credential_ref": "secret://must-drop",
        }
    )

    assert payload == {
        "safe_summary": "[redacted]",
        "nested": {
            "safe_hint": "[redacted]",
            "ok": "retry with model fallback",
        },
        "items": ["[redacted]", {"note": "normal"}],
    }


def test_public_artifact_payload_removes_generated_file_storage_key() -> None:
    payload = _public_artifact_payload(
        {
            "id": "artifact-1",
            "type": "tool_result",
            "producer": "document_writer",
            "content": {
                "result": {
                    "file": {
                        "filename": "delivery-plan.docx",
                        "download_url": "/api/v1/admin/runs/run-1/artifacts/artifact-1/download",
                    },
                    "metadata": {
                        "filename": "delivery-plan.docx",
                        "storage_key": "tenant/run/artifact/delivery-plan.docx",
                    },
                }
            },
        }
    )

    content = payload["content"]
    assert isinstance(content, Mapping)
    result = content["result"]
    assert isinstance(result, dict)
    metadata = result["metadata"]
    assert isinstance(metadata, dict)
    assert "storage_key" not in metadata
