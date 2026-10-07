from __future__ import annotations

import http.client
import importlib
import json
import math
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from collections import Counter
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from urllib.parse import parse_qs, urlsplit

import pytest

requirements = importlib.import_module('agent_hub.harness.project_requirements')
_FIXTURE = Path(__file__).resolve().parents[2] / 'fixtures/project_business/ultra_scale.cjs'
_EXPECTED = {
    'target_projects': 1000, 'foreign_projects': 17, 'module_records': 44,
    'concurrent_clients': 4, 'peak_active_clients': 4, 'initial_traversals': 4,
    'restart_traversals': 1, 'updated_traversals': 1, 'boundary_pages': 3,
    'read_model_requests': 105, 'write_requests': 1064, 'request_errors': 0,
}


def _application(tmp_path: Path, fault: str = '') -> Path:
    if shutil.which('node') is None or shutil.which('npm') is None:
        pytest.skip('trusted real HTTP fixture requires Node and npm')
    root = tmp_path / 'ultra load with spaces'
    root.mkdir()
    (root / 'package.json').write_text(json.dumps({
        'private': True, 'scripts': {'start': 'node server.cjs'},
    }), encoding='utf-8')
    shutil.copyfile(_FIXTURE, root / 'server.cjs')
    (root / 'fault.txt').write_text(fault, encoding='utf-8')
    return root


def _log(root: Path, name: str) -> list[dict[str, Any]]:
    return [json.loads(line) for line in (root / name).read_text(encoding='utf-8').splitlines()]


def _stopped(root: Path) -> list[dict[str, Any]]:
    launches = _log(root, 'launches.jsonl')
    assert launches, 'npm must actually launch the trusted HTTP fixture'
    deadline = time.monotonic() + 2
    for launch in launches:
        assert launch['home'] != launch['data']
        assert launch['tmp'] != launch['data']
        for key in ('data', 'home', 'tmp', 'cache'):
            assert not Path(launch[key]).exists(), f'{key} leaked'
        for key in ('port', 'childPort'):
            while True:
                remaining = deadline - time.monotonic()
                assert remaining > 0, (
                    f'{key} still accepts connections after validation (2s cleanup deadline)'
                )
                with socket.socket() as connection:
                    connection.settimeout(min(0.1, remaining))
                    if connection.connect_ex(('127.0.0.1', launch[key])) != 0:
                        break
                time.sleep(min(0.05, max(0, deadline - time.monotonic())))
    return launches


@pytest.mark.parametrize('close_later', [True, False])
@pytest.mark.parametrize('child_only', [True, False])
def test_load_cleanup_check_waits_for_close_but_rejects_live_listener(
    tmp_path: Path, close_later: bool, child_only: bool,
) -> None:
    listener = socket.socket()
    listener.bind(('127.0.0.1', 0))
    listener.listen(128)
    port = listener.getsockname()[1]
    non_listener = socket.socket()
    non_listener.bind(('127.0.0.1', 0))
    (tmp_path / 'launches.jsonl').write_text(json.dumps({
        'port': non_listener.getsockname()[1] if child_only else port, 'childPort': port,
        'data': str(tmp_path / 'removed-data'),
        'home': str(tmp_path / 'removed-home'),
        'tmp': str(tmp_path / 'removed-home'),
        'cache': str(tmp_path / 'removed-home/npm-cache'),
    }), encoding='utf-8')
    timer = threading.Timer(0.25, listener.close) if close_later else None
    started = time.monotonic()
    try:
        if timer is not None:
            timer.start()
            _stopped(tmp_path)
        else:
            with pytest.raises(AssertionError, match='still accepts connections'):
                _stopped(tmp_path)
        assert 0.2 <= time.monotonic() - started < 3
    finally:
        if timer is not None:
            timer.cancel()
            timer.join(timeout=1)
        listener.close()
        non_listener.close()


