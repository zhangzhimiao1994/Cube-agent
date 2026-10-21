"""Self-created unittest code only; local smoke is not a native sandbox proof."""

from __future__ import annotations

import hashlib
import importlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

PHASE = Path(__file__).parents[4] / 'src/agent_hub/skills/sandbox/python_project_phase.py'
KEY = 'generated_test_execution'
NAME = 'test_acceptance.Acceptance.test_value'


def manifest() -> dict[str, Any]:
    return {'schema_version': 1, 'stack': 'python', 'editable_paths': ['src/sample'],
        'test_files': ['tests/test_acceptance.py'], 'criteria': [
            {'id': 'AC-001', 'description': 'Installed value is correct', 'tests': [NAME]}]}


def proof() -> dict[str, Any]:
    payload = {'schema_version': 1, 'manifest_sha256': 'a' * 64, 'count': 1, 'names': [NAME]}
    return {**payload, 'test_evidence_sha256': hashlib.sha256(json.dumps(
        payload, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode('ascii')).hexdigest()}


HARNESS = r'''
import importlib.util,json,os,sys
from pathlib import Path
path,workspace,output,expected=sys.argv[1:]
spec=importlib.util.spec_from_file_location('trusted_phase',path)
phase=importlib.util.module_from_spec(spec)
sys.modules[spec.name]=phase
spec.loader.exec_module(phase)
phase._WORKSPACE=Path(workspace)
phase._OUTPUT=Path(output)
# No Linux sandbox or installed venv is claimed by this portable dummy smoke.
phase._isolation_guard=lambda:None
phase._mounts=lambda:None
phase._environment_interpreter=lambda:None
def directory(path,**kwargs):
    assert path.is_dir() and path.resolve()==path
    return path.stat()
phase._directory=directory
if os.name=='nt':
    original=phase._stamp
    phase._stamp=lambda info:original(info)[:-1]
args=['test','--import-module','sample']
if expected: args+=['--generated-manifest-sha256',expected]
raise SystemExit(phase.main(args))
'''


def smoke(tmp_path: Path, script: str, *, payload: Any = None, raw_manifest: bytes | None = None,
          with_manifest: bool = True, installed: str = 'VALUE = 7\n', generated: bool = True,
          expected_sha256: str | None = None) -> subprocess.CompletedProcess[str]:
    work, out = tmp_path / 'work', tmp_path / 'out'
    tests, site = work / 'tests', out / 'project-site' / 'sample'
    tests.mkdir(parents=True)
    site.mkdir(parents=True)
    (site / '__init__.py').write_text(installed, encoding='utf-8')
    (tests / 'test_acceptance.py').write_text(script, encoding='utf-8')
    if with_manifest:
        (work / 'ACCEPTANCE.json').write_bytes(raw_manifest if raw_manifest is not None else
            json.dumps(manifest() if payload is None else payload).encode('utf-8'))
    expected = expected_sha256 or (hashlib.sha256((work / 'ACCEPTANCE.json').read_bytes()).hexdigest()
        if with_manifest and generated else '')
    return subprocess.run([sys.executable, '-I', '-S', '-B', '-c', HARNESS, str(PHASE), str(work), str(out), expected],
        capture_output=True, text=True, encoding='utf-8', timeout=15, check=False)


GOOD = '''import unittest, sample
class Acceptance(unittest.TestCase):
    def test_value(self):
        self.assertEqual(sample.VALUE, 7)
'''


def test_actual_named_result_has_manifest_bound_immutable_evidence(tmp_path: Path) -> None:
    result = smoke(tmp_path, GOOD)
    assert result.returncode == 0, result.stderr
    detail = json.loads(result.stdout)
    assert KEY in detail, 'generated phase did not retain actual named unittest evidence'
    observed = detail[KEY]
    assert observed['names'] == [NAME] and observed['count'] == detail['test_count'] == 1
    assert observed['manifest_sha256'] == hashlib.sha256((tmp_path / 'work/ACCEPTANCE.json').read_bytes()).hexdigest()
    api = importlib.import_module('agent_hub.capabilities.python_project_reports')
    validated = api.validate_generated_python_test_execution(observed)
    assert tuple(validated['names']) == (NAME,)
    with pytest.raises(TypeError):
        validated['count'] = 3


def test_without_manifest_keeps_exact_original_phase_shape(tmp_path: Path) -> None:
    result = smoke(tmp_path, GOOD, with_manifest=False)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {'status': 'completed', 'phase': 'test',
        'evidence': 'native_process_only', 'test_count': 1,
        'import': {'module': 'sample', 'origin': 'sample/__init__.py'}}


def test_legacy_same_named_manifest_is_ignored_without_internal_opt_in(tmp_path: Path) -> None:
    result = smoke(tmp_path, GOOD, raw_manifest=b'not an acceptance manifest', generated=False)
    assert result.returncode == 0, result.stderr
    assert KEY not in json.loads(result.stdout)


@pytest.mark.parametrize('present', [False, True])
def test_explicit_generated_opt_in_requires_exact_manifest_bytes(tmp_path: Path, present: bool) -> None:
    result = smoke(tmp_path, GOOD, with_manifest=present, expected_sha256='b' * 64)
    assert result.returncode != 0
    assert json.loads(result.stdout)['status'] == 'failed'


def test_standard_mock_and_class_fixture_data_are_not_framework_tampering(tmp_path: Path) -> None:
    script = '''import unittest
from unittest.mock import patch
import sample
class Acceptance(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.expected = 7
    def test_value(self):
        with patch.object(sample, 'VALUE', self.expected):
            self.assertEqual(sample.VALUE, self.expected)
'''
    result = smoke(tmp_path, script)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)[KEY]['names'] == [NAME]


