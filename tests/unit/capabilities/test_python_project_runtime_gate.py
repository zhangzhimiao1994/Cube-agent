"""Runtime routing and delivery gates; mocked services are not execution proof."""

from pathlib import Path
from typing import Any, Never
from uuid import UUID

import pytest

from agent_hub.capabilities.project_verification import ProjectVerificationService
from agent_hub.capabilities.runtime import RuntimeCapabilityError, RuntimeCapabilityGateway
from agent_hub.capabilities.types import CapabilityRequest
from tests.unit.capabilities.capability_lifecycle_typed_fixture import untrusted_authorization
from tests.unit.capabilities.test_project_verification import Executor, gateway
from tests.unit.capabilities.test_scoped_read import RUN, TENANT, project_path, write_file
from tests.unit.runs.reviewed_report_revision_typed_fixture import read_map, string_value


async def python_runtime(tmp_path: Path) -> tuple[Any, ...]:
    from agent_hub.capabilities.approvals import capability_approval_scope
    from tests.unit.capabilities.test_python_project_service import configured, observed, request

    verifier, repository, executor, invocation, source = await configured(tmp_path)
    exact = await verifier.authorization_request(request(repository, invocation))
    repository.approvals.add(capability_approval_scope(exact))
    original = await observed(verifier, repository, invocation)
    backend = RuntimeCapabilityGateway(skill_store_dir=tmp_path / 'skills',
        project_workspace_dir=verifier.root, generated_artifact_dir=tmp_path / 'artifacts',
        run_repository=repository)
    backend._project_executor_configured = True
    backend._project_verification = verifier
    return backend, repository, executor, invocation, source, original


@pytest.mark.asyncio
async def test_runtime_routes_recipe_authorization_to_trusted_service(tmp_path: Path) -> None:
    backend = gateway(tmp_path, Executor())
    request = CapabilityRequest(tenant_id=TENANT, run_id=RUN, user_id=UUID(int=3),
        agent_id='engineer', capability='command', operation='run',
        resource='workspace/current/project-verification', arguments={}, idempotency_key='recipe')
    calls = []
    class Service(ProjectVerificationService):
        async def authorization_request(self, received: CapabilityRequest) -> CapabilityRequest:
            calls.append(received)
            return received
    backend._project_verification = object.__new__(Service)
    assert await backend.project_verification_authorization(request) is request
    assert calls == [request]


@pytest.mark.asyncio
async def test_runtime_does_not_silently_fallback_on_recipe_resolution_error(tmp_path: Path) -> None:
    backend = gateway(tmp_path, Executor())
    class Service(ProjectVerificationService):
        async def authorization_request(self, request: CapabilityRequest) -> Never:
            raise ValueError('private recipe data')
    backend._project_verification = object.__new__(Service)
    with pytest.raises(ValueError):
        await untrusted_authorization(backend, object())


@pytest.mark.asyncio
async def test_inline_python_project_cannot_bypass_workspace_verification(tmp_path: Path) -> None:
    backend = gateway(tmp_path, Executor())
    with pytest.raises(RuntimeCapabilityError, match='workspace.write_text'):
        await backend.execute(tenant_id=TENANT, run_id=RUN, actor='engineer',
            name='project.generate_zip', arguments={'title':'Unchecked',
            'files':{'pyproject.toml':'[project]\nname="unchecked"\n'}}, idempotency_key='inline')
    assert not (tmp_path / 'artifacts').exists()


@pytest.mark.asyncio
async def test_workspace_python_project_requires_receipt_even_without_original_marker(tmp_path: Path) -> None:
    backend = gateway(tmp_path, Executor())
    root = tmp_path / 'projects'
    (root / project_path() / 'package.json').unlink()
    write_file(root, f'{project_path()}/pyproject.toml', '[project]\nname="unchecked"\n')
    with pytest.raises(RuntimeCapabilityError, match='verification required'):
        await backend.execute(tenant_id=TENANT, run_id=RUN, actor='engineer',
            name='workspace.bundle', arguments={}, idempotency_key='bundle')
    assert not (tmp_path / 'artifacts').exists()


@pytest.mark.asyncio
async def test_no_project_verifier_preserves_non_project_authorization(tmp_path: Path) -> None:
    backend = RuntimeCapabilityGateway(skill_store_dir=tmp_path / 'skills')
    assert await untrusted_authorization(backend, object()) is None


