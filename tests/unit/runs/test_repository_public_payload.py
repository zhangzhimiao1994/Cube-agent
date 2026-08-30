from __future__ import annotations

from agent_hub.runs.repository import _public_artifact_payload, _public_event_payload


def test_public_artifact_payload_filters_generated_file_storage_key_recursively() -> None:
    payload: dict[str, object] = {
        "id": "artifact-1",
        "content": {
            "result": {
                "file": {
                    "filename": "report.docx",
                    "storage_key": "tenant/run/artifact/report.docx",
                    "download_url": "/api/v1/admin/runs/run/artifacts/artifact/download",
                }
            }
        },
    }

    sanitized = _public_artifact_payload(payload)

    assert sanitized == {
        "id": "artifact-1",
        "content": {
            "result": {
                "file": {
                    "filename": "report.docx",
                    "download_url": "/api/v1/admin/runs/run/artifacts/artifact/download",
                }
            }
        },
    }


def test_public_event_payload_filters_generated_file_storage_key_recursively() -> None:
    payload: dict[str, object] = {
        "type": "artifact.created",
        "artifact": {
            "content": {
                "file": {
                    "filename": "deck.pptx",
                    "storage_key": "tenant/run/artifact/deck.pptx",
                }
            }
        },
    }

    sanitized = _public_event_payload(payload)

    assert sanitized == {
        "type": "artifact.created",
        "artifact": {"content": {"file": {"filename": "deck.pptx"}}},
    }