def test_exact_unicode_unittest_class_id(tmp_path: Path) -> None:
    name = NAME.replace('Acceptance', 'Accept\u00e9')
    payload = manifest()
    payload['criteria'][0]['tests'] = [name]
    result = smoke(tmp_path, GOOD.replace('Acceptance', 'Accept\u00e9'), payload=payload)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)[KEY]['names'] == [name]


@pytest.mark.parametrize('script', [
    "print('forged success')\n",
    GOOD.replace('self.assertEqual(sample.VALUE, 7)', 'self.skipTest("skip")'),
    GOOD.replace('    def test_value', '    @unittest.expectedFailure\n    def test_value').replace('VALUE, 7', 'VALUE, 8'),
    GOOD.replace('VALUE, 7', 'VALUE, 8'),
    GOOD.replace('test_value', 'test_other'),
    GOOD + '\n    def test_extra(self): pass\n',
    GOOD + '\n    def run(self, result=None): result.testsRun += 1; result.addSuccess(self)\n',
    GOOD + '\n    def id(self): return "forged"\n',
    GOOD + '\ndef load_tests(loader, tests, pattern): return unittest.TestSuite()\n',
    'import unittest\nunittest.TextTestRunner.run=lambda *a:None\n' + GOOD,
    'import unittest\nunittest.TestResult.addSuccess=lambda *a:None\n' + GOOD,
    'import os\nos._exit(0)\n' + GOOD,
    'import sys\nsys.exit(0)\n' + GOOD,
], ids=['zero', 'skip', 'todo', 'failure', 'wrong', 'unexpected', 'fake_run', 'fake_id',
        'load_tests', 'patch_runner', 'patch_result', 'early_exit', 'system_exit'])
def test_false_success_cannot_produce_named_proof(tmp_path: Path, script: str) -> None:
    result = smoke(tmp_path, script)
    if result.stdout.strip():
        assert json.loads(result.stdout).get('status') != 'completed'
    else:
        assert not result.stdout  # os._exit(0) has no protocol evidence, regardless of exit0.


@pytest.mark.parametrize('defect', ['schema_bool', 'stack', 'extra', 'criterion_extra', 'duplicate_id',
    'duplicate_name', 'missing_name', 'unsafe_file', 'editable_test', 'empty_tests', 'missing_file'])
def test_strict_readonly_manifest_is_not_a_claimed_result(tmp_path: Path, defect: str) -> None:
    payload = manifest()
    if defect == 'schema_bool':
        payload['schema_version'] = True
    elif defect == 'stack':
        payload['stack'] = 'node'
    elif defect == 'extra':
        payload['passed'] = True
    elif defect == 'criterion_extra':
        payload['criteria'][0]['passed'] = True
    elif defect == 'duplicate_id':
        payload['criteria'].append({**payload['criteria'][0], 'tests': ['another.Test.test_x']})
    elif defect == 'duplicate_name':
        payload['criteria'][0]['tests'] *= 2
    elif defect == 'missing_name':
        payload['criteria'][0]['tests'] = ['test_acceptance.Acceptance.test_missing']
    elif defect == 'unsafe_file':
        payload['test_files'] = ['../escape.py']
    elif defect == 'editable_test':
        payload['editable_paths'] = ['tests']
    elif defect == 'empty_tests':
        payload['criteria'][0]['tests'] = []
    else:
        payload['test_files'] = ['tests/test_missing.py']
    result = smoke(tmp_path, GOOD, payload=payload)
    assert result.returncode != 0
    assert json.loads(result.stdout) == {'status': 'failed', 'error': 'phase_failed'}


@pytest.mark.parametrize('raw', [b'{"schema_version":1,"schema_version":1}', b'NaN', b'x' * (128 * 1024 + 1)],
                         ids=['duplicate', 'nonfinite', 'large'])
