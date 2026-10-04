"""Bounded, isolated transport for an untrusted generated Node module bridge."""

from __future__ import annotations

import json
import math
import os
import queue
import runpy
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import BinaryIO, cast
from uuid import uuid4

_FRAME_BYTES = 65536
_TOTAL_BYTES = 2 * 1024 * 1024
_GUARD = r'''
import json, os, sys
from pathlib import Path
pidns, netns, blocked, token, node = json.loads(sys.argv[1])
assert Path('/proc/self/ns/pid').stat().st_ino != pidns
assert Path('/proc/self/ns/net').stat().st_ino == netns
for name in blocked:
    assert not Path(name).exists() and not Path(name).is_symlink(), name
for name in os.listdir('/proc/self/fd'):
    fd = int(name)
    if fd <= 2:
        continue
    try:
        os.fstat(fd)
    except OSError:
        continue
    raise RuntimeError('inherited result descriptor')
assert Path('/data').is_dir() and Path('/project').is_dir()
print(json.dumps({'kind':'boundary','token':token}), flush=True)
os.execv(node, [node, '/bridge.cjs'])
'''
_CLEANUP = (
    'import pathlib,shutil,sys; p=pathlib.Path(sys.argv[1]); s=p.lstat(); '
    'assert str(s.st_dev)==sys.argv[2] and str(s.st_ino)==sys.argv[3]; '
    'assert not p.is_symlink(); shutil.rmtree(p)'
)


def _object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate protocol key')
        result[key] = value
    return result


def _constant(value: str) -> object:
    raise ValueError('nonfinite protocol number')


def _finite_float(value: str) -> float:
    result = float(value)
    if not math.isfinite(result):
        raise ValueError('overflowed protocol number')
    return result


def _launch_command(
    root: Path, scratch: Path, bridge: Path, env: dict[str, str],
) -> tuple[list[str], str | None]:
    if sys.platform == 'win32':
        node = shutil.which('node')
        if node is None:
            raise RuntimeError('Node executable unavailable')
        return [node, str(bridge)], None
    if sys.platform != 'linux':
        raise RuntimeError('module isolation unavailable on this platform')
    trusted = Path('/opt/validator/src/agent_hub/harness/project_validation_module_process.py')
    if root != Path('/workspace') or not trusted.is_file() or not trusted.samefile(__file__):
        raise RuntimeError('module validation requires the outer private Linux sandbox')
    token = uuid4().hex
    node_home = Path('/opt/validator/node')
    node = '/opt/validator/node/bin/node' if node_home.is_dir() else '/usr/bin/node'
    argv = [
        '/usr/bin/bwrap', '--die-with-parent', '--new-session', '--cap-drop', 'ALL',
        '--unshare-user', '--unshare-pid', '--unshare-ipc', '--unshare-uts',
        '--ro-bind', '/usr', '/usr', '--symlink', 'usr/bin', '/bin',
        '--ro-bind', '/lib', '/lib', '--proc', '/proc', '--dev', '/dev',
        '--tmpfs', '/tmp', '--tmpfs', '/run', '--tmpfs', '/var',
        '--ro-bind', str(root), '/project',
        '--bind', str(scratch / 'data'), '/data',
        '--bind', str(scratch / 'home'), '/home',
        '--ro-bind', str(bridge), '/bridge.cjs',
    ]
    for system in (Path('/lib64'), node_home):
        if system.exists():
            argv.extend(('--ro-bind', str(system), str(system)))
    argv.extend(('--chdir', '/project', '--clearenv'))
    inner_env = {
        'PATH': '/opt/validator/node/bin:/usr/bin:/bin', 'HOME': '/home',
        'DATA_DIR': '/data', 'TMPDIR': '/tmp', 'TMP': '/tmp', 'TEMP': '/tmp',
        'NODE_OPTIONS': '', 'NODE_PATH': '',
    }
    for key, value in inner_env.items():
        argv.extend(('--setenv', key, value))
    guard = [
        Path('/proc/self/ns/pid').stat().st_ino,
        Path('/proc/self/ns/net').stat().st_ino,
        [str(root), str(scratch), '/opt/validator/src'], token, node,
    ]
    return [*argv, '--', '/usr/bin/python3', '-I', '-B', '-c', _GUARD, json.dumps(guard)], token


