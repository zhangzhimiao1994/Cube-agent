"""Real wheel archives and generated capture; simulated in-process Root transport.

These tests prove the local capture/write boundary with actual metadata checking,
not native Root identity, installation, approval, or end-to-end delivery. Fixture wheels
are valid synthetic distributions, not working setuptools/wheel toolchains.
"""

from __future__ import annotations

import base64
import csv
import hashlib
import io
import json
import zipfile
from email.parser import BytesParser
from pathlib import Path

import pytest

from agent_hub.capabilities.generated_project_service import (
    GeneratedProjectDraftRejected,
    GeneratedProjectError,
    GeneratedProjectService,
    validate_generated_preparation,
)
from agent_hub.capabilities.python_dependency_closure import check_python_dependency_closure
from agent_hub.capabilities.python_project_recipe import parse_python_project_declaration
from agent_hub.capabilities.runtime import RuntimeCapabilityError, RuntimeCapabilityGateway
from agent_hub.skills.sandbox.project import project_source_fingerprint
from agent_hub.skills.sandbox.python_dependency_metadata import (
    PythonDependencyMetadataIndex,
    extract_python_wheel_metadata,
)
from agent_hub.skills.sandbox.python_dependency_protocol import PythonDependencyClosureObservation
from agent_hub.skills.sandbox.python_input_fetch import WheelDownload, validate_wheel_bytes
from tests.unit.capabilities.test_generated_dependency_draft import observation
from tests.unit.capabilities.test_generated_project_service import USER, Repository, setup
from tests.unit.capabilities.test_python_project_recipe import MANIFEST
from tests.unit.capabilities.test_scoped_read import RUN, TENANT, project_path, write_file


class NormalizedRepository(Repository):
    """Match production normalization before comparing nested JSON state."""

    async def get(self, tenant_id, run_id):
        row = await super().get(tenant_id, run_id)
        if 'generated_project_preparation' in row.routing_decision:
            row.routing_decision = {**row.routing_decision, 'generated_project_preparation':
                validate_generated_preparation(row.routing_decision['generated_project_preparation'])}
        return row

    async def compare_and_set_generated_preparation(self, tenant_id, run_id, **kw):
        guard = kw.pop('before_reservation', None)
        if guard is not None:
            await guard()
        if 'generated_project_preparation' in self.row.routing_decision:
            self.row.routing_decision = {**self.row.routing_decision, 'generated_project_preparation':
                validate_generated_preparation(self.row.routing_decision['generated_project_preparation'])}
        return await super().compare_and_set_generated_preparation(tenant_id, run_id, **kw)


def _wheel(catalog: Path, name: str, version: str, requires: str = '') -> WheelDownload:
    distribution = name.replace('-', '_')
    info = f'{distribution}-{version}.dist-info'
    metadata = f'Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n'
    if requires:
        metadata += f'Requires-Dist: {requires}\n'
    files = {
        f'{distribution}/__init__.py': b'',
        f'{info}/METADATA': (metadata + '\n').encode('ascii'),
        f'{info}/WHEEL': (
            b'Wheel-Version: 1.0\nGenerator: closure-fixture\n'
            b'Root-Is-Purelib: true\nTag: py3-none-any\n\n'
        ),
    }
    record = io.StringIO(newline='')
    writer = csv.writer(record, lineterminator='\n')
    for path, body in files.items():
        digest = base64.urlsafe_b64encode(hashlib.sha256(body).digest()).rstrip(b'=').decode()
        writer.writerow((path, 'sha256=' + digest, len(body)))
    writer.writerow((f'{info}/RECORD', '', ''))
    files[f'{info}/RECORD'] = record.getvalue().encode('ascii')
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w') as archive:
        for path, body in files.items():
            member = zipfile.ZipInfo(path, date_time=(2026, 1, 1, 0, 0, 0))
            member.external_attr = 0o100644 << 16
            archive.writestr(member, body)
    raw = output.getvalue()
    filename = f'{distribution}-{version}-py3-none-any.whl'
    entry = WheelDownload(filename, name, version, hashlib.sha256(raw).hexdigest(), len(raw),
        f'https://files.pythonhosted.org/packages/closure/fixture/{filename}')
    target = catalog / filename
    target.write_bytes(raw)
    validate_wheel_bytes(entry, target.read_bytes())
    return entry


