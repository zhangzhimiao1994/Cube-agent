import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from argon2 import PasswordHasher
from argon2.low_level import Type
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agent_hub.auth.models import (
    BootstrapClosed,
    InvalidBootstrapCode,
    InvalidCredentials,
    Role,
)
from agent_hub.auth.passwords import PasswordService
from agent_hub.auth.service import AuthService
from agent_hub.auth.tokens import AccessTokenService
from agent_hub.db.models import BootstrapCodeRow, TenantRow, UserRow

NOW = datetime(2026, 8, 5, 0, 0, tzinfo=UTC)


class MutableClock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now


class SpyPasswordService(PasswordService):
    def __init__(self) -> None:
        super().__init__()
        self.verified_hashes: list[str] = []

    def verify(self, password_hash: str, password: str) -> bool:
        self.verified_hashes.append(password_hash)
        return super().verify(password_hash, password)


async def _tenant(factory: async_sessionmaker[AsyncSession], *, slug: str = "bootstrap") -> UUID:
    tenant_id = uuid4()
    async with factory() as session, session.begin():
        session.add(TenantRow(id=tenant_id, slug=f"{slug}-{tenant_id}", name="Bootstrap"))
    return tenant_id


def _service(
    factory: async_sessionmaker[AsyncSession],
    tenant_id: UUID,
    *,
    clock: MutableClock | None = None,
    passwords: PasswordService | None = None,
) -> AuthService:
    effective_clock = clock or MutableClock()
    return AuthService(
        factory,
        tenant_id,
        passwords or PasswordService(),
        AccessTokenService(b"t" * 32, clock=effective_clock),
        clock=effective_clock,
    )


@pytest.mark.integration
async def test_bootstrap_code_is_random_and_only_its_hash_is_stored(
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id = await _tenant(auth_session_factory)
    service = _service(auth_session_factory, tenant_id)

    code = await service.issue_bootstrap_code()

    assert len(code) == 43
    assert "=" not in code
    async with auth_session_factory() as session:
        row = await session.scalar(select(BootstrapCodeRow))
        assert row is not None
        assert len(row.code_hash) == 64
        assert code not in row.code_hash
        assert row.tenant_id == tenant_id


@pytest.mark.integration
@pytest.mark.parametrize("ttl_seconds", [0, 59, 3601, True])
async def test_bootstrap_ttl_is_bounded_before_any_row_is_written(
    auth_session_factory: async_sessionmaker[AsyncSession], ttl_seconds: object
) -> None:
    tenant_id = await _tenant(auth_session_factory)
    service = _service(auth_session_factory, tenant_id)

    with pytest.raises(ValueError, match="TTL"):
        await service.issue_bootstrap_code(ttl_seconds=ttl_seconds)  # type: ignore[arg-type]

    async with auth_session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(BootstrapCodeRow)) == 0


