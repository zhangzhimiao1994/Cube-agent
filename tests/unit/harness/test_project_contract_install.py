"""Actual trusted npm guard checks, not native isolation or ordinary-user H05 proof."""

from __future__ import annotations

import errno
import json
import stat
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from tests.unit.harness.test_project_validation_sandbox import _install_locally, sandbox

_REGISTRY = 'https://registry.invalid'
_SOURCE_FILES = ('package.json', 'src/input.txt')
_LOCK_FILES = ('package-lock.json', 'npm-shrinkwrap.json')


@pytest.mark.parametrize('locked', [False, True])
def test_contract_install_recipe_keeps_checks_readonly_and_uses_original_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, locked: bool,
) -> None:
    monkeypatch.setattr(sandbox, 'sandbox_available', lambda: True)
    monkeypatch.setattr(sandbox, '_NODE_HOME', Path('/absent-trusted-node-home'))
    build: Any = getattr(sandbox, 'generated_contract_install_command', None)
    assert callable(build), 'contract install recipe missing'
    command = build(cwd=tmp_path, config={}, has_lock=locked)
    assert '--unshare-net' not in command
    assert ['--setenv', 'AGENT_HUB_CONTRACT_EMPTY_NODE_MODULES', '1'] in [
        command[i:i + 3] for i in range(len(command) - 2)]
    assert command[-4:] == (['ci', '--ignore-scripts', '--no-audit', '--no-fund'] if locked
                            else ['--ignore-scripts', '--no-audit', '--no-fund', '--package-lock=false'])
    assert '--ignore-scripts=false' not in command
    assert 'readdirSync' in command[command.index('-e') + 1]
    legacy = sandbox.generated_command(sandbox._INSTALL, cwd=tmp_path, config={})
    assert 'AGENT_HUB_CONTRACT_EMPTY_NODE_MODULES' not in legacy


def test_contract_install_option_is_not_model_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sandbox, 'sandbox_available', lambda: True)
    assert 'AGENT_HUB_CONTRACT_EMPTY_NODE_MODULES' not in sandbox.sandbox_environment({
        'AGENT_HUB_CONTRACT_EMPTY_NODE_MODULES': '1'})


@pytest.fixture
def install_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    monkeypatch.setattr(sandbox, 'sandbox_available', lambda: True)
    monkeypatch.setattr(sandbox, '_NODE_HOME', Path('/absent-trusted-node-home'))
    root = tmp_path / 'project'
    (root / 'src').mkdir(parents=True)
    (root / 'package.json').write_text(json.dumps({
        'name': 'contract-guard-fixture', 'version': '1.0.0', 'private': True,
        'dependencies': {}, 'devDependencies': {}, 'optionalDependencies': {}, 'scripts': {},
    }, indent=2) + '\n', encoding='utf-8')
    (root / 'src/input.txt').write_bytes(b'immutable fixture data; not executable code\n')
    for relative in _SOURCE_FILES:
        (root / relative).chmod(0o444)
    try:
        yield root
    finally:
        for relative in (*_SOURCE_FILES, *_LOCK_FILES):
            if (root / relative).is_file():
                (root / relative).chmod(0o600)


def _source_state(root: Path) -> dict[str, tuple[bytes, int, int] | None]:
    return {
        relative: (path.read_bytes(), stat.S_IMODE(path.stat().st_mode), path.stat().st_mtime_ns)
        if path.exists() else None
        for relative in (*_SOURCE_FILES, *_LOCK_FILES) for path in (root / relative,)
    }


