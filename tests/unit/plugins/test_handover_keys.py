"""SQL doubles test the loader boundary, not native PostgreSQL locking."""

from __future__ import annotations

import base64
import importlib.util
import traceback
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import cast
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from asyncpg.pgproto.pgproto import UUID as DriverUUID  # type: ignore[import-untyped]
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from sqlalchemy.dialects import postgresql
from sqlalchemy.engine import Dialect
from sqlalchemy.exc import SQLAlchemyError, StatementError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.sql import Select

from agent_hub.db.models import AdminResourceRow
from agent_hub.plugins.handover_storage import StorageUnavailable
from agent_hub.plugins.handover_verification import FrozenSigningKey, PackageVerificationError

KEY_ID = "signing-key:current"
PUBLIC = Ed25519PrivateKey.from_private_bytes(bytes(range(32))).public_key().public_bytes_raw()
ENCODED = base64.urlsafe_b64encode(PUBLIC).decode("ascii").rstrip("=")
NOW = datetime(2026, 10, 12, 12, tzinfo=UTC)
POSTGRES = cast(type[Dialect], postgresql.dialect)


async def load(session: AsyncSession, tenant_id: UUID, key_id: str = KEY_ID) -> FrozenSigningKey:
    assert importlib.util.find_spec("agent_hub.plugins.handover_keys") is not None, (
        "the current SQL signing-key loader is not implemented"
    )
    from agent_hub.plugins.handover_keys import load_locked_signing_key

    return await load_locked_signing_key(session, tenant_id=tenant_id, key_id=key_id)


@dataclass
class Context:
    session: AsyncSession
    row: AdminResourceRow
    scalar: AsyncMock
    writes: tuple[AsyncMock, ...]


@pytest.fixture
async def context(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Context]:
    # SQL is intercepted before any connection; the actual session owns BEGIN.
    engine = create_async_engine("postgresql+asyncpg://unit:unit@127.0.0.1:1/unit")
    row = AdminResourceRow(
        id=uuid4(),
        tenant_id=uuid4(),
        kind="plugin_signing_key",
        resource_id=KEY_ID,
        payload={"key_id": KEY_ID, "algorithm": "ed25519", "public_key": ENCODED, "trusted": True},
    )
    async with AsyncSession(engine) as session, session.begin():
        scalar = AsyncMock(return_value=row)
        monkeypatch.setattr(session, "scalar", scalar)
        writes = tuple(
            AsyncMock(side_effect=AssertionError("read-only loader wrote")) for _ in range(4)
        )
        for name, method in zip(("commit", "rollback", "flush", "execute"), writes):
            monkeypatch.setattr(session, name, method)
        yield Context(session, row, scalar, writes)
    await engine.dispose()


async def test_current_key_uses_one_tenant_share_query_without_writes(context: Context) -> None:
    key = await load(context.session, context.row.tenant_id)
    assert key == FrozenSigningKey(KEY_ID, "ed25519", PUBLIC, True)
    assert context.scalar.await_count == 1
    statement = context.scalar.await_args_list[0].args[0]
    assert isinstance(statement, Select)
    assert statement.column_descriptions[0]["entity"] is AdminResourceRow
    compiled = statement.compile(dialect=POSTGRES())
    assert compiled.params == {
        "tenant_id_1": context.row.tenant_id,
        "kind_1": "plugin_signing_key",
        "resource_id_1": KEY_ID,
    }
    assert "FOR SHARE" in str(compiled) and "FOR UPDATE" not in str(compiled)
    assert statement.get_execution_options()["populate_existing"] is True
    assert statement.get_execution_options()["autoflush"] is False
    assert all(method.await_count == 0 for method in context.writes)


@pytest.mark.parametrize("field", ["id", "tenant_id", "both"])
async def test_asyncpg_row_uuid_loads_current_key(context: Context, field: str) -> None:
    tenant = context.row.tenant_id
    if field in {"id", "both"}:
        context.row.id = DriverUUID(str(context.row.id))
    if field in {"tenant_id", "both"}:
        context.row.tenant_id = DriverUUID(str(context.row.tenant_id))
    assert await load(context.session, tenant) == FrozenSigningKey(KEY_ID, "ed25519", PUBLIC, True)
    assert all(method.await_count == 0 for method in context.writes)


@pytest.mark.parametrize("field", ["id", "tenant_id"])
async def test_key_row_uuid_string_is_rejected(context: Context, field: str) -> None:
    tenant = context.row.tenant_id
    setattr(context.row, field, str(getattr(context.row, field)))
    with pytest.raises(StorageUnavailable):
        await load(context.session, tenant)


async def test_asyncpg_input_tenant_uuid_is_rejected(context: Context) -> None:
    with pytest.raises(StorageUnavailable):
        await load(context.session, DriverUUID(str(context.row.tenant_id)))
    assert context.scalar.await_count == 0