def test_duplicate_nonfinite_and_large_manifest_refused(tmp_path: Path, raw: bytes) -> None:
    result = smoke(tmp_path, GOOD, raw_manifest=raw)
    assert result.returncode != 0


@pytest.mark.parametrize('change', [{'count': True}, {'count': 0}, {'count': 2}, {'schema_version': True},
    {'manifest_sha256': 'A' * 64}, {'names': [NAME, NAME]}, {'names': []}, {'names': ['bad\n']},
    {'test_evidence_sha256': '0' * 64}, {'extra': True}])
def test_pure_proof_validator_is_strict_and_recomputes_digest(change: dict[str, Any]) -> None:
    api = importlib.import_module('agent_hub.capabilities.python_project_reports')
    assert hasattr(api, 'validate_generated_python_test_execution')
    with pytest.raises(ValueError):
        api.validate_generated_python_test_execution({**proof(), **change})


def _phase_result(projection: dict[str, Any]) -> Any:
    from tests.unit.skills.sandbox.test_python_project_runner import result
    observed = result('test')
    detail = json.loads(observed.stdout)
    detail.update(test_count=1, **{KEY: projection})
    observed.stdout = json.dumps(detail)
    observed.stdout_observed_bytes = len(observed.stdout.encode())
    return observed


def test_outer_phase_allows_only_old_or_precise_generated_shape() -> None:
    api = importlib.import_module('agent_hub.skills.sandbox.python_project_runner')
    observed = _phase_result(proof())
    assert api.python_phase_detail(observed, phase='test', import_module='billing') is not None
    for changes in ({'count': 2}, {'test_evidence_sha256': '0' * 64}, {'extra': 1}):
        assert api.python_phase_detail(_phase_result({**proof(), **changes}), phase='test', import_module='billing') is None


def test_outer_phase_refuses_duplicate_metadata_keys() -> None:
    api = importlib.import_module('agent_hub.skills.sandbox.python_project_runner')
    observed = _phase_result(proof())
    observed.stdout = observed.stdout.replace('"test_count": 1', '"test_count": 0, "test_count": 1')
    assert api.python_phase_detail(observed, phase='test', import_module='billing') is None


@pytest.mark.parametrize('failure', ['assertion', 'exception', 'subtest'])
def test_forged_success_callback_cannot_hide_actual_failure(tmp_path: Path, failure: str) -> None:
    action = {'assertion': "self.fail('real failure')", 'exception': "raise RuntimeError('real error')",
              'subtest': "with self.subTest(value=1): self.fail('real subtest failure')"}[failure]
    script = '''import unittest
class Acceptance(unittest.TestCase):
    def test_value(self):
        result = self._outcome.result
        result.addSuccess(self)
        result.wasSuccessful = lambda: True
        self.addCleanup(result.failures.clear)
        self.addCleanup(result.errors.clear)
        ''' + action + '\n'
    result = smoke(tmp_path, script)
    assert result.returncode != 0
    assert json.loads(result.stdout)['status'] == 'failed'


@pytest.mark.parametrize('patch', ['runner', 'method'])
def test_one_test_cannot_replace_a_later_protected_test(tmp_path: Path, patch: str) -> None:
    changed = 'Acceptance._callTestMethod = lambda *args: None' if patch == 'runner' else (
        'Acceptance.test_value = lambda self: None')
    script = '''import unittest
class Acceptance(unittest.TestCase):
    def test_first(self):
        ''' + changed + '''
    def test_value(self):
        self.fail('must actually fail')
'''
    payload = manifest()
    payload['criteria'][0]['tests'].insert(0, 'test_acceptance.Acceptance.test_first')
    result = smoke(tmp_path, script, payload=payload)
    assert result.returncode != 0


@pytest.mark.parametrize('generated', [False, True])
def test_outer_runner_includes_proof_only_after_successful_generated_test(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, generated: bool,
) -> None:
    from tests.unit.skills.sandbox.test_python_project_runner import orchestrator
    api = importlib.import_module('agent_hub.skills.sandbox.python_project_runner')
    configured = orchestrator(api, tmp_path, monkeypatch)
    if generated:
        configured.plan = configured.plan.model_copy(update={'generated_manifest_sha256': 'a' * 64,
            'invocation': configured.plan.invocation.model_copy(update={'source_kind': 'candidate'})})
    original = api.run_python_phase
    commands: list[tuple[str, ...]] = []
    def run(command: tuple[str, ...], **kwargs: Any) -> Any:
        commands.append(command)
        if generated and command[0] == 'test':
            return _phase_result(proof())
        return original(command, **kwargs)
    monkeypatch.setattr(api, 'run_python_phase', run)
    raw = api.run_python_project(configured.plan)
    assert raw['status'] == 'passed'
    assert (KEY in raw) is generated
    if generated:
        assert raw[KEY]['count'] == raw['test_count'] == 1
        assert commands[-1][-2:] == ('--generated-manifest-sha256', 'a' * 64)
        assert all('--generated-manifest-sha256' not in command for command in commands[:-1])
    else:
        assert all('--generated-manifest-sha256' not in command for command in commands)


