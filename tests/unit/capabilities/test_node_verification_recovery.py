"""Cold Node receipts use real scopes and frozen files, without executing again."""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol, TypedDict, cast
from uuid import UUID, uuid4

import pytest

from agent_hub.capabilities.project_verification import (
    ProjectVerificationError,
    ProjectVerificationService,
)
from agent_hub.skills.sandbox.project import ProjectInvocation
from tests.unit.capabilities.test_generated_project_delivery import configured as generated
from tests.unit.capabilities.test_project_original_verification import service as original
from tests.unit.capabilities.test_project_verification import Executor, gateway

Kind = Literal['node_plain', 'node_original', 'node_generated']
KINDS: tuple[Kind, ...] = ('node_plain', 'node_original', 'node_generated')


class MutableRun(Protocol):
    routing_decision: dict[str, object]
    request: str


class MutableRepository(Protocol):
    async def get(self, tenant_id: UUID, run_id: UUID) -> MutableRun: ...


class PlainPayload(TypedDict):
    verification: dict[str, object]
    summary: str


class NoExecution:
    async def run(self, invocation: ProjectInvocation) -> Any:
        pytest.fail('cold recovery must not execute a project')


@dataclass
class Case:
    owner: ProjectVerificationService
    payload: dict[str, Any]
    invocation: ProjectInvocation
    source: str
    kind: Kind

    @property
    def key(self) -> tuple[UUID, UUID]:
        return self.invocation.tenant_id, self.invocation.run_id

    async def restore(self, **changes: Any) -> None:
        from agent_hub.capabilities.node_verification_recovery import restore_node_verification

        await restore_node_verification(self.owner, **{
            'kind': self.kind, 'tenant_id': self.key[0], 'run_id': self.key[1],
            'actor': 'engineer', 'payload': self.payload,
            'source_sha256': self.source, 'execution_id': self.invocation.execution_id,
            **changes,
        })


async def cold_case(tmp_path: Path, kind: Kind, monkeypatch: pytest.MonkeyPatch) -> Case:
    from tests.unit.capabilities.test_scoped_read import RUN, TENANT

    tenant, run = TENANT, RUN
    if kind == 'node_plain':
        warm = gateway(tmp_path, Executor())._project_verification
    elif kind == 'node_original':
        warm, _, _, initial, _ = await original(tmp_path)
        tenant, run = initial.tenant_id, initial.run_id
    else:
        backend, _ = await generated(tmp_path)
        warm = backend._project_verification
    assert warm is not None
    result = await warm.verify(tenant_id=tenant, run_id=run, actor='engineer')
    invocation, source = warm._receipts[(tenant, run)]
    owner = ProjectVerificationService(root=warm.root, repository=warm.repository,
                                       executor=NoExecution())

    async def forbidden(*args: Any, **kwargs: Any) -> Any:
        pytest.fail('cold recovery must not prepare or change routing')

    monkeypatch.setattr(owner.generated, 'prepare', forbidden)
    monkeypatch.setattr(owner._python, 'prepare', forbidden)
    if hasattr(owner.repository, 'compare_and_set_generated_preparation'):
        monkeypatch.setattr(owner.repository, 'compare_and_set_generated_preparation', forbidden)
    return Case(owner, json.loads(json.dumps(result, default=dict)), invocation, source, kind)


def caches(owner: ProjectVerificationService) -> tuple[dict[Any, Any], ...]:
    return (owner._receipts, owner._original_receipts, owner._generated_receipts,
            owner._python.receipts, owner._python_generated.receipts)


@pytest.mark.parametrize('kind', KINDS)
async def test_cold_node_receipt_restores_without_execution_or_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: Kind,
) -> None:
    case = await cold_case(tmp_path, kind, monkeypatch)
    record = await cast(MutableRepository, case.owner.repository).get(*case.key)
    route = json.dumps(record.routing_decision, default=dict, sort_keys=True)
    files = {p: p.read_bytes() for p in case.owner.root.rglob('*') if p.is_file()}
    await case.restore()
    assert await case.owner.require_current_receipt(*case.key) == case.source
    assert case.owner._receipts[case.key][0].execution_id == case.invocation.execution_id
    if kind == 'node_original':
        assert case.owner.original_receipt_binding(*case.key) == case.payload['original_check_contract']
    if kind == 'node_generated':
        assert case.key in case.owner._generated_receipts
    assert json.dumps(record.routing_decision, default=dict, sort_keys=True) == route
    assert {p: p.read_bytes() for p in case.owner.root.rglob('*') if p.is_file()} == files


@pytest.mark.parametrize('kind', KINDS)
async def test_source_drift_rejects_and_clears_only_this_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: Kind,
) -> None:
    case = await cold_case(tmp_path, kind, monkeypatch)
    await case.restore()
    other = (case.key[0], uuid4())
    for cache in caches(case.owner):
        cache[other] = 'other run'
    invocation = case.invocation
    source = (case.owner.root / str(invocation.tenant_id) / 'projects' / invocation.project_id
              / 'sessions' / invocation.session_id)
    (source / 'new-file.js').write_text('changed', encoding='utf-8')
    with pytest.raises(ProjectVerificationError):
        await case.restore()
    assert all(case.key not in cache and cache[other] == 'other run' for cache in caches(case.owner))
    with pytest.raises(ProjectVerificationError):
        await case.owner.require_current_receipt(*case.key)