@pytest.mark.parametrize("trust", [None, False, 0, 1, "true", "false", [], {}])
async def test_raw_trust_requires_literal_true(context: Context, trust: object) -> None:
    context.row.payload["trusted"] = trust
    with pytest.raises(StorageUnavailable):
        await load(context.session, context.row.tenant_id)


@pytest.mark.parametrize("field", ["trusted", "key_id", "algorithm", "public_key"])
async def test_required_fields_never_receive_dto_defaults(context: Context, field: str) -> None:
    del context.row.payload[field]
    with pytest.raises(StorageUnavailable):
        await load(context.session, context.row.tenant_id)


@pytest.mark.parametrize("field", ["id", "tenant_id", "kind", "resource_id"])
async def test_actual_row_identity_must_match_query(context: Context, field: str) -> None:
    tenant = context.row.tenant_id
    setattr(context.row, field, uuid4() if field in {"id", "tenant_id"} else "different")
    if field == "id":
        context.row.id = cast(UUID, "not-a-uuid")
    with pytest.raises(StorageUnavailable):
        await load(context.session, tenant)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("key_id", "other-key"),
        ("key_id", 1),
        ("algorithm", "rsa"),
        ("algorithm", 1),
        ("public_key", 1),
        ("public_key", ENCODED.encode()),
        ("unknown", "private-payload-sentinel"),
        ("disabled", True),
        ("not_before", 0),
        ("not_after", True),
        ("not_before", "2026-10-12T12:00:00"),
        ("not_after", "not-a-timestamp"),
        ("not_before", NOW),
        ("not_before", "0001-01-01T00:00:00+14:00"),
        ("not_after", "9999-12-31T23:59:59-14:00"),
    ],
)
async def test_payload_types_envelope_and_fields_fail_closed(
    context: Context, field: str, value: object
) -> None:
    context.row.payload[field] = value
    with pytest.raises(StorageUnavailable) as caught:
        await load(context.session, context.row.tenant_id)
    assert "private-payload-sentinel" not in str(caught.value)


@pytest.mark.parametrize("payload", [None, [], "private-payload-sentinel", 1])
async def test_nonobject_payload_is_not_a_key(context: Context, payload: object) -> None:
    context.row.payload = cast(dict[str, object], payload)
    with pytest.raises(StorageUnavailable):
        await load(context.session, context.row.tenant_id)


@pytest.mark.parametrize(
    "encoded",
    [
        ENCODED + "=",
        ENCODED[:-1],
        ENCODED + "A",
        "+" + ENCODED[1:],
        "/" + ENCODED[1:],
        " " + ENCODED[1:],
        ENCODED[:-1] + "\n",
        base64.urlsafe_b64encode(PUBLIC[:-1]).decode().rstrip("="),
        base64.urlsafe_b64encode(PUBLIC + b"x").decode().rstrip("="),
        # Pad bits are not part of the bytes: a permissive decoder accepts this alias.
        ENCODED[:-1]
        + "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"[
            "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_".index(ENCODED[-1])
            + 1
        ],
    ],
)
async def test_public_key_requires_exact_canonical_43_character_base64url(
    context: Context, encoded: str
) -> None:
    context.row.payload["public_key"] = encoded
    with pytest.raises(StorageUnavailable):
        await load(context.session, context.row.tenant_id)


@pytest.mark.parametrize("delta", [timedelta(0), -timedelta(seconds=1)])
async def test_inverted_or_empty_window_is_invalid(context: Context, delta: timedelta) -> None:
    context.row.payload.update(not_before=NOW.isoformat(), not_after=(NOW + delta).isoformat())
    with pytest.raises(StorageUnavailable):
        await load(context.session, context.row.tenant_id)


@pytest.mark.parametrize("window", ["absent", "null", "offset", "future", "expired"])
async def test_windows_are_frozen_not_evaluated_with_an_early_clock(
    context: Context, window: str
) -> None:
    if window == "null":
        context.row.payload.update(not_before=None, not_after=None)
    elif window == "offset":
        context.row.payload.update(
            not_before="2026-10-12T19:00:00+08:00", not_after="2026-10-12T21:00:00+08:00"
        )
    elif window == "future":
        context.row.payload["not_before"] = "2099-01-01T00:00:00Z"
    elif window == "expired":
        context.row.payload["not_after"] = "2000-01-01T00:00:00Z"
    key = await load(context.session, context.row.tenant_id)
    if window in {"absent", "null"}:
        assert key.not_before is None and key.not_after is None
    elif window == "offset":
        assert key.not_before == NOW - timedelta(hours=1)
        assert key.not_after == NOW + timedelta(hours=1)
        assert key.not_before.tzinfo is UTC and key.not_after.tzinfo is UTC
    elif window == "future":
        assert key.not_before == datetime(2099, 1, 1, tzinfo=UTC)
    else:
        assert key.not_after == datetime(2000, 1, 1, tzinfo=UTC)