@pytest.fixture
def draft(tmp_path: Path) -> tuple[GeneratedProjectService, Repository, str]:
    service, repo = setup(tmp_path)
    normalized = NormalizedRepository()
    normalized.row = repo.row
    repo = normalized
    source = service.root / project_path()
    for relative in ('package.json', 'src/index.js', 'test/business.test.cjs'):
        (source / relative).unlink()
    catalog = tmp_path / 'wheel-catalog'
    catalog.mkdir()
    selected = (
        _wheel(catalog, 'demo-dep', '1.0', 'demo-helper==1.0'),
        _wheel(catalog, 'setuptools', '80.9.0'),
        _wheel(catalog, 'wheel', '0.45.1'),
    )
    helper = _wheel(catalog, 'demo-helper', '1.0')
    with zipfile.ZipFile(catalog / selected[0].filename) as archive:
        metadata = BytesParser().parsebytes(archive.read('demo_dep-1.0.dist-info/METADATA'))
    assert metadata.get_all('Requires-Dist') == ['demo-helper==1.0']
    lock = ''.join(f'{w.name}=={w.version} --hash=sha256:{w.sha256}\n' for w in selected)
    manifest = MANIFEST.replace(b'packaging==25.0', b'demo-dep==1.0')
    declaration = parse_python_project_declaration(manifest, lock.encode(),
        max_control_bytes=256 * 1024, max_requirements=128)
    assert {w.name for w in declaration.requirements} == {'demo-dep', 'setuptools', 'wheel'}
    acceptance = {
        'schema_version': 1, 'stack': 'python', 'editable_paths': ['src'],
        'test_files': ['tests/test_billing.py'], 'criteria': [{
            'id': 'AC-001', 'description': 'Returns the requested total',
            'tests': ['test_billing.BillingTests.test_total'],
        }],
    }
    for path, body in {
        'ACCEPTANCE.json': json.dumps(acceptance), 'pyproject.toml': manifest.decode(),
        'requirements.lock': lock, 'src/billing/__init__.py': 'def total(): return 12\n',
        'tests/test_billing.py': (
            'import unittest\nfrom billing import total\n'
            'class BillingTests(unittest.TestCase):\n'
            '    def test_total(self): self.assertEqual(total(), 12)\n'
        ),
    }.items():
        write_file(service.root, f'{project_path()}/{path}', body)
    corrected = lock + f'{helper.name}=={helper.version} --hash=sha256:{helper.sha256}\n'
    records = tuple(sorted((extract_python_wheel_metadata(entry,
        (catalog / entry.filename).read_bytes()) for entry in (*selected, helper)),
        key=lambda row:(row.wheel.name,row.wheel.version,row.wheel.filename,row.wheel.sha256)))
    index = PythonDependencyMetadataIndex('d'*64, records)

    async def inspect_dependencies(invocation, expected_source):
        assert invocation.sandbox_profile == 'restricted' and invocation.install_allowed is False
        assert await project_source_fingerprint(service.root, invocation) == expected_source
        manifest_bytes = (source / 'pyproject.toml').read_bytes()
        lock_bytes = (source / 'requirements.lock').read_bytes()
        declared = parse_python_project_declaration(manifest_bytes, lock_bytes,
            max_control_bytes=256*1024, max_requirements=128)
        data = observation()
        data.update(source_sha256=expected_source,
            manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
            lock_sha256=hashlib.sha256(lock_bytes).hexdigest(), metadata_index_sha256=index.sha256,
            closure=check_python_dependency_closure(declared.requirements, index.records,
                data['target_environment']).to_payload())
        return PythonDependencyClosureObservation.model_validate(data, strict=True)

    return GeneratedProjectService(root=service.root, repository=repo,
        dependency_inspector=inspect_dependencies), repo, corrected


