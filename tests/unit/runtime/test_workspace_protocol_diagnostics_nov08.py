"""Structural protocol evidence must explain rejection without leaking a project."""
import hashlib
import json

import pytest

from agent_hub.runtime.workspace_protocol_diagnostics import workspace_protocol_diagnostic


def diagnostic(text, **kwargs):
    return workspace_protocol_diagnostic(text, rejection_code='invalid_batch_controls',
        allow_reads=True, allow_catalog=False, **kwargs)


def test_rejected_read_controls_are_observable_without_paths_or_contents():
    secret = 'private-customer-source-987'
    text = json.dumps({'workspace_batch': {'files': {'secret/path.py': secret},
        'read_paths': ['private/account.txt'], 'complete': 'yes', 'continuation': secret},
        'summary': secret, secret: secret})
    value = diagnostic(text)
    assert value['response_sha256'] == hashlib.sha256(text.encode()).hexdigest()
    assert value['response_bytes'] == len(text.encode())
    assert value['envelope'] == 'workspace_batch'
    assert value['field_types'] == {'files': 'object', 'read_paths': 'array',
        'complete': 'string', 'continuation': 'string'}
    assert value['file_count'] == value['read_path_count'] == 1
    assert value['allow_reads'] is True and value['allow_catalog'] is False
    rendered = json.dumps(value)
    assert secret not in rendered and 'secret/path.py' not in rendered
    assert 'private/account.txt' not in rendered


@pytest.mark.parametrize('text', [
    '{"workspace_batch":{"files":{},"read_paths":[],"complete":false,"complete":true}}',
    '{"workspace_batch":{"files":{},"complete":NaN}}',
    '{"workspace_batch":{"files":{},"complete":Infinity}}',
])
def test_ambiguous_or_non_json_input_never_yields_a_trusted_shape(text):
    value = diagnostic(text)
    assert value['parse_status'] == 'invalid_json'
    assert 'field_types' not in value


@pytest.mark.parametrize('text', [None, '', 'prose', '[]', 'true', 'null'])
def test_missing_and_non_envelopes_stay_bounded_metadata(text):
    value = diagnostic(text)
    assert value['schema_version'] == 1
    assert value['response_present'] is (text is not None)
    assert value['response_bytes'] == len((text or '').encode())
    assert 'field_types' not in value
    assert len(json.dumps(value)) < 1000


def test_large_response_is_hashed_but_not_reparsed_or_exported():
    text = json.dumps({'workspace_batch': {'files': {'a.py': 'z' * 70000}}})
    value = diagnostic(text)
    assert value['parse_status'] == 'omitted_size'
    assert value['response_sha256'] == hashlib.sha256(text.encode()).hexdigest()
    assert len(json.dumps(value)) < 1000


def test_reason_is_finite_and_never_copies_untrusted_text():
    value = workspace_protocol_diagnostic('{}', rejection_code='secret-dangerous-input',
        allow_reads=False, allow_catalog=True, native_tool_count=2)
    assert value['rejection_code'] == 'invalid_batch_controls'
    assert value['native_tool_count'] == 2
    assert 'secret-dangerous-input' not in json.dumps(value)


def test_catalog_shape_contains_no_query_values_and_round_trips_as_json():
    text = json.dumps({'workspace_batch': {'files': {}, 'python_catalog': {'secret': 'value'},
        'complete': False, 'continuation': 'private instruction'}})
    value = diagnostic(text)
    assert value['field_types']['python_catalog'] == 'object'
    assert value['complete'] is False
    assert json.loads(json.dumps(value)) == value
    assert 'private instruction' not in json.dumps(value)
