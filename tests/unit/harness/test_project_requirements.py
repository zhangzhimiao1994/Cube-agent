from __future__ import annotations

import importlib
import json
import math
import shutil
import socket
import threading
import time
from pathlib import Path

import pytest

# This is an actual npm application, using only Node built-ins (no install/network).
# Faults change HTTP behavior or storage, rather than mocking the validator.
_APP = r"""
const http = require('node:http');
const fs = require('node:fs');
const path = require('node:path');
const {spawn} = require('node:child_process');
const fault = fs.existsSync('fault.txt') ? fs.readFileSync('fault.txt', 'utf8') : '';
const data = process.env.DATA_DIR;
fs.mkdirSync(data, {recursive: true});
const file = path.join(data, 'tasks.json');
let tasks = fault === 'volatile' || !fs.existsSync(file)
  ? [] : JSON.parse(fs.readFileSync(file, 'utf8'));
if (fault === 'lost_status') tasks.forEach(t => { t.status = 'todo'; });
const write = () => fs.writeFileSync(file, JSON.stringify(tasks));
const child = spawn(process.execPath, ['-e',
  "require('node:http').createServer(()=>{}).listen(0,'127.0.0.1',function(){" +
  "console.log(this.address().port)});process.on('SIGTERM',()=>{});"],
  {stdio: ['ignore', 'pipe', 'ignore']});
child.stdout.once('data', value => {
  fs.appendFileSync('launches.jsonl', JSON.stringify({
    pid: process.pid, port: Number(process.env.PORT), childPort: Number(value.toString()), data,
    leakedSecret: process.env.API_SECRET_SENTINEL || null
  }) + '\n');
  if (fault === 'no_listen') { setInterval(() => {}, 1000); }
  else server.listen(Number(process.env.PORT), '127.0.0.1');
});
const server = http.createServer(async (req, res) => {
  fs.appendFileSync('requests.jsonl', JSON.stringify({method: req.method, url: req.url}) + '\n');
  const send = (status, body) => {
    res.writeHead(status, {'Content-Type': 'application/json'});
    res.end(body === undefined ? undefined : JSON.stringify(body));
  };
  if (fault === 'hang') return;
  if (fault === 'bad_json') { res.writeHead(200); res.end('not json'); return; }
  if (fault === 'constant') { send(200, {items: []}); return; }
  let text = '';
  for await (const part of req) text += part;
  const body = text ? JSON.parse(text) : {};
  if (req.method === 'GET' && req.url === '/tasks') {
    send(200, {items: tasks.filter(t => !t.deleted)}); return;
  }
  if (req.method === 'POST' && req.url === '/tasks') {
    const task = {id: fault === 'duplicate_id' ? 'same' : require('node:crypto').randomUUID(),
      title: body.title, status: 'todo', created_at: new Date().toISOString()};
    if (fault === 'missing_created') delete task.created_at;
    tasks.push(task); write(); send(201, task); return;
  }
  const id = decodeURIComponent(req.url.split('/')[2] || '');
  const task = tasks.find(t => t.id === id);
  if (!task) {
    send(fault === 'missing_200' ? 200 : 404,
      fault === 'bad_error' ? {error: 'missing'} : {error: {code: 'NOT_FOUND', message: 'No task'}});
    return;
  }
  if (req.method === 'PATCH') {
    const updated = {...task, status: body.status};
    if (fault !== 'patch_noop') Object.assign(task, updated);
    write(); send(200, updated); return;
  }
  if (req.method === 'DELETE') {
    if (fault !== 'delete_noop') task.deleted = true;
    write(); send(204); return;
  }
  if (req.method === 'POST' && req.url.endsWith('/restore')) {
    if (fault !== 'restore_noop') task.deleted = false;
    write(); send(200, task); return;
  }
  send(404, {error: {code: 'NOT_FOUND', message: 'Unknown endpoint'}});
});
"""


def _validate(root: Path, timeout: float = 20) -> tuple[str, ...]:
    module = importlib.import_module('agent_hub.harness.project_requirements')
    result = module.validate_small_task_api(root, timeout_seconds=timeout)
    assert isinstance(result, tuple)
    assert all(isinstance(reason, str) and reason for reason in result)
    return result


@pytest.fixture
def application(tmp_path: Path) -> Path:
    if shutil.which('node') is None or shutil.which('npm') is None:
        pytest.skip('real HTTP fixture requires Node and npm')
    root = tmp_path / 'project with spaces'
    root.mkdir()
    (root / 'package.json').write_text(
        json.dumps({'private': True, 'scripts': {'start': 'node server.cjs'}}), encoding='utf-8'
    )
    (root / 'server.cjs').write_text(_APP, encoding='utf-8')
    return root


def _assert_stopped(root: Path) -> list[dict[str, object]]:
    launches = [
        json.loads(line)
        for line in (root / 'launches.jsonl').read_text(encoding='utf-8').splitlines()
    ]
    assert launches
    deadline = time.monotonic() + 2
    for launch in launches:
        assert not Path(str(launch['data'])).exists(), 'temporary DATA_DIR leaked'
        for key in ('port', 'childPort'):
            while True:
                remaining = deadline - time.monotonic()
                assert remaining > 0, (
                    f'{key} still accepts connections after validation (2s cleanup deadline)'
                )
                with socket.socket() as connection:
                    connection.settimeout(min(0.1, remaining))
                    if connection.connect_ex(('127.0.0.1', int(str(launch[key])))) != 0:
                        break
                time.sleep(min(0.05, max(0, deadline - time.monotonic())))
    return launches


