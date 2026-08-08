from __future__ import annotations

import base64
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Self
from uuid import UUID

import pytest
from sqlalchemy.dialects import postgresql

from agent_hub.cli import bootstrap
from agent_hub.db.models import BootstrapCodeRow

SETUP_CODE = base64.urlsafe_b64encode(b"x" * 32).decode("ascii").rstrip("=")
TENANT_ID = UUID("00000000-0000-4000-8000-000000000001")


@dataclass(slots=True)
class FakeSession:
    has_super_admin: bool
    executed: list[object] = field(default_factory=list)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    def begin(self) -> FakeSession:
        return self

    async def scalar(self, statement: object) -> int | None:
        self.executed.append(statement)
        return 1 if self.has_super_admin else None

    async def execute(self, statement: object) -> None:
        self.executed.append(statement)


@dataclass(slots=True)
class FakeDatabase:
    session: FakeSession
    disposed: bool = False

    def session_factory(self) -> FakeSession:
        return self.session

    async def dispose(self) -> None:
        self.disposed = True


@pytest.mark.asyncio
async def test_seed_bootstrap_code_skips_when_super_admin_exists() -> None:
    session = FakeSession(has_super_admin=True)
    database = FakeDatabase(session=session)

    await bootstrap.seed_bootstrap_code(
        database_url="postgresql+asyncpg://example",
        code=SETUP_CODE,
        minutes=60,
        tenant_id=TENANT_ID,
        tenant_slug="default",
        tenant_name="Default",
        database_factory=lambda _: database,
    )

    assert database.disposed is True
    assert len(session.executed) == 1


def test_bootstrap_code_upsert_does_not_resurrect_consumed_codes() -> None:
    assert hasattr(bootstrap, "_bootstrap_code_insert_statement")
    statement = bootstrap._bootstrap_code_insert_statement(
        code_hash="a" * 64,
        tenant_id=TENANT_ID,
        expires_at=datetime(2026, 8, 8, tzinfo=UTC),
    )

    compiled = str(statement.compile(dialect=postgresql.dialect())).lower()  # type: ignore[no-untyped-call]
    conflict_clause = compiled.split("do update set", maxsplit=1)[1]

    assert "consumed_at =" not in conflict_clause
    assert "where agent_hub_bootstrap_codes.consumed_at is null" in compiled
    assert BootstrapCodeRow.consumed_at.key == "consumed_at"