@pytest.mark.parametrize('kind', KINDS)
@pytest.mark.parametrize('change', ['kind', 'source', 'execution', 'receipt'])
async def test_wrong_kind_or_receipt_identity_cannot_restore(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: Kind, change: str,
) -> None:
    case = await cold_case(tmp_path, kind, monkeypatch)
    changes: dict[str, Any] = {}
    if change == 'kind':
        changes['kind'] = 'node_original' if kind != 'node_original' else 'node_plain'
    elif change == 'source':
        changes['source_sha256'] = 'f' * 64
    elif change == 'execution':
        changes['execution_id'] = '../invalid'
    else:
        case.payload['verification']['checks'][0]['execution_diagnostic']['exit_code'] = 1
    with pytest.raises(ProjectVerificationError):
        await case.restore(**changes)
    assert all(case.key not in cache for cache in caches(case.owner))


@pytest.mark.parametrize('kind', KINDS)
async def test_current_permissions_are_rechecked_before_restoring(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: Kind,
) -> None:
    case = await cold_case(tmp_path, kind, monkeypatch)
    record = await cast(MutableRepository, case.owner.repository).get(*case.key)
    cast(list[str], record.routing_decision['requested_permissions']).remove('command.run')
    with pytest.raises(ProjectVerificationError):
        await case.restore()
    assert all(case.key not in cache for cache in caches(case.owner))


@pytest.mark.parametrize('field', ['baseline_observation_sha256', 'baseline_envelope_sha256'])
async def test_original_binding_is_rebuilt_from_current_baseline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: str,
) -> None:
    case = await cold_case(tmp_path, 'node_original', monkeypatch)
    case.payload['original_check_contract'][field] = 'f' * 64
    with pytest.raises(ProjectVerificationError):
        await case.restore()
    assert all(case.key not in cache for cache in caches(case.owner))


@pytest.mark.parametrize('field', ['names', 'count', 'manifest_sha256', 'test_evidence_sha256',
                                  'acceptance_sha256'])
async def test_generated_named_evidence_and_envelope_are_required(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: str,
) -> None:
    case = await cold_case(tmp_path, 'node_generated', monkeypatch)
    binding = case.payload['generated_project_acceptance']
    if field == 'acceptance_sha256':
        binding[field] = 'f' * 64
    else:
        binding['test_execution'][field] = ['different test'] if field == 'names' else (
            0 if field == 'count' else 'f' * 64)
    with pytest.raises(ProjectVerificationError):
        await case.restore()
    assert all(case.key not in cache for cache in caches(case.owner))


@pytest.mark.parametrize('kind', KINDS)
@pytest.mark.parametrize('cancelled', [False, True])
async def test_final_current_receipt_failure_clears_all_run_caches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: Kind, cancelled: bool,
) -> None:
    case = await cold_case(tmp_path, kind, monkeypatch)

    async def final_check(tenant: UUID, run: UUID) -> str:
        assert (tenant, run) == case.key and case.key in case.owner._receipts
        if cancelled:
            raise asyncio.CancelledError
        raise RuntimeError('private backend details')

    monkeypatch.setattr(case.owner, 'require_current_receipt', final_check)
    with pytest.raises(asyncio.CancelledError if cancelled else ProjectVerificationError) as caught:
        await case.restore()
    assert 'private backend details' not in str(caught.value)
    assert all(case.key not in cache for cache in caches(case.owner))


async def test_selected_generated_cannot_downgrade_when_capture_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = await cold_case(tmp_path, 'node_generated', monkeypatch)
    record = await cast(MutableRepository, case.owner.repository).get(*case.key)
    record.routing_decision.pop('generated_project_preparation')
    plain: PlainPayload = {'verification': dict(case.payload['verification']), 'summary': 'historical success'}
    plain['verification'].pop('generated_acceptance_sha256')
    with pytest.raises(ProjectVerificationError):
        await case.restore(kind='node_plain', payload=plain)
    assert all(case.key not in cache for cache in caches(case.owner))


@pytest.mark.parametrize('kind', KINDS)
async def test_recovery_replaces_stale_cross_stack_caches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: Kind,
) -> None:
    case = await cold_case(tmp_path, kind, monkeypatch)
    for cache in caches(case.owner):
        cache[case.key] = None
    await case.restore()
    assert await case.owner.require_current_receipt(*case.key) == case.source
    assert case.key not in case.owner._python.receipts
    assert case.key not in case.owner._python_generated.receipts


async def test_generated_authority_change_during_final_check_clears_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = await cold_case(tmp_path, 'node_generated', monkeypatch)
    current = case.owner._current_generated
    calls = 0

    async def changed(context: Any, actor: str) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            record = await cast(MutableRepository, case.owner.repository).get(*case.key)
            record.request = 'changed authoritative request'
        await current(context, actor)

    monkeypatch.setattr(case.owner, '_current_generated', changed)
    with pytest.raises(ProjectVerificationError):
        await case.restore()
    assert calls == 2
    assert all(case.key not in cache for cache in caches(case.owner))