@pytest.mark.asyncio
async def test_python_original_guards_permitted_write_and_protected_tests(tmp_path: Path) -> None:
    backend, _, _, invocation, source, _ = await python_runtime(tmp_path)
    async def write(path: str) -> Any:
        return await backend.execute(tenant_id=invocation.tenant_id, run_id=invocation.run_id,
            actor='engineer', name='workspace.write_text', arguments={'path':path,
            'content':'def total(): return 12\n'}, idempotency_key='write')
    await write('src/billing/__init__.py')
    assert (source / 'src/billing/__init__.py').read_bytes() == b'def total(): return 12\n'
    before = (source / 'tests/test_billing.py').read_bytes()
    with pytest.raises(RuntimeCapabilityError, match='protected'):
        await write('tests/test_billing.py')
    assert (source / 'tests/test_billing.py').read_bytes() == before


@pytest.mark.asyncio
async def test_python_verify_and_real_zip_bytes_share_source_and_canonical_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import hashlib
    import json
    import zipfile

    from tests.unit.capabilities.capability_lifecycle_typed_fixture import (
        record_environment_inspections,
    )
    from tests.unit.capabilities.test_python_project_service import PythonExecutor

    backend, _, executor, invocation, source, original = await python_runtime(tmp_path)
    assert isinstance(executor, PythonExecutor)
    inspections = record_environment_inspections(executor, monkeypatch)
    assert original['status'] == 'failed'
    executor.failure_index = None
    await backend.execute(tenant_id=invocation.tenant_id, run_id=invocation.run_id,
        actor='engineer', name='workspace.write_text', arguments={'path':'src/billing/__init__.py',
        'content':'def total(): return 12\n'}, idempotency_key='repair')
    verified = await backend.execute(tenant_id=invocation.tenant_id, run_id=invocation.run_id,
        actor='engineer', name='project.verify', arguments={}, idempotency_key='verify')
    bundle = await backend.execute(tenant_id=invocation.tenant_id, run_id=invocation.run_id,
        actor='engineer', name='workspace.bundle', arguments={}, idempotency_key='bundle')
    receipt = verified['verification']
    assert bundle['verification_source_sha256'] == receipt['source_sha256']
    assert bundle['verification_receipt_sha256'] == hashlib.sha256(json.dumps(receipt,
        default=dict, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode('ascii')).hexdigest()
    assert bundle['original_check_contract'] == verified['original_check_contract']
    path = tmp_path / 'artifacts' / str(invocation.tenant_id) / str(invocation.run_id)
    path = path / str(bundle['artifact_id']) / bundle['file']['filename']
    with zipfile.ZipFile(path) as archive:
        assert archive.read('tests/test_billing.py') == (source / 'tests/test_billing.py').read_bytes()
        assert archive.read('src/billing/__init__.py') == b'def total(): return 12\n'
    # Bundle checks the receipt directly and again when selecting the Python
    # product. Both real inspections must bind the same current environment;
    # neither may install or replay the original/candidate executions.
    assert len(executor.calls) == 2 and len(executor.inspections) == 2
    assert len(inspections) == 2
    assert [item.invocation for item in inspections] == executor.inspections
    assert inspections[0].expected == inspections[1].expected
    assert len({item.invocation.execution_id for item in inspections}) == 2
    for item in inspections:
        current = item.invocation
        expected = item.expected
        assert (current.tenant_id, current.run_id, current.project_id, current.session_id) == (
            invocation.tenant_id, invocation.run_id, invocation.project_id, invocation.session_id,
        )
        assert (expected.scope.tenant_id, expected.scope.run_id,
                expected.scope.project_id, expected.scope.session_id) == (
            invocation.tenant_id, invocation.run_id, invocation.project_id, invocation.session_id,
        )
        assert current.original_reference == expected.scope.original_reference
        assert current.recipe_sha256 == expected.recipe_sha256 == executor.calls[-1].recipe_sha256
        assert current.environment_input_sha256 == expected.environment_input_sha256
        assert item.observed == expected
        assert current.install_allowed is False and current.source_kind == 'candidate'
        assert current.execution_id not in {call.execution_id for call in executor.calls}


@pytest.mark.asyncio
async def test_python_root_environment_drift_blocks_new_zip_and_clears_verification(tmp_path: Path) -> None:
    backend, _, executor, invocation, _, _ = await python_runtime(tmp_path)
    executor.failure_index = None
    await backend.execute(tenant_id=invocation.tenant_id, run_id=invocation.run_id,
        actor='engineer', name='project.verify', arguments={}, idempotency_key='verify')
    executor.changed_environment = True
    with pytest.raises(RuntimeCapabilityError):
        await backend.execute(tenant_id=invocation.tenant_id, run_id=invocation.run_id,
            actor='engineer', name='workspace.bundle', arguments={}, idempotency_key='stale')
    assert not (tmp_path / 'artifacts').exists()


@pytest.mark.asyncio
async def test_partial_inline_zip_cannot_evade_original_project_delivery_or_overwrite_checks(tmp_path: Path) -> None:
    backend, _, _, invocation, source, _ = await python_runtime(tmp_path)
    before = (source / 'tests/test_billing.py').read_bytes()
    with pytest.raises(RuntimeCapabilityError, match='requires scoped workspace operations'):
        await backend.execute(tenant_id=invocation.tenant_id, run_id=invocation.run_id,
            actor='engineer', name='project.generate_zip', arguments={'title':'Bypass',
            'project_id':invocation.project_id, 'workspace_session_id':invocation.session_id,
            'files':{'tests/test_billing.py':'weakened checks'}}, idempotency_key='partial-inline')
    assert (source / 'tests/test_billing.py').read_bytes() == before
    assert not (tmp_path / 'artifacts').exists()


@pytest.mark.asyncio
async def test_real_default_policy_waits_for_exact_recipe_then_gateway_executes_typed_failure(tmp_path: Path) -> None:
    from agent_hub.auth.models import Role
    from agent_hub.capabilities.approvals import ApprovalService, InMemoryApprovalStore
    from agent_hub.capabilities.defaults import DefaultRuntimeCapabilityPolicyGateway
    from agent_hub.harness.tool_gateway import HarnessToolGateway
    from agent_hub.harness.types import HarnessToolCallRequest
    from tests.unit.capabilities.test_python_project_service import configured, observed

    verifier, repository, executor, invocation, _ = await configured(tmp_path)
    backend = RuntimeCapabilityGateway(skill_store_dir=tmp_path / 'skills',
        project_workspace_dir=verifier.root, generated_artifact_dir=tmp_path / 'artifacts',
        run_repository=repository)
    backend._project_executor_configured = True
    backend._project_verification = verifier
    pending = []
    async def begin(tenant: Any, run: Any, **fields: Any) -> None:
        assert (tenant, run) == (invocation.tenant_id, invocation.run_id)
        pending.append(fields)
    repository.begin_capability_approval = begin
    policy = DefaultRuntimeCapabilityPolicyGateway(ApprovalService(InMemoryApprovalStore()), repository)
    gateway = HarnessToolGateway(backend, policy_gateway=policy)
    call = HarnessToolCallRequest(run_id=invocation.run_id, actor='engineer',
        tool_name='project.verify', arguments={}, approval_required=False,
        sandbox='workspace_write', idempotency_key='exact-approval')
    identity = {'user_id':repository.record.actor_id, 'role':Role.OPERATOR}
    static = await gateway.authorize(invocation.tenant_id, call, **identity)
    assert static.status == 'succeeded'
    assert pending == [] and not executor.calls
    waiting = await gateway.invoke(invocation.tenant_id, call, **identity)
    assert waiting.status == 'failed' and waiting.payload['approval_id'] == pending[0]['approval_id']
    assert '/project-verification/python/' in pending[0]['approval_scope']
    assert not executor.calls
    repository.approvals.add(pending[0]['approval_scope'])
    allowed = await gateway.authorize(invocation.tenant_id, call, **identity)
    assert allowed.status == 'succeeded'
    await observed(verifier, repository, invocation)
    failed = await gateway.invoke(invocation.tenant_id, call, **identity)
    assert failed.status == 'failed' and read_map(failed.payload['python_verification_failure'])['schema_version'] == 2
    assert 'AssertionError' in string_value(read_map(failed.payload['execution_diagnostic'])['stderr'])
    assert 'verification_failure' not in failed.payload
    assert len(pending) == 1 and len(executor.calls) == 2