@pytest.mark.integration
async def test_bootstrap_creates_one_super_admin_and_returns_a_safe_result(
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id = await _tenant(auth_session_factory)
    service = _service(auth_session_factory, tenant_id)
    code = await service.issue_bootstrap_code()
    password = "a very strong local password"

    result = await service.consume_bootstrap_code(code, "first-admin", password)

    assert result.principal.role is Role.SUPER_ADMIN
    assert result.principal.tenant_id == tenant_id
    assert result.access_token not in repr(result)
    assert password not in repr(result)
    async with auth_session_factory() as session:
        user = await session.scalar(select(UserRow))
        assert user is not None
        assert user.username == "first-admin"
        assert user.password_hash is not None
        assert user.password_hash.startswith("$argon2id$")
        assert password not in user.password_hash
        assert await session.scalar(select(func.count()).select_from(UserRow)) == 1


@pytest.mark.integration
async def test_invalid_expired_and_consumed_codes_never_create_another_user(
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id = await _tenant(auth_session_factory)
    clock = MutableClock()
    service = _service(auth_session_factory, tenant_id, clock=clock)

    with pytest.raises(InvalidBootstrapCode, match="invalid bootstrap code"):
        await service.consume_bootstrap_code("not-a-code", "first-admin", "valid password 123")

    expired = await service.issue_bootstrap_code(ttl_seconds=60)
    clock.now += timedelta(seconds=61)
    with pytest.raises(InvalidBootstrapCode, match="invalid bootstrap code"):
        await service.consume_bootstrap_code(expired, "first-admin", "valid password 123")

    clock.now = NOW
    valid = await service.issue_bootstrap_code()
    await service.consume_bootstrap_code(valid, "first-admin", "valid password 123")
    with pytest.raises(InvalidBootstrapCode, match="invalid bootstrap code"):
        await service.consume_bootstrap_code(valid, "second-admin", "valid password 456")

    async with auth_session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(UserRow)) == 1


@pytest.mark.integration
async def test_bootstrap_is_globally_closed_after_any_super_admin_exists(
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id = await _tenant(auth_session_factory)
    service = _service(auth_session_factory, tenant_id)
    code = await service.issue_bootstrap_code()
    await service.consume_bootstrap_code(code, "first-admin", "valid password 123")

    other_tenant = await _tenant(auth_session_factory, slug="other")
    with pytest.raises(BootstrapClosed, match="bootstrap is closed"):
        await _service(auth_session_factory, other_tenant).issue_bootstrap_code()


@pytest.mark.integration
@pytest.mark.parametrize("same_code", [True, False])
async def test_concurrent_consumers_can_create_only_one_global_super_admin(
    auth_session_factory: async_sessionmaker[AsyncSession], same_code: bool
) -> None:
    tenant_id = await _tenant(auth_session_factory)
    service = _service(auth_session_factory, tenant_id)
    first = await service.issue_bootstrap_code()
    second = first if same_code else await service.issue_bootstrap_code()

    outcomes = await asyncio.gather(
        service.consume_bootstrap_code(first, "admin-one", "valid password 123"),
        service.consume_bootstrap_code(second, "admin-two", "valid password 456"),
        return_exceptions=True,
    )

    assert sum(not isinstance(item, Exception) for item in outcomes) == 1
    assert sum(isinstance(item, InvalidBootstrapCode) for item in outcomes) == 1
    async with auth_session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(UserRow)) == 1
        assert (
            await session.scalar(
                select(func.count()).select_from(UserRow).where(UserRow.role == "super_admin")
            )
            == 1
        )


@pytest.mark.integration
async def test_login_is_tenant_scoped_and_returns_token_principal(
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id = await _tenant(auth_session_factory)
    service = _service(auth_session_factory, tenant_id)
    code = await service.issue_bootstrap_code()
    created = await service.consume_bootstrap_code(code, "first-admin", "valid password 123")

    logged_in = await service.login(tenant_id, "first-admin", "valid password 123")
    authenticated = service.authenticate_token(logged_in.access_token)

    assert logged_in.principal == created.principal
    assert authenticated == logged_in.principal
    with pytest.raises(InvalidCredentials, match="invalid credentials"):
        await service.login(uuid4(), "first-admin", "valid password 123")


@pytest.mark.integration
async def test_wrong_and_unknown_login_are_identical_and_both_verify_argon2(
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id = await _tenant(auth_session_factory)
    passwords = SpyPasswordService()
    service = _service(auth_session_factory, tenant_id, passwords=passwords)
    code = await service.issue_bootstrap_code()
    await service.consume_bootstrap_code(code, "first-admin", "valid password 123")
    passwords.verified_hashes.clear()

    failures = []
    for username in ("first-admin", "missing-user"):
        try:
            await service.login(tenant_id, username, "wrong password 123")
        except InvalidCredentials as error:
            failures.append(error)

    assert [str(error) for error in failures] == ["invalid credentials"] * 2
    assert len(passwords.verified_hashes) == 2
    assert all(value.startswith("$argon2id$") for value in passwords.verified_hashes)


@pytest.mark.integration
async def test_login_with_no_local_password_uses_dummy_verify_and_fails_closed(
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id = await _tenant(auth_session_factory)
    passwords = SpyPasswordService()
    async with auth_session_factory() as session, session.begin():
        session.add(
            UserRow(
                tenant_id=tenant_id,
                username="oauth-user",
                password_hash=None,
                role=Role.VIEWER.value,
            )
        )
    service = _service(auth_session_factory, tenant_id, passwords=passwords)
    passwords.verified_hashes.clear()

    with pytest.raises(InvalidCredentials, match="invalid credentials"):
        await service.login(tenant_id, "oauth-user", "valid password 123")

    assert len(passwords.verified_hashes) == 1
    assert passwords.verified_hashes[0].startswith("$argon2id$")


@pytest.mark.integration
async def test_successful_login_upgrades_an_old_argon2_hash(
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id = await _tenant(auth_session_factory)
    old_passwords = PasswordService(
        PasswordHasher(time_cost=1, memory_cost=8192, parallelism=1, type=Type.ID)
    )
    bootstrap = _service(auth_session_factory, tenant_id, passwords=old_passwords)
    code = await bootstrap.issue_bootstrap_code()
    password = "valid password 123"
    await bootstrap.consume_bootstrap_code(code, "first-admin", password)
    async with auth_session_factory() as session:
        old_hash = await session.scalar(select(UserRow.password_hash))
    assert old_hash is not None

    current_passwords = PasswordService()
    await _service(auth_session_factory, tenant_id, passwords=current_passwords).login(
        tenant_id, "first-admin", password
    )

    async with auth_session_factory() as session:
        new_hash = await session.scalar(select(UserRow.password_hash))
    assert new_hash is not None
    assert new_hash != old_hash
    assert current_passwords.needs_rehash(new_hash) is False


@pytest.mark.integration
@pytest.mark.parametrize(
    "username", ["ab", "a" * 65, " Admin", "admin ", "Admin", "admin@example.com", "admin..one"]
)
async def test_bootstrap_rejects_noncanonical_username_without_creating_user(
    auth_session_factory: async_sessionmaker[AsyncSession], username: str
) -> None:
    tenant_id = await _tenant(auth_session_factory)
    service = _service(auth_session_factory, tenant_id)
    code = await service.issue_bootstrap_code()

    with pytest.raises(ValueError, match="username"):
        await service.consume_bootstrap_code(code, username, "valid password 123")

    async with auth_session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(UserRow)) == 0
