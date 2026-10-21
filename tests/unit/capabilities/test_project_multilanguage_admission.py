from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import pytest

from agent_hub.capabilities.runtime import RuntimeCapabilityError
from agent_hub.runtime.contracts import JsonValue
from tests.unit.capabilities.test_project_inspection import backend, inspect
from tests.unit.capabilities.test_project_verification import Executor, gateway, verify
from tests.unit.capabilities.test_scoped_read import RUN, TENANT, project_path, write_file

CASES = (
    ('go.mod', 'module example/demo\ngo 1.23\n', 'go'),
    ('pom.xml', '<project/>', 'java'),
    ('build.gradle.kts', '// no execution', 'java'),
    ('Cargo.toml', '[package]\nname = "demo"\nversion = "0.1.0"\n', 'rust'),
    ('demo.csproj', '<Project Sdk="Microsoft.NET.Sdk"/>', 'dotnet'),
    ('CMakeLists.txt', 'cmake_minimum_required(VERSION 3.20)\n', 'cmake'),
)


@pytest.mark.parametrize('filename,content,stack', CASES)
async def test_inspection_distinguishes_additional_stack_from_execution_readiness(
    tmp_path: Path, filename: str, content: str, stack: str,
) -> None:
    write_file(tmp_path / 'projects', f'{project_path()}/{filename}', content)
    result = await inspect(backend(tmp_path))
    observed = result.get('stack_discovery')
    assert isinstance(observed, Mapping)
    assert observed['detected_stacks'] == (stack,)
    assert observed['stack'] == stack
    assert observed['execution_supported'] is False
    assert observed['environment_ready'] is False
    assert observed['execution_proved'] is False
    assert result['install_plan_state'] == 'blocked'
    assert result['operations'] == {'build': 'unknown', 'test': 'unknown'}


@pytest.mark.parametrize('filename,content,_stack', CASES)
async def test_unsupported_project_never_uses_a_node_receipt(
    tmp_path: Path, filename: str, content: str, _stack: str,
) -> None:
    executor = Executor()
    subject = gateway(tmp_path, executor)
    (tmp_path / 'projects' / project_path() / 'package.json').unlink()
    write_file(tmp_path / 'projects', f'{project_path()}/{filename}', content)
    with pytest.raises(RuntimeCapabilityError, match='project stack execution is unsupported'):
        await verify(subject)
    assert executor.calls == []


@pytest.mark.parametrize('filename,content,_stack', CASES)
async def test_unsupported_project_cannot_bypass_delivery_verification(
    tmp_path: Path, filename: str, content: str, _stack: str,
) -> None:
    executor = Executor()
    subject = gateway(tmp_path, executor)
    (tmp_path / 'projects' / project_path() / 'package.json').unlink()
    write_file(tmp_path / 'projects', f'{project_path()}/{filename}', content)
    with pytest.raises(RuntimeCapabilityError, match='project stack execution is unsupported'):
        await subject.execute(tenant_id=TENANT, run_id=RUN, actor='engineer',
            name='workspace.bundle', arguments={}, idempotency_key='unsupported-bundle')
    assert executor.calls == []
    assert not (tmp_path / 'artifacts').exists()


async def test_adding_another_stack_invalidates_a_previously_passed_node_delivery(
    tmp_path: Path,
) -> None:
    executor = Executor()
    subject = gateway(tmp_path, executor)
    await verify(subject)
    assert len(executor.calls) == 1
    write_file(tmp_path / 'projects', f'{project_path()}/Cargo.toml', '[package]\n')
    with pytest.raises(RuntimeCapabilityError, match='project stack execution is unsupported'):
        await subject.execute(tenant_id=TENANT, run_id=RUN, actor='engineer',
            name='workspace.bundle', arguments={}, idempotency_key='changed-stack')
    assert len(executor.calls) == 1


