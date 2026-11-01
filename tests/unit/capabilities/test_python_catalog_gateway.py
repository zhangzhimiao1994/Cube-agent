"""Real inspection/policy/Harness boundaries; only SQL and profile broker transport are offline."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import pytest

from agent_hub.auth.models import Role
from agent_hub.capabilities.approvals import ApprovalService, InMemoryApprovalStore
from agent_hub.capabilities.defaults import DefaultRuntimeCapabilityPolicyGateway
from agent_hub.capabilities.runtime import RuntimeCapabilityGateway
from agent_hub.harness.tool_gateway import HarnessToolGateway
from agent_hub.harness.types import HarnessToolCallRequest, HarnessToolCallResult
from agent_hub.runs.repository import RunRepository
from agent_hub.skills.sandbox import python_project_profile as profile_api
from tests.unit.runs.test_repository_approval_upserts import ApprovalProjectionSession


def encoded(value: object) -> bytes:
    return json.dumps(value, default=dict, ensure_ascii=True, sort_keys=True, separators=(',', ':')).encode('ascii')


def configured_profile(*, large: bool) -> profile_api.PythonProjectProfile:
    identities = ([(f'catalog-package-{index:04d}', '1.0.0') for index in range(432)]
                  + [(name, f'{version}.0.0') for name in ('setuptools', 'wheel') for version in range(1, 41)])
    if not large:
        identities = [('setuptools', '80.1.0'), ('wheel', '0.45.1')]
    rows = [{'name': name, 'version': version,
             'filename': f'{name.replace("-", "_")}-{version}-py3-none-any.whl',
             'sha256': hashlib.sha256(f'{name}=={version}'.encode()).hexdigest(), 'size_bytes': 1000 + index}
            for index, (name, version) in enumerate(sorted(identities))]
    return profile_api.PythonProjectProfile.model_validate_json(json.dumps({
        'status': 'configured_not_probed',
        'interpreter': {'implementation': 'cpython', 'version': '3.12.14', 'abi': 'cp312',
            'platform': 'linux_x86_64', 'binary_sha256': hashlib.sha256(b'interpreter declaration').hexdigest(),
            'base_runtime_sha256': hashlib.sha256(b'base runtime declaration').hexdigest()},
        'catalog': rows, 'catalog_sha256': hashlib.sha256(encoded(rows)).hexdigest(),
    }), strict=True)


@dataclass
class CatalogCase:
    db: ApprovalProjectionSession
    backend: RuntimeCapabilityGateway
    harness: HarnessToolGateway
    profile: profile_api.PythonProjectProfile
    broker_calls: list[profile_api.PythonProjectProfileRequest]

    async def inspect(self, arguments: Mapping[str, Any] | None = None, *,
                      tenant: UUID | None = None, run: UUID | None = None) -> HarnessToolCallResult:
        request = HarnessToolCallRequest(run_id=self.db.run.id if run is None else run,
            actor='main_agent', tool_name='project.inspect', arguments={} if arguments is None else arguments,
            approval_required=False, sandbox='read_only', idempotency_key='catalog-inspect-' + uuid4().hex)
        return await self.harness.invoke(self.db.run.tenant_id if tenant is None else tenant, request,
            user_id=self.db.run.actor_id, role=Role.OPERATOR)


def catalog_case(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, large: bool = True,
                 foreign_broker_scope: str | None = None) -> CatalogCase:
    db = ApprovalProjectionSession()
    db.run.actor_role = Role.OPERATOR.value
    db.run.routing_decision = {'project_id': 'catalog-project', 'workspace_session_id': 'catalog-session',
        'sandbox_profile': 'workspace_write', 'permission_mode': 'default',
        'requested_permissions': ['workspace.read', 'workspace.write', 'command.run']}
    repo = RunRepository(cast(Any, db))
    backend = RuntimeCapabilityGateway(skill_store_dir=tmp_path / 'skills',
        project_workspace_dir=tmp_path / 'projects', run_repository=repo)
    policy = DefaultRuntimeCapabilityPolicyGateway(ApprovalService(InMemoryApprovalStore()), repo)
    harness = HarnessToolGateway(backend, policy_gateway=policy, require_actor_identity=True)
    profile = configured_profile(large=large)
    calls: list[profile_api.PythonProjectProfileRequest] = []

    async def broker(request: profile_api.PythonProjectProfileRequest, **_kwargs: Any) -> profile_api.PythonProjectProfileResponse:
        calls.append(request)
        invocation = request.invocation
        assert invocation.tenant_id == db.run.tenant_id and invocation.run_id == db.run.id
        assert invocation.project_id == 'catalog-project' and invocation.session_id == 'catalog-session'
        assert invocation.install_allowed is False and invocation.sandbox_profile == 'restricted'
        assert request.action == 'inspect_python_project_profile' and request.version == 6
        scope = profile_api.PythonProjectProfileScope.for_invocation(invocation)
        if foreign_broker_scope is not None:
            scope = scope.model_copy(update={foreign_broker_scope: uuid4()})
        return profile_api.PythonProjectProfileResponse(scope=scope, ok=True, profile=profile)

    # The Windows test host emulates only the supported broker transport, never a runtime probe.
    monkeypatch.setattr(profile_api, '_systemd_supported', lambda: True)
    monkeypatch.setattr(profile_api, 'request_python_project_profile_broker', broker)
    return CatalogCase(db, backend, harness, profile, calls)


def assert_observation(case: CatalogCase, result: HarnessToolCallResult) -> Mapping[str, Any]:
    assert result.status == 'succeeded', result.failure_reason
    assert result.truncated is False, 'the complete inspection envelope must survive the 64KB Harness boundary'
    assert result.payload['scope'] == {'tenant_id': str(case.db.run.tenant_id), 'run_id': str(case.db.run.id),
        'project_id': 'catalog-project', 'workspace_session_id': 'catalog-session'}
    environment = result.payload['environment']
    assert isinstance(environment, Mapping)
    assert environment['readiness'] == 'not_probed'
    profile = result.payload['python_build_profile']
    assert isinstance(profile, Mapping)
    assert profile['status'] == case.profile.status == 'configured_not_probed'
    assert profile['catalog_sha256'] == case.profile.catalog_sha256
    assert profile['interpreter'] == case.profile.interpreter.model_dump(mode='json')
    assert profile['recipe'] == 'python_offline_wheel_v1' and profile['build_backend'] == 'setuptools.build_meta'
    assert profile['build_requirement_names'] == ('setuptools', 'wheel')
    usage = profile['catalog_usage']
    assert isinstance(usage, str)
    assert 'Not an installation receipt' in usage
    assert len(encoded(profile)) <= 8192
    assert case.db.run.routing_decision is not None
    assert 'project_verification_receipt' not in case.db.run.routing_decision
    assert case.backend._project_delivery_receipts == {} and case.db.approvals == {}
    return profile


def assert_page(profile: Mapping[str, Any], *, total: int, matched: int, offset: int,
                names: tuple[str, ...]) -> int | None:
    rows, page = profile['catalog'], profile['catalog_page']
    assert len(rows) <= 24 and len(encoded(profile)) <= 8192
    assert set(page) == {'schema_version', 'total_count', 'matched_count', 'offset', 'returned_count', 'next_offset', 'names'}
    assert type(page['schema_version']) is int and page['schema_version'] == 1
    for key, expected in (('total_count', total), ('matched_count', matched), ('offset', offset), ('returned_count', len(rows))):
        assert type(page[key]) is int and page[key] == expected
    assert tuple(page['names']) == names
    assert type(profile['catalog_complete']) is bool
    expected_next = offset + len(rows) if offset + len(rows) < matched else None
    assert page['next_offset'] == expected_next
    if expected_next is not None:
        assert type(page['next_offset']) is int and expected_next > offset
    return expected_next


async def test_large_legal_profile_does_not_truncate_empty_argument_inspection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = catalog_case(tmp_path, monkeypatch)
    assert len(case.profile.catalog) == 512 and len(encoded(case.profile.model_dump(mode='json'))) > 65_536
    result = await case.inspect()
    profile = assert_observation(case, result)
    assert profile['catalog_complete'] is False
    assert_page(profile, total=512, matched=512, offset=0, names=())
    expected = [row.model_dump(mode='json') for row in case.profile.catalog[:len(profile['catalog'])]]
    assert list(profile['catalog']) == expected
    assert len(case.broker_calls) == 1


async def test_small_default_preserves_complete_catalog_and_recipe_shape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = catalog_case(tmp_path, monkeypatch, large=False)
    profile = assert_observation(case, await case.inspect())
    assert list(profile['catalog']) == [row.model_dump(mode='json') for row in case.profile.catalog]
    assert profile['requires_python'] == '>=3.12,<3.13'
    assert profile['source_directory'] == 'src' and profile['test_directory'] == 'tests'
    assert profile['test_runner'] == 'unittest'
    assert profile['lock_format'] == 'name==version --hash=sha256:actual-catalog-digest'


@pytest.mark.parametrize('names', [None, ('setuptools', 'wheel')])
async def test_catalog_queries_page_without_missing_duplicate_or_foreign_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, names: tuple[str, ...] | None,
) -> None:
    case = catalog_case(tmp_path, monkeypatch)
    expected = [row.model_dump(mode='json') for row in case.profile.catalog if names is None or row.name in names]
    offset = 0
    collected = []
    for _ in range(513):
        query: dict[str, Any] = {'catalog_sha256': case.profile.catalog_sha256, 'offset': offset}
        if names is not None:
            query['names'] = list(names)
        profile = assert_observation(case, await case.inspect({'python_catalog': query}))
        next_offset = assert_page(profile, total=512, matched=len(expected), offset=offset, names=names or ())
        collected.extend(profile['catalog'])
        if next_offset is None:
            break
        offset = next_offset
    else:
        pytest.fail('catalog pagination did not terminate')
    assert collected == expected
    assert len({(row['name'], row['version']) for row in collected}) == len(expected)
    assert len(case.broker_calls) > 1


async def test_query_optional_offset_defaults_to_zero_and_absent_name_never_returns_full_catalog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = catalog_case(tmp_path, monkeypatch)
    query = {'catalog_sha256': case.profile.catalog_sha256, 'names': ['setuptools', 'wheel']}
    implicit = assert_observation(case, await case.inspect({'python_catalog': query}))
    explicit = assert_observation(case, await case.inspect({'python_catalog': {**query, 'offset': 0}}))
    assert implicit['catalog'] == explicit['catalog'] and implicit['catalog_page'] == explicit['catalog_page']
    absent = assert_observation(case, await case.inspect({'python_catalog': {
        'catalog_sha256': case.profile.catalog_sha256, 'names': ['not-in-this-catalog']}}))
    assert absent['catalog'] == ()
    assert_page(absent, total=512, matched=0, offset=0, names=('not-in-this-catalog',))


@pytest.mark.parametrize('defect', [
    {'catalog_sha256': '0' * 64}, {'catalog_sha256': 'a' * 63}, {'catalog_sha256': True},
    {'names': []}, {'names': [f'package-{index}' for index in range(17)]},
    {'names': ['Wheel']}, {'names': ['some_package']}, {'names': ['../wheel']},
    {'names': ['/wheel']}, {'names': ['C:/wheel']}, {'names': ['wheel.whl']},
    {'names': ['wheel', 'wheel']}, {'names': 'wheel'}, {'names': [True]},
    {'offset': True}, {'offset': False}, {'offset': -1}, {'offset': 1.0}, {'offset': '0'},
    {'path': '/private/catalog'}, {'limit': 1000},
])
async def test_catalog_query_rejects_malformed_or_stale_selection_without_catalog_leak(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, defect: dict[str, Any],
) -> None:
    case = catalog_case(tmp_path, monkeypatch)
    result = await case.inspect({'python_catalog': {'catalog_sha256': case.profile.catalog_sha256, **defect}})
    assert result.status == 'failed' and result.truncated is False
    assert 'python_build_profile' not in result.payload and 'catalog' not in result.payload
    assert case.profile.catalog[0].filename not in repr(result.payload)
    assert case.backend._project_delivery_receipts == {} and case.db.approvals == {}


@pytest.mark.parametrize('arguments', [{}, {'python_catalog': {}}, {'python_catalog': None},
    {'python_catalog': {'catalog_sha256': 'placeholder'}, 'project_id': 'other-project'}])
async def test_missing_digest_and_extra_top_level_fields_are_not_query_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, arguments: dict[str, Any],
) -> None:
    case = catalog_case(tmp_path, monkeypatch)
    if not arguments:
        arguments = {'python_catalog': {'offset': 0}}
    if 'project_id' in arguments:
        arguments = {**arguments, 'python_catalog': {'catalog_sha256': case.profile.catalog_sha256}}
    result = await case.inspect(arguments)
    assert result.status == 'failed' and 'python_build_profile' not in result.payload


@pytest.mark.parametrize('foreign', ['tenant', 'run'])
async def test_foreign_request_scope_never_reaches_catalog_broker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, foreign: str,
) -> None:
    case = catalog_case(tmp_path, monkeypatch)
    result = await case.inspect(tenant=uuid4() if foreign == 'tenant' else None,
                                run=uuid4() if foreign == 'run' else None)
    assert result.status == 'failed' and result.payload == {}
    assert case.broker_calls == []


@pytest.mark.parametrize('foreign', ['tenant_id', 'run_id'])
async def test_foreign_broker_scope_cannot_publish_pins_or_catalog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, foreign: str,
) -> None:
    case = catalog_case(tmp_path, monkeypatch, foreign_broker_scope=foreign)
    result = await case.inspect()
    assert result.status == 'succeeded' and result.truncated is False
    profile = result.payload['python_build_profile']
    assert isinstance(profile, Mapping)
    assert profile['status'] == 'unavailable'
    assert 'catalog' not in profile
    assert case.profile.catalog_sha256 not in repr(result.payload)
    assert len(case.broker_calls) == 1
