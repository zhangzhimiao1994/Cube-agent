from __future__ import annotations

import importlib.util
import os
import runpy
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

SOURCE = Path(__file__).resolve().parents[3] / (
    'src/agent_hub/harness/project_validation_module_process.py'
)


def module() -> Any:
    assert SOURCE.is_file(), 'isolated module bridge transport is missing'
    spec = importlib.util.spec_from_file_location('module_process_under_test', SOURCE)
    assert spec is not None and spec.loader is not None
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


def launch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: str,
           seconds: float = 4) -> Any:
    mod = module()
    script = tmp_path / 'trusted-wire-fixture.py'
    script.write_text('import sys,json,time,os\n' + body, encoding='utf-8')
    monkeypatch.setattr(mod, '_launch_command', lambda *args: (
        [sys.executable, '-I', '-u', str(script)], None,
    ))
    return mod.ModuleProcess(tmp_path, time.monotonic() + seconds)


def test_module_transport_handles_challenged_event_without_sharing_final_stdout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = launch(tmp_path, monkeypatch, '''
r=json.loads(sys.stdin.readline())
print(json.dumps({'kind':'event','id':r['id'],'seq':1,'event':{'value':17}}), flush=True)
a=json.loads(sys.stdin.readline())
print(json.dumps({'kind':'result','id':r['id'],'result':a['data']}), flush=True)
time.sleep(30)
''')
    owned = process.scratch
    try:
        assert process.request({'operation': 'test'}, lambda e: {'observed': e['value']}) == {
            'observed': 17,
        }
        assert process.isolated is False
        assert process.process.stdout is not sys.stdout
    finally:
        process.close()
    assert process.process.poll() is not None
    assert not owned.exists()
    process.close()


@pytest.mark.parametrize('payload', [
    '{"status":"passed","cleanup_ok":true}',
    '{"kind":"result","id":"stale","result":true}',
    '{"kind":"result","id":"ID","result":NaN}',
    '{"kind":"result","id":"ID","result":1e999}',
    '{"kind":"result","id":"ID","result":false,"result":true}',
    '{"kind":"event","id":"ID","seq":true,"event":{}}',
    '{"kind":"event","id":"ID","seq":2,"event":{}}',
    '{"kind":"result","id":"ID","result":true,"extra":1}',
])
def test_module_transport_rejects_forged_or_ambiguous_frames(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, payload: str,
) -> None:
    process = launch(tmp_path, monkeypatch, (
        "r=json.loads(sys.stdin.readline())\n"
        f"print({payload!r}.replace('ID',r['id']),flush=True)\n"
        'time.sleep(30)\n'
    ))
    try:
        with pytest.raises(RuntimeError):
            process.request({'operation': 'test'}, lambda event: {})
    finally:
        process.close()
    assert not process.scratch.exists()


@pytest.mark.parametrize('body', [
    'sys.exit(0)\n',
    "sys.stdout.write('x'*100000);sys.stdout.flush();time.sleep(30)\n",
    'time.sleep(30)\n',
])
def test_exit_flood_and_timeout_never_become_verdicts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: str,
) -> None:
    process = launch(tmp_path, monkeypatch, body, seconds=0.3)
    started = time.monotonic()
    try:
        with pytest.raises(RuntimeError):
            process.request({'operation': 'test'})
    finally:
        process.close()
    assert time.monotonic() - started < 5
    assert not process.scratch.exists()


def test_replayed_result_cannot_satisfy_next_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = launch(tmp_path, monkeypatch, '''
r=json.loads(sys.stdin.readline())
line=json.dumps({'kind':'result','id':r['id'],'result':1})
print(line,flush=True)
sys.stdin.readline()
print(line,flush=True)
time.sleep(30)
''')
    try:
        assert process.request({'operation': 'first'}) == 1
        with pytest.raises(RuntimeError, match='id|replay'):
            process.request({'operation': 'second'})
    finally:
        process.close()


def test_nonfinite_or_elapsed_deadline_does_not_start_child(tmp_path: Path) -> None:
    mod = module()
    for deadline in (float('nan'), float('inf'), time.monotonic() - 1):
        with pytest.raises((ValueError, RuntimeError)):
            mod.ModuleProcess(tmp_path, deadline)