def _shape(result: dict[str, Any]) -> None:
    assert set(result) == {
        'schema_version', 'profile', 'scale', 'status', 'reasons', 'cleanup_ok', 'measurements',
    }
    assert result['schema_version'] == 1
    assert result['profile'] == 'ultra-load-v1' and result['scale'] == 'ultra'
    assert result['status'] in {'passed', 'failed', 'unknown'}
    assert type(result['cleanup_ok']) is bool
    assert isinstance(result['reasons'], list)
    assert all(isinstance(reason, str) and reason.strip() for reason in result['reasons'])
    assert bool(result['reasons']) == (result['status'] != 'passed')
    measurements = result['measurements']
    assert set(measurements) == {*_EXPECTED, 'elapsed_seconds'}
    for key, maximum in _EXPECTED.items():
        assert type(measurements[key]) is int and measurements[key] >= 0
        if key != 'request_errors':
            assert measurements[key] <= maximum
    elapsed = measurements['elapsed_seconds']
    assert type(elapsed) is float and math.isfinite(elapsed) and elapsed >= 0


def _validate(root: Path, timeout: float = 25) -> dict[str, Any]:
    validate = getattr(requirements, '_validate_ultra_portfolio_load', None)
    assert callable(validate), 'R5b1 inner load validator is missing'
    result = validate(root, timeout)
    _shape(result)
    return cast(dict[str, Any], result)


def _assert_counts(root: Path, result: dict[str, Any]) -> None:
    requests = _log(root, 'requests.jsonl')
    reads = [r for r in requests if urlsplit(r['url']).path == '/portfolio/read-model']
    writes = [r for r in requests if r['method'] == 'POST']
    assert result['measurements']['read_model_requests'] == len(reads)
    assert result['measurements']['write_requests'] == len(writes)


