"""Boundary doubles only: Windows/root runs are not native business proof."""

from __future__ import annotations

import asyncio
import hashlib
import importlib
import importlib.util
import json
from contextlib import contextmanager
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from agent_hub.capabilities.go_project_business import GoBusinessCase
from agent_hub.skills.sandbox.python_project_process import (
    PYTHON_PHASE_OUTPUT_WITHHELD,
    PythonPhaseCleanupError,
    PythonPhaseProcessResult,
)
from agent_hub.subprocess_cleanup import ProcessCleanupIncomplete


@pytest.fixture
def api() -> Any:
    name = 'agent_hub.skills.sandbox.go_project_business_runner'
    assert importlib.util.find_spec(name) is not None, 'business runner missing'
    return importlib.import_module(name)


@pytest.fixture
def case() -> GoBusinessCase:
    return GoBusinessCase('usage', 'AC-001', ('--help', '; touch /host'),
                          'input\n', 'ok\n', 'error\n', 7)


@pytest.fixture
def boundary(api: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Any:
    state = SimpleNamespace(active=False, events=[], result=PythonPhaseProcessResult(
        7, False, 'ok\n', 'error\n', False, False, 3, 6), error=None)

    @contextmanager
    def mounts(**arguments: Any) -> Any:
        state.events.append(('mount', arguments))
        state.active = True
        try:
            yield SimpleNamespace(argv=('/usr/bin/bwrap', '--', '/fixed'), pass_fds=(50, 51))
        finally:
            state.events.append(('release', None))
            state.active = False

    def run(command: tuple[str, ...], **arguments: Any) -> Any:
        assert state.active and command[0] == '/usr/bin/bwrap'
        assert arguments == {'cwd': tmp_path / 'work', 'deadline': 1234.5,
                             'capture_limit_bytes': 32, 'pass_fds': (50, 51)}
        state.events.append(('drained', None))
        if state.error is not None:
            raise state.error
        return state.result

    monkeypatch.setattr(api, 'build_go_business_mounts', mounts)
    monkeypatch.setattr(api, 'run_python_phase', run)
    state.options = {'work_root': tmp_path / 'work', 'binary_root': tmp_path / 'binary',
                     'expected_binary_sha256': 'a' * 64, 'expected_binary_bytes': 64,
                     'contract_arguments': {'opaque': True}, 'deadline': 1234.5,
                     'output_limit_bytes': 32}
    return state


FIELDS = {'case_id', 'criterion_id', 'case_sha256', 'expected_exit', 'exit_code', 'timed_out',
          'stdout_sha256', 'stderr_sha256', 'stdout_observed_bytes', 'stderr_observed_bytes',
          'stdout_truncated', 'stderr_truncated', 'stdout_utf8_complete', 'stderr_utf8_complete',
          'stdout_matches', 'stderr_matches', 'passed', 'reason'}


def test_exact_nonzero_native_result_and_safe_fixed_payload(api: Any, case: Any, boundary: Any) -> None:
    observation = api.run_go_business_case(case, **boundary.options)
    payload = observation.to_payload()
    assert set(payload) == FIELDS and payload['passed'] is True and payload['reason'] is None
    assert payload['exit_code'] == payload['expected_exit'] == 7
    assert payload['stdout_sha256'] == hashlib.sha256(b'ok\n').hexdigest()
    assert payload['stderr_sha256'] == hashlib.sha256(b'error\n').hexdigest()
    canonical = json.dumps(case.to_payload(), sort_keys=True, separators=(',', ':'), ensure_ascii=True)
    assert payload['case_sha256'] == hashlib.sha256(canonical.encode('ascii')).hexdigest()
    assert [name for name, _ in boundary.events] == ['mount', 'drained', 'release']
    assert not boundary.active and 'touch' not in json.dumps(payload)
    with pytest.raises(FrozenInstanceError):
        observation.passed = False
    for name in ('passed', 'timed_out', 'stdout_matches', 'stderr_matches',
                 'stdout_truncated', 'stderr_truncated', 'stdout_utf8_complete', 'stderr_utf8_complete'):
        assert type(payload[name]) is bool


@pytest.mark.parametrize('changes,reason,stream', [
    ({'timed_out': True}, 'timeout', None),
    ({'exit_code': 0}, 'exit_mismatch', None),
    ({'stdout': 'OK\n'}, 'stdout_mismatch', None),
    ({'stderr': 'ERROR\n'}, 'stderr_mismatch', None),
    ({'stdout_truncated': True}, 'output_incomplete', 'stdout'),
    ({'stderr_truncated': True}, 'output_incomplete', 'stderr'),
    ({'stdout_observed_bytes': 4}, 'output_incomplete', 'stdout'),
    ({'stderr_observed_bytes': 7}, 'output_incomplete', 'stderr'),
    ({'exit_code': None}, 'output_incomplete', None),
])
def test_incomplete_or_different_output_never_passes(
    api: Any, case: Any, boundary: Any, changes: dict[str, Any], reason: str, stream: str | None,
) -> None:
    boundary.result = replace(boundary.result, **changes)
    payload = api.run_go_business_case(case, **boundary.options).to_payload()
    assert payload['passed'] is False and payload['reason'] == reason
    if stream:
        assert payload[stream + '_sha256'] is None
        assert payload[stream + '_matches'] is False
        assert payload[stream + '_utf8_complete'] is False


def test_utf8_bytes_not_characters_and_literal_replacement_character(
    api: Any, case: Any, boundary: Any,
) -> None:
    case = replace(case, expected_stdout='\N{REPLACEMENT CHARACTER}\N{CJK UNIFIED IDEOGRAPH-4E2D}\n')
    boundary.result = replace(boundary.result, stdout=case.expected_stdout, stdout_observed_bytes=7)
    assert api.run_go_business_case(case, **boundary.options).passed
    boundary.result = replace(boundary.result, stdout_observed_bytes=8)
    result = api.run_go_business_case(case, **boundary.options)
    assert not result.passed and result.stdout_sha256 is None


@pytest.mark.parametrize('error', [KeyboardInterrupt(), SystemExit(), asyncio.CancelledError(),
                                 ProcessCleanupIncomplete('original')])
def test_cancellation_and_cleanup_propagate_after_pin_release(
    api: Any, case: Any, boundary: Any, error: BaseException,
) -> None:
    boundary.error = error
    with pytest.raises(type(error)) as raised:
        api.run_go_business_case(case, **boundary.options)
    assert raised.value is error and not boundary.active


def test_phase_cleanup_uncertainty_is_typed(api: Any, case: Any, boundary: Any) -> None:
    boundary.error = PythonPhaseCleanupError('/host/private cleanup detail')
    with pytest.raises(ProcessCleanupIncomplete, match='go_business_cleanup_incomplete'):
        api.run_go_business_case(case, **boundary.options)
    assert not boundary.active


def test_trusted_diagnostic_callback_receives_actual_output_only_after_drain_and_pin_checks(
    api: Any, case: Any, boundary: Any,
) -> None:
    boundary.result = replace(boundary.result, stdout='NO\n')
    collected = []

    def diagnostics(result: PythonPhaseProcessResult) -> None:
        assert not boundary.active
        assert [name for name, _ in boundary.events] == ['mount', 'drained', 'release']
        collected.append(result)

    observation = api.run_go_business_case(case, **boundary.options, on_collected=diagnostics)
    assert not observation.passed and observation.reason == 'stdout_mismatch'
    assert collected == [boundary.result]
    assert 'stdout' not in observation.to_payload()


def test_cleanup_uncertainty_never_calls_diagnostic_success_callback(
    api: Any, case: Any, boundary: Any,
) -> None:
    boundary.error = PythonPhaseCleanupError('private uncertain drain')
    with pytest.raises(ProcessCleanupIncomplete):
        api.run_go_business_case(case, **boundary.options,
                                 on_collected=lambda _: pytest.fail('unconfirmed collection'))


def test_noncallable_diagnostic_sink_is_rejected_before_io(api: Any, case: Any, boundary: Any) -> None:
    with pytest.raises(ValueError):
        api.run_go_business_case(case, **boundary.options, on_collected=True)
    assert not boundary.events


def test_lossy_capture_is_withheld_in_diagnostics_without_changing_observation(
    api: Any, case: Any, boundary: Any,
) -> None:
    boundary.result = replace(boundary.result, stdout='OK', stdout_observed_bytes=3)
    collected = []
    observation = api.run_go_business_case(case, **boundary.options, on_collected=collected.append)
    assert observation.reason == 'output_incomplete' and not observation.stdout_truncated
    assert observation.stdout_sha256 is None
    assert collected[0].stdout == PYTHON_PHASE_OUTPUT_WITHHELD[:32]
    assert collected[0].stdout_truncated and collected[0].stdout_observed_bytes == 3


@pytest.mark.parametrize('value', [True, 0, -1, 1.5])
def test_invalid_output_budget_never_opens_mounts(api: Any, case: Any, boundary: Any, value: Any) -> None:
    boundary.options['output_limit_bytes'] = value
    with pytest.raises(ValueError):
        api.run_go_business_case(case, **boundary.options)
    assert not boundary.events


@pytest.mark.parametrize('stream', ['stdout', 'stderr'])
def test_withheld_prefix_cannot_be_hashed_as_business_output(
    api: Any, case: Any, boundary: Any, stream: str,
) -> None:
    text = PYTHON_PHASE_OUTPUT_WITHHELD[:32]
    case = replace(case, **{'expected_' + stream: text})
    boundary.result = replace(boundary.result, **{
        stream: text, stream + '_observed_bytes': 9999, stream + '_truncated': True})
    observation = api.run_go_business_case(case, **boundary.options)
    assert not observation.passed and observation.reason == 'output_incomplete'
    assert getattr(observation, stream + '_sha256') is None
    assert getattr(observation, stream + '_matches') is False


@pytest.mark.parametrize('changes', [
    {'timed_out': 0}, {'stdout_truncated': 0}, {'stderr_truncated': 1},
    {'exit_code': True}, {'stdout_observed_bytes': True}, {'stderr_observed_bytes': -1},
])
def test_capture_metadata_is_strictly_typed(
    api: Any, case: Any, boundary: Any, changes: dict[str, Any],
) -> None:
    boundary.result = replace(boundary.result, **changes)
    with pytest.raises(ValueError, match='go_business_capture_invalid'):
        api.run_go_business_case(case, **boundary.options)


@pytest.mark.parametrize('deadline', [True, float('nan'), float('inf'), 0, -1])
def test_invalid_deadline_never_opens_mounts(
    api: Any, case: Any, boundary: Any, deadline: Any,
) -> None:
    boundary.options['deadline'] = deadline
    with pytest.raises(ValueError):
        api.run_go_business_case(case, **boundary.options)
    assert not boundary.events


def test_real_collector_utf8_loss_cannot_match_expected_text(api: Any, case: Any, boundary: Any) -> None:
    from agent_hub.skills.sandbox.python_project_process import _Capture

    capture = _Capture(32)
    capture.feed(b'ok\xff\n')
    assert capture.text() == case.expected_stdout
    boundary.result = replace(boundary.result, stdout=capture.text(),
                              stdout_observed_bytes=capture.observed)
    result = api.run_go_business_case(case, **boundary.options)
    assert not result.passed and result.reason == 'output_incomplete'
    assert result.stdout_sha256 is None and result.stdout_utf8_complete is False


def test_missing_outer_environment_is_explicitly_unavailable_without_launch(
    api: Any, case: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.delenv('AGENT_HUB_GENERATED_PROJECT_VALIDATION_SANDBOX', raising=False)
    monkeypatch.setattr(api, 'run_python_phase', lambda *args, **kwargs: pytest.fail('host launch'))
    with pytest.raises(api.GoBusinessNotAvailable, match='go_business_not_available'):
        api.run_go_business_case(case, work_root=tmp_path / 'work', binary_root=tmp_path / 'binary',
            expected_binary_sha256='a' * 64, expected_binary_bytes=64, contract_arguments={},
            deadline=1234.5, output_limit_bytes=32)
