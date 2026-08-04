import asyncio
from dataclasses import dataclass
from time import monotonic

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)


def build_engine(database_url: str) -> AsyncEngine:
    return create_async_engine(database_url, pool_pre_ping=True)


def _session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


@dataclass(slots=True)
class Database:
    engine: AsyncEngine
    session_factory: async_sessionmaker[AsyncSession]

    async def dispose(self) -> None:
        await self.engine.dispose()

    async def wait_until_ready(
        self, timeout_seconds: float = 30, retry_interval_seconds: float = 0.2
    ) -> None:
        deadline = monotonic() + timeout_seconds
        while True:
            try:
                async with self.engine.connect():
                    return
            except (OSError, SQLAlchemyError) as error:
                if monotonic() >= deadline:
                    message = f"Database did not become ready within {timeout_seconds} seconds"
                    raise TimeoutError(message) from error
                await asyncio.sleep(retry_interval_seconds)


def build_database(database_url: str) -> Database:
    engine = build_engine(database_url)
    return Database(engine=engine, session_factory=_session_factory(engine))


def build_session_factory(database_url: str) -> async_sessionmaker[AsyncSession]:
    return _session_factory(build_engine(database_url))