async def test_key_is_detached_and_real_ed25519_bytes_remain_verifiable(context: Context) -> None:
    key = await load(context.session, context.row.tenant_id)
    context.row.payload.update(public_key="A" * 43, trusted=False)
    message = b"key-loader-real-crypto-test"
    signed = Ed25519PrivateKey.from_private_bytes(bytes(range(32))).sign(message)
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    Ed25519PublicKey.from_public_bytes(key.public_key).verify(signed, message)
    assert key.trusted is True and key.public_key == PUBLIC


async def test_repeat_reads_current_row_instead_of_caching_key(context: Context) -> None:
    assert (await load(context.session, context.row.tenant_id)).trusted is True
    context.row.payload["trusted"] = False
    with pytest.raises(StorageUnavailable):
        await load(context.session, context.row.tenant_id)
    assert context.scalar.await_count == 2


@pytest.mark.parametrize("failure", ["missing", "sql", "statement"])
async def test_missing_or_sql_failure_is_safe_and_never_falls_back(
    context: Context, failure: str
) -> None:
    if failure == "missing":
        context.scalar.return_value = None
    elif failure == "sql":
        context.scalar.side_effect = SQLAlchemyError("private-sql-sentinel")
    else:
        context.scalar.side_effect = StatementError(
            "private-sql-sentinel",
            "private-query-sentinel",
            {"secret": "private-param-sentinel"},
            None,
        )
    with pytest.raises(StorageUnavailable) as caught:
        await load(context.session, context.row.tenant_id)
    rendered = "".join(traceback.format_exception(caught.value))
    assert "private-sql-sentinel" not in rendered
    assert "private-query-sentinel" not in rendered
    assert "private-param-sentinel" not in rendered
    assert caught.value.__cause__ is None
    assert all(method.await_count == 0 for method in context.writes)


@pytest.mark.parametrize("identity", ["tenant", "key-type", "key-empty", "key-invalid"])
async def test_invalid_lookup_identity_is_rejected_before_sql(
    context: Context, identity: str
) -> None:
    tenant = (
        cast(UUID, str(context.row.tenant_id)) if identity == "tenant" else context.row.tenant_id
    )
    key = {"key-type": cast(str, 1), "key-empty": "", "key-invalid": "../key"}.get(identity, KEY_ID)
    with pytest.raises(StorageUnavailable):
        await load(context.session, tenant, key)
    assert context.scalar.await_count == 0


@pytest.mark.parametrize("boundary", ["no-begin", "autobegin", "sqlite", "not-session"])
async def test_only_explicit_postgres_begin_is_accepted(boundary: str) -> None:
    engine = create_async_engine("postgresql+asyncpg://unit:unit@127.0.0.1:1/unit")
    try:
        async with AsyncSession(engine) as session:
            if boundary == "not-session":
                with pytest.raises(StorageUnavailable):
                    await load(cast(AsyncSession, object()), uuid4())
            elif boundary in {"no-begin", "autobegin"}:
                if boundary == "autobegin":
                    session.add(AdminResourceRow(tenant_id=uuid4(), kind="plugin_signing_key"))
                with pytest.raises(StorageUnavailable):
                    await load(session, uuid4())
            else:
                async with session.begin():
                    original = engine.dialect.name
                    engine.dialect.name = "sqlite"
                    try:
                        with pytest.raises(StorageUnavailable):
                            await load(session, uuid4())
                    finally:
                        engine.dialect.name = original
    finally:
        await engine.dispose()


async def test_caller_verifier_must_use_clock_after_key_lock_wait(
    context: Context, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent_hub.plugins.handover_verification import verify_signed_snapshot_package
    from tests.unit.plugins.test_handover_verification import frozen, signed

    private = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
    snapshot, archive = frozen(), signed(private, key_id="test-key")
    context.row.resource_id = "test-key"
    context.row.payload.update(
        key_id="test-key",
        public_key=base64.urlsafe_b64encode(private.public_key().public_bytes_raw())
        .decode()
        .rstrip("="),
        not_after=(NOW + timedelta(seconds=1)).isoformat(),
    )
    clock = [NOW]

    async def lock_wait(statement: object) -> AdminResourceRow:
        clock[0] = NOW + timedelta(seconds=1)
        return context.row

    monkeypatch.setattr(context.session, "scalar", AsyncMock(side_effect=lock_wait))
    key = await load(context.session, context.row.tenant_id, "test-key")
    assert (
        verify_signed_snapshot_package(
            snapshot=snapshot, archive_bytes=archive, key=key, now=NOW
        ).key_id
        == "test-key"
    )
    with pytest.raises(PackageVerificationError):
        verify_signed_snapshot_package(
            snapshot=snapshot, archive_bytes=archive, key=key, now=clock[0]
        )
