"""Task7D native PG read projection only; inert synthetic ledger rows, no permits."""

import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, event, select
from sqlalchemy.engine import make_url

from agent_hub.auth.models import AuthenticatedPrincipal, Role
from agent_hub.capabilities.review_repository import ReviewRepository
from agent_hub.db.models import (
    RunAdmissionAttemptRow,
    RunAdmissionEventRow,
    RunAdmissionPoolRow,
    RunApprovalRow,
    RunAuthenticatedInputRow,
    RunInputAuthorizationRow,
    RunReviewEffectRow,
    RunReviewEventRow,
    RunReviewIntentRow,
    RunRow,
)
from agent_hub.db.session import build_database
from agent_hub.runs.authorization import _capture_authenticated_input, _persist_authenticated_input
from agent_hub.runs.repository import RunNotFound, RunRepository

PRIVATE = "synthetic-private-action-rationale-lease-never-public"


@pytest.fixture(scope="session")
def database_url():
    value = os.environ.get("AGENT_HUB_TEST_DATABASE_URL")
    assert value, "explicit dedicated test database binding required"
    url = make_url(value)
    assert url.drivername == "postgresql+asyncpg"
    assert url.username == url.database == "agent_hub_test"
    assert (url.host in ("127.0.0.1", "localhost") and url.port is not None) or (
        url.host is None and str(url.query.get("host", "")).startswith("/run/")
    )
    return value


async def snapshot(db, run_ids):
    evidence = []
    async with db.session_factory() as session:
        for table in (
            RunAdmissionAttemptRow,
            RunAdmissionEventRow,
            RunAdmissionPoolRow,
            RunApprovalRow,
            RunAuthenticatedInputRow,
            RunInputAuthorizationRow,
            RunReviewEffectRow,
            RunReviewEventRow,
            RunReviewIntentRow,
        ):
            rows = (
                await session.execute(
                    select(*table.__table__.columns)
                    .where(table.run_id.in_(run_ids))
                    .order_by(*table.__table__.primary_key.columns)
                )
            ).all()
            evidence.append([tuple(row) for row in rows])
        evidence.append(
            [
                tuple(row)
                for row in (
                    await session.execute(
                        select(*RunRow.__table__.columns)
                        .where(RunRow.id.in_(run_ids))
                        .order_by(RunRow.id)
                    )
                ).all()
            ]
        )
    return evidence