@pytest.mark.parametrize('filename,content,_stack', CASES)
async def test_inline_zip_cannot_bypass_additional_language_checks(
    tmp_path: Path, filename: str, content: str, _stack: str,
) -> None:
    subject = gateway(tmp_path, Executor())
    with pytest.raises(RuntimeCapabilityError, match='project stack execution is unsupported'):
        await subject.execute(tenant_id=TENANT, run_id=RUN, actor='engineer',
            name='project.generate_zip', arguments={'title': 'Unchecked', 'files': {filename: content}},
            idempotency_key='unsupported-inline')
    assert not (tmp_path / 'artifacts').exists()


async def test_source_without_go_mod_is_not_treated_as_node(tmp_path: Path) -> None:
    executor = Executor()
    subject = gateway(tmp_path, executor)
    (tmp_path / 'projects' / project_path() / 'package.json').unlink()
    write_file(tmp_path / 'projects', f'{project_path()}/main.go', 'package main\nfunc main() {}\n')
    with pytest.raises(RuntimeCapabilityError, match='project stack execution is unsupported'):
        await verify(subject)
    assert executor.calls == []


async def test_readme_only_attachments_can_still_be_bundled(tmp_path: Path) -> None:
    executor = Executor()
    subject = gateway(tmp_path, executor)
    (tmp_path / 'projects' / project_path() / 'package.json').unlink()
    write_file(tmp_path / 'projects', f'{project_path()}/README.md', 'ordinary document\n')
    result: Mapping[str, JsonValue] = await subject.execute(tenant_id=TENANT, run_id=RUN,
        actor='engineer', name='workspace.bundle', arguments={}, idempotency_key='document-bundle')
    assert 'verification_receipt_sha256' not in result
    assert 'bundle_download_url' in result
    assert executor.calls == []


@pytest.mark.parametrize('filename', ['GO.MOD', 'Cargo.TOML', 'Demo.CSPROJ', 'Main.GO'])
async def test_case_variant_cannot_bypass_node_or_inline_delivery(
    tmp_path: Path, filename: str,
) -> None:
    executor = Executor()
    subject = gateway(tmp_path, executor)
    write_file(tmp_path / 'projects', f'{project_path()}/{filename}', 'declaration\n')
    with pytest.raises(RuntimeCapabilityError, match='project stack execution is unsupported'):
        await verify(subject)
    with pytest.raises(RuntimeCapabilityError, match='project stack execution is unsupported'):
        await subject.execute(tenant_id=TENANT, run_id=RUN, actor='engineer',
            name='workspace.bundle', arguments={}, idempotency_key='variant-bundle')
    with pytest.raises(RuntimeCapabilityError, match='project stack execution is unsupported'):
        await subject.execute(tenant_id=TENANT, run_id=RUN, actor='engineer',
            name='project.generate_zip', arguments={'title': 'Unchecked', 'files': {filename: 'declaration'}},
            idempotency_key='variant-inline')
    assert executor.calls == []


async def test_unicode_casefold_is_not_a_new_node_path_collision(tmp_path: Path) -> None:
    executor = Executor()
    subject = gateway(tmp_path, executor)
    write_file(tmp_path / 'projects', f'{project_path()}/Stra\u00dfe/a.js', 'module.exports = 1;\n')
    write_file(tmp_path / 'projects', f'{project_path()}/Strasse/b.js', 'module.exports = 2;\n')
    result = await verify(subject)
    assert isinstance(result['verification'], Mapping)
    assert result['verification']['status'] == 'passed'
    assert len(executor.calls) == 1


def test_go_manifest_block_comment_is_not_a_supported_declaration() -> None:
    from agent_hub.capabilities.project_stack_registry import inspect_stack

    observed = inspect_stack({'go.mod': b'module example.org/demo\ngo 1.23\n/* invalid */\n'})
    assert observed.declaration_supported is False
    assert observed.reason == 'go_declaration_invalid'
