from __future__ import annotations

import asyncio

from agent_hub.runtime.worker import _run


def test_worker_entrypoint_can_be_cancelled_cleanly() -> None:
    async def scenario() -> None:
        task = asyncio.create_task(_run())
        await asyncio.sleep(0)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(scenario())
