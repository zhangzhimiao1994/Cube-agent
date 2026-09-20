from __future__ import annotations

import json
from collections.abc import Callable
from uuid import uuid4

import pytest

from agent_hub.api.routers.admin import _admin_run_event
from agent_hub.runs.repository import _public_event_payload


def admin_projection(event: dict[str, object]) -> dict[str, object]:
    return _admin_run_event(event).model_dump(mode="json")


@pytest.mark.parametrize("project", [_public_event_payload, admin_projection])
@pytest.mark.parametrize("kind", ["context.loaded", "context.injected"])
def test_context_events_never_expose_unstructured_guidance(
    project: Callable[[dict[str, object]], dict[str, object]], kind: str,
) -> None:
    marker = "private-guidance-sentinel"
    event: dict[str, object] = {
        "kind": kind, "sequence": 2, "run_id": str(uuid4()),
        "message": marker, "reason": marker, "summary": marker,
        "actor": marker, "participants": [marker], "action": marker,
        "body": marker,
        "payload": {
            "text": marker, "content": marker, "load_id": marker,
            "read_sha256": marker, "sources": [{"text": marker, "relative_path": marker}],
            "surprise": {"nested": marker},
        },
        "artifact": {"id": str(uuid4()), "type": "text", "producer": marker,
                     "content": {"text": marker}},
    }

    response = project(event)

    assert marker not in json.dumps(response)
    assert response["kind"] == kind
    assert response["sequence"] == 2


@pytest.mark.parametrize("project", [_public_event_payload, admin_projection])
def test_context_projection_does_not_change_regular_message_events(
    project: Callable[[dict[str, object]], dict[str, object]],
) -> None:
    event: dict[str, object] = {
        "kind": "message.created", "sequence": 1,
        "message": "normal message", "payload": {"note": "normal metadata"},
    }

    response = project(event)

    assert response["message"] == "normal message"
    assert response["payload"] == {"note": "normal metadata"}


@pytest.mark.parametrize("project", [_public_event_payload, admin_projection])
@pytest.mark.parametrize("kind", [[], {}, None])
def test_unknown_event_kind_keeps_existing_safe_fallback(
    project: Callable[[dict[str, object]], dict[str, object]], kind: object,
) -> None:
    assert project({"kind": kind, "sequence": 1})["sequence"] == 1


@pytest.mark.parametrize("project", [_public_event_payload, admin_projection])
@pytest.mark.parametrize("kind", ["context.loaded", "context.injected"])
def test_context_projection_preserves_bounded_evidence(
    project: Callable[[dict[str, object]], dict[str, object]], kind: str,
) -> None:
    identity = str(uuid4())
    digest = "a" * 64
    source: dict[str, object] = {"path": "AGENTS.md", "truncated": False}
    if kind == "context.loaded":
        source.update(kind="project_guidance", status="loaded", project_id="project",
                      session_id="session", read_bytes=12, content_bytes=12,
                      read_sha256=digest, file_sha256=digest, content_sha256=digest)
    else:
        source.update(injected_bytes=12, injected_sha256=digest)
    metadata: dict[str, object] = {
        "schema_version": 1, "load_id": identity, "tenant_id": identity,
        "run_id": identity, "sources": [source],
    }
    if kind == "context.loaded":
        metadata["source"] = "session_root"
    else:
        metadata.update(boundary="model_gateway", logical_model="main", request_sha256=digest)

    response = project({"kind": kind, "sequence": 1, "payload": metadata})

    assert response["payload"] == metadata


@pytest.mark.parametrize("project", [_public_event_payload, admin_projection])
def test_context_projection_rejects_unsafe_paths_and_malformed_evidence(
    project: Callable[[dict[str, object]], dict[str, object]],
) -> None:
    response = project({
        "kind": "context.loaded", "sequence": 1,
        "payload": {"schema_version": True, "load_id": "not-a-uuid", "sources": [
            {"path": "../../AGENTS.md", "status": "loaded"},
            {"path": "AGENTS.md", "read_bytes": True, "content_bytes": 8193,
             "read_sha256": "x" * 64, "status": "private", "project_id": "../private",
             "text": "private-guidance-sentinel"},
            {"path": "SKILL.md", "status": "loaded"},
        ]},
    })

    assert response["payload"] == {
        "sources": [{"path": "AGENTS.md", "file_sha256": None,
                     "content_sha256": None, "session_id": None}],
    }
