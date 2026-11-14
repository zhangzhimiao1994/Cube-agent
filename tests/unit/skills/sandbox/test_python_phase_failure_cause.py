"""Real phase failures retain finite cause evidence without exposing exceptions."""
import json
import sys

import pytest

from agent_hub.capabilities.python_project_reports import _check
from tests.unit.skills.sandbox.python_project_phase_typed_fixture import PhaseModule

pytest_plugins = ('tests.unit.skills.sandbox.test_python_project_phase',)


def cause_record(stderr: str) -> dict[str, object]:
    lines = [line for line in stderr.splitlines() if line.startswith('PYTHON_PHASE_FAILURE ')]
    assert len(lines) == 1
    value: object = json.loads(lines[0].removeprefix('PYTHON_PHASE_FAILURE '))
    assert isinstance(value, dict)
    return value


@pytest.mark.parametrize('failure', ['import', 'assertion'])
def test_real_test_phase_distinguishes_import_from_test_result_failure(
    isolated: PhaseModule, capfd: pytest.CaptureFixture[str], failure: str,
) -> None:
    site = isolated._OUTPUT / 'project-site'
    site.mkdir()
    name = 'phase_evidence_' + failure
    package = site / name
    package.mkdir()
    package.joinpath('__init__.py').write_text(
        "raise ModuleNotFoundError('password=SHOULD_NEVER_LEAVE_IMPORT')\n" if failure == 'import' else '',
        encoding='utf-8')
    if failure == 'assertion':
        (isolated._WORKSPACE / 'tests/test_diagnostic_case.py').write_text(
            'import unittest\nclass Contract(unittest.TestCase):\n'
            ' def test_contract(self):\n  self.assertEqual(1, 2, "controlled assertion detail")\n',
            encoding='utf-8')
    try:
        assert isolated.main(['test', '--import-module', name]) == 1
        captured = capfd.readouterr()
        assert json.loads(captured.out) == {'status': 'failed', 'error': 'phase_failed'}
        record = cause_record(captured.err)
        assert record['phase'] == 'test'
        assert record['stage'] == ('test_import' if failure == 'import' else 'test_execution')
        assert record['cause'] == ('import_error' if failure == 'import' else 'test_result_failed')
        assert 'SHOULD_NEVER_LEAVE_IMPORT' not in captured.err
        if failure == 'assertion':
            assert 'controlled assertion detail' in captured.err
        safe, detail, diagnostic = _check({
            'phase': 'test', 'status': 'failed', 'exit_code': 1, 'timed_out': False,
            'stdout': captured.out, 'stderr': captured.err,
            'stdout_truncated': False, 'stderr_truncated': False,
            'stdout_observed_bytes': len(captured.out.encode()),
            'stderr_observed_bytes': len(captured.err.encode()),
        }, phase='test', actual_phase='test', import_module=name, limit=4096)
        assert detail is None and safe['failure_reason'] == 'process_failed'
        assert 'PYTHON_PHASE_FAILURE' in str(diagnostic.to_payload()['stderr'])
    finally:
        sys.modules.pop(name, None)
        sys.modules.pop('test_diagnostic_case', None)


@pytest.mark.parametrize('kind', ['unknown', 'runtime', 'guard', 'syntax'])
def test_phase_cause_never_reflects_error_text_paths_or_custom_class_names(
    isolated: PhaseModule, monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str], kind: str,
) -> None:
    class SupplierSecretFailure(Exception):
        @property
        def name(self) -> str:
            raise AssertionError('unknown exception name must never be inspected')

        def __str__(self) -> str:
            raise AssertionError('unknown exception text must never be inspected')

    error: Exception
    expected: str
    if kind == 'unknown':
        error, expected = SupplierSecretFailure('password=SECRET_PATH'), 'unknown'
    elif kind == 'runtime':
        error, expected = RuntimeError('password=SECRET_PATH'), 'runtime_error'
    elif kind == 'guard':
        error, expected = isolated.PhaseError('password=SECRET_PATH'), 'phase_guard'
    else:
        error = SyntaxError('password=SECRET_PATH', ('/private/SECRET_PATH.py', 1, 1, 'SECRET_PATH'))
        expected = 'syntax_error'

    def fail(module: str, *, generated_manifest_sha256: str | None = None) -> dict[str, object]:
        raise error

    monkeypatch.setattr(isolated, '_test', fail)
    assert isolated.main(['test', '--import-module', 'sample']) == 1
    captured = capfd.readouterr()
    assert json.loads(captured.out) == {'status': 'failed', 'error': 'phase_failed'}
    record = cause_record(captured.err)
    assert record['cause'] == expected
    assert set(record) == {'phase', 'stage', 'cause', 'exception_type'}
    assert len(captured.err.encode()) <= 512
    assert 'SECRET_PATH' not in captured.err and 'SupplierSecretFailure' not in captured.err


def test_cause_dispatch_never_hashes_or_compares_unknown_exception_class(
    capfd: pytest.CaptureFixture[str],
) -> None:
    from agent_hub.skills.sandbox.python_project_phase import _write_phase_failure

    calls: list[str] = []

    class SupplierMeta(type):
        def __hash__(cls) -> int:
            calls.append('hash')
            return hash(RuntimeError)

        def __eq__(cls, other: object) -> bool:
            calls.append('eq')
            return cls is other

    class SupplierError(Exception, metaclass=SupplierMeta):
        def __str__(self) -> str:
            calls.append('str')
            return 'password=DO_NOT_REFLECT'

    error = SupplierError()
    calls.clear()
    _write_phase_failure('test', error)
    captured = capfd.readouterr()
    assert calls == []
    assert cause_record(captured.err)['cause'] == 'unknown'
    assert 'SupplierError' not in captured.err and 'DO_NOT_REFLECT' not in captured.err


def test_phase_subclass_is_rejected_without_hash_equality_or_string_hooks(
    capfd: pytest.CaptureFixture[str],
) -> None:
    from agent_hub.skills.sandbox.python_project_phase import _write_phase_failure

    calls: list[str] = []

    class SupplierPhase(str):
        def __hash__(self) -> int:
            calls.append('hash')
            return str.__hash__(self)

        def __eq__(self, other: object) -> bool:
            calls.append('eq')
            return str.__eq__(self, other)

        def __ne__(self, other: object) -> bool:
            calls.append('ne')
            return str.__ne__(self, other)

        def __str__(self) -> str:
            calls.append('str')
            return 'password=DO_NOT_REFLECT'

    phase = SupplierPhase('test')
    calls.clear()
    _write_phase_failure(phase, RuntimeError())
    captured = capfd.readouterr()
    assert calls == []
    assert cause_record(captured.err)['phase'] == 'unknown'
    assert 'DO_NOT_REFLECT' not in captured.err
