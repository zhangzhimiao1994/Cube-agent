"""Explicit per-operation Windows Job stdio; no filesystem or token isolation."""

from __future__ import annotations

import asyncio
import os
import signal
import sys
from collections.abc import Callable, Mapping
from contextlib import suppress
from typing import Any, cast

from agent_hub.windows_process_job import WindowsProcessJob

# Unconfirmed native registrations may still borrow these owners' handles.
_retained_owners: set[_JobTransport] = set()


def _close_raw_handles(handles: list[Any]) -> list[Exception]:
    errors: list[Exception] = []
    for handle in handles:
        try:
            handle.close()
        except Exception as error:  # noqa: BLE001 - every owned handle must be attempted
            errors.append(error)
    return errors


class _Streams(asyncio.SubprocessProtocol):
    def __init__(self) -> None:
        self.stdin: asyncio.StreamWriter | None = None
        self.stdout: asyncio.StreamReader | None = None
        self.stderr: asyncio.StreamReader | None = None


class _PipeProtocol(asyncio.StreamReaderProtocol):
    def __init__(self, reader: asyncio.StreamReader, owner: _JobTransport, descriptor: int) -> None:
        super().__init__(reader)
        self._owned_reader = reader
        self._owner = owner
        self._descriptor = descriptor

    def eof_received(self) -> bool:
        super().eof_received()
        # A subprocess read pipe has no TCP-style writable half to keep open.
        return False

    def connection_lost(self, exc: Exception | None) -> None:
        super().connection_lost(exc)
        # Waiting for writer closure is optional, just as for stdlib subprocesses.
        closed = cast(Any, self)._closed
        if closed.done() and not closed.cancelled():
            closed.exception()
        self._owner.pipe_lost(self._descriptor)


