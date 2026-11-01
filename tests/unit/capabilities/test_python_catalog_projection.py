"""Pure public catalog projection; no broker, workspace, downloads or model calls."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import FrozenInstanceError

import pytest

from agent_hub.capabilities import project_inspection as inspection
from agent_hub.skills.sandbox.python_project_profile import (
    PythonProjectProfile,
    PythonProjectProfileInterpreter,
    PythonProjectProfileWheel,
)
from tests.unit.capabilities.catalog_product_delivery_typed_fixture import (
    construct_query,
    parse_query,
)
from tests.unit.runs.project_auto_repair_typed_fixture import mapping
from tests.unit.runs.reviewed_report_revision_typed_fixture import (
    integer_value,
    object_array,
    object_map,
    string_value,
)

_DIGEST = 'd' * 64
_CORE_BYTES = 6144


def encoded_size(value: object) -> int:
    return len(json.dumps(value, ensure_ascii=False, allow_nan=False,
                          sort_keys=True, separators=(',', ':')).encode('utf-8'))


def profile(count: int = 3, *, long: bool = False) -> PythonProjectProfile:
    wheels = []
    for index in range(count):
        name = f'fixture{index:04d}' + ('-' + 'x' * 110 if long else '')
        version = '1.' + '2' * 75 if long else '1.0.0'
        wheels.append(PythonProjectProfileWheel(
            name=name, version=version,
            filename=f'{name.replace("-", "_")}-{version}-py3-none-any.whl',
            sha256=f'{index + 1:064x}', size_bytes=1,
        ))
    return PythonProjectProfile(
        interpreter=PythonProjectProfileInterpreter(
            implementation='cpython', version='3.12.321', abi='cp312', platform='linux_aarch64',
            binary_sha256='a' * 64, base_runtime_sha256='b' * 64,
        ), catalog=tuple(wheels), catalog_sha256=_DIGEST,
    )


def profile_at_size(target: int) -> PythonProjectProfile:
    payload = object_map(profile(28).model_dump(mode='json'))
    gap = target - encoded_size(payload)
    assert gap >= 0
    for item in object_array(payload['catalog']):
        wheel = object_map(item)
        added = min(100, gap // 2)
        wheel['version'] = string_value(wheel['version']) + '0' * added
        wheel['filename'] = f'{wheel["name"]}-{wheel["version"]}-py3-none-any.whl'
        gap -= 2 * added
    if gap == 1:
        object_map(object_array(payload['catalog'])[0])['size_bytes'] = 10
        gap -= 1
    assert gap == 0 and encoded_size(payload) == target
    return PythonProjectProfile.model_validate_json(json.dumps(payload))


def test_catalog_query_and_projection_api_exists() -> None:
    assert callable(getattr(inspection, 'PythonCatalogQuery', None))
    assert callable(getattr(inspection, 'parse_python_catalog_query', None))
    assert callable(getattr(inspection, 'python_catalog_projection', None))


def test_empty_arguments_keep_initial_inspection() -> None:
    assert parse_query({}) is None


@pytest.mark.parametrize('names', [['fixture0000', 'fixture0002'], ('fixture0000', 'fixture0002')])
def test_query_is_frozen_and_roundtrips_canonical_tool_arguments(names: list[str] | tuple[str, ...]) -> None:
    query = parse_query({
        'python_catalog': {'catalog_sha256': _DIGEST, 'names': names},
    })
    assert type(query) is inspection.PythonCatalogQuery
    assert query.catalog_sha256 == _DIGEST and query.offset == 0
    assert query.names == ('fixture0000', 'fixture0002')
    assert query.to_payload() == {'python_catalog': {
        'catalog_sha256': _DIGEST, 'offset': 0, 'names': ('fixture0000', 'fixture0002'),
    }}
    assert parse_query(query.to_payload()) == query
    with pytest.raises(FrozenInstanceError):
        query.__setattr__('offset', 1)


@pytest.mark.parametrize('offset', [0, 1000])
def test_query_without_names_means_unfiltered_and_omits_empty_names(offset: int) -> None:
    query = inspection.PythonCatalogQuery(_DIGEST, offset=offset)
    assert query.to_payload() == {'python_catalog': {'catalog_sha256': _DIGEST, 'offset': offset}}
    assert parse_query(query.to_payload()) == query


@pytest.mark.parametrize('arguments', [
    {'other': {}}, {'python_catalog': {}, 'extra': 1},
    {'python_catalog': None}, {'python_catalog': []}, {'python_catalog': {}},
    {'python_catalog': {'catalog_sha256': True}},
    {'python_catalog': {'catalog_sha256': 'A' * 64}},
    {'python_catalog': {'catalog_sha256': 'a' * 63}},
    {'python_catalog': {'catalog_sha256': ' ' + _DIGEST}},
    {'python_catalog': {'catalog_sha256': _DIGEST, 'extra': 1}},
    *({'python_catalog': {'catalog_sha256': _DIGEST, 'offset': value}}
      for value in (True, False, 0.0, 1.5, -1, 1001, '0', None)),
    *({'python_catalog': {'catalog_sha256': _DIGEST, 'names': value}}
      for value in ([], (), None, 'wheel', [True], [1], ['Wheel'], ['my_pkg'], ['my.pkg'],
                    ['a--b'], ['-wheel'], ['wheel-'], [''], ['a' * 129],
                    ['wheel', 'setuptools'], ['wheel', 'wheel'],
                    [f'fixture{i:04d}' for i in range(17)])),
])
def test_malformed_queries_fail_with_a_finite_error(arguments: object) -> None:
    with pytest.raises(inspection.ProjectInspectionError, match='^python_catalog_query_invalid$'):
        parse_query(arguments)


@pytest.mark.parametrize('changes', [
    {'offset': True}, {'offset': 1.0}, {'offset': -1}, {'offset': 1001},
    {'names': ['wheel']}, {'names': ('Wheel',)}, {'names': ('wheel', 'wheel')},
    {'names': ('wheel', 'setuptools')}, {'catalog_sha256': True},
])
def test_direct_dto_construction_cannot_bypass_query_validation(changes: dict[str, object]) -> None:
    with pytest.raises(inspection.ProjectInspectionError, match='^python_catalog_query_invalid$'):
        construct_query({'catalog_sha256': _DIGEST, **changes})


@pytest.mark.parametrize('size', [None, _CORE_BYTES - 1, _CORE_BYTES])
def test_small_initial_profile_preserves_entire_original_shape(size: int | None) -> None:
    value = profile() if size is None else profile_at_size(size)
    original = value.model_dump(mode='json')
    projected = inspection.python_catalog_projection(value)
    assert projected == original
    assert set(projected) == set(original)
    assert encoded_size(projected) <= _CORE_BYTES
    assert value.model_dump(mode='json') == original


@pytest.mark.parametrize('count', [25, 512, 1000])
def test_explicit_query_is_bounded_even_if_full_profile_would_fit(count: int) -> None:
    value = profile(count)
    result = inspection.python_catalog_projection(value, inspection.PythonCatalogQuery(_DIGEST))
    assert len(object_array(result['catalog'])) == 24
    assert result['catalog_complete'] is False
    assert mapping(result['catalog_page']) == {
        'schema_version': 1, 'total_count': count, 'matched_count': count,
        'offset': 0, 'returned_count': 24, 'next_offset': 24, 'names': (),
    }
    assert encoded_size(result) <= _CORE_BYTES


def test_large_initial_profile_pages_instead_of_changing_authoritative_metadata() -> None:
    value = profile_at_size(_CORE_BYTES + 1)
    original = value.model_dump(mode='json')
    result = inspection.python_catalog_projection(value)
    assert set(result) == {*original, 'catalog_complete', 'catalog_page'}
    for key in ('status', 'interpreter', 'catalog_sha256'):
        assert result[key] == original[key]
    assert result['catalog_sha256'] == _DIGEST
    assert len(object_array(result['catalog'])) <= 24 and encoded_size(result) <= _CORE_BYTES
    assert result['catalog_complete'] is False
    assert value.model_dump(mode='json') == original


def test_all_pages_reconstruct_catalog_exactly_without_overlap_or_lost_hashes() -> None:
    value = profile(1000)
    original = value.model_dump(mode='json')
    rows: list[object] = []
    offset = 0
    while True:
        result = inspection.python_catalog_projection(value, inspection.PythonCatalogQuery(_DIGEST, offset))
        page = mapping(result['catalog_page'])
        assert page['offset'] == offset and page['returned_count'] == len(object_array(result['catalog']))
        assert page['total_count'] == page['matched_count'] == 1000
        assert 1 <= len(object_array(result['catalog'])) <= 24 and encoded_size(result) <= _CORE_BYTES
        assert result['catalog_complete'] is False
        rows.extend(object_array(result['catalog']))
        next_offset = page['next_offset']
        if next_offset is None:
            break
        assert next_offset == offset + len(object_array(result['catalog']))
        offset = integer_value(next_offset)
    assert rows == original['catalog']
    assert value.model_dump(mode='json') == original


def test_name_filter_offsets_are_in_the_filtered_catalog_not_the_global_catalog() -> None:
    value = profile(10)
    names = ('fixture0002', 'fixture0007', 'missing-package')
    query = inspection.PythonCatalogQuery(_DIGEST, offset=1, names=names)
    result = inspection.python_catalog_projection(value, query)
    assert object_array(result['catalog']) == [value.catalog[7].model_dump(mode='json')]
    assert mapping(result['catalog_page']) == {
        'schema_version': 1, 'total_count': 10, 'matched_count': 2,
        'offset': 1, 'returned_count': 1, 'next_offset': None, 'names': names,
    }
    assert result['catalog_complete'] is False


def test_filtered_match_on_all_rows_is_not_a_complete_unfiltered_catalog() -> None:
    value = profile(1)
    result = inspection.python_catalog_projection(
        value, inspection.PythonCatalogQuery(_DIGEST, names=('fixture0000',)),
    )
    assert len(object_array(result['catalog'])) == 1 and result['catalog_complete'] is False


def test_small_explicit_unfiltered_page_can_attest_complete_catalog() -> None:
    result = inspection.python_catalog_projection(profile(), inspection.PythonCatalogQuery(_DIGEST))
    assert result['catalog_complete'] is True
    assert mapping(result['catalog_page'])['next_offset'] is None


@pytest.mark.parametrize('offset,names', [(0, ('missing-package',)), (3, ())])
def test_no_matches_and_exact_end_cursor_are_valid_empty_pages(offset: int, names: tuple[str, ...]) -> None:
    result = inspection.python_catalog_projection(profile(), inspection.PythonCatalogQuery(_DIGEST, offset, names))
    page = mapping(result['catalog_page'])
    assert object_array(result['catalog']) == [] and page['returned_count'] == 0 and page['next_offset'] is None
    assert page['matched_count'] == (0 if names else 3)
    assert result['catalog_complete'] is False


@pytest.mark.parametrize('offset,names', [(4, ()), (2, ('fixture0000',)), (1, ('missing-package',))])
def test_offset_cannot_skip_beyond_filtered_matches(offset: int, names: tuple[str, ...]) -> None:
    with pytest.raises(inspection.ProjectInspectionError, match='^python_catalog_offset_invalid$'):
        inspection.python_catalog_projection(profile(), inspection.PythonCatalogQuery(_DIGEST, offset, names))


def test_query_digest_must_match_root_digest_not_a_hash_of_the_public_projection() -> None:
    with pytest.raises(inspection.ProjectInspectionError, match='^python_catalog_changed$'):
        inspection.python_catalog_projection(profile(), inspection.PythonCatalogQuery('e' * 64))


@pytest.mark.parametrize('filtered', [False, True])
def test_byte_limit_covers_rows_and_all_page_metadata(filtered: bool) -> None:
    value = profile(40, long=True)
    names = tuple(wheel.name for wheel in value.catalog[:16]) if filtered else ()
    result = inspection.python_catalog_projection(value, inspection.PythonCatalogQuery(_DIGEST, names=names))
    count = len(object_array(result['catalog']))
    page = mapping(result['catalog_page'])
    assert 0 < count < (16 if filtered else 24)
    assert encoded_size(result) <= _CORE_BYTES
    assert page['next_offset'] == count and page['names'] == names
    # Including one more complete row plus its updated metadata must exceed the limit.
    larger = {**result, 'catalog': [wheel.model_dump(mode='json') for wheel in value.catalog[:count + 1]],
              'catalog_page': {**page, 'returned_count': count + 1, 'next_offset': count + 1}}
    assert encoded_size(larger) > _CORE_BYTES
    assert all(set(object_map(row)) == {'name', 'version', 'filename', 'sha256', 'size_bytes'} for row in object_array(result['catalog']))


def test_unpageable_metadata_fails_closed_without_returning_an_empty_looping_page() -> None:
    value = profile().model_copy(update={
        'interpreter': profile().interpreter.model_copy(update={'version': 'x' * _CORE_BYTES}),
    })
    with pytest.raises(inspection.ProjectInspectionError, match='^python_catalog_projection_limit$'):
        inspection.python_catalog_projection(value)


def test_projection_is_deterministic_and_not_a_live_view_of_the_frozen_profile() -> None:
    value = profile(100)
    query = inspection.PythonCatalogQuery(_DIGEST)
    original = value.model_dump(mode='json')
    first = inspection.python_catalog_projection(value, query)
    assert first == inspection.python_catalog_projection(value, query)
    assert isinstance(first, Mapping)
    object_map(object_array(first['catalog'])[0])['sha256'] = '0' * 64
    assert value.model_dump(mode='json') == original
