"""Private current-export disposal; native FD proofs are Linux-only."""
from __future__ import annotations

import hashlib
import os
import sys
import time
from pathlib import Path

import pytest

from agent_hub.capability_installer.python_attestation import ScanLimits
from agent_hub.skills.sandbox import python_project_copy as copy

NATIVE = pytest.mark.skipif(sys.platform != 'linux' or os.geteuid() != 0,
                            reason='requires Linux Root private-fixture FDs, not simulated')


def tree(parent: Path) -> Path:
    target = parent / 'environment'
    target.mkdir(mode=0o700)
    (target / 'bin').mkdir(mode=0o700)
    (target / 'lib').mkdir(mode=0o700)
    for name in ('python', 'python3', 'python3.12'):
        path = target / 'bin' / name
        path.write_bytes(b'dummy interpreter; never executed')
        path.chmod(0o555)
    (target / 'lib64').symlink_to('lib')
    for path in (target / 'bin', target / 'lib', target):
        path.chmod(0o555)
    return target


@NATIVE
@pytest.mark.parametrize('mutation', [None, 'hardlink', 'symlink', 'new_member', 'replaced'])
def test_disposal_only_removes_exact_verified_export_and_preserves_product_and_controls(
    tmp_path: Path, mutation: str | None,
) -> None:
    ready = tmp_path / 'ready'
    ready.mkdir(mode=0o700)
    environment = tree(ready)
    (tmp_path / 'product').write_bytes(b'preserve same-artifact product')
    protected = tmp_path / 'protected-cache.json'
    protected.write_bytes(b'preserve all historical cache controls')
    before = hashlib.sha256(protected.read_bytes()).hexdigest()
    parent = os.open(ready, copy._directory_flags())
    descriptor = os.open(environment, copy._directory_flags())
    try:
        snapshot = copy._snapshot(descriptor, 0, ScanLimits(1024**2), time.monotonic() + 5)
        if mutation is not None:
            environment.chmod(0o755)
            if mutation == 'new_member':
                (environment / 'unexpected').write_bytes(b'x')
            elif mutation == 'replaced':
                environment.rename(ready / 'old-environment')
                tree(ready)
            else:
                (environment / 'bin').chmod(0o755)
                (environment / 'bin/python').unlink()
                if mutation == 'hardlink':
                    os.link(protected, environment / 'bin/python')
                else:
                    (environment / 'bin/python').symlink_to(protected)
            # The held-directory observer rejects the changed root identity
            # before disposal; its shared finite error is a plain ValueError.
            with pytest.raises(ValueError, match='^project original contract staging unavailable or unsafe$'):
                copy.remove_exported_python_environment(parent, descriptor, snapshot,
                    owner=0, deadline=time.monotonic() + 5)
            assert environment.exists()
        else:
            copy.remove_exported_python_environment(parent, descriptor, snapshot,
                owner=0, deadline=time.monotonic() + 5)
            assert not environment.exists() and ready.is_dir()
        assert (tmp_path / 'product').read_bytes() == b'preserve same-artifact product'
        assert hashlib.sha256(protected.read_bytes()).hexdigest() == before
    finally:
        os.close(descriptor)
        os.close(parent)
        # Test fixture cleanup, never production/cache cleanup.
        for path in (tmp_path, *tmp_path.rglob('*')):
            if path.is_dir() and not path.is_symlink():
                path.chmod(0o700)


@NATIVE
def test_timeout_before_disposal_preserves_environment(tmp_path: Path) -> None:
    ready = tmp_path / 'ready'
    ready.mkdir(mode=0o700)
    environment = tree(ready)
    parent = os.open(ready, copy._directory_flags())
    descriptor = os.open(environment, copy._directory_flags())
    try:
        captured = copy._snapshot(descriptor, 0, ScanLimits(1024**2), time.monotonic() + 5)
        with pytest.raises(copy.PythonProjectCopyError, match='copy_timeout'):
            copy.remove_exported_python_environment(parent, descriptor, captured,
                owner=0, deadline=time.monotonic() - 1)
        assert environment.is_dir()
    finally:
        os.close(parent)
        os.close(descriptor)
        for path in (tmp_path, *tmp_path.rglob('*')):
            if path.is_dir() and not path.is_symlink():
                path.chmod(0o700)


