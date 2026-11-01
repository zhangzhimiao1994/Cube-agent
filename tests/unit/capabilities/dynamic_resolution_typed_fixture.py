"""Typed SQL/transport boundaries for the existing offline resolution scenarios."""
from __future__ import annotations

import base64
import csv
import hashlib
import io
import json
import os
import zipfile
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from email.parser import BytesParser
from pathlib import Path
from typing import Literal, NotRequired, Protocol, TypedDict, TypeGuard, Unpack
from uuid import UUID, uuid4

from agent_hub.auth.models import Role
from agent_hub.capabilities.generated_project_service import (
    GeneratedProjectService,
    validate_generated_preparation,
)
from agent_hub.capabilities.python_dependency_closure import (
    PythonDependencyClosure,
    PythonDependencyClosureIssue,
    check_python_dependency_closure,
)
from agent_hub.capabilities.python_project_recipe import parse_python_project_declaration
from agent_hub.domain.runs import RunStatus, TaskMode
from agent_hub.runs.repository import RunRecord
from agent_hub.runtime.contracts import JsonValue
from agent_hub.skills.sandbox.project import ProjectInvocation, project_source_fingerprint
from agent_hub.skills.sandbox.python_dependency_metadata import (
    PythonDependencyMetadataIndex,
    extract_python_wheel_metadata,
)
from agent_hub.skills.sandbox.python_dependency_protocol import PythonDependencyClosureObservation
from agent_hub.skills.sandbox.python_input_fetch import WheelDownload, validate_wheel_bytes
from agent_hub.skills.sandbox.python_metadata_plan_protocol import PythonMetadataPlanningRequest
from agent_hub.skills.sandbox.python_registry_protocol import DependencyNeed, ResolutionDescriptor
from agent_hub.skills.sandbox.python_resolution_protocol import (
    ResolutionClaim,
    ResolutionPlan,
    ResolutionStatus,
)
from tests.unit.capabilities.test_python_project_recipe import MANIFEST
from tests.unit.capabilities.test_scoped_read import RUN, TENANT, project_path, write_file


class ResolveArgs(TypedDict):
    needs: tuple[DependencyNeed, ...]
    environment: dict[str, str]
    planning: PythonMetadataPlanningRequest


class InspectArgs(ResolveArgs):
    plan: NotRequired[ResolutionPlan | None]
    expected_plan_sha256: NotRequired[str | None]


class PublishArgs(TypedDict):
    needs: tuple[DependencyNeed, ...]
    environment: dict[str, str]


class CASArgs(TypedDict):
    actor_id: UUID
    actor_role: Role
    lease_token: UUID
    request_sha256: str
    project_id: str
    session_id: str
    expected: Mapping[str, JsonValue] | None
    replacement: Mapping[str, JsonValue]
    before_reservation: NotRequired[Callable[[], Awaitable[None]]]


class CAS(Protocol):
    async def __call__(self, tenant: UUID, run: UUID, **kwargs: Unpack[CASArgs]) -> RunRecord: ...


class FlowRepository:
    def __init__(self, row: RunRecord) -> None:
        self.row = row
        self.updates = 0
        self.get: Callable[[UUID, UUID], Awaitable[RunRecord]] = self._get
        self.is_capability_approval_scope_approved: Callable[[UUID, UUID, str], Awaitable[bool]] = self._denied
        self.compare_and_set_generated_preparation: CAS = self._cas

    async def _get(self, tenant: UUID, run: UUID) -> RunRecord:
        assert (tenant, run) == (self.row.tenant_id, self.row.id)
        if 'generated_project_preparation' in present(self.row.routing_decision):
            self.row = replace(self.row, routing_decision={**present(self.row.routing_decision),
                'generated_project_preparation': validate_generated_preparation(present(self.row.routing_decision)['generated_project_preparation'])})
        return self.row

    async def _denied(self, tenant: UUID, run: UUID, scope: str) -> bool:
        return False

    async def _cas(self, tenant: UUID, run: UUID, **kwargs: Unpack[CASArgs]) -> RunRecord:
        assert (tenant, run) == (self.row.tenant_id, self.row.id)
        guard = kwargs.get('before_reservation')
        if guard is not None:
            await guard()
        route = present(self.row.routing_decision)
        if 'generated_project_preparation' in route:
            self.row = replace(self.row, routing_decision={**route,
                'generated_project_preparation': validate_generated_preparation(route['generated_project_preparation'])})
        assert kwargs['actor_id'] == self.row.actor_id
        assert kwargs['lease_token'] == self.row.worker_lease_token
        assert kwargs['request_sha256'] == hashlib.sha256(self.row.request.encode()).hexdigest()
        assert present(self.row.routing_decision).get('generated_project_preparation') == kwargs['expected']
        self.row = replace(self.row, routing_decision={**present(self.row.routing_decision),
            'generated_project_preparation': kwargs['replacement']})
        self.updates += 1
        return self.row


@dataclass
class RootState:
    descriptor: ResolutionDescriptor | None = None
    origin: ResolutionClaim | None = None
    plan: ResolutionPlan | None = None
    published: bool = False