async def test_missing_transitive_dependency_is_captured_and_lock_correction_is_frozen(
    draft: tuple[GeneratedProjectService, Repository, str], tmp_path: Path,
) -> None:
    service, repo, corrected = draft
    context = await service.prepare(TENANT, RUN, 'engineer', user_id=USER)
    assert context is not None and context.state['phase'] == 'captured'
    assert repo.updates == 2
    original_lock = (service.root / project_path() / 'requirements.lock').read_bytes()
    assert b'demo-helper' not in original_lock
    assert {'pyproject.toml', 'requirements.lock'} <= context.stored.contract.protected_files.keys()
    backend = RuntimeCapabilityGateway(skill_store_dir=tmp_path / 'skills',
        project_workspace_dir=service.root, run_repository=repo)
    with pytest.raises(RuntimeCapabilityError, match='original project checks are protected'):
        await backend.execute(tenant_id=TENANT, run_id=RUN, actor='engineer',
            name='workspace.write_text', arguments={'path': 'requirements.lock', 'content': corrected},
            idempotency_key='add-missing-transitive-dependency')
    assert (service.root / project_path() / 'requirements.lock').read_bytes() == original_lock
    current = await service.load(TENANT, RUN, 'engineer')
    assert current is not None and current.envelope.sha256 == context.envelope.sha256


async def test_missing_transitive_dependency_is_repairable_before_generated_capture(
    draft: tuple[GeneratedProjectService, Repository, str],
) -> None:
    service, repo, _ = draft
    repo.row.routing_decision['python_dependency_preflight_version'] = 1
    with pytest.raises(GeneratedProjectDraftRejected) as caught:
        await service.prepare(TENANT, RUN, 'engineer', user_id=USER)
    rejection = caught.value.rejection.to_payload()
    assert rejection['phase'] == 'before_capture' and rejection['execution_started'] is False
    assert 'demo-helper' in json.dumps(rejection), 'Name the missing dependency so the draft can be fixed'
    assert repo.updates == 0
    assert 'generated_project_preparation' not in repo.row.routing_decision
    assert not (service.root / '.project-contracts' / str(TENANT) / str(RUN)).exists()


async def test_rejection_then_actual_lock_write_recheck_and_capture(draft, tmp_path: Path) -> None:
    service, repo, corrected = draft
    repo.row.routing_decision['python_dependency_preflight_version'] = 1
    with pytest.raises(GeneratedProjectDraftRejected):
        await service.prepare(TENANT, RUN, 'engineer', user_id=USER)
    backend = RuntimeCapabilityGateway(skill_store_dir=tmp_path/'skills',
        project_workspace_dir=service.root, run_repository=repo)
    await backend.execute(tenant_id=TENANT, run_id=RUN, actor='engineer',
        name='workspace.write_text', arguments={'path':'requirements.lock','content':corrected},
        idempotency_key='correct-uncaptured-dependency-lock')
    context = await service.prepare(TENANT, RUN, 'engineer', user_id=USER)
    assert context is not None and context.state['phase'] == 'captured'
    assert repo.updates == 2
    proof = context.state['python_dependency_preflight']
    assert proof['closure']['status'] == 'complete'
    assert proof['source_sha256'] == context.stored.reference.input_source_sha256


async def test_forged_preflight_source_cannot_capture(draft) -> None:
    service, repo, _ = draft
    repo.row.routing_decision['python_dependency_preflight_version'] = 1
    actual = service._dependency_inspector

    async def changed(invocation, source):
        value = await actual(invocation, source)
        return value.model_copy(update={'source_sha256':'f'*64})

    service._dependency_inspector = changed
    with pytest.raises(GeneratedProjectError):
        await service.prepare(TENANT, RUN, 'engineer', user_id=USER)
    assert repo.updates == 0
