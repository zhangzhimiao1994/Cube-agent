"""Function-loop Redis for the explicit, isolated Linux native test worker."""
from __future__ import annotations

import asyncio
import os
import re
import shutil
import tempfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from redis.asyncio import Redis
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError


def require_native_pg_environment() -> None:
    """Require the worker's post-probe opt-in and its exact private Unix DSN."""
    value = os.environ.get("AGENT_HUB_TEST_DATABASE_URL")
    if (
        os.name != "posix"
        or os.environ.get("AGENT_HUB_NATIVE_OWNED_UNIX_REDIS") != "1"
        or os.environ.get("AGENT_HUB_HANDOVER_TEST_DB_ISOLATED") != "1"
        or not value
        or value != os.environ.get("AGENT_HUB_DATABASE_URL")
    ):
        raise ValueError("owned Redis requires the verified private PG test environment")
    try:
        url = make_url(value)
    except ArgumentError:
        raise ValueError("owned Redis requires the verified private PG test environment") from None
    socket = url.query.get("host")
    if (
        url.drivername != "postgresql+asyncpg"
        or url.username != "agent_hub_test"
        or url.database != "agent_hub_test"
        or url.password is not None
        or url.host is not None
        or url.port is not None
        or set(url.query) != {"host"}
        or not isinstance(socket, str)
        or re.fullmatch(r"/run/mofang-review-pg-[a-f0-9]{32}/socket", socket) is None
    ):
        raise ValueError("owned Redis requires the verified private PG test environment")


@asynccontextmanager
async def owned_native_redis() -> AsyncIterator[Redis]:
    require_native_pg_environment()
    executable = shutil.which("redis-server")
    if executable is None:
        raise RuntimeError("native binding must supply redis-server; missing runtime is not PASS")
    # Same real server/startup/cleanup contract as native mode tests, using this
    # fixture's function event loop rather than sharing their module-loop client.
    with tempfile.TemporaryDirectory(prefix="capacity-native-redis-", dir="/tmp") as directory:
        socket = Path(directory) / "redis.sock"
        process = await asyncio.create_subprocess_exec(
            executable, "--port", "0", "--unixsocket", str(socket), "--unixsocketperm", "700",
            "--save", "", "--appendonly", "no", "--daemonize", "no",
            "--maxmemory", "16mb", "--maxmemory-policy", "noeviction", "--dir", directory,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        client = Redis(unix_socket_path=str(socket), socket_connect_timeout=1, socket_timeout=2)
        try:
            async with asyncio.timeout(5):
                while True:
                    if process.returncode is not None:
                        raise RuntimeError("owned redis-server exited before startup")
                    try:
                        if await client.ping():
                            break
                    except (ConnectionError, OSError):
                        pass
                    except Exception as error:
                        if not type(error).__module__.startswith("redis"):
                            raise
                    await asyncio.sleep(0.03)
            memory = await client.config_get("maxmemory")
            assert int(next(iter(memory.values()))) == 16 * 1024 * 1024
            yield client
        finally:
            try:
                await client.aclose()
            finally:
                if process.returncode is None:
                    try:
                        process.terminate()
                    except ProcessLookupError:
                        pass
                try:
                    await asyncio.wait_for(process.wait(), timeout=5)
                except TimeoutError:
                    process.kill()
                    await process.wait()
                assert process.returncode is not None