class ModuleProcess:
    """Only framed observations cross this pipe; no child frame is a final verdict."""

    def __init__(self, root: Path, deadline: float) -> None:
        if type(deadline) not in (int, float) or not math.isfinite(deadline):
            raise ValueError('module deadline must be finite')
        self.deadline = deadline
        self._remaining()
        root = root.resolve(strict=True)
        if not root.is_dir():
            raise ValueError('module project root must be a directory')
        self.isolated = False
        self._closed = False
        self._failed = False
        self._error: str | None = None
        self._frames: queue.Queue[bytes | None] = queue.Queue(maxsize=16)
        self.scratch = Path(tempfile.mkdtemp(prefix='large-module-owned-'))
        self._identity = self.scratch.stat()
        self.process: subprocess.Popen[bytes]
        self._reader: threading.Thread | None = None
        try:
            for name in ('data', 'home', 'tmp'):
                (self.scratch / name).mkdir()
            env = {key: os.environ[key] for key in (
                'SYSTEMROOT', 'SystemRoot', 'WINDIR', 'COMSPEC', 'PATHEXT', 'PATH',
            ) if key in os.environ}
            env.update({
                'DATA_DIR': str(self.scratch / 'data'), 'HOME': str(self.scratch / 'home'),
                'USERPROFILE': str(self.scratch / 'home'), 'TMPDIR': str(self.scratch / 'tmp'),
                'TMP': str(self.scratch / 'tmp'), 'TEMP': str(self.scratch / 'tmp'),
                'NODE_OPTIONS': '', 'NODE_PATH': '',
            })
            command, token = _launch_command(
                root, self.scratch, Path(__file__).with_name('project_validation_bridge.cjs'), env,
            )
            self.process = subprocess.Popen(
                command, cwd=root, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, close_fds=True, bufsize=0,
                start_new_session=os.name == 'posix',
            )
            assert self.process.stdout is not None
            self._reader = threading.Thread(
                target=self._read, args=(self.process.stdout,), daemon=True,
            )
            self._reader.start()
            if token is not None:
                if self._frame() != {'kind': 'boundary', 'token': token}:
                    raise RuntimeError('module namespace guard failed')
                self.isolated = True
        except BaseException:
            self._failed = True
            self.close()
            raise

    def _remaining(self) -> float:
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeError('module protocol deadline exceeded')
        return remaining

    def _read(self, stream: BinaryIO) -> None:
        total = 0
        pending = bytearray()
        try:
            while True:
                chunk = stream.read(4096)
                if not chunk:
                    if pending:
                        raise RuntimeError('unterminated module frame')
                    self._frames.put_nowait(None)
                    return
                total += len(chunk)
                if total > _TOTAL_BYTES:
                    raise RuntimeError('module output exceeds lifetime budget')
                pending.extend(chunk)
                while b'\n' in pending:
                    line, _, tail = pending.partition(b'\n')
                    if len(line) > _FRAME_BYTES:
                        raise RuntimeError('module frame exceeds byte budget')
                    self._frames.put_nowait(bytes(line))
                    pending = bytearray(tail)
                if len(pending) > _FRAME_BYTES:
                    raise RuntimeError('module frame exceeds byte budget')
        except (OSError, ValueError, RuntimeError, queue.Full) as exc:
            self._error = f'module protocol reader failed: {type(exc).__name__}: {exc}'

    def _frame(self) -> dict[str, object]:
        while True:
            if self._error is not None:
                raise RuntimeError(self._error)
            try:
                raw = self._frames.get(timeout=min(0.05, self._remaining()))
                break
            except queue.Empty:
                continue
        if raw is None:
            raise RuntimeError('module exited without a complete result')
        try:
            result: object = json.loads(
                raw.decode('utf-8', errors='strict'), object_pairs_hook=_object,
                parse_constant=_constant, parse_float=_finite_float,
            )
        except (ValueError, UnicodeError, RecursionError) as exc:
            raise RuntimeError('invalid module JSON frame') from exc
        if type(result) is not dict:
            raise RuntimeError('module frame must be an object')
        return cast(dict[str, object], result)

    def _write(self, frame: dict[str, object]) -> None:
        payload = (json.dumps(frame, allow_nan=False) + '\n').encode('utf-8')
        if len(payload) > _FRAME_BYTES:
            raise RuntimeError('module request exceeds byte budget')
        completed = threading.Event()
        failure: list[Exception] = []

        def write() -> None:
            try:
                assert self.process.stdin is not None
                view = memoryview(payload)
                while view:
                    written = self.process.stdin.write(view)
                    if not written:
                        raise RuntimeError('module input closed')
                    view = view[written:]
            except (OSError, ValueError, RuntimeError) as exc:
                failure.append(exc)
            finally:
                completed.set()

        writer = threading.Thread(target=write, daemon=True)
        writer.start()
        if not completed.wait(self._remaining()):
            raise RuntimeError('module input deadline exceeded')
        if failure:
            raise RuntimeError('module input failed') from failure[0]

    def request(
        self, command: dict[str, object],
        on_event: Callable[[dict[str, object]], dict[str, object]] | None = None,
    ) -> object:
        if self._closed:
            raise RuntimeError('module process already closed')
        if self._failed:
            raise RuntimeError('module process already failed')
        try:
            request_id = uuid4().hex
            self._write({'kind': 'request', 'id': request_id, 'command': command})
            sequence = 1
            while True:
                frame = self._frame()
                if frame.get('id') != request_id:
                    raise RuntimeError('module frame id mismatch or replay')
                if frame.get('kind') == 'result' and frame.keys() == {'kind', 'id', 'result'}:
                    return frame['result']
                if (
                    frame.keys() != {'kind', 'id', 'seq', 'event'} or frame.get('kind') != 'event'
                    or type(frame.get('seq')) is not int or frame['seq'] != sequence
                    or not isinstance(frame.get('event'), dict) or on_event is None
                ):
                    raise RuntimeError('unexpected or out-of-order module event')
                data = on_event(cast(dict[str, object], frame['event']))
                self._write({'kind': 'event_reply', 'id': request_id, 'seq': sequence, 'data': data})
                sequence += 1
        except BaseException:
            self._failed = True
            raise

    def close(self) -> None:
        if self._closed:
            return
        errors: list[str] = []
        if hasattr(self, 'process'):
            try:
                helpers = runpy.run_path(str(Path(__file__).with_name('project_requirements.py')))
                helpers['_stop_tree'](self.process, shutil.which('taskkill'))
            except Exception as exc:  # noqa: BLE001 - cleanup must continue after helper failures
                errors.append(f'process cleanup failed: {exc}')
            # Stop writers first, then consume buffered output through EOF before closing the pipe.
            if self._reader is not None:
                self._reader.join(timeout=1)
                if self._reader.is_alive():
                    errors.append('module reader did not stop')
            for stream in (self.process.stdin, self.process.stdout):
                if stream is not None:
                    try:
                        stream.close()
                    except (OSError, ValueError) as exc:
                        errors.append(f'pipe cleanup failed: {exc}')
        if self.scratch.exists() or self.scratch.is_symlink():
            try:
                result = subprocess.run(
                    [sys.executable, '-I', '-B', '-c', _CLEANUP, str(self.scratch),
                     str(self._identity.st_dev), str(self._identity.st_ino)],
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL, timeout=3, check=False,
                )
                if result.returncode or self.scratch.exists() or self.scratch.is_symlink():
                    raise RuntimeError('private module directory remains')
            except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
                errors.append(f'directory cleanup failed: {exc}')
        if not errors:
            self._closed = True
        while True:
            try:
                raw = self._frames.get_nowait()
            except queue.Empty:
                break
            if raw is not None and self._error is None:
                self._error = 'unexpected trailing module frame'
        # A request failure already fails validation; retain it instead of masking it on close.
        if self._error is not None and not self._failed:
            errors.append(self._error)
        if errors:
            raise RuntimeError('; '.join(errors))