@pytest.mark.parametrize('close_later', [True, False])
def test_cleanup_check_waits_for_close_but_rejects_live_listener(
    tmp_path: Path, close_later: bool
) -> None:
    listener = socket.socket()
    listener.bind(('127.0.0.1', 0))
    listener.listen(128)
    port = listener.getsockname()[1]
    (tmp_path / 'launches.jsonl').write_text(
        json.dumps({'port': port, 'childPort': port, 'data': str(tmp_path / 'removed-data')}),
        encoding='utf-8',
    )
    timer = threading.Timer(0.25, listener.close) if close_later else None
    started = time.monotonic()
    try:
        if timer is not None:
            timer.start()
            _assert_stopped(tmp_path)
        else:
            with pytest.raises(AssertionError, match='still accepts connections'):
                _assert_stopped(tmp_path)
        assert 0.2 <= time.monotonic() - started < 3
    finally:
        if timer is not None:
            timer.cancel()
            timer.join(timeout=1)
        listener.close()


def test_real_npm_api_crud_restart_and_cleanup_without_proxy(
    application: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'http_proxy', 'https_proxy', 'all_proxy'):
        monkeypatch.setenv(name, 'http://127.0.0.1:1')
    monkeypatch.setenv('NO_PROXY', '')
    monkeypatch.setenv('no_proxy', '')
    monkeypatch.setenv('DATA_DIR', str(application / 'must-not-use'))
    monkeypatch.setenv('PORT', '1')
    monkeypatch.setenv('API_SECRET_SENTINEL', 'must-not-reach-generated-code')

    assert _validate(application) == ()

    launches = _assert_stopped(application)
    assert len(launches) >= 2, 'must actually restart npm to verify persistence'
    assert len({launch['data'] for launch in launches}) == 1
    assert len({launch['pid'] for launch in launches}) == len(launches)
    assert all(launch['leakedSecret'] is None for launch in launches)
    assert not (application / 'must-not-use').exists()
    requests = [
        json.loads(line)
        for line in (application / 'requests.jsonl').read_text(encoding='utf-8').splitlines()
    ]
    assert {request['method'] for request in requests} == {'POST', 'GET', 'PATCH', 'DELETE'}
    assert any(request['url'].endswith('/restore') for request in requests)


@pytest.mark.parametrize(
    ('fault', 'reason'),
    [
        ('volatile', 'persist'),
        ('lost_status', 'status'),
        ('patch_noop', 'status'),
        ('delete_noop', 'delet'),
        ('restore_noop', 'restor'),
        ('missing_200', '404'),
        ('bad_error', 'error'),
        ('missing_created', 'created_at'),
        ('duplicate_id', 'id'),
        ('constant', '201'),
        ('bad_json', 'JSON'),
    ],
)
def test_real_broken_api_cannot_pass(
    application: Path, fault: str, reason: str
) -> None:
    (application / 'fault.txt').write_text(fault, encoding='utf-8')

    failures = _validate(application)

    assert failures, f'{fault} incorrectly passed independent acceptance'
    assert reason.lower() in ' '.join(failures).lower()
    _assert_stopped(application)


@pytest.mark.parametrize('fault', ['no_listen', 'hang'])
def test_real_timeout_cleans_up_server_and_descendant(
    application: Path, fault: str
) -> None:
    (application / 'fault.txt').write_text(fault, encoding='utf-8')
    started = time.monotonic()

    failures = _validate(application, timeout=3)

    assert failures
    assert 'timeout' in ' '.join(failures).lower()
    assert time.monotonic() - started < 12
    _assert_stopped(application)


def test_missing_project_fails_without_starting_npm(tmp_path: Path) -> None:
    assert _validate(tmp_path)


def test_medium_crm_create_failure_includes_response_payload() -> None:
    module = importlib.import_module('agent_hub.harness.project_requirements')
    api = module._TenantCRMAPI(port=1, deadline=time.monotonic() + 30)
    api.request = lambda method, path, body=None: (
        400,
        {'error': {'code': 'INVALID_STAGE', 'message': 'stage must be open|won|lost'}},
    )

    with pytest.raises(module._ValidationFailure) as excinfo:
        api.create('tenant-a', 'opportunities', {'stage': 'open'})

    message = str(excinfo.value)
    assert 'expected 201, got 400' in message
    assert 'INVALID_STAGE' in message
    assert 'open|won|lost' in message


@pytest.mark.parametrize('timeout', [0.0, -1.0, math.inf, math.nan])
def test_invalid_timeout_fails(tmp_path: Path, timeout: float) -> None:
    failures = _validate(tmp_path, timeout)
    assert failures
    assert 'timeout' in ' '.join(failures).lower()


def test_missing_npm_is_failure(application: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('PATH', '')
    failures = _validate(application)
    assert failures
    assert 'npm' in ' '.join(failures).lower()


def test_unsupported_platform_is_failure(application: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    module = importlib.import_module('agent_hub.harness.project_requirements')
    monkeypatch.setattr(module, '_PLATFORM', 'unsupported')
    failures = _validate(application)
    assert failures
    assert 'unsupported' in ' '.join(failures).lower()
