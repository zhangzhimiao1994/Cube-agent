"""Gateway recovery uses verifier-issued private receipts and real ZIP delivery."""
from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from agent_hub.capabilities.runtime import RuntimeCapabilityError, RuntimeCapabilityGateway
from agent_hub.runs.project_verification_receipts import RECEIPT_KEY
from agent_hub.runtime.contracts import JsonValue
from tests.unit.capabilities.test_generated_project_delivery import configured
from tests.unit.capabilities.test_scoped_read import RUN, TENANT, project_path, write_file
from tests.unit.capabilities.test_verification_cold_recovery import Fixture


def gateway(fixture: Fixture, *, durable: bool = True) -> RuntimeCapabilityGateway:
    return RuntimeCapabilityGateway(
        skill_store_dir=fixture.root.parent / 'skills',
        project_workspace_dir=fixture.root,
        generated_artifact_dir=fixture.root.parent / 'downloads',
        run_repository=fixture.runs, project_executor=fixture.executor,
        artifact_repository=fixture.artifacts,
        **({'receipt_repository': fixture.db.repository()} if durable else {}),
    )


async def case(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str) -> Fixture:
    fixture = Fixture(tmp_path / 'durable')
    if kind == 'generated':
        backend, repository = await configured(tmp_path / 'generated')
        service = backend._project_verification
        assert service is not None and service.executor is not None
        await service.generated.prepare(TENANT, RUN, 'main_agent', user_id=repository.row.actor_id)
        fixture.root = service.root
        fixture.db.row.actor_id = repository.row.actor_id
        fixture.db.row.request = repository.row.request
        fixture.db.row.routing_decision = dict(repository.row.routing_decision)

        async def generated_run(invocation: Any) -> Any:
            fixture.executor.calls.append(invocation)
            assert service.executor is not None
            return await service.executor.run(invocation)

        monkeypatch.setattr(fixture.executor, 'run', generated_run)
    else:
        original = fixture.executor.run

        async def unicode_run(invocation: Any) -> Any:
            result = await original(invocation)
            report = json.loads(result.stdout)
            report['checks'][0]['stdout'] = '\u9a8c\u8bc1\u901a\u8fc7'
            return result.model_copy(update={'stdout': json.dumps(report)})

        monkeypatch.setattr(fixture.executor, 'run', unicode_run)
    return fixture


async def execute(backend: RuntimeCapabilityGateway, name: str = 'project.verify',
                  key: str = 'verify-one') -> Mapping[str, JsonValue]:
    return await backend.execute(tenant_id=TENANT, run_id=RUN, actor='main_agent',
        name=name, arguments={}, idempotency_key=key)


async def restore(backend: RuntimeCapabilityGateway, key: str = 'verify-one') -> Mapping[str, JsonValue]:
    return await backend.restore_project_verification(
        tenant_id=TENANT, run_id=RUN, actor='main_agent', action_key=key)


@pytest.mark.parametrize('kind', ['plain', 'generated'])
async def test_cold_gateway_restores_complete_delivery_without_second_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str,
) -> None:
    fixture = await case(tmp_path, monkeypatch, kind)
    warm = gateway(fixture)
    with fixture.bind():
        verified = await execute(warm)
        warm_bundle = await execute(warm, 'workspace.bundle', 'warm-bundle')
    assert fixture.db.row.routing_decision[RECEIPT_KEY]['action_key'] == 'verify-one'
    assert fixture.artifacts._artifacts
    fixture.db.new_lease()
    cold = gateway(fixture)
    with fixture.bind():
        with pytest.raises(RuntimeCapabilityError, match='project verification required'):
            await execute(cold, 'workspace.bundle', 'before-restore')
        recovered = await restore(cold)
        delivered = await execute(cold, 'workspace.bundle', 'cold-bundle')
    assert recovered == verified
    receipt = recovered['verification']
    assert isinstance(receipt, Mapping)
    digest = hashlib.sha256(json.dumps(receipt, default=dict, sort_keys=True,
        separators=(',', ':'), ensure_ascii=True).encode('ascii')).hexdigest()
    assert delivered['verification_receipt_sha256'] == warm_bundle['verification_receipt_sha256'] == digest
    assert delivered['verification_source_sha256'] == receipt['source_sha256']
    assert 'artifact_id' in delivered
    if kind == 'generated':
        assert delivered['generated_project_acceptance'] == recovered['generated_project_acceptance']
        assert recovered['generated_project_acceptance'] == verified['generated_project_acceptance']
    else:
        assert '\u9a8c\u8bc1\u901a\u8fc7' in json.dumps(receipt, default=dict, ensure_ascii=False)
        assert 'generated_project_acceptance' not in delivered
    assert len(fixture.executor.calls) == 1


