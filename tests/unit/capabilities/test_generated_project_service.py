from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from agent_hub.auth.models import Role
from agent_hub.capabilities.generated_project_service import (
    GeneratedProjectError,
    GeneratedProjectService,
)
from agent_hub.domain.runs import RunStatus
from tests.unit.capabilities.test_scoped_read import RUN, TENANT, project_path, write_file

USER = UUID('66666666-6666-4666-8666-666666666666')


def manifest() -> bytes:
    return json.dumps({'schema_version': 1, 'stack': 'node', 'editable_paths': ['src'],
        'test_files': ['test/business.test.cjs'], 'criteria': [
            {'id': 'AC-001', 'description': 'Adds two numbers', 'tests': ['adds two numbers']}]}).encode()


class Repository:
    def __init__(self) -> None:
        self.row = SimpleNamespace(tenant_id=TENANT, id=RUN, actor_id=USER, actor_role=Role.OPERATOR,
            request='Build a working addition project.', status=RunStatus.RUNNING,
            worker_id='worker-a', worker_lease_token=uuid4(),
            worker_lease_expires_at=datetime.now(UTC) + timedelta(minutes=10),
            routing_decision={'generated_project_contract_version': 1, 'project_id': 'project-a',
                'workspace_session_id': 'session-a', 'sandbox_profile': 'workspace_write',
                'execution_backend': 'systemd', 'runtime_timeout_seconds': 120,
                'requested_permissions': ['workspace.read', 'workspace.write', 'command.run']})
        self.updates = 0

    async def get(self, tenant_id: UUID, run_id: UUID) -> object:
        assert (tenant_id, run_id) == (TENANT, RUN)
        return self.row

    async def compare_and_set_generated_preparation(self, tenant_id: UUID, run_id: UUID, **kw: object) -> object:
        assert (tenant_id, run_id) == (TENANT, RUN)
        assert kw['actor_id'] == self.row.actor_id
        assert kw['lease_token'] == self.row.worker_lease_token
        assert kw['request_sha256'] == hashlib.sha256(self.row.request.encode()).hexdigest()
        assert self.row.routing_decision.get('generated_project_preparation') == kw['expected']
        self.row.routing_decision = {**self.row.routing_decision, 'generated_project_preparation': kw['replacement']}
        self.updates += 1
        return self.row


def setup(tmp_path: Path) -> tuple[GeneratedProjectService, Repository]:
    root = tmp_path / 'projects'
    root.parent.mkdir(parents=True, exist_ok=True)
    if os.name == 'nt':
        from agent_hub.plugins.handover_snapshots import _windows_security
        _windows_security(root, private=True, create=True)
    else:
        root.mkdir(mode=0o700)
    for name, body in {'ACCEPTANCE.json': manifest().decode(), 'src/index.js': 'exports.add=(a,b)=>a+b;',
        'package.json': '{"scripts":{"build":"node -v","test":"node --test"}}',
        'test/business.test.cjs': 'require("node:test").test("adds two numbers",()=>{});'}.items():
        write_file(root, f'{project_path()}/{name}', body)
    repo = Repository()
    return GeneratedProjectService(root=root, repository=repo), repo


async def test_generated_capture_is_post_model_and_reuses_frozen_scaffold(tmp_path: Path) -> None:
    service, repo = setup(tmp_path)
    assert await service.load(TENANT, RUN, 'engineer') is None
    context = await service.prepare(TENANT, RUN, 'engineer', user_id=USER)
    assert context is not None
    assert context.envelope.capture_phase == 'post_model'
    assert context.envelope.proposer_actor == 'engineer'
    assert context.stored.generated_metadata is not None
    assert 'project_input_preparation' not in repo.row.routing_decision
    assert 'project_input' not in repo.row.routing_decision
    assert repo.updates == 2
    second = await service.prepare(TENANT, RUN, 'tester', user_id=USER)
    assert second is not None and second.envelope.sha256 == context.envelope.sha256
    assert repo.updates == 2


async def test_missing_manifest_can_be_authored_before_reserving_capture(tmp_path: Path) -> None:
    service, repo = setup(tmp_path)
    (service.root / project_path() / 'ACCEPTANCE.json').unlink()
    with pytest.raises(GeneratedProjectError, match='generated_acceptance_manifest_required'):
        await service.prepare(TENANT, RUN, 'engineer', user_id=USER)
    assert repo.updates == 0


