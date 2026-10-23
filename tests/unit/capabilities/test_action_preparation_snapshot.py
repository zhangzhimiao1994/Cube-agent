"""Root preparation snapshots are continuity metadata, never execution authority.

Local controls and typed records are real; SQL and Python Root transport are doubles.
"""
from __future__ import annotations

import importlib
import json
import re
from collections.abc import Mapping
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from agent_hub.capabilities.execution_authority import bind_execution_authority
from agent_hub.capabilities.project_verification import ProjectVerificationError
from agent_hub.capabilities.runtime import RuntimeCapabilityGateway
from agent_hub.files.workspace import ProjectWorkspaceStore
from agent_hub.runs.project_verification_receipts import RECEIPT_KEY
from tests.unit.capabilities.test_generated_project_service import setup as generated_setup
from tests.unit.capabilities.test_project_original_verification import service as original_setup
from tests.unit.capabilities.test_python_durable_verification import Runs
from tests.unit.capabilities.test_python_generated_service import configured as python_setup
from tests.unit.capabilities.test_scoped_read import RUN, TENANT, project_path
from tests.unit.capabilities.test_verification_cold_recovery import Fixture
from tests.unit.capabilities.test_verification_cold_recovery import Runs as FixtureRuns


class SnapshotRuns(Runs, FixtureRuns):
    """Preparation helpers with the base Fixture's repository contract."""


def gateway(fixture: Fixture) -> RuntimeCapabilityGateway:
    return RuntimeCapabilityGateway(skill_store_dir=fixture.root.parent / 'skills',
        project_workspace_dir=fixture.root, generated_artifact_dir=fixture.root.parent / 'downloads',
        run_repository=fixture.runs, project_executor=fixture.executor,
        artifact_repository=fixture.artifacts, receipt_repository=fixture.db.repository())


def ordinary(tmp_path: Path) -> Fixture:
    fixture = Fixture(tmp_path)
    fixture.runs = SnapshotRuns(fixture.db)
    return fixture


async def generated(tmp_path: Path, *, captured: bool = True) -> Fixture:
    root_service, previous = generated_setup(tmp_path / 'generated')
    fixture = ordinary(tmp_path / 'ordinary')
    fixture.root = root_service.root
    fixture.db.row.actor_id = previous.row.actor_id
    fixture.db.row.request = previous.row.request
    fixture.db.row.routing_decision = deepcopy(previous.row.routing_decision)
    if captured:
        await fixture.verifier().generated.prepare(TENANT, RUN, 'main_agent',
                                                   user_id=fixture.db.row.actor_id)
    return fixture


def files(root: Path) -> dict[Path, bytes]:
    return {path.relative_to(root): path.read_bytes() for path in root.rglob('*') if path.is_file()}


def readonly(backend: RuntimeCapabilityGateway, fixture: Fixture,
             monkeypatch: pytest.MonkeyPatch) -> None:
    owner = backend._project_verification
    assert owner is not None

    async def forbidden(*args: Any, **kwargs: Any) -> Any:
        pytest.fail('snapshot attempted preparation, supply, execution, persistence or approval')

    for target, methods in (
        (backend, ('execute', 'project_verification_static_authorization')),
        (owner, ('verify', '_verify', '_begin_verification', 'authorization_request')),
        (owner.generated, ('prepare', 'update')),
        (owner._python, ('prepare', 'verify', '_run', '_approval')),
        (owner._python_generated, ('_prepare', '_supply', '_plan', 'verify', '_approved')),
        (fixture.executor, ('run',)),
        (fixture.runs, ('compare_and_set_generated_preparation',
                        'is_capability_approval_scope_approved')),
        (fixture.artifacts, ('reserve_write', 'put', 'abort_write')),
    ):
        for method in methods:
            monkeypatch.setattr(target, method, forbidden)
    for executor in (owner._python.executor, owner._python_generated.executor):
        if executor is not None:
            for method in ('run', 'probe', 'inspect_environment'):
                if hasattr(executor, method):
                    monkeypatch.setattr(executor, method, forbidden)


def no_authority(fixture: Fixture, backend: RuntimeCapabilityGateway) -> None:
    owner = backend._project_verification
    assert owner is not None
    assert not any((owner._receipts, owner._original_receipts, owner._generated_receipts,
                    owner._python.receipts, owner._python_generated.receipts))
    assert RECEIPT_KEY not in fixture.db.row.routing_decision
    assert not fixture.artifacts._artifacts
    assert fixture.executor.calls == []


