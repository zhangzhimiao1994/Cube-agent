import asyncio
import base64
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from argon2 import PasswordHasher
from argon2.exceptions import HashingError
from argon2.low_level import Type
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agent_hub.auth.models import (
    AuthenticationOperationError,
    AuthenticationPersistenceError,
    AuthResult,
    BootstrapClosed,
    InvalidBootstrapCode,
    InvalidCredentials,
    Role,
)
from agent_hub.auth.passwords import PasswordService
from agent_hub.auth.service import AuthService
from agent_hub.auth.tokens import AccessTokenService, InvalidTokenError, TokenBackendError
from agent_hub.db.models import BootstrapCodeRow, TenantRow, UserRow

NOW = datetime(2026, 8, 5, 0, 0, tzinfo=UTC)


class MutableClock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now


class SpyPasswordService(PasswordService):
    def __init__(self, hasher: PasswordHasher | None = None) -> None:
        super().__init__(hasher)
        self.verified_hashes: list[str] = []

    async def verify_async(self, password_hash: str, password: str) -> bool:
        self.verified_hashes.append(password_hash)
        return await super().verify_async(password_hash, password)


class CountingPasswordService(PasswordService):
    def __init__(self) -> None:
        super().__init__()
        self.hash_calls = 0

    def hash(self, password: str) -> str:
        self.hash_calls += 1
        return super().hash(password)

    async def hash_async(self, password: str) -> str:
        self.hash_calls += 1
        return await super().hash_async(password)


class AsyncOnlyPasswordService(PasswordService):
    def hash(self, password: str) -> str:
        raise AssertionError("AuthService must not call synchronous Argon2 hash")

    def verify(self, password_hash: str, password: str) -> bool:
        raise AssertionError("AuthService must not call synchronous Argon2 verify")


class PausingRehashPasswordService(PasswordService):
    def __init__(self) -> None:
        super().__init__()
        self.rehash_started = asyncio.Event()
        self.allow_rehash = asyncio.Event()

    async def hash_async(self, password: str) -> str:
        self.rehash_started.set()
        await self.allow_rehash.wait()
        return await super().hash_async(password)


class FailingTokenService(AccessTokenService):
    def encode(
        self,
        user_id: UUID,
        tenant_id: UUID,
        role: Role,
        *,
        ttl_seconds: int = 900,
    ) -> str:
        raise TokenBackendError("forced token failure")


class UnknownRuntimeTokenService(AccessTokenService):
    def encode(
        self,
        user_id: UUID,
        tenant_id: UUID,
        role: Role,
        *,
        ttl_seconds: int = 900,
    ) -> str:
        raise RuntimeError("forced unknown token failure")


class CancellableHashPasswordService(PasswordService):
    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()

    async def hash_async(self, password: str) -> str:
        self.started.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


class CancellableVerifyPasswordService(PasswordService):
    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()

    async def verify_async(self, password_hash: str, password: str) -> bool:
        self.started.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


class RuntimeVerifyPasswordService(PasswordService):
    async def verify_async(self, password_hash: str, password: str) -> bool:
        raise RuntimeError("forced password backend failure")


class HashingFailureHasher(PasswordHasher):
    def __init__(self) -> None:
        super().__init__(time_cost=3, memory_cost=65_536, parallelism=4, type=Type.ID)

    def hash(self, password: str | bytes, *, salt: bytes | None = None) -> str:
        raise HashingError("forced hashing failure")