class _JobTransport(asyncio.SubprocessTransport):
    def __init__(
        self, job: WindowsProcessJob, parents: dict[int, Any], loop: asyncio.AbstractEventLoop
    ) -> None:
        super().__init__({"windows_job": job})
        self.job = job
        self.parents = parents
        self.loop = loop
        self.streams = _Streams()
        self.pipes: dict[int, asyncio.BaseTransport] = {}
        self._pending_outputs = set(parents) - {0}
        self._outputs_closed = asyncio.Event()
        if not self._pending_outputs:
            self._outputs_closed.set()
        self._root_exited = False
        self._returncode: int | None = None
        self._closing = False
        self._close_error: BaseException | None = None
        self._forced_error: asyncio.Future[BaseException] = loop.create_future()
        self._finished: asyncio.Future[int] = loop.create_future()
        self._retirement: asyncio.Future[None] = loop.create_future()
        self._root_wait: Any = None
        self._registration_attempted = False
        self._watcher: asyncio.Task[None] | None = None

    def start(self) -> None:
        watcher = self._watch_root()
        try:
            self._watcher = self.loop.create_task(watcher)
        except BaseException:
            watcher.close()
            raise

    def _register_root_wait(self) -> None:
        # Register only inside the already-owned cleanup task, after allocations.
        native_loop = cast(Any, self.loop)
        self._registration_attempted = True
        self._root_wait = native_loop._proactor.wait_for_handle(self.job.root_wait_handle)
        unregister_callback = self._root_wait._unregister_wait_cb

        def record_retirement(future: asyncio.Future[bool]) -> None:
            error: BaseException | None = None
            try:
                if future.result() is not True:
                    raise OSError("Windows wait retirement unconfirmed")
            except BaseException as exc:  # noqa: BLE001 - retain native ownership on uncertainty
                error = exc
            if error is None:
                try:
                    unregister_callback(future)
                except BaseException as exc:  # noqa: BLE001 - callback is part of the proof
                    error = exc
            if error is None:
                self._retirement.set_result(None)
            else:
                self._retirement.set_exception(error)
                self._retirement.exception()

        # The stdlib clears _event_fut in this callback, including on failure.
        self._root_wait._unregister_wait_cb = record_retirement

    def pipe_lost(self, descriptor: int) -> None:
        self._pending_outputs.discard(descriptor)
        if not self._pending_outputs:
            self._outputs_closed.set()

    async def connect(self) -> None:
        def bind_protocol(protocol: _PipeProtocol) -> Callable[[], _PipeProtocol]:
            return lambda: protocol

        for descriptor, handle in list(self.parents.items()):
            reader = asyncio.StreamReader(limit=65536)
            protocol = _PipeProtocol(reader, self, descriptor)
            transport: asyncio.BaseTransport
            if descriptor == 0:
                transport, _ = await self.loop.connect_write_pipe(bind_protocol(protocol), handle)
                self.streams.stdin = asyncio.StreamWriter(transport, protocol, None, self.loop)
            else:
                transport, _ = await self.loop.connect_read_pipe(bind_protocol(protocol), handle)
                if descriptor == 1:
                    self.streams.stdout = reader
                else:
                    self.streams.stderr = reader
            self.pipes[descriptor] = transport
            self.parents.pop(descriptor, None)
            if self._closing or (descriptor == 0 and self._root_exited):
                transport.close()

    async def _watch_root(self) -> None:
        failure: BaseException | None = None
        code: int | None = None
        outputs: asyncio.Task[bool] | None = None
        try:
            self._register_root_wait()
            done, _ = await asyncio.wait(
                (self._root_wait, self._forced_error), return_when=asyncio.FIRST_COMPLETED
            )
            if self._forced_error in done:
                raise self._forced_error.result()
            await self._root_wait
            code = self.job.poll_root()
            if code is None:
                raise OSError("Native Windows root exit unconfirmed")
            self._returncode = code
            self._root_exited = True
            self.job.terminate_members()
            stdin = self.pipes.get(0)
            if stdin is not None:
                stdin.close()
            outputs = self.loop.create_task(self._outputs_closed.wait())
            done, _ = await asyncio.wait(
                (outputs, self._forced_error), return_when=asyncio.FIRST_COMPLETED
            )
            if self._forced_error in done:
                raise self._forced_error.result()
            await outputs
        except BaseException as error:  # noqa: BLE001 - even cancellation must reap owned resources
            failure = error
            self._close_pipes()
        if outputs is not None:
            outputs.cancel()
            await asyncio.gather(outputs, return_exceptions=True)
        retirement_error: BaseException | None = None
        try:
            # Unregister the native wait before releasing its borrowed process handle.
            if self._registration_attempted and self._root_wait is None:
                raise OSError("Windows native registration state unknown")
            if self._root_wait is not None:
                if not self._root_wait.done():
                    self._root_wait.cancel()
                unregistered = getattr(self._root_wait, "_event_fut", None)
                if unregistered is not None:
                    while not unregistered.done():
                        with suppress(asyncio.CancelledError):
                            await asyncio.shield(unregistered)
                    unregistered.result()
                if not self._retirement.done():
                    raise OSError("Windows native wait retirement unconfirmed")
                self._retirement.result()
        except BaseException as error:  # noqa: BLE001 - never release a still-borrowed handle
            retirement_error = error
            _retained_owners.add(self)
            self._close_pipes()
            try:
                self.job.terminate_members()
            except BaseException as terminate_error:  # noqa: BLE001 - incomplete stays explicit
                error.add_note(f"Family termination also failed: {terminate_error!r}")
        else:
            try:
                self.job.close()
            except BaseException as error:  # noqa: BLE001 - incomplete is never success
                failure = error
        if self._close_error is not None:
            failure = self._close_error
        elif self._forced_error.done() and not self._forced_error.cancelled():
            failure = self._forced_error.result()
        if retirement_error is not None:
            incomplete = OSError("Windows native wait retirement incomplete; ownership retained")
            incomplete.__cause__ = retirement_error
            if failure is not None:
                incomplete.add_note(f"Additional cleanup failure: {failure!r}")
            failure = incomplete
        self._forced_error.cancel()
        if not self._finished.done():
            if failure is not None:
                self._finished.set_exception(failure)
                self._finished.exception()
            elif code is not None:
                self._finished.set_result(code)
            else:
                self._finished.set_exception(OSError("Windows root exit unknown"))
                self._finished.exception()

    def _close_pipes(self) -> list[Exception]:
        errors = _close_raw_handles(list(self.pipes.values()))
        for descriptor, handle in list(self.parents.items()):
            closed = _close_raw_handles([handle])
            errors.extend(closed)
            if not closed:
                self.parents.pop(descriptor, None)
                self.pipe_lost(descriptor)
        return errors

    def get_pid(self) -> int:
        return self.job.pid

    def get_returncode(self) -> int | None:
        return self._returncode

    def get_pipe_transport(self, fd: int) -> asyncio.BaseTransport | None:
        return self.pipes.get(fd)

    def get_protocol(self) -> asyncio.BaseProtocol:
        return self.streams

    def set_protocol(self, protocol: asyncio.BaseProtocol) -> None:
        raise NotImplementedError("Managed subprocess protocol cannot be replaced")

    def is_closing(self) -> bool:
        return self._closing

    def terminate(self) -> None:
        self.job.terminate_members()

    def kill(self) -> None:
        self.terminate()

    def send_signal(self, sig: int) -> None:
        if sig != signal.SIGTERM:
            raise ValueError("Only process-family termination is supported")
        self.terminate()

    def close(self) -> None:
        if self._closing:
            if self._close_error is not None:
                raise OSError(
                    "Windows managed cleanup previously incomplete"
                ) from self._close_error
            return
        self._closing = True
        errors: list[Exception] = []
        if not self._finished.done():
            try:
                self.job.terminate_members()
            except Exception as error:  # noqa: BLE001 - close pipes despite termination failure
                errors.append(error)
        errors.extend(self._close_pipes())
        if errors:
            self._close_error = errors[0]
            if not self._forced_error.done():
                self._forced_error.set_result(errors[0])
            raise OSError("Windows managed process cleanup incomplete") from errors[0]

    async def _wait(self) -> int:
        try:
            return await asyncio.shield(self._finished)
        except asyncio.CancelledError:
            with suppress(OSError):
                self.close()
            while not self._finished.done():
                with suppress(asyncio.CancelledError):
                    await asyncio.shield(self._finished)
            self._finished.result()
            raise

    async def abort_startup(self) -> None:
        if self._watcher is None:
            errors = self._close_pipes()
            if self._registration_attempted:
                try:
                    if not self._retirement.done():
                        raise OSError("Windows native registration state unknown")
                    self._retirement.result()
                except BaseException as error:
                    _retained_owners.add(self)
                    try:
                        self.job.terminate_members()
                    except Exception as terminate_error:  # noqa: BLE001 - keep original cause
                        errors.append(terminate_error)
                    incomplete = OSError(
                        "Windows startup retirement incomplete; ownership retained"
                    )
                    for extra in errors:
                        incomplete.add_note(f"Additional cleanup failure: {extra!r}")
                    raise incomplete from error
            self.job.close()
            if errors:
                raise OSError("Windows startup pipe cleanup incomplete") from errors[0]
            return
        with suppress(OSError):
            self.close()
        while not self._finished.done():
            with suppress(asyncio.CancelledError):
                await asyncio.shield(self._finished)
        self._finished.result()


