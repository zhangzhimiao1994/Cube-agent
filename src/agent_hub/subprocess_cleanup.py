from __future__ import annotations

import asyncio
from contextlib import suppress
from typing import NoReturn


class ProcessCleanupIncomplete(RuntimeError):
    """Owned pipe cleanup did not reach EOF; no descendant-termination guarantee."""


def _close_output_pipes(process: asyncio.subprocess.Process) -> None:
    # Process does not expose pipe transports; validate its asyncio-owned adapter.
    transport = getattr(process, "_transport", None)
    if not isinstance(transport, asyncio.SubprocessTransport):
        raise ProcessCleanupIncomplete("Owned subprocess pipe transports unavailable")
    for descriptor in (1, 2):
        pipe = transport.get_pipe_transport(descriptor)
        if pipe is not None:
            pipe.close()


async def drain_and_reap_process(
    process: asyncio.subprocess.Process, *, grace_seconds: float = 1.0
) -> None:
    async def discard(stream: asyncio.StreamReader) -> None:
        with suppress(Exception):
            while await stream.read(65536):
                pass

    async def incomplete(error: BaseException) -> NoReturn:
        try:
            _close_output_pipes(process)
        except ProcessCleanupIncomplete as close_error:
            raise ProcessCleanupIncomplete(str(close_error)) from error
        with suppress(Exception):
            await asyncio.wait_for(process.wait(), timeout=grace_seconds)
        raise ProcessCleanupIncomplete("Owned subprocess output did not reach EOF") from error

    waiter = asyncio.create_task(process.wait())
    drains = [
        asyncio.create_task(discard(stream))
        for stream in (process.stdout, process.stderr)
        if stream is not None
    ]
    try:
        try:
            await asyncio.wait_for(asyncio.shield(waiter), timeout=grace_seconds)
        except TimeoutError:
            with suppress(ProcessLookupError):
                process.kill()
        except BaseException as error:
            if process.returncode is None:
                with suppress(ProcessLookupError):
                    process.kill()
            try:
                await asyncio.wait_for(process.wait(), timeout=grace_seconds)
            except TimeoutError:
                await incomplete(error)
            except Exception:  # noqa: BLE001 - any reap failure still needs owned pipe closure
                await incomplete(error)
            raise
        try:
            await asyncio.wait_for(
                asyncio.gather(waiter, *drains, return_exceptions=True), timeout=grace_seconds
            )
            waiter.result()
        except TimeoutError as error:
            await incomplete(error)
    finally:
        waiter.cancel()
        for drain in drains:
            drain.cancel()
        await asyncio.gather(waiter, *drains, return_exceptions=True)