def _assert_production_frames_do_not_contain(
    error: BaseException, *secrets: object
) -> None:
    rendered = [repr(secret) for secret in secrets]
    traceback = error.__traceback__
    while traceback is not None:
        filename = traceback.tb_frame.f_code.co_filename.replace("\\", "/")
        if "/src/agent_hub/" in filename:
            for value in traceback.tb_frame.f_locals.values():
                rendered_value = repr(value)
                assert all(secret not in rendered_value for secret in rendered)
        traceback = traceback.tb_next
    assert error.__cause__ is None
    assert error.__context__ is None


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
async def test_rejected_bootstrap_codes_do_not_run_argon2(
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id = await _tenant(auth_session_factory)
    clock = MutableClock()
    passwords = CountingPasswordService()
    service = _service(auth_session_factory, tenant_id, clock=clock, passwords=passwords)

    with pytest.raises(InvalidBootstrapCode):
        await service.consume_bootstrap_code(
            "not-a-code", "first-admin", "valid password 123"
        )
    assert passwords.hash_calls == 0

    expired = await service.issue_bootstrap_code(ttl_seconds=60)
    clock.now += timedelta(seconds=61)
    with pytest.raises(InvalidBootstrapCode):
        await service.consume_bootstrap_code(
            expired, "first-admin", "valid password 123"
        )
    assert passwords.hash_calls == 0

    clock.now = NOW
    valid = await service.issue_bootstrap_code()
    await service.consume_bootstrap_code(valid, "first-admin", "valid password 123")
    assert passwords.hash_calls == 1
    passwords.hash_calls = 0
    with pytest.raises(InvalidBootstrapCode):
        await service.consume_bootstrap_code(valid, "second-admin", "valid password 456")
    assert passwords.hash_calls == 0


@pytest.mark.integration
async def test_auth_service_uses_only_async_password_operations(
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id = await _tenant(auth_session_factory)
    service = _service(
        auth_session_factory, tenant_id, passwords=AsyncOnlyPasswordService()
    )
    code = await service.issue_bootstrap_code()

    await service.consume_bootstrap_code(code, "first-admin", "valid password 123")
    result = await service.login(tenant_id, "first-admin", "valid password 123")

    assert result.principal.role is Role.SUPER_ADMIN


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
async def test_separate_services_serialize_issue_against_consume(
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id = await _tenant(auth_session_factory)
    issuer = _service(auth_session_factory, tenant_id)
    consumer = _service(auth_session_factory, tenant_id)
    initial_code = await issuer.issue_bootstrap_code()
    start = asyncio.Event()

    async def issue() -> object:
        await start.wait()
        try:
            return await issuer.issue_bootstrap_code()
        except BootstrapClosed as error:
            return error

    async def consume() -> object:
        await start.wait()
        return await consumer.consume_bootstrap_code(
            initial_code, "first-admin", "valid password 123"
        )

    issue_task = asyncio.create_task(issue())
    consume_task = asyncio.create_task(consume())
    start.set()
    issued, consumed = await asyncio.gather(issue_task, consume_task)

    assert isinstance(consumed, AuthResult)
    assert isinstance(issued, (str, BootstrapClosed))
    async with auth_session_factory() as session:
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
async def test_auth_failures_do_not_retain_credentials_or_tokens_in_tracebacks(
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id = await _tenant(auth_session_factory)
    service = _service(auth_session_factory, tenant_id)
    code = "invalid-bootstrap-code"
    password = "sensitive password 123"

    with pytest.raises(InvalidBootstrapCode) as bootstrap_error:
        await service.consume_bootstrap_code(code, "first-admin", password)
    _assert_production_frames_do_not_contain(
        bootstrap_error.value, code, password
    )

    with pytest.raises(InvalidCredentials) as login_error:
        await service.login(tenant_id, "missing-user", password)
    _assert_production_frames_do_not_contain(login_error.value, password)

    token = "not.a.sensitive-token"
    with pytest.raises(InvalidTokenError) as token_error:
        service.authenticate_token(token)
    _assert_production_frames_do_not_contain(token_error.value, token)


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
    passwords = SpyPasswordService(
        PasswordHasher(time_cost=1, memory_cost=8192, parallelism=1, type=Type.ID)
    )
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
    assert passwords.verified_hashes[0] == passwords.dummy_hash
    assert "$m=8192,t=1,p=1$" in passwords.dummy_hash
    assert passwords.needs_rehash(passwords.dummy_hash) is False


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
async def test_rehash_cas_never_overwrites_a_concurrent_password_change(
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id = await _tenant(auth_session_factory)
    old_passwords = PasswordService(
        PasswordHasher(time_cost=1, memory_cost=8192, parallelism=1, type=Type.ID)
    )
    bootstrap = _service(auth_session_factory, tenant_id, passwords=old_passwords)
    code = await bootstrap.issue_bootstrap_code()
    old_password = "old valid password 123"
    new_password = "new valid password 456"
    await bootstrap.consume_bootstrap_code(code, "first-admin", old_password)

    pausing = PausingRehashPasswordService()
    login_task = asyncio.create_task(
        _service(auth_session_factory, tenant_id, passwords=pausing).login(
            tenant_id, "first-admin", old_password
        )
    )
    await asyncio.wait_for(pausing.rehash_started.wait(), timeout=2)
    replacement_hash = await PasswordService().hash_async(new_password)
    async with auth_session_factory() as session, session.begin():
        user = await session.scalar(select(UserRow).where(UserRow.username == "first-admin"))
        assert user is not None
        user.password_hash = replacement_hash
    pausing.allow_rehash.set()

    with pytest.raises(InvalidCredentials):
        await login_task
    async with auth_session_factory() as session:
        stored_hash = await session.scalar(select(UserRow.password_hash))
    assert stored_hash == replacement_hash
    await _service(auth_session_factory, tenant_id).login(
        tenant_id, "first-admin", new_password
    )


@pytest.mark.integration
async def test_bootstrap_database_failure_rolls_back_and_hides_secrets(
    auth_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant_id = await _tenant(auth_session_factory)
    service = _service(auth_session_factory, uuid4())
    raw_code = b"z" * 32
    expected_code = base64.urlsafe_b64encode(raw_code).decode("ascii").rstrip("=")
    monkeypatch.setattr("agent_hub.auth.service.secrets.token_bytes", lambda _: raw_code)

    with pytest.raises(AuthenticationPersistenceError) as captured:
        await service.issue_bootstrap_code()

    _assert_production_frames_do_not_contain(captured.value, expected_code)
    async with auth_session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(BootstrapCodeRow)) == 0
        assert await session.scalar(select(func.count()).select_from(UserRow)) == 0

    valid_service = _service(auth_session_factory, tenant_id)
    code = await valid_service.issue_bootstrap_code()
    async with auth_session_factory() as session, session.begin():
        session.add(
            UserRow(
                tenant_id=tenant_id,
                username="duplicate",
                password_hash=None,
                role=Role.VIEWER.value,
            )
        )
    password = "sensitive password 123"
    with pytest.raises(AuthenticationPersistenceError) as consume_error:
        await valid_service.consume_bootstrap_code(code, "duplicate", password)
    _assert_production_frames_do_not_contain(
        consume_error.value, code, password
    )
    async with auth_session_factory() as session:
        stored_code = await session.scalar(
            select(BootstrapCodeRow).where(BootstrapCodeRow.code_hash.is_not(None))
        )
        assert stored_code is not None
        assert stored_code.consumed_at is None
        assert await session.scalar(select(func.count()).select_from(UserRow)) == 1


@pytest.mark.integration
async def test_token_creation_failure_rolls_back_bootstrap_without_secret_trace(
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id = await _tenant(auth_session_factory)
    issuer = _service(auth_session_factory, tenant_id)
    code = await issuer.issue_bootstrap_code()
    password = "sensitive password 123"
    failing = AuthService(
        auth_session_factory,
        tenant_id,
        PasswordService(),
        FailingTokenService(b"t" * 32, clock=MutableClock()),
        clock=MutableClock(),
    )

    with pytest.raises(AuthenticationOperationError) as captured:
        await failing.consume_bootstrap_code(code, "first-admin", password)

    _assert_production_frames_do_not_contain(captured.value, code, password)
    async with auth_session_factory() as session:
        stored_code = await session.scalar(select(BootstrapCodeRow))
        assert stored_code is not None
        assert stored_code.consumed_at is None
        assert await session.scalar(select(func.count()).select_from(UserRow)) == 0


@pytest.mark.integration
async def test_actual_hashing_error_rolls_back_bootstrap_without_secret_trace(
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id = await _tenant(auth_session_factory)
    issuer = _service(auth_session_factory, tenant_id)
    code = await issuer.issue_bootstrap_code()
    password = "sensitive password 123"
    failing = _service(
        auth_session_factory,
        tenant_id,
        passwords=PasswordService(HashingFailureHasher()),
    )

    with pytest.raises(AuthenticationOperationError) as captured:
        await failing.consume_bootstrap_code(code, "first-admin", password)

    _assert_production_frames_do_not_contain(captured.value, code, password)
    async with auth_session_factory() as session:
        stored_code = await session.scalar(select(BootstrapCodeRow))
        assert stored_code is not None
        assert stored_code.consumed_at is None
        assert await session.scalar(select(func.count()).select_from(UserRow)) == 0


@pytest.mark.integration
async def test_unknown_runtime_propagates_after_bootstrap_rollback_and_cleanup(
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id = await _tenant(auth_session_factory)
    issuer = _service(auth_session_factory, tenant_id)
    code = await issuer.issue_bootstrap_code()
    password = "sensitive password 123"
    failing = AuthService(
        auth_session_factory,
        tenant_id,
        PasswordService(),
        UnknownRuntimeTokenService(b"t" * 32, clock=MutableClock()),
        clock=MutableClock(),
    )

    with pytest.raises(RuntimeError, match="forced unknown token failure") as captured:
        await failing.consume_bootstrap_code(code, "first-admin", password)

    _assert_production_frames_do_not_contain(captured.value, code, password)
    async with auth_session_factory() as session:
        stored_code = await session.scalar(select(BootstrapCodeRow))
        assert stored_code is not None
        assert stored_code.consumed_at is None
        assert await session.scalar(select(func.count()).select_from(UserRow)) == 0


@pytest.mark.integration
async def test_cancelled_consume_rolls_back_and_sanitizes_credentials(
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id = await _tenant(auth_session_factory)
    issuer = _service(auth_session_factory, tenant_id)
    code = await issuer.issue_bootstrap_code()
    passwords = CancellableHashPasswordService()
    service = _service(auth_session_factory, tenant_id, passwords=passwords)
    password = "sensitive password 123"
    task = asyncio.create_task(
        service.consume_bootstrap_code(code, "first-admin", password)
    )
    await asyncio.wait_for(passwords.started.wait(), timeout=2)

    task.cancel()
    with pytest.raises(asyncio.CancelledError) as captured:
        await task

    _assert_production_frames_do_not_contain(captured.value, code, password)
    async with auth_session_factory() as session:
        stored_code = await session.scalar(select(BootstrapCodeRow))
        assert stored_code is not None
        assert stored_code.consumed_at is None
        assert await session.scalar(select(func.count()).select_from(UserRow)) == 0


@pytest.mark.integration
async def test_cancelled_login_rolls_back_and_sanitizes_password(
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id = await _tenant(auth_session_factory)
    issuer = _service(auth_session_factory, tenant_id)
    code = await issuer.issue_bootstrap_code()
    password = "sensitive password 123"
    await issuer.consume_bootstrap_code(code, "first-admin", password)
    async with auth_session_factory() as session:
        old_hash = await session.scalar(select(UserRow.password_hash))
    passwords = CancellableVerifyPasswordService()
    service = _service(auth_session_factory, tenant_id, passwords=passwords)
    task = asyncio.create_task(service.login(tenant_id, "first-admin", password))
    await asyncio.wait_for(passwords.started.wait(), timeout=2)

    task.cancel()
    with pytest.raises(asyncio.CancelledError) as captured:
        await task

    _assert_production_frames_do_not_contain(captured.value, password)
    async with auth_session_factory() as session:
        assert await session.scalar(select(UserRow.password_hash)) == old_hash


@pytest.mark.integration
async def test_cancelled_issue_after_code_generation_hides_code_and_writes_nothing(
    auth_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant_id = await _tenant(auth_session_factory)
    raw_code = b"c" * 32
    expected_code = base64.urlsafe_b64encode(raw_code).decode("ascii").rstrip("=")
    monkeypatch.setattr("agent_hub.auth.service.secrets.token_bytes", lambda _: raw_code)

    def cancel_clock() -> datetime:
        raise asyncio.CancelledError

    service = AuthService(
        auth_session_factory,
        tenant_id,
        PasswordService(),
        AccessTokenService(b"t" * 32, clock=MutableClock()),
        clock=cancel_clock,
    )
    with pytest.raises(asyncio.CancelledError) as captured:
        await service.issue_bootstrap_code()

    _assert_production_frames_do_not_contain(captured.value, expected_code)
    async with auth_session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(BootstrapCodeRow)) == 0


@pytest.mark.integration
async def test_runtime_token_failure_prevents_login_rehash_and_is_sanitized(
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id = await _tenant(auth_session_factory)
    old_passwords = PasswordService(
        PasswordHasher(time_cost=1, memory_cost=8192, parallelism=1, type=Type.ID)
    )
    issuer = _service(auth_session_factory, tenant_id, passwords=old_passwords)
    code = await issuer.issue_bootstrap_code()
    password = "sensitive password 123"
    await issuer.consume_bootstrap_code(code, "first-admin", password)
    async with auth_session_factory() as session:
        old_hash = await session.scalar(select(UserRow.password_hash))
    service = AuthService(
        auth_session_factory,
        tenant_id,
        PasswordService(),
        FailingTokenService(b"t" * 32, clock=MutableClock()),
        clock=MutableClock(),
    )

    with pytest.raises(AuthenticationOperationError) as captured:
        await service.login(tenant_id, "first-admin", password)

    _assert_production_frames_do_not_contain(captured.value, password)
    async with auth_session_factory() as session:
        assert await session.scalar(select(UserRow.password_hash)) == old_hash


@pytest.mark.integration
async def test_actual_hashing_error_rolls_back_login_rehash_without_secret_trace(
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id = await _tenant(auth_session_factory)
    old_passwords = PasswordService(
        PasswordHasher(time_cost=1, memory_cost=8192, parallelism=1, type=Type.ID)
    )
    issuer = _service(auth_session_factory, tenant_id, passwords=old_passwords)
    code = await issuer.issue_bootstrap_code()
    password = "sensitive password 123"
    await issuer.consume_bootstrap_code(code, "first-admin", password)
    async with auth_session_factory() as session:
        old_hash = await session.scalar(select(UserRow.password_hash))
    service = _service(
        auth_session_factory,
        tenant_id,
        passwords=PasswordService(HashingFailureHasher()),
    )

    with pytest.raises(AuthenticationOperationError) as captured:
        await service.login(tenant_id, "first-admin", password)

    _assert_production_frames_do_not_contain(captured.value, password)
    async with auth_session_factory() as session:
        assert await session.scalar(select(UserRow.password_hash)) == old_hash


@pytest.mark.integration
async def test_unknown_runtime_password_failure_propagates_after_cleanup(
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id = await _tenant(auth_session_factory)
    password = "sensitive password 123"
    service = _service(
        auth_session_factory,
        tenant_id,
        passwords=RuntimeVerifyPasswordService(),
    )

    with pytest.raises(RuntimeError, match="forced password backend failure") as captured:
        await service.login(tenant_id, "missing-user", password)

    _assert_production_frames_do_not_contain(captured.value, password)


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