@pytest.mark.parametrize('field,value', [('actor_id', uuid4()), ('actor_role', Role.VIEWER),
    ('status', RunStatus.COMPLETED), ('worker_lease_token', None),
    ('worker_lease_expires_at', datetime(2000, 1, 1, tzinfo=UTC))])
async def test_capture_requires_live_authoritative_actor_and_scope(tmp_path: Path, field: str, value: object) -> None:
    service, repo = setup(tmp_path)
    setattr(repo.row, field, value)
    with pytest.raises(GeneratedProjectError):
        await service.prepare(TENANT, RUN, 'engineer', user_id=USER)
    assert repo.updates == 0


async def test_frozen_tests_and_request_cannot_be_replaced(tmp_path: Path) -> None:
    service, repo = setup(tmp_path)
    context = await service.prepare(TENANT, RUN, 'engineer', user_id=USER)
    assert context is not None
    (service.root / project_path() / 'test/business.test.cjs').write_text('echo success', encoding='utf-8')
    with pytest.raises(GeneratedProjectError):
        await service.load(TENANT, RUN, 'engineer')
    repo.row.request = 'A different request.'
    with pytest.raises(GeneratedProjectError):
        await service.load(TENANT, RUN, 'engineer')


async def test_implementation_changes_do_not_redefine_acceptance(tmp_path: Path) -> None:
    service, _ = setup(tmp_path)
    first = await service.prepare(TENANT, RUN, 'engineer', user_id=USER)
    assert first is not None
    (service.root / project_path() / 'src/index.js').write_text('exports.add=(a,b)=>Number(a)+Number(b);', encoding='utf-8')
    current = await service.load(TENANT, RUN, 'tester')
    assert current is not None and current.envelope.sha256 == first.envelope.sha256


async def test_uncertain_capture_is_never_recaptured(tmp_path: Path) -> None:
    service, repo = setup(tmp_path)
    first = await service.prepare(TENANT, RUN, 'engineer', user_id=USER)
    assert first is not None
    repo.row.routing_decision['generated_project_preparation'] = {'schema_version': 1, 'phase': 'capturing'}
    with pytest.raises(GeneratedProjectError):
        await service.prepare(TENANT, RUN, 'engineer', user_id=USER)
    assert repo.updates == 2


async def test_invalid_python_recipe_can_be_repaired_before_freeze(tmp_path: Path) -> None:
    service, repo = setup(tmp_path)
    original = json.loads(manifest())
    original.update(stack='python', test_files=['tests/test_app.py'], criteria=[
        {'id': 'AC-001', 'description': 'Adds numbers', 'tests': ['test_app.Case.test_add']}])
    for name, body in {'ACCEPTANCE.json': json.dumps(original), 'pyproject.toml': '[project]\nname="app"\n',
                       'requirements.lock': 'not-a-hash-lock', 'tests/test_app.py': 'import unittest\n'}.items():
        write_file(service.root, f'{project_path()}/{name}', body)
    with pytest.raises(GeneratedProjectError, match='generated_acceptance_python_declaration_invalid'):
        await service.prepare(TENANT, RUN, 'engineer', user_id=USER)
    assert repo.updates == 0


@pytest.mark.parametrize('package', ['not json', '{}', '{"scripts":{"build":"","test":"node --test"}}',
    '{"scripts":{"build":"node -v","test":"node --test"},"workspaces":[]}',
    '{"scripts":{"build":"node -v","test":"node --test"},"devEngines":{}}'])
async def test_invalid_node_declaration_remains_repairable_before_freeze(tmp_path: Path, package: str) -> None:
    service, repo = setup(tmp_path)
    write_file(service.root, f'{project_path()}/package.json', package)
    with pytest.raises(GeneratedProjectError, match='generated_acceptance_node_declaration_invalid'):
        await service.prepare(TENANT, RUN, 'engineer', user_id=USER)
    assert repo.updates == 0
    write_file(service.root, f'{project_path()}/package.json', '{"scripts":{"build":"node -v","test":"node --test"}}')
    assert await service.prepare(TENANT, RUN, 'engineer', user_id=USER) is not None