def _actual_install(root: Path, *, contract: bool, locked: bool = False) -> subprocess.CompletedProcess[str]:
    """Run the unchanged trusted guard/npm, not bwrap or generated project code."""
    command_environment: dict[str, str] = {}

    def generated_command(command: Any, *, cwd: Path, config: Any) -> list[str]:
        if contract:
            argv: list[str] = sandbox.generated_contract_install_command(
                cwd=cwd, config=config, has_lock=locked,
            )
        else:
            argv = sandbox.generated_command(command, cwd=cwd, config=config)
        prefix = argv[:argv.index('--')]
        for index, token in enumerate(prefix):
            if token == '--setenv':
                command_environment[prefix[index + 1]] = prefix[index + 2]
        return argv

    def sandbox_environment(config: Any) -> dict[str, str]:
        env: dict[str, str] = sandbox.sandbox_environment(config)
        env.update(command_environment)
        # No dependencies exist and npm itself must not attempt registry fetches.
        env['NPM_CONFIG_OFFLINE'] = 'true'
        env['NPM_CONFIG_FETCH_RETRIES'] = '0'
        return env

    assembly = SimpleNamespace(
        generated_command=generated_command, sandbox_environment=sandbox_environment,
        _INSTALL=sandbox._INSTALL,
    )
    return _install_locally(assembly, root, _REGISTRY)


@pytest.mark.parametrize('entry', ['empty', 'nonempty', 'symlink', 'non_directory'])
def test_real_contract_guard_only_accepts_empty_node_modules(
    install_project: Path, tmp_path: Path, entry: str,
) -> None:
    root = install_project
    modules = root / 'node_modules'
    target = tmp_path / 'empty-link-target'
    if entry in {'empty', 'nonempty'}:
        modules.mkdir()
        if entry == 'nonempty':
            (modules / 'keep.txt').write_bytes(b'pre-existing metadata; never package code')
    elif entry == 'symlink':
        target.mkdir()
        try:
            modules.symlink_to(target, target_is_directory=True)
        except OSError as error:
            if sys.platform == 'win32' and (
                error.errno in {errno.EACCES, errno.EPERM, errno.ENOTSUP}
                or getattr(error, 'winerror', None) in {5, 50, 1314}
            ):
                pytest.skip('Windows filesystem policy denies directory symlink creation')
            raise
    else:
        modules.write_bytes(b'not a directory')

    before = _source_state(root)
    result = _actual_install(root, contract=True)
    assert _source_state(root) == before, 'guard/npm must not mutate source or root lockfiles'
    if entry == 'empty':
        assert result.returncode == 0, result.stderr
        assert 'generated dependency source rejected' not in result.stderr
        assert modules.is_dir() and not modules.is_symlink()
    else:
        assert result.returncode != 0
        assert 'generated dependency source rejected: pre-existing node_modules' in result.stderr
        if entry == 'nonempty':
            assert (modules / 'keep.txt').read_bytes() == b'pre-existing metadata; never package code'
        elif entry == 'symlink':
            assert modules.is_symlink() and list(target.iterdir()) == []
        else:
            assert modules.read_bytes() == b'not a directory'


def test_real_legacy_guard_rejects_even_empty_node_modules(
    install_project: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = install_project
    (root / 'node_modules').mkdir()
    # Ambient host state cannot grant the contract-only exception to legacy.
    monkeypatch.setenv('AGENT_HUB_CONTRACT_EMPTY_NODE_MODULES', '1')
    before = _source_state(root)
    result = _actual_install(root, contract=False)
    assert result.returncode != 0
    assert 'generated dependency source rejected: pre-existing node_modules' in result.stderr
    assert _source_state(root) == before
    assert list((root / 'node_modules').iterdir()) == []


@pytest.mark.parametrize('filename', _LOCK_FILES)
def test_real_contract_ci_keeps_trusted_empty_lock_and_source_unchanged(
    install_project: Path, filename: str,
) -> None:
    root = install_project
    (root / 'node_modules').mkdir()
    (root / filename).write_text(json.dumps({
        'name': 'contract-guard-fixture', 'version': '1.0.0', 'lockfileVersion': 3,
        'requires': True, 'packages': {
            '': {'name': 'contract-guard-fixture', 'version': '1.0.0'},
        },
    }, indent=2) + '\n', encoding='utf-8')
    (root / filename).chmod(0o444)
    before = _source_state(root)
    result = _actual_install(root, contract=True, locked=True)
    assert result.returncode == 0, result.stderr
    assert 'generated dependency source rejected' not in result.stderr
    assert _source_state(root) == before, 'npm ci must preserve original source and root lock bytes/modes/mtime'
