"""Bounded structural evidence for rejected batches; never an execution parser."""
from __future__ import annotations

import hashlib
import json

from agent_hub.runtime.contracts import JsonValue

_REASONS = frozenset({
    'native_tools_not_executed', 'empty_response', 'invalid_json_envelope',
    'invalid_batch_envelope', 'invalid_control_placement', 'invalid_completion_control',
    'invalid_files_mapping', 'invalid_relative_path', 'invalid_file_content',
    'empty_batch_without_completion', 'invalid_batch_controls',
    'native_read_tools_not_executed', 'read_not_enabled', 'catalog_not_enabled',
    'unsafe_read_envelope', 'mixed_read_operations', 'invalid_read_paths',
    'invalid_catalog_query', 'invalid_read_batch', 'read_batch_controls',
    'catalog_batch_controls',
})
_FIELDS = ('files', 'read_paths', 'python_catalog', 'complete', 'continuation', 'summary')
_TYPE_NAMES = {dict: 'object', list: 'array', str: 'string', bool: 'boolean',
               int: 'number', float: 'number', type(None): 'null'}


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate key')
        result[key] = value
    return result


def _reject_constant(value: str) -> object:
    raise ValueError('non-JSON constant')


def workspace_protocol_diagnostic(
    text: str | None, *, rejection_code: str, allow_reads: bool,
    allow_catalog: bool, native_tool_count: int = 0,
) -> dict[str, JsonValue]:
    """Describe only known field types, counts and booleans, never field values.

    The parse ceiling applies only to optional diagnostics. It cannot change the
    accepted project size, grant a read, or affect the authoritative batch parser.
    """
    raw = (text or '').encode('utf-8', errors='replace')
    result: dict[str, JsonValue] = {
        'schema_version': 1,
        'rejection_code': rejection_code if rejection_code in _REASONS else 'invalid_batch_controls',
        'response_present': text is not None,
        'response_sha256': hashlib.sha256(raw).hexdigest(),
        'response_bytes': len(raw),
        'allow_reads': allow_reads,
        'allow_catalog': allow_catalog,
        'native_tool_count': min(max(native_tool_count, 0), 100000),
    }
    if len(raw) > 65536:
        result['parse_status'] = 'omitted_size'
        return result
    try:
        value = json.loads(text or '', object_pairs_hook=_unique_object,
                           parse_constant=_reject_constant)
    except (ValueError, RecursionError):
        result['parse_status'] = 'invalid_json'
        return result
    if type(value) is not dict:
        result['parse_status'] = 'not_object'
        return result
    result['parse_status'] = 'object'
    envelope = next((name for name in ('workspace_batch', 'workspace_bundle')
                     if name in value), 'root')
    result['envelope'] = envelope
    batch = value if envelope == 'root' else value[envelope]
    if type(batch) is not dict:
        result['batch_type'] = _TYPE_NAMES.get(type(batch), 'other')
        return result
    field_types: dict[str, JsonValue] = {
        name: _TYPE_NAMES.get(type(batch[name]), 'other') for name in _FIELDS if name in batch
    }
    if field_types:
        result['field_types'] = field_types
    for field, output in (('files', 'file_count'), ('read_paths', 'read_path_count')):
        child = batch.get(field)
        if isinstance(child, dict | list):
            result[output] = len(child)
    if type(batch.get('complete')) is bool:
        result['complete'] = batch['complete']
    return result