@pytest.mark.parametrize('kind', ['plain', 'generated'])
@pytest.mark.parametrize('defect', ['action', 'source', 'permissions', 'missing_artifact'])
async def test_rejected_restore_clears_delivery_authority_and_blocks_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str, defect: str,
) -> None:
    fixture = await case(tmp_path, monkeypatch, kind)
    with fixture.bind():
        await execute(gateway(fixture))
    fixture.db.new_lease()
    cold = gateway(fixture)
    other = (TENANT, uuid4())
    cold._project_delivery_receipts[other] = ('a' * 64, 'b' * 64)
    cold._generated_delivery_bindings[other] = {'unrelated': True}
    with fixture.bind():
        await restore(cold)
        if defect == 'source':
            write_file(fixture.root, project_path() + '/changed.js', 'source changed')
        elif defect == 'permissions':
            fixture.db.row.routing_decision['requested_permissions'].remove('network.read')
        elif defect == 'missing_artifact':
            fixture.artifacts._artifacts.clear()
        with pytest.raises(RuntimeCapabilityError):
            await restore(cold, 'wrong-action' if defect == 'action' else 'verify-one')
        assert (TENANT, RUN) not in cold._project_delivery_receipts
        assert (TENANT, RUN) not in cold._generated_delivery_bindings
        assert cold._project_delivery_receipts[other] == ('a' * 64, 'b' * 64)
        assert cold._generated_delivery_bindings[other] == {'unrelated': True}
        with pytest.raises(RuntimeCapabilityError):
            await execute(cold, 'workspace.bundle', 'rejected-bundle')
    assert len(fixture.executor.calls) == 1


async def test_cancelled_restore_clears_both_delivery_caches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = await case(tmp_path, monkeypatch, 'generated')
    with fixture.bind():
        await execute(gateway(fixture))
    fixture.db.new_lease()
    cold = gateway(fixture)
    with fixture.bind():
        await restore(cold)

        async def cancelled(*args: Any, **kwargs: Any) -> Any:
            raise asyncio.CancelledError

        monkeypatch.setattr(fixture.artifacts, 'get_many', cancelled)
        with pytest.raises(asyncio.CancelledError):
            await restore(cold)
        assert (TENANT, RUN) not in cold._project_delivery_receipts
        assert (TENANT, RUN) not in cold._generated_delivery_bindings
        with pytest.raises(RuntimeCapabilityError):
            await execute(cold, 'workspace.bundle', 'cancelled-bundle')
    assert len(fixture.executor.calls) == 1


async def test_artifact_repository_alone_keeps_legacy_verification(tmp_path: Path) -> None:
    fixture = Fixture(tmp_path)
    backend = gateway(fixture, durable=False)
    verified = await execute(backend)
    delivered = await execute(backend, 'workspace.bundle', 'legacy-bundle')
    assert RECEIPT_KEY not in fixture.db.row.routing_decision
    assert not fixture.artifacts._artifacts
    receipt = verified['verification']
    assert isinstance(receipt, Mapping)
    assert delivered['verification_source_sha256'] == receipt['source_sha256']
    with pytest.raises(RuntimeCapabilityError, match='durable verification is not configured'):
        await restore(backend)
    with pytest.raises(RuntimeCapabilityError):
        await execute(backend, 'workspace.bundle', 'legacy-after-restore')
    assert len(fixture.executor.calls) == 1


def test_receipt_repository_requires_private_artifact_repository(tmp_path: Path) -> None:
    fixture = Fixture(tmp_path)
    with pytest.raises(ValueError, match='both repositories'):
        RuntimeCapabilityGateway(skill_store_dir=tmp_path / 'skills',
            project_workspace_dir=fixture.root, run_repository=fixture.runs,
            project_executor=fixture.executor, receipt_repository=fixture.db.repository())