async def test_generated_root_frame_is_complete_readonly_and_stable_on_fresh_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = await generated(tmp_path)
    before_route, before_files = deepcopy(fixture.db.row.routing_decision), files(fixture.root)
    expected_contracts = {key: before_route[key] for key in (
        'generated_project_contract_version', 'generated_project_preparation')}
    backend = gateway(fixture)
    readonly(backend, fixture, monkeypatch)
    with fixture.bind():
        frame = await backend.action_preparation_snapshot(fixture.context())
    assert set(frame) == {'schema_version', 'scope', 'contracts', 'workspace_sha256'}
    assert frame['schema_version'] == 1
    assert frame['scope'] == fixture.db.authority().stable_scope()
    assert json.loads(json.dumps(frame['contracts'], default=dict)) == expected_contracts
    workspace_sha256 = frame['workspace_sha256']
    assert isinstance(workspace_sha256, str)
    assert re.fullmatch('[a-f0-9]{64}', workspace_sha256)
    assert all(name not in json.dumps(frame, default=dict)
               for name in ('execution_token', 'worker_id', 'artifact_references'))
    fixture.db.new_lease()
    fresh = gateway(fixture)
    readonly(fresh, fixture, monkeypatch)
    with fixture.bind():
        assert await fresh.action_preparation_snapshot(fixture.context()) == frame
    assert fixture.db.row.routing_decision == before_route
    assert files(fixture.root) == before_files
    no_authority(fixture, backend)
    no_authority(fixture, fresh)


async def test_missing_trusted_binding_fails_before_reading_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = ordinary(tmp_path)
    backend = gateway(fixture)
    readonly(backend, fixture, monkeypatch)

    def forbidden_root(*args: Any, **kwargs: Any) -> Any:
        pytest.fail('unbound caller reached Root')

    monkeypatch.setattr(ProjectWorkspaceStore, 'list_files', forbidden_root)
    with pytest.raises(ProjectVerificationError):
        await backend.action_preparation_snapshot(fixture.context())
    no_authority(fixture, backend)


@pytest.mark.parametrize('lease', ['expired', 'replaced'])
async def test_live_record_cannot_refresh_an_expired_or_replaced_bound_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, lease: str,
) -> None:
    fixture = ordinary(tmp_path)
    backend = gateway(fixture)
    readonly(backend, fixture, monkeypatch)
    context = fixture.context()
    with fixture.bind():
        if lease == 'expired':
            fixture.db.row.worker_lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        else:
            fixture.db.new_lease()
        with pytest.raises(ProjectVerificationError):
            await backend.action_preparation_snapshot(context)
    no_authority(fixture, backend)


@pytest.mark.parametrize('scope', ['tenant_id', 'run_id'])
async def test_valid_other_scope_record_cannot_use_this_workers_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scope: str,
) -> None:
    fixture = ordinary(tmp_path)
    backend = gateway(fixture)
    readonly(backend, fixture, monkeypatch)
    context = fixture.context()
    with fixture.bind():
        other = uuid4()
        setattr(fixture.db.row, 'tenant_id' if scope == 'tenant_id' else 'id', other)
        context = context.model_copy(update={scope: other})
        with pytest.raises(ProjectVerificationError):
            await backend.action_preparation_snapshot(context)
    no_authority(fixture, backend)


@pytest.mark.parametrize('defect', ['reference', 'protected_source'])
async def test_generated_frame_rejects_bad_root_reference_or_changed_protected_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, defect: str,
) -> None:
    fixture = await generated(tmp_path)
    if defect == 'reference':
        state = fixture.db.row.routing_decision['generated_project_preparation']
        state['reference']['contract_sha256'] = 'f' * 64
    else:
        (fixture.root / project_path() / 'test/business.test.cjs').write_text(
            'require("node:test").test("replacement",()=>{});', encoding='utf-8')
    before_route, before_files = deepcopy(fixture.db.row.routing_decision), files(fixture.root)
    backend = gateway(fixture)
    readonly(backend, fixture, monkeypatch)
    with fixture.bind(), pytest.raises(ProjectVerificationError):
        await backend.action_preparation_snapshot(fixture.context())
    assert fixture.db.row.routing_decision == before_route
    assert files(fixture.root) == before_files
    no_authority(fixture, backend)


