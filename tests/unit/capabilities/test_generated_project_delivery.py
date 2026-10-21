"""Native transport is stubbed; source, frozen controls and ZIP binding are real."""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

import pytest

from agent_hub.capabilities.project_baselines import ProjectBaselineReference, ProjectBaselineStore
from agent_hub.capabilities.runtime import RuntimeCapabilityError, RuntimeCapabilityGateway
from agent_hub.capabilities.types import CapabilityRequest
from agent_hub.runtime.contracts import JsonValue
from agent_hub.skills.sandbox.base import SkillResult
from agent_hub.skills.sandbox.project import ProjectInvocation, project_source_fingerprint
from tests.unit.capabilities.test_generated_project_service import USER, Repository, manifest, setup
from tests.unit.capabilities.test_scoped_read import RUN, TENANT


async def configured(tmp_path: Path, *, absent: bool = False) -> tuple[RuntimeCapabilityGateway, Repository]:
    service, repo = setup(tmp_path)
    repo.row.routing_decision['requested_permissions'].append('network.read')

    class Executor:
        async def run(self, invocation: Any) -> SkillResult:
            assert invocation.source_kind == 'candidate' and invocation.install_allowed
            reference = ProjectBaselineReference.from_payload(invocation.original_reference.model_dump(mode='json'))
            base = ProjectInvocation.model_validate({name: getattr(invocation, name) for name in ProjectInvocation.model_fields})
            stored = ProjectBaselineStore(service.root).load(base, reference)
            binding = {'schema_version': 1, 'contract_sha256': stored.contract.protected_sha256,
                       'protected_sha256': stored.contract.protected_sha256}
            proof = {'schema_version': 1, 'manifest_sha256': hashlib.sha256(manifest()).hexdigest(),
                     'count': 1, 'names': ['adds two numbers'], 'test_evidence_sha256': hashlib.sha256(
                         b'{"passed_tests":["adds two numbers"],"schema_version":1}').hexdigest()}
            report: dict[str, object] = {'schema_version': 1, 'status': 'passed', 'source_sha256': await project_source_fingerprint(service.root, invocation),
                      'runtime': {'node': 'v22.12.0', 'npm': '10.9.0'}, 'original_check_contract': binding,
                      'checks': [{'phase': phase, 'status': 'passed', 'exit_code': 0, 'timed_out': False,
                                  'stdout': '', 'stderr': ''} for phase in ('install', 'build', 'test')]}
            if not absent:
                report['generated_test_execution'] = proof
            return SkillResult(exit_code=0, timed_out=False, stdout=json.dumps(report), stderr='')

    backend = RuntimeCapabilityGateway(skill_store_dir=tmp_path / 'skills', project_workspace_dir=service.root,
        generated_artifact_dir=tmp_path / 'artifacts', run_repository=repo, project_executor=Executor())
    return backend, repo


async def execute(backend: RuntimeCapabilityGateway, name: str,
                  args: Mapping[str, JsonValue] | None = None) -> Mapping[str, JsonValue]:
    return await backend.execute(tenant_id=TENANT, run_id=RUN, actor='engineer', name=name,
                                 arguments=args or {}, idempotency_key='generated-' + name.replace('.', '-'))


async def test_generated_runtime_freezes_controls_and_binds_current_zip(tmp_path: Path) -> None:
    backend, repo = await configured(tmp_path)
    assert await backend.project_verification_static_authorization(CapabilityRequest(
            tenant_id=TENANT, user_id=USER, run_id=RUN, agent_id='engineer', capability='command',
            operation='run', resource='workspace/current/project-verification', arguments={}, idempotency_key='static')) is None
    assert 'generated_project_preparation' not in repo.row.routing_decision
    result = await execute(backend, 'project.verify')
    delivered = await execute(backend, 'workspace.bundle')
    assert delivered['generated_project_acceptance'] == result['generated_project_acceptance']
    assert 'original_check_contract' not in result and 'original_check_contract' not in delivered
    assert delivered['verification_source_sha256'] == cast(Mapping[str, JsonValue], result['verification'])['source_sha256']
    with pytest.raises(RuntimeCapabilityError):
        await execute(backend, 'workspace.write_text', {'path': 'test/business.test.cjs', 'content': 'skip'})
    await execute(backend, 'workspace.write_text', {'path': 'src/index.js', 'content': 'exports.add=()=>0;'})
    with pytest.raises(RuntimeCapabilityError, match='project verification required'):
        await execute(backend, 'workspace.bundle')


async def test_generated_zero_or_unattested_tests_cannot_deliver(tmp_path: Path) -> None:
    backend, _ = await configured(tmp_path, absent=True)
    with pytest.raises(RuntimeCapabilityError, match='generated_acceptance_test_evidence_missing'):
        await execute(backend, 'project.verify')
    with pytest.raises(RuntimeCapabilityError, match='project verification required'):
        await execute(backend, 'workspace.bundle')
