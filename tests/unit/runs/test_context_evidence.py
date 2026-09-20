from __future__ import annotations

import hashlib
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


@pytest.mark.parametrize('project', [_public_event_payload, admin_projection])
@pytest.mark.parametrize('stage,purpose', [('dispatch_step', 'step'), ('dispatch_review', 'review')])
def test_dispatch_projection_preserves_trusted_ledger_coordinates(
    project: Callable[[dict[str, object]], dict[str, object]], stage: str, purpose: str,
) -> None:
    run_id = str(uuid4())
    key = hashlib.sha256(f'{run_id}:draft.v1:17:{purpose}:worker.v1:64'.encode()).hexdigest()
    metadata: dict[str, object] = {
        'run_id': run_id, 'stage': stage, 'actor': 'worker.v1', 'step_id': 'draft.v1',
        'attempt': 17, 'call_index': 64, 'ledger_key': key, 'ledger_request_sha256': 'a' * 64,
    }
    result = project({'kind': 'context.injected', 'run_id': run_id, 'payload': metadata,
                      'actor': 'untrusted-top-level', 'message': 'private-body'})
    assert result['actor'] == 'worker.v1'
    assert result['payload'] == metadata


@pytest.mark.parametrize('project', [_public_event_payload, admin_projection])
@pytest.mark.parametrize('stage', ['provider.says.direct', None, [], {}, True])
def test_unknown_explicit_stage_does_not_fall_back_to_direct(
    project: Callable[[dict[str, object]], dict[str, object]], stage: object,
) -> None:
    result = project({'kind': 'context.injected', 'payload': {'stage': stage, 'actor': 'main_agent'}})
    assert result.get('actor') != 'direct'
    assert result.get('actor') != 'main_agent'
    assert 'stage' not in cast_payload(result)
    assert 'actor' not in cast_payload(result)


def cast_payload(event: dict[str, object]) -> dict[str, object]:
    value = event['payload']
    assert isinstance(value, dict)
    return value


@pytest.mark.parametrize('updates', [
    {'actor': '../private'}, {'step_id': 'private text'}, {'attempt': True},
    {'attempt': 18}, {'call_index': 65}, {'ledger_key': 'x' * 64},
    {'ledger_request_sha256': 'private'}, {'run_id': str(uuid4())},
])
def test_dispatch_identity_rejects_invalid_or_inconsistent_coordinates(updates: dict[str, object]) -> None:
    run_id = str(uuid4())
    key = hashlib.sha256(f'{run_id}:draft:0:step:writer:0'.encode()).hexdigest()
    metadata: dict[str, object] = {
        'run_id': run_id, 'stage': 'dispatch_step', 'actor': 'writer', 'step_id': 'draft',
        'attempt': 0, 'call_index': 0, 'ledger_key': key, 'ledger_request_sha256': 'a' * 64,
    }
    metadata.update(updates)
    result = _public_event_payload({'kind': 'context.injected', 'run_id': run_id, 'payload': metadata})
    assert 'stage' not in cast_payload(result)
    assert 'ledger_key' not in cast_payload(result)
    assert result.get('actor') not in ('writer', 'direct')


@pytest.mark.parametrize('identity', [
    {'stage': 'unknown', 'actor': 'private-body'},
    {'stage': 'dispatch_step', 'actor': '../private'},
    {'stage': 'direct', 'actor': 'other_actor'},
    {'stage': None},
])
def test_rejected_identity_survives_public_then_admin_projection(identity: dict[str, object]) -> None:
    event: dict[str, object] = {'kind': 'context.injected', 'payload': identity}
    first = _public_event_payload(event)
    second = admin_projection(first)
    third = _public_event_payload(second)
    assert first['actor'] == second['actor'] == third['actor'] == 'context_unknown'
    assert cast_payload(first) == cast_payload(second) == cast_payload(third)
    assert 'private' not in json.dumps(third, default=str)


@pytest.mark.parametrize('stage', ['legacy', 'direct', 'dispatch_step', 'dispatch_review'])
def test_valid_identity_survives_public_then_admin_projection(stage: str) -> None:
    run_id = str(uuid4())
    metadata: dict[str, object] = {'run_id': run_id}
    expected_actor = 'direct'
    if stage == 'direct':
        metadata.update(stage=stage, actor='main_agent')
        expected_actor = 'main_agent'
    elif stage != 'legacy':
        purpose = 'step' if stage == 'dispatch_step' else 'review'
        metadata.update(stage=stage, actor='writer', step_id='draft', attempt=0, call_index=0,
                        ledger_key=hashlib.sha256(f'{run_id}:draft:0:{purpose}:writer:0'.encode()).hexdigest(),
                        ledger_request_sha256='a' * 64)
        expected_actor = 'writer'
    event: dict[str, object] = {'kind': 'context.injected', 'run_id': run_id, 'payload': metadata}
    first = _public_event_payload(event)
    second = admin_projection(first)
    assert first['actor'] == second['actor'] == expected_actor
    assert first['payload'] == second['payload'] == metadata
