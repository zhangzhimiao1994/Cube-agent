from __future__ import annotations

import importlib
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from collections import Counter
from pathlib import Path
from typing import Any, cast
from urllib.parse import parse_qs, urlsplit

import pytest

requirements = importlib.import_module('agent_hub.harness.project_requirements')
FIXTURES = Path(__file__).resolve().parents[2] / 'fixtures/project_business'


def validate(root: Path, timeout: float = 30) -> dict[str, Any]:
    protocol = importlib.import_module('agent_hub.harness.project_validation_result')
    result = requirements._validate_ultra_portfolio_storage(root, timeout)
    return cast(dict[str, Any], protocol.validate_scale_validation_result(
        result, expected_profile=protocol.STORAGE_PROFILE,
    ))


def application(tmp_path: Path, fault: str = '') -> Path:
    if not shutil.which('node') or not shutil.which('npm'):
        pytest.skip('trusted fixture requires Node and npm')
    if os.name != 'nt':
        pytest.skip('host execution is only for trusted Windows fixtures; use native sandbox test')
    root = tmp_path / 'storage source with spaces'
    root.mkdir()
    (root / 'package.json').write_text(json.dumps({
        'private': True, 'scripts': {'start': 'node server.cjs'},
    }), encoding='utf-8')
    shutil.copyfile(FIXTURES / 'ultra_storage.cjs', root / 'server.cjs')
    (root / 'fault.txt').write_text(fault, encoding='utf-8')
    return root


def observe(monkeypatch: pytest.MonkeyPatch) -> tuple[list[Any], list[Any]]:
    launches: list[Any] = []
    requests: list[Any] = []
    ready = requirements._ready_portfolio
    request = requirements._PortfolioAPI.request

    def observed_request(api: Any, method: str, path: str, body: Any = None) -> Any:
        status, payload = request(api, method, path, body)
        requests.append((api.port, method, path, body, status, payload, api.deadline))
        return status, payload

    def observed_ready(api: Any, process: Any) -> None:
        ready(api, process)
        launches.append(api.get_object('/__fixture'))

    monkeypatch.setattr(requirements._PortfolioAPI, 'request', observed_request)
    monkeypatch.setattr(requirements, '_ready_portfolio', observed_ready)
    return launches, requests


def assert_clean(launches: list[Any]) -> None:
    deadline = time.monotonic() + 2
    for launch in launches:
        for key in ('code', 'data', 'home', 'tmp', 'cache'):
            assert not Path(launch[key]).exists(), f'{key} leaked'
        for key in ('port', 'childPort'):
            while True:
                with socket.socket() as connection:
                    connection.settimeout(0.1)
                    if connection.connect_ex(('127.0.0.1', launch[key])) != 0:
                        break
                assert time.monotonic() < deadline, f'{key} listener survived cleanup'
                time.sleep(0.025)


def test_storage_entrypoints_exist() -> None:
    assert callable(getattr(requirements, 'validate_ultra_portfolio_storage', None))
    assert callable(getattr(requirements, '_validate_ultra_portfolio_storage', None))