async def create_subprocess_exec(
    program: str,
    *args: str,
    stdin: int = asyncio.subprocess.DEVNULL,
    stdout: int = asyncio.subprocess.PIPE,
    stderr: int = asyncio.subprocess.PIPE,
    env: Mapping[str, str] | None = None,
) -> asyncio.subprocess.Process:
    if (
        sys.platform != "win32"
        or sys.version_info[:2] != (3, 12)
        or sys.implementation.name != "cpython"
    ):
        raise OSError("Windows managed subprocess requires CPython3.12 on Windows")
    loop = asyncio.get_running_loop()
    if not isinstance(loop, asyncio.ProactorEventLoop):
        raise OSError("Windows managed subprocess requires Proactor event loop")
    if loop.get_task_factory() is not None:
        raise OSError("Windows managed subprocess requires the default task factory")
    if any(
        mode not in {asyncio.subprocess.PIPE, asyncio.subprocess.DEVNULL}
        for mode in (stdin, stdout, stderr)
    ):
        raise ValueError("Managed subprocess supports only PIPE or DEVNULL")
    import _winapi
    from asyncio import windows_utils

    parents: dict[int, Any] = {}
    children: list[Any] = []
    handles: list[int] = []
    null = windows_utils.PipeHandle(_winapi.CreateFile("NUL", 0xC0000000, 3, 0, 3, 0, 0))
    children.append(null)
    owner: _JobTransport | None = None
    job: WindowsProcessJob | None = None
    try:
        for descriptor, mode in enumerate((stdin, stdout, stderr)):
            if mode == asyncio.subprocess.DEVNULL:
                handle = null.fileno()
            else:
                read, write = windows_utils.pipe(
                    overlapped=(False, True) if descriptor == 0 else (True, False), duplex=True
                )
                parent, child = (write, read) if descriptor == 0 else (read, write)
                parents[descriptor] = windows_utils.PipeHandle(parent)
                children.append(windows_utils.PipeHandle(child))
                handle = child
            os.set_handle_inheritable(handle, True)
            handles.append(handle)
        job = WindowsProcessJob(
            (program, *args), env=env, stdio_handles=(handles[0], handles[1], handles[2])
        )
        child_errors = _close_raw_handles(children)
        if child_errors:
            raise OSError("Windows child stdio handle cleanup incomplete") from child_errors[0]
        owner = _JobTransport(job, parents, loop)
        owner.start()
        await owner.connect()
        return asyncio.subprocess.Process(owner, cast(Any, owner.streams), loop)
    except BaseException:
        if owner is not None:
            await owner.abort_startup()
        elif job is not None:
            job.close()
        raise
    finally:
        handle_errors = _close_raw_handles(children)
        handle_errors.extend(_close_raw_handles(list(parents.values())))
        if handle_errors:
            raise OSError("Windows raw stdio handle cleanup incomplete") from handle_errors[0]
