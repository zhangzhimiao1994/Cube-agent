"""Prompt-only coverage: real projection functions, no files, models or native calls."""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any, cast
from uuid import uuid4

import pytest

from agent_hub.runtime import direct
from agent_hub.runtime.autogen import adapter as autogen
from agent_hub.runtime.contracts import Artifact
from agent_hub.runtime.crew import adapter as crew


def original(text: str = '\u00e9\u4e2d\u6587' * 3000, *, omitted: bool = False) -> Artifact:
    return Artifact(id=uuid4(), type='original_project_observation', producer='original_project_preparer',
        content={'source_context': {'files': ({'path': 'src/main.py', 'content': text,
            'sha256': hashlib.sha256(b'f\0' + text.encode()).hexdigest()},),
            'file_count': 2 if omitted else 1, 'included_count': 1, 'truncated': omitted}})


def projection(kind: str, artifact: Artifact) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    if kind == 'autogen':
        content = cast(Mapping[str, Any], autogen._artifact_prompt_content(artifact))
        return content, content['source_preview']
    if kind in {'dependent', 'recovery'}:
        packet = cast(Mapping[str, Any], crew._artifact_review_packet_payload(
            artifact, max_preview_bytes=512 if kind == 'recovery' else 1200)['artifact_review_packet'])
        return packet['content_preview'], packet['source_preview']
    if kind == 'synthesis':
        payload = crew._artifact_final_synthesis_payload(artifact, max_text_bytes=512)
    else:
        payload = crew._artifact_prompt_payload(artifact)
    return cast(Mapping[str, Any], payload['content']), cast(Mapping[str, Any], payload['source_preview'])


@pytest.mark.parametrize('kind', ['ordinary', 'dependent', 'recovery', 'synthesis', 'autogen'])
def test_second_projection_marks_actual_shown_text_without_changing_stored_v1(kind: str) -> None:
    artifact = original()
    before = artifact.to_payload()
    content, notice = projection(kind, artifact)
    source = content['source_context']
    row = source['files'][0]
    assert source['truncated'] is True and notice['coverage'] == 'partial'
    assert notice['schema_version'] == 1
    assert notice['snapshot_file_hash_semantics'] == 'mode_tagged_snapshot_file'
    assert 'verification' in notice['note'].lower()
    assert '[truncated:' in row['content']
    assert row['shown_text_bytes'] == len(row['content'].encode('utf-8'))
    assert row['shown_text_sha256'] == hashlib.sha256(row['content'].encode('utf-8')).hexdigest()
    assert row['content_truncated'] is True
    assert row['source_file_sha256'] == cast(Any, artifact.content)['source_context']['files'][0]['sha256']
    assert row['shown_text_sha256'] != row['source_file_sha256']
    assert 'sha256' not in row
    assert artifact.to_payload() == before
    assert artifact.content_sha256 == artifact.recompute_content_sha256()


@pytest.mark.parametrize('kind', ['ordinary', 'dependent', 'recovery', 'autogen'])
@pytest.mark.parametrize('omitted', [False, True])
def test_uncut_text_and_producer_omissions_keep_distinct_coverage(kind: str, omitted: bool) -> None:
    content, notice = projection(kind, original('small source', omitted=omitted))
    row = content['source_context']['files'][0]
    assert row['content_truncated'] is False
    assert notice['coverage'] == ('partial' if omitted else 'complete')
    assert content['source_context']['truncated'] is omitted


@pytest.mark.parametrize('byte_budget', [500, 4000, 60_000])
def test_direct_notice_and_display_digest_are_outside_the_bounded_excerpt(byte_budget: int) -> None:
    artifact = original('source value ' * 2000)
    before = artifact.to_payload()
    payload, ids = direct._serialized_prior_artifacts((artifact,), token_budget=50_000,
        byte_budget=byte_budget, include_original=True)
    decoded = json.loads(payload)
    assert len(payload.encode()) <= byte_budget
    assert ids == (str(artifact.id),)
    entry = decoded['artifacts'][0]
    if 'text' in entry:
        notice = entry['source_preview']
        assert notice['displayed_kind'] == 'artifact_json_excerpt'
        assert notice['displayed_text_bytes'] == len(entry['text'].encode())
        assert notice['displayed_text_sha256'] == hashlib.sha256(entry['text'].encode()).hexdigest()
        assert notice['snapshot_file_hash_semantics'] == 'mode_tagged_snapshot_file'
        assert notice['coverage'] == ('partial' if '[truncated:' in entry['text'] else 'complete')
    else:
        assert decoded['compacted_artifact_count'] == 1
    assert artifact.to_payload() == before


def test_direct_partial_json_has_an_exact_external_digest_and_notice() -> None:
    artifact = original('source value ' * 2000)
    small = Artifact(id=uuid4(), type='text', producer='writer', content={'text': 'small'})
    payload, _ = direct._serialized_prior_artifacts((artifact, small), token_budget=50_000,
        byte_budget=6000, include_original=True)
    entry = json.loads(payload)['artifacts'][0]
    assert '[truncated:' in entry['text']
    assert entry['source_preview']['coverage'] == 'partial'
    assert entry['source_preview']['displayed_text_sha256'] == hashlib.sha256(entry['text'].encode()).hexdigest()
    assert len(payload.encode()) <= 6000


def test_direct_html_escaping_is_counted_in_the_existing_byte_budget() -> None:
    artifact = original('<x>' * 4000)
    small = Artifact(id=uuid4(), type='text', producer='writer', content={'text': 'small'})
    payload, _ = direct._serialized_prior_artifacts((artifact, small), token_budget=50_000,
        byte_budget=6000, include_original=True)
    assert len(payload.encode()) <= 6000


def test_literal_truncation_words_do_not_turn_complete_text_into_partial() -> None:
    for kind in ('ordinary', 'dependent', 'recovery', 'autogen'):
        content, notice = projection(kind, original('[truncated: this is literal source text]'))
        assert notice['coverage'] == 'complete'
        assert content['source_context']['files'][0]['content_truncated'] is False


@pytest.mark.parametrize('recover', [False, True])
async def test_actual_crew_gateway_receives_coverage_on_initial_and_recovery_requests(recover: bool) -> None:
    from agent_hub.models.capacity import CapacityUnavailable
    from agent_hub.models.types import ModelRequest
    from tests.unit.runtime.crew.test_adapter_failure_reason import (
        FastFactory,
        SequenceGateway,
        _context,
        _structured_dependent_final_plan,
    )

    class Gateway(SequenceGateway):
        async def complete_with_context(self, request: ModelRequest) -> Any:
            if recover and not self.requests:
                self.requests.append(request)
                raise CapacityUnavailable('model capacity unavailable')
            return await super().complete_with_context(request)

    artifact = original()
    before = artifact.to_payload()
    gateway = Gateway('{"summary":"done","findings":[],"risks":[]}', 'final')
    runtime = crew.CrewDispatchRuntime(gateway, _structured_dependent_final_plan(), crew_factory=FastFactory())
    events = [event async for event in runtime.run(_context(artifacts=(artifact,)))]
    assert events[-1].kind == 'runtime.completed'
    assert len(gateway.requests) == (3 if recover else 2)
    checked = gateway.requests[:2] if recover else gateway.requests[:1]
    for request in checked:
        content = '\n'.join(str(message.content) for message in request.messages)
        assert '"source_preview"' in content
        assert 'mode_tagged_snapshot_file' in content
        assert 'shown_text_sha256' in content
        assert 'may omit files or clip text' in content
    assert artifact.to_payload() == before