def test_non_native_disposal_rejects_before_any_fd_access() -> None:
    if sys.platform == 'linux' and os.geteuid() == 0:
        pytest.skip('non-native rejection only')
    with pytest.raises(copy.PythonProjectCopyError, match='copy_unsupported'):
        copy.remove_exported_python_environment(-1, -1, {}, owner=0, deadline=time.monotonic() + 5)


@NATIVE
@pytest.mark.parametrize('defect', [None, 'hash', 'limit', 'interpreter', 'unlink'])
def test_root_measures_and_removes_current_export_before_issuing_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, defect: str | None,
) -> None:
    from agent_hub.capabilities import python_project_inputs as inputs_api
    from agent_hub.skills.sandbox import python_project_backend as backend
    from agent_hub.skills.sandbox.project import _Execution
    from agent_hub.skills.sandbox.python_project_protocol import (
        PythonProjectInvocation,
        base_python_invocation,
    )
    from agent_hub.subprocess_cleanup import ProcessCleanupIncomplete
    from tests.unit.capabilities.test_python_project_inputs import bind, runtime
    from tests.unit.skills.sandbox.test_python_project_backend import configuration_payload

    binary = b'dummy interpreter; never executed'
    inputs = inputs_api.PythonProjectInputs.from_payload(bind(inputs_api,
        interpreter=runtime(inputs_api, binary_sha256=hashlib.sha256(binary).hexdigest())).to_payload())
    item = PythonProjectInvocation.model_validate({'execution_id': 'ephemeral_one',
        'tenant_id': inputs.contract.tenant_id, 'run_id': inputs.contract.run_id,
        'project_id': inputs.contract.project_id, 'session_id': inputs.contract.session_id,
        'sandbox_profile': 'workspace_write', 'install_allowed': True, 'source_kind': 'original',
        'timeout_seconds': 30, 'original_reference': inputs.original_reference.to_payload(),
        'recipe_sha256': inputs.sha256, 'environment_input_sha256': inputs.environment_input_sha256}, strict=True)
    payload = configuration_payload()
    payload['interpreter'] = inputs.interpreter.to_payload()
    if defect == 'interpreter':
        payload['interpreter']['binary_sha256'] = '0' * 64
    config = backend.RootConfig.from_payload(payload)
    export = tmp_path / 'export'
    export.mkdir(mode=0o1777)
    export.chmod(0o1777)
    ready = export / 'ready'
    ready.mkdir(mode=0o700)
    environment = tree(ready)
    product = export / 'product'
    product.write_bytes(b'product must survive verified environment removal')
    actual = copy._observe(environment, inputs.environment_input_sha256, ScanLimits(1024**2),
                           time.monotonic() + 5)
    job = _Execution(base_python_invocation(item), None, stage=tmp_path, launched=True, quiet=True,
                     source_sha256=inputs.original_reference.input_source_sha256)
    parent = os.open(tmp_path, copy._directory_flags())
    if defect == 'unlink':
        def refuse(*args: object, **kwargs: object) -> None:
            raise OSError('fixture-only disposal fault')
        monkeypatch.setattr(os, 'unlink', refuse)
    try:
        args = (parent, export, inputs, config, 1 if defect == 'limit' else 1024**2,
                '0' * 64 if defect == 'hash' else actual.environment_sha256, time.monotonic() + 5)
        if defect is None:
            evidence = backend._consume_ephemeral_export(*args, execution=job,
                source_sha256=inputs.original_reference.input_source_sha256, product=True)
            evidence.require_for(item)
            assert evidence.environment_bytes == 3 * len(binary)
            assert not ready.exists() and product.read_bytes().startswith(b'product must survive')
        else:
            expected = ProcessCleanupIncomplete if defect == 'unlink' else ValueError
            with pytest.raises(expected):
                backend._consume_ephemeral_export(*args, execution=job,
                    source_sha256=inputs.original_reference.input_source_sha256, product=True)
            assert ready.exists() and product.exists()
    finally:
        os.close(parent)
        for path in (tmp_path, *tmp_path.rglob('*')):
            if path.is_dir() and not path.is_symlink():
                path.chmod(0o700)