@pytest.fixture
async def pg_history(database_url):
    from types import SimpleNamespace

    db = build_database(database_url)
    tenant, actor = uuid4(), uuid4()
    main, other_actor, other_tenant, legacy = [uuid4() for _ in range(4)]
    base = uuid4().int % (2**120)
    ids = [UUID(int=base + i) for i in range(207)]
    foreign_ids = [uuid4(), uuid4()]
    runs = [main, other_actor, other_tenant, legacy]
    created = datetime(2026, 10, 31, tzinfo=UTC)
    try:
        async with db.session_factory() as session, session.begin():
            for index, (rid, tid, aid, review_ids) in enumerate(
                [
                    (main, tenant, actor, ids),
                    (other_actor, tenant, uuid4(), foreign_ids[:1]),
                    (other_tenant, uuid4(), actor, foreign_ids[1:]),
                    (legacy, tenant, actor, []),
                ]
            ):
                row = RunRow(
                    id=rid,
                    tenant_id=tid,
                    actor_id=aid,
                    actor_role="operator",
                    request=PRIVATE,
                    mode="direct",
                    status="completed",
                )
                session.add(row)
                await session.flush()
                if not review_ids:
                    continue  # A real legacy row: no 2A evidence, pool or review.
                evidence = _capture_authenticated_input(
                    AuthenticatedPrincipal(tenant_id=tid, user_id=aid, role=Role.OPERATOR),
                    PRIVATE,
                    {"private_scope": PRIVATE},
                )
                await _persist_authenticated_input(session, row, evidence)
                session.add(
                    RunAdmissionPoolRow(
                        run_id=rid,
                        tenant_id=tid,
                        authorization_turn_id=evidence.turn_id,
                        actor_id=aid,
                        actor_role="operator",
                        message_sha256=evidence.message_sha256,
                        scope_sha256=evidence.scope_sha256,
                        config_sha256="d" * 64,
                        original_token_grant=1000,
                        original_deadline=created + timedelta(days=1),
                        review_token_limit=1000,
                        review_count_limit=1000,
                        review_cost_limit=Decimal(10),
                        token_charged=len(review_ids),
                        review_token_charged=len(review_ids),
                        review_count_charged=len(review_ids),
                        review_cost_charged=Decimal(0),
                        closed=True,
                    )
                )
                await session.flush()
                # Inert rows exercise display safety, not Task2C admission/permit validity.
                for number, identity in enumerate(review_ids):
                    attempt = uuid4()
                    session.add(
                        RunAdmissionAttemptRow(
                            attempt_id=attempt,
                            run_id=rid,
                            tenant_id=tid,
                            purpose="review",
                            state="prepared",
                            request_sha256="e" * 64,
                            deployment={
                                "provider_id": "synthetic",
                                "deployment_id": "review",
                                "model_id": "model",
                            },
                            worker_id="inert-history-worker",
                            worker_lease_token=uuid4(),
                            token_hold=1,
                            review_cost_hold=Decimal(0),
                        )
                    )
                    await session.flush()
                    session.add(
                        RunReviewIntentRow(
                            review_id=identity,
                            tenant_id=tid,
                            run_id=rid,
                            authorization_turn_id=evidence.turn_id,
                            attempt_id=attempt,
                            action_digest="a" * 64,
                            effect_digest="b" * 64,
                            authorization_sha256="c" * 64,
                            config_sha256="d" * 64,
                            policy_sha256="e" * 64,
                            source_sha256="f" * 64,
                            request_sha256="e" * 64,
                            private_action=PRIVATE,
                            private_config={"private": PRIVATE},
                            private_receipt=PRIVATE,
                            status="allow" if number % 2 == 0 else "deny",
                            risk="low",
                            reason_code=PRIVATE if number == 0 else "authorized_action",
                            consumed=False,
                            created_at=created + timedelta(seconds=number // 3),
                        )
                    )
                await session.flush()
        before = await snapshot(db, runs)
        statements = []

        def record(conn, cursor, statement, params, context, many):
            statements.append(statement)
            assert statement.lstrip().upper().startswith("SELECT"), "history attempted a write"

        event.listen(db.engine.sync_engine, "before_cursor_execute", record)
        try:
            yield SimpleNamespace(
                db=db,
                tenant=tenant,
                actor=actor,
                main=main,
                other_actor=other_actor,
                other_tenant=other_tenant,
                legacy=legacy,
                ids=ids,
                foreign_ids=foreign_ids,
                statements=statements,
            )
            assert all("private_" not in sql and "approval" not in sql for sql in statements)
            assert await snapshot(db, runs) == before
        finally:
            event.remove(db.engine.sync_engine, "before_cursor_execute", record)
    finally:
        async with db.session_factory() as session, session.begin():
            await session.execute(
                delete(RunAdmissionPoolRow).where(RunAdmissionPoolRow.run_id.in_(runs))
            )
            await session.execute(delete(RunRow).where(RunRow.id.in_(runs)))
        await db.dispose()


async def test_pg_history_keyset_over_200_and_same_timestamp_order(pg_history):
    p = pg_history
    repo = RunRepository(p.db.session_factory)
    seen, cursor = [], None
    for _ in range(10):
        page = await repo.model_reviews(p.tenant, p.main, actor_id=p.actor, limit=50, cursor=cursor)
        seen.extend(item.id for item in page.items)
        cursor = page.next_cursor
        if cursor is None:
            break
    assert seen == ["model-review:" + str(identity) for identity in p.ids]
    assert len(seen) == len(set(seen)) == 207
    page = await repo.model_reviews(p.tenant, p.main, actor_id=p.actor, limit=200)
    assert len(page.items) == 200 and page.next_cursor == p.ids[199]


async def test_pg_history_cross_scope_cursor_and_actor_cannot_read(pg_history):
    p = pg_history
    repo = RunRepository(p.db.session_factory)
    for tenant, actor, run in [
        (uuid4(), p.actor, p.main),
        (p.tenant, uuid4(), p.main),
        (p.tenant, p.actor, p.other_actor),
        (p.tenant, p.actor, p.other_tenant),
    ]:
        with pytest.raises(RunNotFound):
            await repo.model_reviews(tenant, run, actor_id=actor)
    for cursor in p.foreign_ids + [uuid4()]:
        with pytest.raises(ValueError):
            await repo.model_reviews(p.tenant, p.main, actor_id=p.actor, cursor=cursor)


async def test_pg_history_private_projection_and_legacy_zero_authority_writes(pg_history):
    from dataclasses import asdict

    p = pg_history
    repo = ReviewRepository(p.db.session_factory)
    page = await repo.list_for_run(p.tenant, p.main, actor_id=p.actor)
    first = asdict(page.items[0])
    assert set(first) == {
        "id",
        "created_at",
        "status",
        "risk",
        "reason_code",
        "reviewer",
        "tool_name",
    }
    assert first["reason_code"] is None
    assert first["status"] == "allow"  # Verdict only, not an execution receipt.
    assert PRIVATE not in str(first)
    empty = await repo.list_for_run(p.tenant, p.legacy, actor_id=p.actor)
    assert empty.items == () and empty.next_cursor is None