def mutable_mapping(value: object) -> TypeGuard[dict[str, object]]:
    return isinstance(value, dict) and all(isinstance(key, str) for key in value)


def mutable(value: object) -> dict[str, object]:
    assert mutable_mapping(value)
    return value


def object_mapping(value: object) -> TypeGuard[Mapping[str, object]]:
    return isinstance(value, Mapping) and all(isinstance(key, str) for key in value)


def mapping(value: object) -> Mapping[str, object]:
    assert object_mapping(value)
    return value


def present[T](value: T | None) -> T:
    assert value is not None
    return value


def text(value: object) -> str:
    assert isinstance(value, str)
    return value


def _strings(value: object) -> TypeGuard[Mapping[str, str]]:
    return object_mapping(value) and all(isinstance(item, str) for item in value.values())


def strings(value: object) -> Mapping[str, str]:
    assert _strings(value)
    return value


class RejectEffects:
    async def resolve(self, **kwargs: Unpack[ResolveArgs]) -> ResolutionStatus:
        raise AssertionError("inspect-only fixture cannot resolve")

    async def freeze(self, **kwargs: Unpack[ResolveArgs]) -> ResolutionStatus:
        raise AssertionError("inspect-only fixture cannot freeze")

    async def publish(self, plan: ResolutionPlan, **kwargs: Unpack[PublishArgs]) -> ResolutionStatus:
        raise AssertionError("inspect-only fixture cannot publish")


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


def draft_case(tmp_path: Path) -> tuple[GeneratedProjectService, FlowRepository, str]:
    root = tmp_path / 'projects'
    root.parent.mkdir(parents=True, exist_ok=True)
    if os.name == 'nt':
        from agent_hub.plugins.handover_snapshots import _windows_security
        _windows_security(root, private=True, create=True)
    else:
        root.mkdir(mode=0o700)
    acceptance = {'schema_version': 1, 'stack': 'node', 'editable_paths': ['src'],
        'test_files': ['test/business.test.cjs'], 'criteria': [
            {'id': 'AC-001', 'description': 'Adds two numbers', 'tests': ['adds two numbers']}]}
    for name, content in {'ACCEPTANCE.json': json.dumps(acceptance), 'src/index.js': 'exports.add=(a,b)=>a+b;',
        'package.json': '{"scripts":{"build":"node -v","test":"node --test"}}',
        'test/business.test.cjs': 'require("node:test").test("adds two numbers",()=>{});'}.items():
        write_file(root, f'{project_path()}/{name}', content)
    row = RunRecord(id=RUN, tenant_id=TENANT, actor_id=UUID('66666666-6666-4666-8666-666666666666'),
        actor_role=Role.OPERATOR, request='Build a working addition project.',
        status=RunStatus.RUNNING, mode=TaskMode.DIRECT, version=1, created_at=datetime.now(UTC),
        worker_id='worker-a', worker_lease_token=uuid4(), worker_lease_expires_at=datetime.now(UTC)+timedelta(minutes=10),
        routing_decision={'generated_project_contract_version': 1, 'project_id': 'project-a',
            'workspace_session_id': 'session-a', 'sandbox_profile': 'workspace_write',
            'execution_backend': 'systemd', 'runtime_timeout_seconds': 120,
            'requested_permissions': ['workspace.read', 'workspace.write', 'command.run']})
    repo = FlowRepository(row)
    service = GeneratedProjectService(root=root, repository=repo)
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

    async def inspect_dependencies(invocation: ProjectInvocation, expected_source: str) -> PythonDependencyClosureObservation:
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
                strings(data['target_environment'])).to_payload())
        return PythonDependencyClosureObservation.model_validate(data, strict=True)

    return GeneratedProjectService(root=service.root, repository=repo,
        dependency_inspector=inspect_dependencies), repo, corrected


def observation(status: Literal['complete', 'incomplete'] = 'incomplete') -> dict[str, object]:
    environment = {
        'implementation_name':'cpython', 'implementation_version':'3.12.14',
        'os_name':'posix', 'platform_machine':'x86_64', 'platform_release':'6.1',
        'platform_system':'Linux', 'platform_version':'1',
        'platform_python_implementation':'CPython', 'python_full_version':'3.12.14',
        'python_version':'3.12', 'sys_platform':'linux',
    }
    issues = () if status == 'complete' else (
        PythonDependencyClosureIssue('missing_dependency', 'wheel', 'packaging'),)
    closure = PythonDependencyClosure(status, issues)
    data = PythonDependencyClosureObservation.model_validate({
        'source_sha256':'a'*64, 'manifest_sha256':'b'*64, 'lock_sha256':'c'*64,
        'catalog_sha256':'d'*64, 'metadata_index_sha256':'e'*64,
        'target_environment':environment,
        'target_environment_sha256':hashlib.sha256(json.dumps(environment, sort_keys=True,
            separators=(',', ':'), ensure_ascii=True).encode()).hexdigest(),
        'closure':closure.to_payload(),
    }, strict=True)
    return data.model_dump(mode='json')
