from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from agent_hub.auth.service import _try_hash_canonical_bootstrap_code
from agent_hub.db.models import BootstrapCodeRow, TenantRow, UserRow
from agent_hub.db.session import build_database


def hash_setup_code(code: str) -> str:
    return hashlib.sha256(code.encode("utf-8")).hexdigest()


def create_bootstrap_record(code: str, *, minutes: int = 15) -> dict[str, str]:
    expires_at = datetime.now(UTC) + timedelta(minutes=minutes)
    return {
        "code_hash": hash_setup_code(code),
        "expires_at": expires_at.isoformat(),
        "status": "open",
    }


async def seed_bootstrap_code(
    *,
    database_url: str,
    code: str,
    minutes: int,
    tenant_id: UUID,
    tenant_slug: str,
    tenant_name: str,
    database_factory: Callable[[str], Any] = build_database,
) -> None:
    code_hash = _try_hash_canonical_bootstrap_code(code)
    if code_hash is None:
        raise ValueError("setup code must be a canonical 32-byte base64url value")
    expires_at = datetime.now(UTC) + timedelta(minutes=minutes)
    database = database_factory(database_url)
    try:
        async with database.session_factory() as session, session.begin():
            if await _has_super_admin(session):
                return
            tenant_statement = (
                insert(TenantRow)
                .values(id=tenant_id, slug=tenant_slug, name=tenant_name)
                .on_conflict_do_update(
                    index_elements=[TenantRow.id],
                    set_={"slug": tenant_slug, "name": tenant_name},
                )
            )
            await session.execute(tenant_statement)
            await session.execute(
                _bootstrap_code_insert_statement(
                    code_hash=code_hash,
                    tenant_id=tenant_id,
                    expires_at=expires_at,
                )
            )
    finally:
        await database.dispose()


async def _has_super_admin(session: Any) -> bool:
    return (
        await session.scalar(
            select(UserRow.id).where(UserRow.role == "super_admin").limit(1)
        )
    ) is not None


def _bootstrap_code_insert_statement(
    *,
    code_hash: str,
    tenant_id: UUID,
    expires_at: datetime,
) -> Any:
    return (
        insert(BootstrapCodeRow)
        .values(
            id=uuid4(),
            code_hash=code_hash,
            tenant_id=tenant_id,
            expires_at=expires_at,
            consumed_at=None,
        )
        .on_conflict_do_update(
            constraint="uq_agent_hub_bootstrap_codes_code_hash",
            set_={"expires_at": expires_at},
            where=BootstrapCodeRow.consumed_at.is_(None),
        )
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Create a one-time Agent Hub setup code record.")
    parser.add_argument("--code")
    parser.add_argument("--code-env")
    parser.add_argument("--output")
    parser.add_argument("--database-url")
    parser.add_argument("--database-url-env")
    parser.add_argument("--minutes", type=int, default=15)
    parser.add_argument(
        "--tenant-id",
        default=os.environ.get(
            "AGENT_HUB_BOOTSTRAP_TENANT_ID",
            "00000000-0000-4000-8000-000000000001",
        ),
    )
    parser.add_argument(
        "--tenant-slug",
        default=os.environ.get("AGENT_HUB_BOOTSTRAP_TENANT_SLUG", "default"),
    )
    parser.add_argument(
        "--tenant-name",
        default=os.environ.get("AGENT_HUB_BOOTSTRAP_TENANT_NAME", "Default"),
    )
    args = parser.parse_args()
    code = args.code
    if code is None and args.code_env:
        code = os.environ.get(args.code_env)
    if not code:
        parser.error("--code or --code-env is required")

    database_url = args.database_url
    if database_url is None and args.database_url_env:
        database_url = os.environ.get(args.database_url_env)

    if database_url:
        asyncio.run(
            seed_bootstrap_code(
                database_url=database_url,
                code=code,
                minutes=args.minutes,
                tenant_id=UUID(args.tenant_id),
                tenant_slug=args.tenant_slug,
                tenant_name=args.tenant_name,
            )
        )
        print("bootstrap record seeded")
        return

    if args.output is None:
        parser.error("--output is required when --database-url is not set")
    record = create_bootstrap_record(code, minutes=args.minutes)
    target = Path(args.output)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(record, sort_keys=True), encoding="utf-8")
    print("bootstrap record written")


if __name__ == "__main__":
    main()
