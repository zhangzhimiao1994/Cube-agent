"""Generated runtime integration; fake native transport is not native test evidence."""

from __future__ import annotations

import hashlib
import json
import zipfile
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, cast
from uuid import UUID

import pytest

from agent_hub.capabilities.runtime import RuntimeCapabilityError, RuntimeCapabilityGateway
from agent_hub.runtime.contracts import JsonValue
from agent_hub.skills.sandbox.base import SkillResult
from agent_hub.skills.sandbox.project import (
    ContractProjectInvocation,
    ProjectInvocation,
    project_source_fingerprint,
)
from tests.unit.capabilities.test_generated_project_service import USER, Repository, setup
from tests.unit.capabilities.test_scoped_read import RUN, TENANT, project_path


class NodeExecutor:
    def __init__(self, root: Path, contract: str) -> None:
        self.root, self.contract = root, contract
        self.failed = False
        self.on_run: Callable[[], None] | None = None
        self.calls: list[ProjectInvocation] = []

    async def run(self, invocation: ProjectInvocation) -> SkillResult:
        self.calls.append(invocation)
        source = await project_source_fingerprint(self.root, invocation)
        if self.on_run is not None:
            self.on_run()
        names = ['adds two numbers']
        evidence = hashlib.sha256(json.dumps({'schema_version': 1, 'passed_tests': names},
            sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode('ascii')).hexdigest()
        report = {'schema_version': 1, 'status': 'failed' if self.failed else 'passed',
            'source_sha256': source, 'runtime': {'node': 'v22.12.0', 'npm': '10.9.0'},
            'original_check_contract': {'schema_version': 1, 'contract_sha256': self.contract,
                'protected_sha256': self.contract},
            'checks': [{'phase': phase, 'status': 'failed' if self.failed and phase == 'test' else 'passed',
                'exit_code': 1 if self.failed and phase == 'test' else 0, 'timed_out': False,
                'stdout': '', 'stderr': 'AssertionError: wrong value\nAPI_KEY=SECRET'
                    if self.failed and phase == 'test' else ''} for phase in ('install', 'build', 'test')],
            'generated_test_execution': {'schema_version': 1, 'count': 1, 'names': names,
                'manifest_sha256': hashlib.sha256((self.root / project_path() / 'ACCEPTANCE.json').read_bytes()).hexdigest(),
                'test_evidence_sha256': evidence}}
        return SkillResult(exit_code=0, timed_out=False, stdout=json.dumps(report), stderr='')


async def configured(tmp_path: Path) -> tuple[RuntimeCapabilityGateway, Repository, NodeExecutor]:
    generated, repo = setup(tmp_path)
    repo.row.routing_decision['requested_permissions'].append('network.read')
    context = await generated.prepare(TENANT, RUN, 'engineer', user_id=USER)
    assert context is not None
    executor = NodeExecutor(generated.root, context.stored.contract.protected_sha256)
    runtime = RuntimeCapabilityGateway(skill_store_dir=tmp_path / 'skills', project_workspace_dir=generated.root,
        generated_artifact_dir=tmp_path / 'artifacts', run_repository=repo, project_executor=executor)
    return runtime, repo, executor


async def call(runtime: RuntimeCapabilityGateway, name: str, **arguments: JsonValue) -> Mapping[str, JsonValue]:
    return await runtime.execute(tenant_id=TENANT, run_id=RUN, actor='engineer',
        name=name, arguments=arguments, idempotency_key='review-' + name.replace('.', '-'))


async def test_node_worker_lease_change_during_execution_cannot_issue_receipt(tmp_path: Path) -> None:
    runtime, repo, executor = await configured(tmp_path)
    executor.on_run = lambda: setattr(repo.row, 'worker_lease_token', UUID(int=99))
    with pytest.raises(RuntimeCapabilityError):
        await call(runtime, 'project.verify')
    assert not runtime._project_delivery_receipts
    assert runtime._project_verification is not None
    assert not runtime._project_verification._generated_receipts


async def test_node_receipt_cannot_cross_worker_lease_even_with_same_acceptance(tmp_path: Path) -> None:
    runtime, repo, _ = await configured(tmp_path)
    await call(runtime, 'project.verify')
    repo.row.worker_lease_token = UUID(int=99)
    with pytest.raises(RuntimeCapabilityError):
        await call(runtime, 'workspace.bundle')
    assert not (tmp_path / 'artifacts').exists()


async def test_generated_zip_does_not_return_bound_delivery_after_lease_changes_during_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, repo, _ = await configured(tmp_path)
    await call(runtime, 'project.verify')
    original = runtime._bundle_workspace

    def copy(**kwargs: Any) -> Any:
        result = original(**kwargs)
        repo.row.worker_lease_token = UUID(int=99)
        return result

    monkeypatch.setattr(runtime, '_bundle_workspace', copy)
    with pytest.raises(RuntimeCapabilityError):
        await call(runtime, 'workspace.bundle')
    assert not runtime._project_delivery_receipts and not runtime._generated_delivery_bindings


async def test_node_fail_repair_keeps_checks_and_binds_actual_zip(tmp_path: Path) -> None:
    runtime, _, executor = await configured(tmp_path)
    source = executor.root / project_path()
    checks = (source / 'test/business.test.cjs').read_bytes()
    executor.failed = True
    with pytest.raises(RuntimeCapabilityError) as error:
        await call(runtime, 'project.verify')
    assert error.value.project_failure is not None and error.value.python_project_failure is None
    assert error.value.execution_diagnostic is not None
    assert 'SECRET' not in str(error.value.execution_diagnostic.to_payload())
    assert not runtime._project_delivery_receipts
    with pytest.raises(RuntimeCapabilityError):
        await call(runtime, 'workspace.bundle')
    with pytest.raises(RuntimeCapabilityError, match='protected'):
        await call(runtime, 'workspace.write_text', path='test/business.test.cjs', content='weakened')
    await call(runtime, 'workspace.write_text', path='src/index.js', content='exports.add=(a,b)=>Number(a)+Number(b);')
    executor.failed = False
    result = await call(runtime, 'project.verify')
    bundle = await call(runtime, 'workspace.bundle')
    binding = cast(Mapping[str, JsonValue], result['generated_project_acceptance'])
    assert bundle['generated_project_acceptance'] == binding
    receipt = cast(Mapping[str, JsonValue], result['verification'])
    assert bundle['verification_source_sha256'] == receipt['source_sha256'] == binding['source_sha256']
    assert bundle['verification_receipt_sha256'] == hashlib.sha256(json.dumps(receipt, default=dict,
        sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode('ascii')).hexdigest()
    assert len(executor.calls) == 2
    for invocation in executor.calls:
        assert isinstance(invocation, ContractProjectInvocation) and invocation.source_kind == 'candidate'
    first, second = (cast(ContractProjectInvocation, invocation) for invocation in executor.calls)
    assert first.original_reference == second.original_reference
    path = tmp_path / 'artifacts' / str(TENANT) / str(RUN) / str(bundle['artifact_id'])
    metadata = cast(Mapping[str, JsonValue], bundle['file'])
    with zipfile.ZipFile(path / cast(str, metadata['filename'])) as archive:
        assert archive.read('test/business.test.cjs') == checks
        assert archive.read('src/index.js') == (source / 'src/index.js').read_bytes()


async def test_generated_python_failure_and_recipe_approval_keep_their_types(tmp_path: Path) -> None:
    from agent_hub.capabilities import python_generated_service as api
    from tests.unit.capabilities.test_python_generated_service import approve, request
    from tests.unit.capabilities.test_python_generated_service import (
        configured as python_configured,
    )

    service, generated, repo, executor, _, context = await python_configured(api, tmp_path)
    runtime = RuntimeCapabilityGateway(skill_store_dir=tmp_path / 'skills', project_workspace_dir=generated.root,
        generated_artifact_dir=tmp_path / 'artifacts', run_repository=repo)
    runtime._project_executor_configured = True
    runtime._project_verification = service.owner
    service.owner._python_generated = service
    executor.probe_error = 'python_project_wheel_missing'
    approve(repo, await service.authorization_request(request(), context))
    with pytest.raises(api.PythonGeneratedRecipeApprovalRequired) as approval:
        await call(runtime, 'project.verify')
    assert '/project-verification/python/' in approval.value.approval_request.resource
    assert not executor.calls and not runtime._project_delivery_receipts
    approve(repo, approval.value.approval_request)
    executor.failure_index = 2
    with pytest.raises(RuntimeCapabilityError) as failed:
        await call(runtime, 'project.verify')
    assert failed.value.project_failure is None and failed.value.python_project_failure is not None
    assert failed.value.execution_diagnostic is failed.value.python_project_failure.diagnostic
    assert not runtime._generated_delivery_bindings and not runtime._project_delivery_receipts


async def test_python_profile_is_configuration_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from agent_hub.skills.sandbox.python_project_profile import (
        PythonProjectProfile,
        PythonProjectProfileResponse,
        PythonProjectProfileSandbox,
        PythonProjectProfileScope,
    )

    runtime, _, _ = await configured(tmp_path)
    observed: list[ProjectInvocation] = []
    profile = PythonProjectProfile.model_validate_json(json.dumps({
        'interpreter': {'implementation': 'cpython', 'version': '3.12.14', 'abi': 'cp312',
            'platform': 'linux_x86_64', 'binary_sha256': 'a' * 64, 'base_runtime_sha256': 'b' * 64},
        'catalog': [{'name': 'wheel', 'filename': 'wheel-0.45.1-py3-none-any.whl',
            'version': '0.45.1', 'sha256': 'c' * 64, 'size_bytes': 100}], 'catalog_sha256': 'd' * 64}), strict=True)

    async def inspect(self: PythonProjectProfileSandbox, invocation: ProjectInvocation) -> PythonProjectProfileResponse:
        observed.append(invocation)
        return PythonProjectProfileResponse(scope=PythonProjectProfileScope.for_invocation(invocation),
            ok=True, profile=profile)

    monkeypatch.setattr(PythonProjectProfileSandbox, 'inspect', inspect)
    result = await runtime._python_build_profile(TENANT, RUN)
    assert result['status'] == 'configured_not_probed'
    assert 'Not an installation receipt' in cast(str, result['catalog_usage'])
    assert len(observed) == 1 and not observed[0].install_allowed
    assert not runtime._project_delivery_receipts