async def test_original_scope_change_on_final_route_read_rejects_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    original, _, _, invocation, _ = await original_setup(tmp_path / 'original')
    record = await original.repository.get(invocation.tenant_id, invocation.run_id)
    fixture = ordinary(tmp_path / 'ordinary')
    fixture.root = original.root
    fixture.db.row.tenant_id, fixture.db.row.id = invocation.tenant_id, invocation.run_id
    if record.actor_id is not None:
        fixture.db.row.actor_id = record.actor_id
    fixture.db.row.routing_decision = json.loads(json.dumps(record.routing_decision, default=dict))
    context = fixture.context().model_copy(update={
        'tenant_id': invocation.tenant_id, 'run_id': invocation.run_id})
    backend = gateway(fixture)
    owner = backend._project_verification
    assert owner is not None
    readonly(backend, fixture, monkeypatch)
    with bind_execution_authority(context, worker_id=fixture.db.row.worker_id):
        valid = await backend.action_preparation_snapshot(context)
    contracts = valid['contracts']
    assert isinstance(contracts, Mapping)
    assert set(contracts) == {
        'project_input_contract_version', 'project_input', 'project_input_preparation'}
    root_loaded = False
    original_scope, original_get = owner._original_scope, fixture.runs.get

    async def root_scope(*args: Any, **kwargs: Any) -> Any:
        nonlocal root_loaded
        value = await original_scope(*args, **kwargs)
        assert value is not None
        root_loaded = True
        return value

    async def get(*args: Any, **kwargs: Any) -> Any:
        if root_loaded:
            state = fixture.db.row.routing_decision['project_input_preparation']
            state['observation_sha256'] = 'f' * 64
        return await original_get(*args, **kwargs)

    monkeypatch.setattr(owner, '_original_scope', root_scope)
    monkeypatch.setattr(fixture.runs, 'get', get)
    with (
        bind_execution_authority(context, worker_id=fixture.db.row.worker_id),
        pytest.raises(ProjectVerificationError),
    ):
        await backend.action_preparation_snapshot(context)
    assert root_loaded
    no_authority(fixture, backend)


async def test_workspace_change_between_root_digests_rejects_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = await generated(tmp_path)
    backend = gateway(fixture)
    readonly(backend, fixture, monkeypatch)
    original_list = ProjectWorkspaceStore.list_files
    reads = 0

    def list_files(*args: Any, **kwargs: Any) -> Any:
        nonlocal reads
        result = original_list(*args, **kwargs)
        reads += 1
        if reads == 1:
            (fixture.root / project_path() / 'src/index.js').write_text(
                'exports.add=(a,b)=>Number(a)+Number(b);', encoding='utf-8')
        return result

    monkeypatch.setattr(ProjectWorkspaceStore, 'list_files', list_files)
    with fixture.bind(), pytest.raises(ProjectVerificationError):
        await backend.action_preparation_snapshot(fixture.context())
    assert reads == 2
    no_authority(fixture, backend)


async def test_uncaptured_generated_scope_remains_uncaptured_after_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = await generated(tmp_path, captured=False)
    before_route, before_files = deepcopy(fixture.db.row.routing_decision), files(fixture.root)
    backend = gateway(fixture)
    readonly(backend, fixture, monkeypatch)
    with fixture.bind():
        frame = await backend.action_preparation_snapshot(fixture.context())
    assert frame['contracts'] == {'generated_project_contract_version': 1}
    assert fixture.db.row.routing_decision == before_route
    assert files(fixture.root) == before_files
    no_authority(fixture, backend)


async def test_python_ready_root_recipe_is_read_without_probe_supply_or_new_approval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = importlib.import_module('agent_hub.capabilities.python_generated_service')
    (tmp_path / 'python').mkdir()
    python, _, previous, executor, supply, captured = await python_setup(api, tmp_path / 'python')
    ready, _ = await python._prepare(captured, 'main_agent')
    assert ready.state['phase'] == 'python_ready'
    fixture = ordinary(tmp_path / 'ordinary')
    fixture.root = python.owner.root
    fixture.db.row.actor_id = previous.row.actor_id
    fixture.db.row.request = previous.row.request
    fixture.db.row.routing_decision = deepcopy(previous.row.routing_decision)
    backend = gateway(fixture)
    before_route, before_files = deepcopy(fixture.db.row.routing_decision), files(fixture.root)
    readonly(backend, fixture, monkeypatch)

    async def forbidden(*args: Any, **kwargs: Any) -> Any:
        pytest.fail('ready snapshot invoked native transport or supply')

    for target, methods in ((executor, ('probe', 'run', 'inspect_environment')),
                            (supply, ('plan', 'supply', 'inspect'))):
        for method in methods:
            monkeypatch.setattr(target, method, forbidden)
    with fixture.bind():
        frame = await backend.action_preparation_snapshot(fixture.context())
    contracts = frame['contracts']
    assert isinstance(contracts, Mapping)
    preparation = contracts['generated_project_preparation']
    assert isinstance(preparation, Mapping)
    assert json.loads(json.dumps(preparation, default=dict)) == before_route['generated_project_preparation']
    assert fixture.db.row.routing_decision == before_route
    assert files(fixture.root) == before_files
    no_authority(fixture, backend)