@pytest.mark.parametrize('fault', ['', 'mixed_ids'], ids=['string-ids', 'mixed-ids'])
def test_actual_ultra_load_counts_pages_restart_update_and_cleanup(
    tmp_path: Path, fault: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _application(tmp_path, fault)
    observed: list[tuple[str, float]] = []
    request = requirements._PortfolioAPI.request

    def observed_request(api: Any, *args: Any, **kwargs: Any) -> tuple[int, object]:
        observed.append((threading.current_thread().name, api.deadline))
        return cast(tuple[int, object], request(api, *args, **kwargs))

    monkeypatch.setattr(requirements._PortfolioAPI, 'request', observed_request)
    result = _validate(root)
    assert result['status'] == 'passed', result
    assert result['cleanup_ok'] is True
    assert {k: result['measurements'][k] for k in _EXPECTED} == _EXPECTED
    assert len({deadline for _, deadline in observed}) == 1
    clients = Counter(name for name, _ in observed if name.startswith('ultra-load'))
    assert sorted(clients.values()) == [11, 11, 29, 29]
    assert not any(t.name in clients for t in threading.enumerate()), 'client thread leaked'
    launches = _stopped(root)
    assert len(launches) == 2 and launches[0]['pid'] != launches[1]['pid']
    assert launches[0]['data'] == launches[1]['data']
    _assert_counts(root, result)
    requests = _log(root, 'requests.jsonl')
    programs = [r for r in requests if r['method'] == 'POST' and r['url'] == '/programs']
    projects = [r for r in requests if r['method'] == 'POST' and r['url'] == '/projects']
    target, foreign = (r['payload']['id'] for r in programs)
    assert target != foreign and programs[0]['body']['name'] != programs[1]['body']['name']
    selected = [r for r in projects if r['body']['program_id'] == target]
    assert len(selected) == 1000 and len(projects) == 1017
    assert len({r['body']['name'] for r in projects}) == 1017
    assert len({str(r['payload']['id']) for r in projects}) == 1017
    modules = [r for r in requests if r['method'] == 'POST' and '/projects/' in r['url']]
    initial_modules = [r for r in modules if r['pid'] == launches[0]['pid']]
    assert len(initial_modules) == 44 and len(modules) == 45
    assert Counter(r['url'].split('/')[-1] for r in initial_modules) == {
        'budgets': 11, 'staffing': 11, 'risks': 11, 'milestones': 11,
    }
    assert {r['url'].split('/')[2] for r in initial_modules} == {
        str(r['payload']['id']) for i, r in enumerate(selected) if i % 97 == 0
    }
    assert len({r['body']['amount'] for r in initial_modules if r['url'].endswith('/budgets')}) > 1
    assert len({r['body']['allocation'] for r in initial_modules
                if r['url'].endswith('/staffing')}) > 1
    ordered = sorted(selected, key=lambda r: str(r['payload']['id']))
    read_requests = [r for r in requests if urlsplit(r['url']).path == '/portfolio/read-model']
    first_reads = [r for r in read_requests if r['pid'] == launches[0]['pid']]
    assert len(first_reads) == 83
    pages = Counter((int(parse_qs(urlsplit(r['url']).query).get('limit', ['100'])[0]),
                     int(parse_qs(urlsplit(r['url']).query).get('offset', ['0'])[0]))
                    for r in first_reads[:80])
    assert pages == Counter({**{(100, i): 2 for i in range(0, 1001, 100)},
                             **{(37, i): 2 for i in range(0, 1037, 37)}})
    assert [(int(parse_qs(urlsplit(r['url']).query)['limit'][0]),
             int(parse_qs(urlsplit(r['url']).query).get('offset', ['0'])[0]))
            for r in first_reads[80:]] == [(1, 0), (1, 999), (1, 1000)]
    # Build the oracle from submitted writes, independently of the validator's result.
    for read in read_requests:
        query = parse_qs(urlsplit(read['url']).query)
        offset = int(query.get('offset', ['0'])[0])
        limit = int(query.get('limit', ['100'])[0])
        expected = ordered[offset:offset + limit]
        items = read['payload']['items']
        assert len(items) == len(expected)
        prior = requests[:requests.index(read)]
        for item, project in zip(items, expected, strict=True):
            pid = project['payload']['id']
            assert (item['project_id'], item['program_id'], item['name']) == (
                pid, target, project['body']['name'],
            )
            recorded = [r for r in prior if r['method'] == 'POST'
                        and r['url'].startswith(f'/projects/{pid}/')]
            assert item['budget_total'] == sum(r['body']['amount'] for r in recorded
                                               if r['url'].endswith('/budgets'))
            assert item['staffing_allocation'] == sum(r['body']['allocation'] for r in recorded
                                                      if r['url'].endswith('/staffing'))
            assert item['risk_count'] == sum(r['url'].endswith('/risks') for r in recorded)
            assert item['milestone_count'] == sum(r['url'].endswith('/milestones') for r in recorded)
    after_restart = [r for r in read_requests if r['pid'] == launches[1]['pid']]
    assert len(after_restart) == 22
    assert after_restart[10]['payload']['items'] == after_restart[21]['payload']['items'] == []
    update = modules[-1]
    assert update['pid'] == launches[1]['pid'] and update['url'].endswith('/budgets')
    assert requests.index(after_restart[10]) < requests.index(update) < requests.index(after_restart[11])


@pytest.mark.parametrize('fault', [
    'ignore_offset', 'ignore_limit', 'first100', 'filter_after_page', 'duplicate',
    'missing_tail', 'restart_tail_loss', 'stale_updated_aggregate', 'wrong_name',
    'boolean_aggregate', 'duplicate_id', 'duplicate_program',
    'invalid_id:[]', 'invalid_id:{}', 'invalid_id:true', 'invalid_id:null', 'invalid_id:""',
])
def test_ultra_load_semantic_mutants_fail_with_honest_partial_counts(tmp_path: Path, fault: str) -> None:
    root = _application(tmp_path, fault)
    result = _validate(root)
    assert result['status'] == 'failed', result
    assert result['cleanup_ok'] is True
    assert not any('timeout' in reason.lower() for reason in result['reasons']), result
    launches = _stopped(root)
    _assert_counts(root, result)
    counts = result['measurements']
    if fault == 'restart_tail_loss':
        assert len(launches) == 2
        assert counts['initial_traversals'] == 4 and counts['restart_traversals'] == 0
        assert counts['write_requests'] == 1063
    if fault == 'stale_updated_aggregate':
        assert len(launches) == 2 and counts['restart_traversals'] == 1
        assert counts['updated_traversals'] == 0 and counts['write_requests'] == 1064


def test_legacy_controlled_bundle_does_not_satisfy_new_load_contract(tmp_path: Path) -> None:
    from agent_hub.runtime.project_scale_artifact import project_scale_artifact_zip_files

    if shutil.which('node') is None or shutil.which('npm') is None:
        pytest.skip('trusted real HTTP fixture requires Node and npm')
    root = tmp_path / 'legacy-controlled'
    root.mkdir()
    files = project_scale_artifact_zip_files(
        'Build a real ultra-large business project for flow=direct. '
        'Acceptance conditions require enterprise portfolio OS APIs, analytics, '
        'RBAC, persistence, tests, and verification evidence.'
    )
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding='utf-8')
    result = _validate(root)
    assert result['status'] == 'failed', result
    assert result['cleanup_ok'] is True
    assert any('portfolio load page' in reason for reason in result['reasons']), result
    assert result['measurements']['target_projects'] == 1000
    assert result['measurements']['initial_traversals'] < 4


