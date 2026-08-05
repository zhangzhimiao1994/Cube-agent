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
    return create_async_engine(
        database_url,
        pool_pre_ping=True,
        hide_parameters=True,
    )


def _session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


@dataclass(slots=True)
class Database:
    """Application-lifespan database resource; dispose it during application shutdown."""

    engine: AsyncEngine
    session_factory: async_sessionmaker[AsyncSession]

    async def dispose(self) -> None:
        await self.engine.dispose()

    async def wait_until_ready(
        self, timeout_seconds: float = 30, retry_interval_seconds: float = 0.2
    ) -> None:
        deadline = monotonic() + timeout_seconds
        while True:
            remaining_seconds = deadline - monotonic()
            if remaining_seconds <= 0:
                raise TimeoutError(f"Database did not become ready within {timeout_seconds} seconds")
            try:
                async with asyncio.timeout(remaining_seconds):
                    async with self.engine.connect():
                        return
            except TimeoutError as error:
                message = f"Database did not become ready within {timeout_seconds} seconds"
                raise TimeoutError(message) from error
            except (OSError, SQLAlchemyError) as error:
                remaining_seconds = deadline - monotonic()
                if remaining_seconds <= 0:
                    message = f"Database did not become ready within {timeout_seconds} seconds"
                    raise TimeoutError(message) from error
                await asyncio.sleep(min(retry_interval_seconds, remaining_seconds))


def build_database(database_url: str) -> Database:
    engine = build_engine(database_url)
    return Database(engine=engine, session_factory=_session_factory(engine))


def build_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return _session_factory(engine)
