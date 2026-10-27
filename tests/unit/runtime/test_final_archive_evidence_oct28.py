"""A receipt hash must never masquerade as the delivered archive checksum."""
from __future__ import annotations

import json

import pytest

from agent_hub.runtime import direct_delivery_report as report
from tests.unit.runtime.test_direct_final_report import report_context


def delivery():
    return {'artifact_id': 'bundle', 'file': {'filename': 'project_20261028T010203.zip',
        'sha256': 'a' * 64, 'size_bytes': 16139},
        'verification': {'status': 'passed', 'source_sha256': 'b' * 64},
        'workspace_files': [{'path': 'README.md', 'sha256': 'c' * 64}]}


def test_final_model_sees_archive_bytes_hash_not_private_receipt_digest():
    value = delivery()
    view = report.evidence(report_context(), value)
    assert view['archive'] == value['file']
    assert 'delivery_sha256' not in view
    assert report.digest(value) not in json.dumps(view)
    # Internal recovery still binds the complete receipt independently.
    assert report.digest(value) != view['archive']['sha256']


def test_all_final_message_paths_use_the_same_unambiguous_archive_projection():
    value = delivery()
    messages = report.messages(report_context(), value)
    body = messages[-1].content
    assert isinstance(body, str)
    assert '"archive"' in body and '"sha256":"' + 'a' * 64 + '"' in body
    assert report.digest(value) not in body
    assert 'Do not invent' in report.PROMPT


@pytest.mark.parametrize('field,bad', [('sha256', 'not-a-checksum'), ('sha256', True),
    ('size_bytes', True), ('size_bytes', 0)])
def test_malformed_supplied_archive_facts_are_not_sent_as_verified(field, bad):
    value = delivery()
    value['file'][field] = bad
    with pytest.raises(ValueError, match='direct final delivery evidence is unavailable'):
        report.evidence(report_context(), value)


def test_legacy_receipt_without_archive_checksum_does_not_invent_one():
    value = delivery()
    value['file'] = {'filename': value['file']['filename']}
    view = report.evidence(report_context(), value)
    assert view['archive'] == value['file']
    assert 'delivery_sha256' not in view
