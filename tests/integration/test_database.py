import asyncio
from datetime import UTC, datetime
from io import StringIO
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from alembic.config import Config
from sqlalchemy import delete, select, text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agent_hub.api.routers.admin import PersistentAdminResourceService
from agent_hub.app import ensure_bootstrap_tenant
from agent_hub.db.models import AdminResourceRow, ConfigRevisionRow, TenantRow, UserRow
from agent_hub.db.session import build_database
from alembic import command


@pytest.mark.integration
async def test_tenant_can_be_persisted_and_loaded_by_slug(db_session: AsyncSession) -> None:
    session = db_session
    tenant = TenantRow(slug="default", name="Default")
    session.add(tenant)
    await session.commit()

    stored_tenant = await session.scalar(select(TenantRow).where(TenantRow.slug == "default"))

    assert stored_tenant is not None
    assert stored_tenant.name == "Default"


@pytest.mark.integration
async def test_database_readiness_confirms_postgres(database_url: str) -> None:
    database = build_database(database_url)
    try:
        await database.wait_until_ready(timeout_seconds=5)
    finally:
        await database.dispose()


@pytest.mark.integration
async def test_bootstrap_tenant_creation_is_idempotent(
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id = uuid4()

    await ensure_bootstrap_tenant(
        auth_session_factory, tenant_id, "bootstrap-default", "Bootstrap Default"
    )
    await ensure_bootstrap_tenant(
        auth_session_factory, tenant_id, "bootstrap-default", "Bootstrap Default"
    )

    async with auth_session_factory() as session:
        tenants = list(
            await session.scalars(select(TenantRow).where(TenantRow.id == tenant_id))
        )
    assert len(tenants) == 1
    assert tenants[0].slug == "bootstrap-default"


@pytest.mark.integration
def test_migrations_downgrade_to_base_and_upgrade_to_head(alembic_config: Config) -> None:
    command.downgrade(alembic_config, "base")
    command.upgrade(alembic_config, "head")


@pytest.mark.integration
async def test_migrated_admin_resource_constraint_allows_plugin_signing_keys(
    db_session: AsyncSession,
) -> None:
    tenant = TenantRow(slug=f"plugin-signing-key-{uuid4()}", name="Plugin signing key")
    db_session.add(tenant)
    await db_session.flush()
    db_session.add(
        AdminResourceRow(
            tenant_id=tenant.id,
            kind="plugin_signing_key",
            resource_id="calendar-prod",
            payload={
                "key_id": "calendar-prod",
                "algorithm": "ed25519",
                "public_key": "test-public-key",
                "trusted": True,
            },
        )
    )

    await db_session.commit()


@pytest.mark.integration
async def test_migrated_admin_resource_constraint_allows_capability_installs(
    db_session: AsyncSession,
) -> None:
    tenant = TenantRow(slug=f"capability-install-{uuid4()}", name="Capability install")
    db_session.add(tenant)
    await db_session.flush()
    db_session.add(
        AdminResourceRow(
            tenant_id=tenant.id,
            kind="capability_install",
            resource_id="security_testing",
            payload={
                "entry_id": "security_testing",
                "plugin_id": "security-testing",
                "plan_id": "capability-plan-security_testing-test",
            },
        )
    )

    await db_session.commit()


@pytest.mark.integration
async def test_concurrent_hermes_review_keeps_candidate_and_memory_consistent(
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id = uuid4()
    actor_id = uuid4()
    insight_id = "hermes-concurrent-review"
    async with auth_session_factory() as session, session.begin():
        session.add(TenantRow(id=tenant_id, slug=f"hermes-{uuid4()}", name="Hermes review"))
        session.add(
            AdminResourceRow(
                tenant_id=tenant_id,
                kind="hermes",
                resource_id=insight_id,
                payload={
                    "id": insight_id,
                    "outcome": "success",
                    "lesson": "Keep concurrent review outcomes consistent.",
                    "tags": ["review"],
                    "weight": 8,
                    "owner_actor_id": str(actor_id),
                    "created_at": datetime.now(UTC).isoformat(),
                },
            )
        )

    def service() -> PersistentAdminResourceService:
        return PersistentAdminResourceService(
            config_service=cast(Any, object()),
            secret_service=cast(Any, object()),
            tenant_id=tenant_id,
            actor_id=actor_id,
            session_factory=auth_session_factory,
        )

    results = await asyncio.gather(
        service().confirm_hermes_insight(insight_id),
        service().reject_hermes_insight(insight_id),
        return_exceptions=True,
    )

    assert sum(not isinstance(result, Exception) for result in results) == 1
    assert sum(isinstance(result, ValueError) for result in results) == 1
    async with auth_session_factory() as session:
        rows = list(
            await session.scalars(
                select(AdminResourceRow)
                .where(AdminResourceRow.tenant_id == tenant_id)
                .where(AdminResourceRow.kind.in_(("hermes", "memory")))
            )
        )
    hermes = next(row for row in rows if row.kind == "hermes")
    memories = [row for row in rows if row.kind == "memory"]
    if hermes.payload.get("promoted_memory_id") is None:
        assert hermes.payload.get("rejected_at") is not None
        assert memories == []
    else:
        assert hermes.payload.get("confirmed_at") is not None
        assert [row.resource_id for row in memories] == [hermes.payload["promoted_memory_id"]]

    forget_insight_id = "hermes-concurrent-forget"
    forget_memory_id = "hermes-rule-hermes-concurrent-forget"
    async with auth_session_factory() as session, session.begin():
        session.add(
            AdminResourceRow(
                tenant_id=tenant_id,
                kind="hermes",
                resource_id=forget_insight_id,
                payload={
                    "id": forget_insight_id,
                    "outcome": "success",
                    "lesson": "Serialize promotion and memory revocation.",
                    "tags": ["review"],
                    "weight": 8,
                    "owner_actor_id": str(actor_id),
                    "created_at": datetime.now(UTC).isoformat(),
                },
            )
        )

    lock_key = f"agent-hub:{tenant_id}:hermes-memory:{forget_memory_id}"
    async with auth_session_factory() as blocker, blocker.begin():
        await blocker.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
            {"lock_key": lock_key},
        )
        confirm_task = asyncio.create_task(service().confirm_hermes_insight(forget_insight_id))
        forget_task = asyncio.create_task(service().forget_memory(forget_memory_id))
        await asyncio.sleep(0.1)
        assert confirm_task.done() is False
        assert forget_task.done() is False

    await asyncio.gather(confirm_task, forget_task, return_exceptions=True)

    async with auth_session_factory() as session:
        forget_hermes = await session.scalar(
            select(AdminResourceRow)
            .where(AdminResourceRow.tenant_id == tenant_id)
            .where(AdminResourceRow.kind == "hermes")
            .where(AdminResourceRow.resource_id == forget_insight_id)
        )
        forget_memory = await session.scalar(
            select(AdminResourceRow)
            .where(AdminResourceRow.tenant_id == tenant_id)
            .where(AdminResourceRow.kind == "memory")
            .where(AdminResourceRow.resource_id == forget_memory_id)
        )
    assert forget_hermes is not None
    if forget_hermes.payload.get("promotion_status") == "rejected" or forget_hermes.payload.get(
        "rejected_at"
    ):
        assert forget_hermes.payload.get("promoted_memory_id") is None
        assert forget_memory is None
    else:
        assert forget_hermes.payload.get("confirmed_at") is not None
        assert forget_hermes.payload.get("promoted_memory_id") == forget_memory_id
        assert forget_memory is not None


@pytest.mark.integration
def test_encrypted_secrets_migration_downgrades_and_reupgrades(
    alembic_config: Config, database_url: str
) -> None:
    command.downgrade(alembic_config, "0003_one_published")
    assert asyncio.run(_table_exists(database_url, "agent_hub_secrets")) is False

    command.upgrade(alembic_config, "0004_encrypted_secrets")
    assert asyncio.run(_table_exists(database_url, "agent_hub_secrets")) is True


@pytest.mark.integration
def test_encrypted_secrets_migration_generates_offline_sql(alembic_config: Config) -> None:
    output = StringIO()
    alembic_config.output_buffer = output

    command.upgrade(
        alembic_config,
        "0003_one_published:0004_encrypted_secrets",
        sql=True,
    )

    generated = output.getvalue()
    assert "CREATE TABLE agent_hub_secrets" in generated
    assert "uq_agent_hub_secrets_tenant_fingerprint" in generated


@pytest.mark.integration
def test_auth_migration_downgrades_reupgrades_and_generates_incremental_sql(
    alembic_config: Config, database_url: str
) -> None:
    command.downgrade(alembic_config, "0004_encrypted_secrets")
    assert asyncio.run(_table_exists(database_url, "agent_hub_bootstrap_codes")) is False

    output = StringIO()
    alembic_config.output_buffer = output
    command.upgrade(alembic_config, "0004_encrypted_secrets:0005_auth", sql=True)
    assert "CREATE TABLE agent_hub_bootstrap_codes" in output.getvalue()
    assert "ck_agent_hub_users_role" in output.getvalue()

    command.upgrade(alembic_config, "head")
    assert asyncio.run(_table_exists(database_url, "agent_hub_bootstrap_codes")) is True


@pytest.mark.integration
def test_username_check_migration_downgrades_reupgrades_and_generates_incremental_sql(
    alembic_config: Config,
) -> None:
    command.downgrade(alembic_config, "0005_auth")

    output = StringIO()
    alembic_config.output_buffer = output
    command.upgrade(alembic_config, "0005_auth:0006_username_check", sql=True)
    assert "ck_agent_hub_users_username" in output.getvalue()

    command.upgrade(alembic_config, "head")


@pytest.mark.integration
async def test_user_role_check_rejects_unknown_role(db_session: AsyncSession) -> None:
    tenant = TenantRow(slug="role-check", name="Role check")
    db_session.add(tenant)
    await db_session.flush()
    db_session.add(UserRow(tenant_id=tenant.id, username="bad-role", role="owner"))

    with pytest.raises(IntegrityError, match="ck_agent_hub_users_role"):
        await db_session.commit()


@pytest.mark.integration
@pytest.mark.parametrize(
    "username", ["Bad", "ab", "bad..name", "bad--name", "bad__name"]
)
async def test_username_check_rejects_noncanonical_values(
    db_session: AsyncSession, username: str
) -> None:
    tenant = TenantRow(slug=f"username-check-{uuid4()}", name="Username check")
    db_session.add(tenant)
    await db_session.flush()
    db_session.add(UserRow(tenant_id=tenant.id, username=username, role="viewer"))

    with pytest.raises(IntegrityError, match="ck_agent_hub_users_username"):
        await db_session.commit()


@pytest.mark.integration
def test_auth_migration_rejects_preexisting_invalid_roles(
    alembic_config: Config, database_url: str
) -> None:
    command.downgrade(alembic_config, "0004_encrypted_secrets")
    tenant_id = asyncio.run(_seed_invalid_role(database_url))
    try:
        with pytest.raises(SQLAlchemyError, match="invalid roles"):
            command.upgrade(alembic_config, "0005_auth")
    finally:
        asyncio.run(_delete_user_and_tenant(database_url, tenant_id))
        command.upgrade(alembic_config, "head")


@pytest.mark.integration
def test_username_migration_rejects_preexisting_invalid_values(
    alembic_config: Config, database_url: str
) -> None:
    command.downgrade(alembic_config, "0005_auth")
    tenant_id = asyncio.run(_seed_invalid_username(database_url))
    try:
        with pytest.raises(SQLAlchemyError, match="invalid usernames"):
            command.upgrade(alembic_config, "0006_username_check")
    finally:
        asyncio.run(_delete_user_and_tenant(database_url, tenant_id))
        command.upgrade(alembic_config, "head")


@pytest.mark.integration
def test_published_unique_index_migration_rejects_corrupt_history(
    alembic_config: Config, database_url: str
) -> None:
    command.downgrade(alembic_config, "0002_config_status_check")
    tenant_id = asyncio.run(_seed_duplicate_published(database_url))
    try:
        with pytest.raises(RuntimeError, match="multiple published revisions"):
            command.upgrade(alembic_config, "head")
    finally:
        asyncio.run(_delete_tenant(database_url, tenant_id))
        command.upgrade(alembic_config, "head")


async def _seed_duplicate_published(database_url: str) -> UUID:
    database = build_database(database_url)
    tenant_id = uuid4()
    try:
        async with database.session_factory() as session:
            session.add(TenantRow(id=tenant_id, slug=f"corrupt-{tenant_id}", name="Corrupt"))
            await session.commit()
            session.add_all(
                [
                    ConfigRevisionRow(
                        tenant_id=tenant_id,
                        version=version,
                        status="published",
                        document={"models": {}, "agents": []},
                        created_by=uuid4(),
                    )
                    for version in (1, 2)
                ]
            )
            await session.commit()
    finally:
        await database.dispose()
    return tenant_id


async def _delete_tenant(database_url: str, tenant_id: UUID) -> None:
    database = build_database(database_url)
    try:
        async with database.session_factory() as session:
            await session.execute(
                delete(ConfigRevisionRow).where(ConfigRevisionRow.tenant_id == tenant_id)
            )
            await session.execute(delete(TenantRow).where(TenantRow.id == tenant_id))
            await session.commit()
    finally:
        await database.dispose()


async def _seed_invalid_role(database_url: str) -> UUID:
    database = build_database(database_url)
    tenant_id = uuid4()
    user_id = uuid4()
    try:
        async with database.session_factory() as session:
            await session.execute(
                text(
                    """
                    INSERT INTO agent_hub_tenants (id, slug, name)
                    VALUES (:tenant_id, :slug, :name)
                    """
                ),
                {
                    "tenant_id": tenant_id,
                    "slug": f"invalid-role-{tenant_id}",
                    "name": "Invalid",
                },
            )
            await session.execute(
                text(
                    """
                    INSERT INTO agent_hub_users
                        (id, tenant_id, username, password_hash, feishu_open_id, role)
                    VALUES
                        (:user_id, :tenant_id, :username, NULL, NULL, :role)
                    """
                ),
                {
                    "user_id": user_id,
                    "tenant_id": tenant_id,
                    "username": "invalid",
                    "role": "owner",
                },
            )
            await session.commit()
    finally:
        await database.dispose()
    return tenant_id


async def _seed_invalid_username(database_url: str) -> UUID:
    database = build_database(database_url)
    tenant_id = uuid4()
    user_id = uuid4()
    try:
        async with database.session_factory() as session:
            await session.execute(
                text(
                    """
                    INSERT INTO agent_hub_tenants (id, slug, name)
                    VALUES (:tenant_id, :slug, :name)
                    """
                ),
                {
                    "tenant_id": tenant_id,
                    "slug": f"invalid-username-{tenant_id}",
                    "name": "Invalid",
                },
            )
            await session.execute(
                text(
                    """
                    INSERT INTO agent_hub_users
                        (id, tenant_id, username, password_hash, feishu_open_id, role)
                    VALUES
                        (:user_id, :tenant_id, :username, NULL, NULL, :role)
                    """
                ),
                {
                    "user_id": user_id,
                    "tenant_id": tenant_id,
                    "username": "Invalid..Name",
                    "role": "viewer",
                },
            )
            await session.commit()
    finally:
        await database.dispose()
    return tenant_id


async def _delete_user_and_tenant(database_url: str, tenant_id: UUID) -> None:
    database = build_database(database_url)
    try:
        async with database.session_factory() as session, session.begin():
            await session.execute(delete(UserRow).where(UserRow.tenant_id == tenant_id))
            await session.execute(delete(TenantRow).where(TenantRow.id == tenant_id))
    finally:
        await database.dispose()


async def _table_exists(database_url: str, table_name: str) -> bool:
    database = build_database(database_url)
    try:
        async with database.session_factory() as session:
            relation = await session.scalar(
                text("SELECT to_regclass(:table_name)"),
                {"table_name": table_name},
            )
            return relation is not None
    finally:
        await database.dispose()
