from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import pytest

from agent_hub.capabilities.project_baselines import ProjectBaselineStore
from agent_hub.capabilities.project_verification import (
    ProjectVerificationError,
    ProjectVerificationService,
)
from agent_hub.skills.sandbox.base import SkillResult
from agent_hub.skills.sandbox.project import ProjectInvocation
from tests.unit.capabilities.test_project_contract_gateway import configured
from tests.unit.capabilities.test_project_verification import Executor


class ContractExecutor(Executor):
    def __init__(self, digest: str, *, failed: bool = False, bind: bool = True) -> None:
        super().__init__(failed=failed)
        self.digest, self.bind = digest, bind

    async def run(self, invocation: ProjectInvocation) -> SkillResult:
        result = await super().run(invocation)
        payload = json.loads(result.stdout)
        if self.bind:
            payload['original_check_contract'] = {'schema_version': 1,
                'contract_sha256': self.digest, 'protected_sha256': self.digest}
        return result.model_copy(update={'stdout': json.dumps(payload)})


async def service(tmp_path: Path, *, failed: bool = False, bind: bool = True):  # type: ignore[no-untyped-def]
    backend, source, invocation = await configured(tmp_path, prepared=True)
    store = ProjectBaselineStore(tmp_path / 'workspaces')
    record = await cast(Any, backend._run_repository).get(invocation.tenant_id, invocation.run_id)
    # Existing fixture defaults omit network approval; this is a component-only authorized scope.
    record.routing_decision['requested_permissions'].append('network.read')
    from agent_hub.capabilities.project_baselines import ProjectBaselineReference
    reference = ProjectBaselineReference.from_payload(record.routing_decision['project_input_preparation']['reference'])
    loaded = store.load(invocation, reference)
    executor = ContractExecutor(loaded.contract.protected_sha256, failed=failed, bind=bind)
    executor.root = tmp_path / 'workspaces'
    verifier = ProjectVerificationService(root=executor.root, repository=backend._run_repository, executor=executor)
    return verifier, executor, source, invocation, reference


async def test_candidate_uses_readonly_contract_protocol_not_legacy_execution(tmp_path: Path) -> None:
    verifier, executor, _, invocation, _ = await service(tmp_path)
    result = await verifier.verify(tenant_id=invocation.tenant_id, run_id=invocation.run_id, actor='engineer')
    assert executor.calls[0].source_kind == 'candidate'
    assert result['original_check_contract']['contract_sha256'] == executor.digest


async def test_legacy_success_cannot_satisfy_original_check_contract(tmp_path: Path) -> None:
    verifier, executor, _, invocation, _ = await service(tmp_path, bind=False)
    with pytest.raises(ProjectVerificationError, match='contract'):
        await verifier.verify(tenant_id=invocation.tenant_id, run_id=invocation.run_id, actor='engineer')
    assert len(executor.calls) == 1


async def test_changed_original_check_is_denied_before_executor(tmp_path: Path) -> None:
    verifier, executor, source, invocation, _ = await service(tmp_path)
    (source / 'tests/original.js').write_text('weakened')
    with pytest.raises(ProjectVerificationError, match='original project'):
        await verifier.verify(tenant_id=invocation.tenant_id, run_id=invocation.run_id, actor='engineer')
    assert not executor.calls


async def test_original_failure_is_separate_from_successful_delivery_receipt(tmp_path: Path) -> None:
    verifier, executor, _, invocation, reference = await service(tmp_path, failed=True)
    record = await verifier.repository.get(invocation.tenant_id, invocation.run_id)
    record.routing_decision['project_input_preparation'] = {'schema_version': 1, 'phase': 'executing',
        'reference': reference.to_payload(), 'execution_id': 'original_once'}
    observer = verifier.observe_original
    observed = await observer(tenant_id=invocation.tenant_id, run_id=invocation.run_id,
        actor='engineer', reference=reference, execution_id='original_once', timeout_seconds=20)
    assert executor.calls[0].source_kind == 'original'
    assert observed['status'] == 'failed'
    assert observed['verification']['source_sha256'] == reference.input_source_sha256
    assert 'SECRET' not in str(observed)
    with pytest.raises(ProjectVerificationError, match='verification required'):
        await verifier.require_current_receipt(invocation.tenant_id, invocation.run_id)


async def test_unobserved_reference_cannot_be_upgraded_into_candidate_success(tmp_path: Path) -> None:
    verifier, executor, _, invocation, reference = await service(tmp_path)
    record = await cast(Any, verifier.repository).get(invocation.tenant_id, invocation.run_id)
    record.routing_decision['project_input_preparation'] = {'schema_version': 1, 'phase': 'observed',
                                                          'reference': reference.to_payload()}
    with pytest.raises(ProjectVerificationError, match='original project'):
        await verifier.verify(tenant_id=invocation.tenant_id, run_id=invocation.run_id, actor='engineer')
    assert not executor.calls


async def test_changed_original_observation_invalidates_existing_delivery_receipt(tmp_path: Path) -> None:
    verifier, _, _, invocation, _ = await service(tmp_path)
    result = await verifier.verify(tenant_id=invocation.tenant_id, run_id=invocation.run_id, actor='engineer')
    assert result['original_check_contract']['baseline_observation_sha256']
    record = await cast(Any, verifier.repository).get(invocation.tenant_id, invocation.run_id)
    record.routing_decision['project_input_preparation']['observation_sha256'] = '0' * 64
    with pytest.raises(ProjectVerificationError):
        await verifier.require_current_receipt(invocation.tenant_id, invocation.run_id)