def test_internal_generated_plan_is_candidate_only_and_has_strict_digest() -> None:
    from tests.unit.skills.sandbox.test_python_project_runner import plan_payload
    api = importlib.import_module('agent_hub.skills.sandbox.python_project_runner')
    payload = plan_payload()
    payload['generated_manifest_sha256'] = 'a' * 64
    with pytest.raises(ValueError):
        api.PythonProjectRunnerPlan.model_validate(payload, strict=True)
    payload['invocation']['source_kind'] = 'candidate'
    plan = api.PythonProjectRunnerPlan.model_validate(payload, strict=True)
    assert plan.generated_manifest_sha256 == 'a' * 64
    for wrong in ('A' * 64, '../manifest', True, 1):
        with pytest.raises(ValueError):
            api.PythonProjectRunnerPlan.model_validate({**payload, 'generated_manifest_sha256': wrong}, strict=True)


@pytest.mark.parametrize('defect', ['none', 'missing-top', 'missing-phase', 'hash', 'count', 'legacy'])
def test_root_backend_report_validates_generated_extra_against_internal_expected(defect: str) -> None:
    api = importlib.import_module('agent_hub.skills.sandbox.python_project_backend')
    invocation, inputs, _, raw = _generated_context()
    if defect == 'missing-top':
        del raw[KEY]
    elif defect == 'missing-phase':
        detail = json.loads(raw['checks'][-1]['stdout'])
        del detail[KEY]
        text = json.dumps(detail)
        raw['checks'][-1].update(stdout=text, stdout_observed_bytes=len(text.encode()))
    elif defect == 'hash':
        raw[KEY] = {**raw[KEY], 'manifest_sha256': 'b' * 64}
    elif defect == 'count':
        raw['test_count'] = 2
    expected = None if defect == 'legacy' else 'a' * 64
    if defect == 'none':
        assert api._report(json.dumps(raw), invocation, inputs, raw['source_sha256'], None,
            generated_manifest_sha256=expected)[KEY]['count'] == 1
    else:
        with pytest.raises(ValueError):
            api._report(json.dumps(raw), invocation, inputs, raw['source_sha256'], None,
                generated_manifest_sha256=expected)


def _generated_context() -> tuple[Any, ...]:
    from tests.unit.capabilities.test_python_project_reports import fixture
    invocation, inputs, reference, raw = fixture()
    detail = json.loads(raw['checks'][-1]['stdout'])
    detail.update(test_count=1, **{KEY: proof()})
    text = json.dumps(detail)
    raw['checks'][-1].update(stdout=text, stdout_observed_bytes=len(text.encode()))
    raw.update(test_count=1, **{KEY: proof()})
    return invocation, inputs, reference, raw


def test_report_accepts_bound_raw_extra_but_does_not_change_public_receipt() -> None:
    from tests.unit.capabilities.test_python_project_reports import decode
    api = importlib.import_module('agent_hub.capabilities.python_project_reports')
    context = _generated_context()
    observed = decode(api, context)
    assert KEY not in observed.to_payload() and observed.to_payload()['test_count'] == 1


@pytest.mark.parametrize('defect', ['top_missing', 'phase_missing', 'count', 'hash', 'unknown', 'failed'])
def test_decoder_refuses_unbound_raw_extra(defect: str) -> None:
    from agent_hub.capabilities.project_verification import ProjectVerificationError
    from tests.unit.capabilities.test_python_project_reports import decode
    api = importlib.import_module('agent_hub.capabilities.python_project_reports')
    context = _generated_context()
    raw = context[-1]
    if defect == 'top_missing':
        del raw[KEY]
    elif defect == 'phase_missing':
        detail = json.loads(raw['checks'][-1]['stdout'])
        del detail[KEY]
        raw['checks'][-1].update(stdout=json.dumps(detail), stdout_observed_bytes=len(json.dumps(detail).encode()))
    elif defect == 'count':
        raw[KEY]['count'] = 2
    elif defect == 'hash':
        raw[KEY]['manifest_sha256'] = 'b' * 64
    elif defect == 'unknown':
        raw['unknown'] = True
    else:
        raw['status'] = 'failed'
    with pytest.raises(ProjectVerificationError):
        decode(api, context)