@pytest.mark.parametrize('stage', ['headers', 'body'])
def test_portfolio_response_has_total_deadline_even_while_bytes_arrive(stage: str) -> None:
    stopped = threading.Event()
    listener = socket.socket()
    listener.bind(('127.0.0.1', 0))
    listener.listen()
    listener.settimeout(2)

    def stream() -> None:
        try:
            with listener.accept()[0] as client:
                client.recv(65536)
                if stage == 'body':
                    client.sendall(b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n'
                                   b'Content-Length: 100\r\n\r\n')
                else:
                    client.sendall(b'HTTP/1.1 200 OK\r\nX-Slow: ')
                for _ in range(60):
                    if stopped.wait(0.025):
                        break
                    client.sendall(b' ')
        except OSError:
            pass

    worker = threading.Thread(target=stream, daemon=True)
    worker.start()
    start = time.monotonic()
    try:
        api = requirements._PortfolioAPI(listener.getsockname()[1], start + 0.35)
        with pytest.raises((requirements._ValidationFailure, OSError, http.client.HTTPException)):
            api.request('GET', '/portfolio/read-model')
        assert time.monotonic() - start < 0.9, 'inactivity timeout did not bound total response time'
    finally:
        stopped.set()
        listener.close()
        worker.join(timeout=2)
    assert not worker.is_alive()


@pytest.mark.parametrize('expiry', ['startup', 'request'])
def test_load_deadline_is_unknown_and_still_cleans_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, expiry: str,
) -> None:
    root = _application(tmp_path, 'slow_program')
    npm = shutil.which('npm')
    assert npm is not None
    taskkill = shutil.which('taskkill') if requirements._PLATFORM == 'nt' else None
    with ExitStack() as stack:
        data = tempfile.TemporaryDirectory(dir=tmp_path, prefix='deadline-data-')
        runtime = tempfile.TemporaryDirectory(dir=tmp_path, prefix='deadline-runtime-')
        stack.enter_context(data)
        stack.enter_context(runtime)
        env = requirements._environment(data.name, runtime.name)
        port = requirements._free_port()
        env['PORT'] = str(port)
        process = subprocess.Popen(
            [npm, 'start'], cwd=root, env=env, stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=requirements._PLATFORM == 'posix',
        )
        try:
            # Prepare the real fixture separately; validation retains its 2s budget.
            ready_deadline = time.monotonic() + 25
            while True:
                assert process.poll() is None, 'trusted fixture exited before readiness'
                assert time.monotonic() < ready_deadline, 'trusted fixture never became ready'
                with socket.socket() as connection:
                    connection.settimeout(0.1)
                    if connection.connect_ex(('127.0.0.1', port)) == 0:
                        break
                time.sleep(0.05)
            assert not (root / 'requests.jsonl').exists()
            original_ready = requirements._ready_portfolio
            clock_offset = 0.0
            launches = 0
            allocated: list[str] = []

            def clock() -> float:
                return time.monotonic() + clock_offset

            def directory(*, prefix: str) -> tempfile.TemporaryDirectory[str]:
                allocated.append(prefix)
                return {'ultra-load-data-': data, 'ultra-load-runtime-': runtime}[prefix]

            def launch(command: list[str], **kwargs: Any) -> subprocess.Popen[bytes]:
                nonlocal launches
                launches += 1
                assert command == [npm, 'start'] and kwargs['cwd'] == root
                assert kwargs['env']['DATA_DIR'] == data.name
                assert kwargs['env']['HOME'] == runtime.name
                assert kwargs['env']['PORT'] == str(port)
                return process

            def ready(api: Any, child: subprocess.Popen[bytes]) -> None:
                nonlocal clock_offset
                assert child is process
                if expiry == 'startup':
                    # Expire the shared deadline before the first HTTP request.
                    clock_offset = api.deadline - time.monotonic()
                original_ready(api, child)

            with monkeypatch.context() as patch:
                patch.setattr(requirements.tempfile, 'TemporaryDirectory', directory)
                subprocess_shim = SimpleNamespace(**vars(subprocess))
                subprocess_shim.Popen = launch
                patch.setattr(requirements, 'subprocess', subprocess_shim)
                patch.setattr(requirements, '_free_port', lambda: port)
                patch.setattr(requirements, '_ready_portfolio', ready)
                patch.setattr(requirements, 'time', SimpleNamespace(
                    monotonic=clock, sleep=time.sleep,
                ))
                result = _validate(root, timeout=2)
            assert launches == 1
            assert allocated == ['ultra-load-data-', 'ultra-load-runtime-']
            assert result['status'] == 'unknown', result
            assert any('timeout' in reason.lower() for reason in result['reasons']), result
            assert result['measurements']['elapsed_seconds'] < 6
            expected = dict.fromkeys(_EXPECTED, 0)
            if expiry == 'request':
                expected.update(write_requests=1, request_errors=1)
            assert {key: result['measurements'][key] for key in _EXPECTED} == expected
            assert result['cleanup_ok'] is True
            assert process.poll() is not None
            _stopped(root)
            if expiry == 'startup':
                assert not (root / 'requests.jsonl').exists()
            else:
                assert [(entry['method'], entry['url']) for entry in _log(
                    root, 'requests.jsonl',
                )] == [('GET', '/programs'), ('POST', '/programs')]
                _assert_counts(root, result)
        finally:
            requirements._stop_tree(process, taskkill)


