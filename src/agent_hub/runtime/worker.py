from __future__ import annotations

import asyncio
import signal


async def _run() -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signum, stop.set)
        except NotImplementedError:
            pass
    await stop.wait()


def main() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main()