@pytest.mark.parametrize('tail', [
    "print('not-json',flush=True)",
    "print(json.dumps({'status':'passed','cleanup_ok':True}),flush=True)",
    "print(json.dumps({'kind':'result','id':r['id'],'result':True}),flush=True)",
    "sys.stdout.buffer.write(b'\\xff\\n');sys.stdout.flush()",
    "sys.stdout.write('partial');sys.stdout.flush()",
    "sys.stdout.write('x'*100000);sys.stdout.flush()",
    "sys.stdout.write('{}\\n'*100);sys.stdout.flush()",
])
def test_close_rejects_trailing_output_after_result_and_cleans_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tail: str,
) -> None:
    process = launch(tmp_path, monkeypatch, (
        "r=json.loads(sys.stdin.readline())\n"
        "print(json.dumps({'kind':'result','id':r['id'],'result':True}),flush=True)\n"
        'sys.stdin.readline()\n' + tail + '\n'
    ))
    try:
        assert process.request({'operation': 'last'}) is True
        process.process.stdin.write(b'release\n')
        process._reader.join(timeout=2)
        assert not process._reader.is_alive()
        with pytest.raises(RuntimeError, match='module.*(frame|budget|Full)'):
            process.close()
        assert process.process.poll() is not None
        assert not process.scratch.exists()
    finally:
        process.close()


@pytest.mark.parametrize('tail', ['partial', 'not-json\n'])
def test_close_drains_output_arriving_during_process_stop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tail: str,
) -> None:
    process = launch(tmp_path, monkeypatch, (
        "r=json.loads(sys.stdin.readline())\n"
        "print(json.dumps({'kind':'result','id':r['id'],'result':True}),flush=True)\n"
        'sys.stdin.readline()\n'
        f'sys.stdout.write({tail!r});sys.stdout.flush()\n'
    ))
    helpers = runpy.run_path(str(SOURCE.with_name('project_requirements.py')))

    def stop_with_pending_output(child: subprocess.Popen[bytes], taskkill: str | None) -> None:
        assert child.stdin is not None
        child.stdin.write(b'release\n')
        child.wait(timeout=2)
        helpers['_stop_tree'](child, taskkill)

    try:
        assert process.request({'operation': 'last'}) is True
        with monkeypatch.context() as patch:
            patch.setattr(runpy, 'run_path', lambda path: {'_stop_tree': stop_with_pending_output})
            with pytest.raises(RuntimeError, match='module.*frame'):
                process.close()
        assert process.process.poll() is not None
        assert not process._reader.is_alive()
        assert not process.scratch.exists()
    finally:
        process.close()


def test_cleanup_preserves_original_callback_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = launch(tmp_path, monkeypatch, '''
r=json.loads(sys.stdin.readline())
print(json.dumps({'kind':'event','id':r['id'],'seq':1,'event':{}}),flush=True)
print('not-json',flush=True)
time.sleep(30)
''')
    failure = ValueError('original callback failure')

    def fail(event: dict[str, object]) -> dict[str, object]:
        raise failure

    with pytest.raises(ValueError) as caught:
        try:
            process.request({'operation': 'test'}, fail)
        finally:
            process.close()
    assert caught.value is failure
    assert process.process.poll() is not None
    assert not process.scratch.exists()


def test_request_cannot_resume_after_protocol_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = launch(tmp_path, monkeypatch, '''
sys.stdin.readline()
print('not-json',flush=True)
r=json.loads(sys.stdin.readline())
print(json.dumps({'kind':'result','id':r['id'],'result':True}),flush=True)
time.sleep(30)
''')
    try:
        with pytest.raises(RuntimeError, match='JSON'):
            process.request({'operation': 'first'})
        with pytest.raises(RuntimeError, match='failed'):
            process.request({'operation': 'second'})
    finally:
        process.close()
    assert not process.scratch.exists()


@pytest.mark.skipif(os.name != 'nt', reason='trusted Windows fixture process only')
def test_child_does_not_inherit_parent_verdict_descriptor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    if sys.platform != 'win32':
        pytest.skip('Windows raw HANDLE probe')
    import msvcrt

    verdict = tmp_path / 'parent-result'
    with verdict.open('wb') as target:
        os.set_inheritable(target.fileno(), True)
        handle = msvcrt.get_osfhandle(target.fileno())
        child = launch(tmp_path, monkeypatch, f'''
import ctypes
from ctypes import wintypes
r=json.loads(sys.stdin.readline())
kernel=ctypes.WinDLL('kernel32',use_last_error=True)
kernel.WriteFile.argtypes=(wintypes.HANDLE,ctypes.c_void_p,wintypes.DWORD,
                          ctypes.POINTER(wintypes.DWORD),ctypes.c_void_p)
kernel.WriteFile.restype=wintypes.BOOL
written=wintypes.DWORD()
data=b'forged verdict'
success=bool(kernel.WriteFile({handle},data,len(data),ctypes.byref(written),None))
print(json.dumps({{'kind':'result','id':r['id'],
                  'result':{{'write_succeeded':success,'written':written.value}}}}),flush=True)
time.sleep(30)
''')
        try:
            assert child.request({'operation': 'test'}) == {
                'write_succeeded': False, 'written': 0,
            }
        finally:
            child.close()
    assert verdict.read_bytes() == b''