def test_storage_real_http_preserves_load_and_relocates_without_windows_credit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = application(tmp_path)
    before = {p.name: p.read_bytes() for p in root.iterdir()}
    launches, requests = observe(monkeypatch)
    result = validate(root)
    expected = json.loads((FIXTURES / 'ultra_storage_result.json').read_text(encoding='utf-8'))
    assert result['status'] == 'unknown', result
    assert result['cleanup_ok'] is True
    checks = result['checks']
    assert checks['load']['status'] == 'passed', result
    load = checks['load']['measurements']
    assert {k: v for k, v in load.items() if k != 'elapsed_seconds'} == {
        k: v for k, v in expected['checks']['load']['measurements'].items() if k != 'elapsed_seconds'
    }
    assert checks['data_dir_isolation'] == expected['checks']['data_dir_isolation']
    relocation = checks['same_version_relocation']
    assert relocation['status'] == 'unknown'
    measured = relocation['measurements']
    for key in ('starts', 'stops', 'target_projects', 'foreign_projects', 'traversals',
                'read_model_requests', 'original_program_checks'):
        assert measured[key] == expected['checks']['same_version_relocation']['measurements'][key]
    assert measured['old_paths_unavailable'] is False
    assert measured['source_data_sha256'] == measured['copied_data_sha256']
    assert measured['frozen_code_sha256'] == measured['relocated_code_sha256']
    assert measured['data_files'] == 2 and measured['data_bytes'] > 0
    assert len(launches) == 4
    a1, b, a2, c = launches
    assert a1['data'] == a2['data'] and len({a1['data'], b['data'], c['data']}) == 3
    assert a1['code'] == b['code'] == a2['code'] and c['code'] != a1['code']
    assert len({launch['home'] for launch in launches}) == 4
    assert len({r[6] for r in requests}) == 1
    reads = [r for r in requests if urlsplit(r[2]).path == '/portfolio/read-model']
    assert Counter(r[0] for r in reads) == {a1['port']: 83, a2['port']: 22, c['port']: 13}
    writes = [r for r in requests if r[1] == 'POST']
    a_writes = [r for r in writes if r[0] in (a1['port'], a2['port'])]
    assert len(a_writes) == 1064 and len(writes) == 1066
    assert not any(r[0] == c['port'] for r in writes)
    projects = [r for r in a_writes if r[2] == '/projects']
    assert len(projects) == 1017
    for read in (r for r in reads if r[0] == c['port']):
        query = parse_qs(urlsplit(read[2]).query)
        selected = sorted((r for r in projects if str(r[3]['program_id']) == query['program_id'][0]),
                          key=lambda r: str(r[5]['id']))
        offset = int(query.get('offset', ['0'])[0])
        wanted = selected[offset:offset + 100]
        assert len(read[5]['items']) == len(wanted)
        for actual, project in zip(read[5]['items'], wanted, strict=True):
            pid = project[5]['id']
            modules = [r for r in a_writes if r[2].startswith(f'/projects/{pid}/')]
            assert actual == {
                'project_id': pid, 'program_id': project[3]['program_id'], 'name': project[3]['name'],
                'budget_total': sum(r[3]['amount'] for r in modules if r[2].endswith('/budgets')),
                'staffing_allocation': sum(r[3]['allocation'] for r in modules if r[2].endswith('/staffing')),
                'risk_count': sum(r[2].endswith('/risks') for r in modules),
                'milestone_count': sum(r[2].endswith('/milestones') for r in modules),
            }
    assert {p.name: p.read_bytes() for p in root.iterdir()} == before
    assert_clean(launches)


@pytest.mark.parametrize('fault', ['cwd', 'home', 'global_tmp', 'foreign_loss', 'stale_aggregate'])
def test_storage_semantic_mutants_fail_and_stop_real_children(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str,
) -> None:
    root = application(tmp_path, fault)
    launches, _ = observe(monkeypatch)
    result = validate(root)
    assert result['status'] == 'failed', result
    assert result['cleanup_ok'] is True
    assert_clean(launches)
    if fault in {'foreign_loss', 'stale_aggregate'}:
        assert result['checks']['load']['status'] == 'passed'
        assert result['checks']['load']['measurements']['read_model_requests'] == 105
        assert result['checks']['load']['measurements']['write_requests'] == 1064
        assert result['checks']['same_version_relocation']['status'] == 'failed'


def test_old_absolute_path_cannot_earn_windows_storage_credit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = application(tmp_path, 'old_absolute')
    launches, _ = observe(monkeypatch)
    result = validate(root)
    assert result['status'] == 'unknown', result
    assert result['checks']['same_version_relocation']['measurements']['old_paths_unavailable'] is False
    assert_clean(launches)


def test_changed_frozen_code_is_rejected_before_c_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = application(tmp_path)
    launches, _ = observe(monkeypatch)
    check_a = requirements._UltraStorageContext.check_a

    def corrupt(context: Any, api: Any) -> None:
        check_a(context, api)
        with (context.code_c / 'server.cjs').open('a', encoding='utf-8') as stream:
            stream.write('\n// changed after freeze\n')

    monkeypatch.setattr(requirements._UltraStorageContext, 'check_a', corrupt)
    result = validate(root)
    assert result['status'] == 'failed', result
    assert result['checks']['load']['status'] == 'passed'
    assert len(launches) == 3
    assert result['checks']['same_version_relocation']['measurements']['starts'] == 0
    assert any('code' in reason for reason in result['reasons'])
    assert_clean(launches)