def test_environment_unavailable_is_unknown(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(requirements.shutil, 'which', lambda _: None)
    result = _validate(tmp_path)
    assert result['status'] == 'unknown'
    assert result['cleanup_ok'] is True
    assert all(result['measurements'][k] == 0 for k in _EXPECTED)


def test_temporary_cleanup_error_fails_even_after_successful_load(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _application(tmp_path)
    original = requirements.tempfile.TemporaryDirectory.cleanup

    def cleanup(directory: Any) -> None:
        original(directory)
        raise OSError('injected temporary cleanup failure')

    monkeypatch.setattr(requirements.tempfile.TemporaryDirectory, 'cleanup', cleanup)
    result = _validate(root)
    assert result['status'] == 'failed' and result['cleanup_ok'] is False
    assert any('cleanup' in reason for reason in result['reasons'])
    assert result['measurements']['updated_traversals'] == 1
    _stopped(root)


def test_inner_is_available_to_isolated_stdlib_runpy(tmp_path: Path) -> None:
    assert isinstance(requirements.__file__, str)
    completed = subprocess.run([
        sys.executable, '-I', '-S', '-c',
        ('import json,runpy,sys; from pathlib import Path; '
         'namespace=runpy.run_path(sys.argv[1]); '
         'print(json.dumps(namespace["_validate_ultra_portfolio_load"](Path(sys.argv[2]),0)))'),
        requirements.__file__, str(tmp_path),
    ], capture_output=True, text=True, timeout=5, check=True)
    result = json.loads(completed.stdout)
    _shape(result)
    assert result['status'] == 'unknown'


def test_public_dispatcher_uses_sandbox_on_posix(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from types import ModuleType

    sandbox = ModuleType('agent_hub.harness.project_validation_sandbox')
    expected = {'status': 'unknown', 'reasons': ['sandbox unavailable']}

    def validate(root: Path, scale: str, timeout: float) -> dict[str, Any]:
        assert (root, scale, timeout) == (tmp_path, 'ultra', 25)
        return expected

    sandbox.validate_scale_load = validate  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, sandbox.__name__, sandbox)
    monkeypatch.setattr(requirements, '_PLATFORM', 'posix')
    dispatcher = getattr(requirements, 'validate_ultra_portfolio_load', None)
    assert callable(dispatcher), 'public load dispatcher is missing'
    assert dispatcher(tmp_path, 25) is expected