def test_storage_deadline_retains_partial_counts_and_cleans_children(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = application(tmp_path, 'slow_program')
    launches, _ = observe(monkeypatch)
    started = time.monotonic()
    result = validate(root, 2)
    assert result['status'] == 'unknown', result
    assert time.monotonic() - started < 8
    assert result['cleanup_ok'] is True
    assert result['checks']['load']['measurements']['write_requests'] == 1
    assert result['checks']['load']['measurements']['target_projects'] == 0
    assert len(launches) == 1
    assert_clean(launches)


def test_public_storage_uses_posix_dispatcher(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    sandbox = importlib.import_module('agent_hub.harness.project_validation_sandbox')
    seen: list[Any] = []

    def dispatch(root: Path, scale: str, timeout: float) -> dict[str, object]:
        seen.append((root, scale, timeout))
        return {'sentinel': True}

    monkeypatch.setattr(requirements, '_PLATFORM', 'posix')
    monkeypatch.setattr(sandbox, 'validate_scale_storage', dispatch)
    assert requirements.validate_ultra_portfolio_storage(tmp_path, 12.5) == {'sentinel': True}
    assert seen == [(tmp_path, 'ultra', 12.5)]


def test_owned_cleanup_failure_has_infrastructure_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = application(tmp_path)
    launches, _ = observe(monkeypatch)
    cleanup = requirements._UltraStorageContext.cleanup_owned

    def fail_cleanup(context: Any) -> None:
        cleanup(context)
        raise OSError('injected owned-directory cleanup failure')

    monkeypatch.setattr(requirements._UltraStorageContext, 'cleanup_owned', fail_cleanup)
    result = validate(root)
    assert result['status'] == 'failed' and result['cleanup_ok'] is False
    assert any('temporary runtime cleanup failed' in reason for reason in result['reasons'])
    assert_clean(launches)


@pytest.mark.parametrize('close_later', [True, False])
def test_storage_stop_observes_listener_close_with_bounded_deadline(
    tmp_path: Path, close_later: bool,
) -> None:
    listener = socket.socket()
    listener.bind(('127.0.0.1', 0))
    listener.listen(128)
    process = subprocess.Popen([sys.executable, '-I', '-B', '-c', 'pass'],
                               start_new_session=os.name == 'posix')
    process.wait(timeout=3)
    context = requirements._UltraStorageContext(tmp_path, time.monotonic() + 3)
    context.processes[process.pid] = (process, 'data_dir_isolation', listener.getsockname()[1])
    context.checks['data_dir_isolation']['measurements']['starts'] = 1
    timer = threading.Timer(0.3, listener.close) if close_later else None
    started = time.monotonic()
    try:
        if timer is not None:
            timer.start()
            context.stop(process)
            assert context.checks['data_dir_isolation']['measurements']['stops'] == 1
        else:
            with pytest.raises(requirements._ValidationFailure, match='listener'):
                context.stop(process)
            check = context.checks['data_dir_isolation']
            assert check['status'] == 'failed' and check['cleanup_ok'] is False
            assert check['measurements']['stops'] == 0
        assert 0.25 <= time.monotonic() - started < 2.8
    finally:
        if timer is not None:
            timer.cancel()
            timer.join(timeout=1)
        listener.close()


@pytest.mark.parametrize('fault, acknowledged', [
    ('b_invalid_programs:null', 0), ('b_invalid_programs:true', 0),
    ('b_invalid_projects:[]', 1), ('b_invalid_projects:""', 1),
])
def test_b_marker_invalid_id_is_rejected_before_readback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str, acknowledged: int,
) -> None:
    root = application(tmp_path, fault)
    launches, requests = observe(monkeypatch)
    result = validate(root)
    assert result['status'] == 'failed', result
    measured = result['checks']['data_dir_isolation']['measurements']
    assert measured['marker_writes'] == acknowledged
    assert measured['marker_readbacks'] == 0
    assert len(launches) == 2
    assert sum(r[1] == 'POST' and r[0] == launches[1]['port'] for r in requests) == acknowledged + 1
    assert result['checks']['load']['measurements']['read_model_requests'] == 83
    assert result['checks']['load']['measurements']['write_requests'] == 1063
    assert_clean(launches)
