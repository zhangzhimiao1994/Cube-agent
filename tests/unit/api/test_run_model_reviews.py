"""Task7D read projection + real SQL predicates + real HTTP, no external services.

SQLite is ONLY a local SELECT boundary surrogate; normalize its naive datetime
results to the UTC-aware values returned by PostgreSQL timestamptz. Native PG
ownership/keyset/zero-write checks are in a separate focused file.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Column, DateTime, MetaData, String, Table, Uuid, create_engine, event
from sqlalchemy.engine import Result
from sqlalchemy.orm import ORMExecuteState
from sqlalchemy.pool import StaticPool

from agent_hub.api.errors import PublicAPIError, public_error_handler
from agent_hub.api.routers import runs
from agent_hub.auth.models import AuthenticatedPrincipal, Role
from agent_hub.capabilities import review_repository
from agent_hub.domain.runs import TaskMode
from agent_hub.runs.repository import RunNotFound, RunRepository
from agent_hub.runs.service import RunService
from agent_hub.runtime.defaults import UnavailableRuntime
from agent_hub.runtime.registry import RuntimeRegistry
from tests.unit.capabilities.review_repository_api_typed_fixture import (
    ReviewStore,
    SQLSession,
    SQLSessions,
    UnexpectedTaskQueue,
)

TENANT, ACTOR, RUN = UUID(int=1), UUID(int=2), UUID(int=3)
CREATED = datetime(2026, 10, 31, tzinfo=UTC)
PRIVATE = "private-body-action-rationale-secret-never-public"


@pytest.mark.parametrize(
    "state,expected",
    [
        ("prepared", "pending"),
        ("issued", "pending"),
        ("allow", "allow"),
        ("deny", "deny"),
        ("needs_human", "needs_human"),
        ("unknown", "unknown"),
        ("timeout", "unknown"),
        ("unavailable", "unknown"),
        ("invalid_response", "unknown"),
        ("cancelled", "unknown"),
        (PRIVATE, "unknown"),
    ],
)
def test_storage_state_is_only_a_review_outcome(state: str, expected: str) -> None:
    item = review_repository._model_review_item(
        UUID(int=7), CREATED, state, "low", "authorized_action"
    )
    assert item.status == expected
    assert item.id == "model-review:00000000-0000-0000-0000-000000000007"
    assert item.reviewer == "independent_model"
    assert item.tool_name == "workspace.write_text"


def test_projection_sanitizes_untrusted_risk_reason_and_dto_rejects_private_fields() -> None:
    item = review_repository._model_review_item(uuid4(), CREATED, "allow", PRIVATE, PRIVATE)
    assert (item.risk, item.reason_code) == (None, None)
    response = runs.RunModelReviewItemResponse.from_record(item)
    payload = response.model_dump(mode="json")
    assert set(payload) == {
        "id",
        "created_at",
        "status",
        "risk",
        "reason_code",
        "reviewer",
        "tool_name",
    }
    assert PRIVATE not in json.dumps(payload)
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        runs.RunModelReviewItemResponse.model_validate({**payload, "rationale": PRIVATE})
    for field, value in [
        ("status", "executed"),
        ("risk", PRIVATE),
        ("reason_code", PRIVATE),
        ("reviewer", "human"),
        ("tool_name", "shell.run"),
    ]:
        with pytest.raises(ValidationError):
            runs.RunModelReviewItemResponse.model_validate({**payload, field: value})


@pytest.fixture
def store() -> Iterator[ReviewStore]:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    metadata = MetaData()
    owners = Table(
        "agent_hub_runs",
        metadata,
        Column("id", Uuid, primary_key=True),
        Column("tenant_id", Uuid),
        Column("actor_id", Uuid),
    )
    reviews = Table(
        "agent_hub_review_intents",
        metadata,
        Column("review_id", Uuid, primary_key=True),
        Column("run_id", Uuid),
        Column("tenant_id", Uuid),
        Column("created_at", DateTime(timezone=True)),
        Column("status", String),
        Column("risk", String),
        Column("reason_code", String),
        Column("private_action", String),
        Column("private_receipt", String),
    )
    metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(
            owners.insert(),
            [
                {"id": RUN, "tenant_id": TENANT, "actor_id": ACTOR},
                {"id": UUID(int=4), "tenant_id": TENANT, "actor_id": UUID(int=9)},
                {"id": UUID(int=5), "tenant_id": UUID(int=8), "actor_id": ACTOR},
            ],
        )
    statements: list[str] = []
    event.listen(
        engine, "before_cursor_execute", lambda c, cur, sql, p, ctx, many: statements.append(sql)
    )

    def seed(count: int = 1) -> None:
        with engine.begin() as conn:
            conn.execute(
                reviews.insert(),
                [
                    {
                        "review_id": UUID(int=1000 + i),
                        "run_id": RUN,
                        "tenant_id": TENANT,
                        "created_at": CREATED + timedelta(seconds=i // 3),
                        "status": "allow" if i % 2 == 0 else "deny",
                        "risk": "low",
                        "reason_code": "authorized_action",
                        "private_action": PRIVATE,
                        "private_receipt": PRIVATE,
                    }
                    for i in reversed(range(count))
                ],
            )
        statements.clear()

    value = ReviewStore(
        engine=engine,
        reviews=reviews,
        owners=owners,
        seed=seed,
        statements=statements,
        factory=SQLSessions(engine),
    )
    yield value
    engine.dispose()


def repository(store: ReviewStore) -> RunRepository:
    return RunRepository(store.factory)


class Auth:
    def __init__(self, principal: AuthenticatedPrincipal) -> None:
        self.principal = principal

    def authenticate_token(self, token: str) -> AuthenticatedPrincipal:
        return self.principal

    async def validate_principal(self, principal: AuthenticatedPrincipal) -> AuthenticatedPrincipal:
        return self.principal


def client(store: ReviewStore, *, tenant: UUID = TENANT, actor: UUID = ACTOR) -> TestClient:
    app = FastAPI()
    app.state.auth_service = Auth(
        AuthenticatedPrincipal(tenant_id=tenant, user_id=actor, role=Role.OPERATOR)
    )
    app.state.run_service = RunService(
        repository(store),
        runtime_registry=RuntimeRegistry([UnavailableRuntime(TaskMode.DIRECT)]),
        router=None,
        task_queue=UnexpectedTaskQueue(),
    )
    app.add_exception_handler(PublicAPIError, public_error_handler)
    app.include_router(runs.router)
    return TestClient(app)


@pytest.mark.asyncio
async def test_keyset_has_no_global_cap_and_stable_equal_timestamp_order(
    store: ReviewStore,
) -> None:
    store.seed(207)
    ids: list[str] = []
    cursor: UUID | None = None
    repo = repository(store)
    for _ in range(10):
        page = await repo.model_reviews(TENANT, RUN, actor_id=ACTOR, cursor=cursor, limit=50)
        ids.extend(item.id for item in page.items)
        cursor = page.next_cursor
        if cursor is None:
            break
    assert ids == ["model-review:" + str(UUID(int=1000 + i)) for i in range(207)]
    assert len(ids) == len(set(ids)) == 207
    assert all(sql.lstrip().upper().startswith("SELECT") for sql in store.statements)
    assert not any(
        "private_" in sql or "approval" in sql or "worker_lease" in sql for sql in store.statements
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tenant,actor,run",
    [
        (UUID(int=8), ACTOR, RUN),
        (TENANT, UUID(int=9), RUN),
        (TENANT, ACTOR, UUID(int=4)),
        (TENANT, ACTOR, uuid4()),
    ],
)
async def test_repository_uses_real_current_run_ownership(
    store: ReviewStore, tenant: UUID, actor: UUID, run: UUID
) -> None:
    store.seed()
    with pytest.raises(RunNotFound):
        await repository(store).model_reviews(tenant, run, actor_id=actor)


@pytest.mark.asyncio
async def test_cursor_must_be_in_owned_run_and_never_falls_back_to_first_page(
    store: ReviewStore,
) -> None:
    store.seed(3)
    foreign = uuid4()
    with store.engine.begin() as conn:
        conn.execute(
            store.reviews.insert(),
            {
                "review_id": foreign,
                "run_id": UUID(int=4),
                "tenant_id": TENANT,
                "created_at": CREATED,
                "status": "deny",
            },
        )
    for cursor in [foreign, uuid4()]:
        with pytest.raises(ValueError):
            await repository(store).model_reviews(TENANT, RUN, actor_id=ACTOR, cursor=cursor)
    page = await repository(store).model_reviews(TENANT, RUN, actor_id=ACTOR, limit=2)
    assert page.next_cursor == UUID(int=1001)
    final = await repository(store).model_reviews(
        TENANT, RUN, actor_id=ACTOR, cursor=page.next_cursor, limit=2
    )
    assert [item.id for item in final.items] == ["model-review:" + str(UUID(int=1002))]
    assert final.next_cursor is None


def test_http_projection_and_pagination_preserve_only_public_fields(store: ReviewStore) -> None:
    store.seed(3)
    with client(store) as api:
        first = api.get(
            "/api/v1/runs/" + str(RUN) + "/model-reviews?limit=2",
            headers={"Authorization": "Bearer synthetic"},
        )
        assert first.status_code == 200, first.text
        body = first.json()
        assert set(body) == {"items", "next_cursor"}
        assert body["next_cursor"] == str(UUID(int=1001))
        assert len(body["items"]) == 2
        assert body["items"][0] == {
            "id": "model-review:" + str(UUID(int=1000)),
            "created_at": "2026-10-31T00:00:00Z",
            "status": "allow",
            "risk": "low",
            "reason_code": "authorized_action",
            "reviewer": "independent_model",
            "tool_name": "workspace.write_text",
        }
        second = api.get(
            "/api/v1/runs/" + str(RUN) + "/model-reviews?cursor=" + body["next_cursor"],
            headers={"Authorization": "Bearer synthetic"},
        )
        assert second.status_code == 200
        assert [item["id"] for item in second.json()["items"]] == [
            "model-review:" + str(UUID(int=1002))
        ]
        assert second.json()["next_cursor"] is None
        assert PRIVATE not in first.text + second.text
    assert all(sql.lstrip().upper().startswith("SELECT") for sql in store.statements)


@pytest.mark.parametrize("tenant,actor", [(UUID(int=8), ACTOR), (TENANT, UUID(int=9))])
def test_http_cross_tenant_or_actor_is_not_found(
    store: ReviewStore, tenant: UUID, actor: UUID
) -> None:
    store.seed()
    with client(store, tenant=tenant, actor=actor) as api:
        result = api.get(
            "/api/v1/runs/" + str(RUN) + "/model-reviews",
            headers={"Authorization": "Bearer synthetic"},
        )
    assert result.status_code == 404
    assert result.json()["error"]["code"] == "run_not_found"
    assert PRIVATE not in result.text


@pytest.mark.parametrize(
    "query", ["limit=0", "limit=201", "limit=-1", "limit=true", "cursor=private-invalid-uuid"]
)
def test_http_invalid_query_is_rejected(store: ReviewStore, query: str) -> None:
    with client(store) as api:
        result = api.get(
            "/api/v1/runs/" + str(RUN) + "/model-reviews?" + query,
            headers={"Authorization": "Bearer synthetic"},
        )
    assert result.status_code == 422


def test_http_legacy_empty_and_scoped_missing_cursor(store: ReviewStore) -> None:
    with client(store) as api:
        result = api.get(
            "/api/v1/runs/" + str(RUN) + "/model-reviews",
            headers={"Authorization": "Bearer synthetic"},
        )
        assert result.status_code == 200
        assert result.json() == {"items": [], "next_cursor": None}
        missing = api.get(
            "/api/v1/runs/" + str(RUN) + "/model-reviews?cursor=" + str(uuid4()),
            headers={"Authorization": "Bearer synthetic"},
        )
        assert missing.status_code == 422
        assert missing.json()["error"]["code"] == "invalid_cursor"


@pytest.mark.parametrize(
    "reason",
    [
        "authorized_action",
        "outside_authorized_scope",
        "protected_resource",
        "untrusted_content",
        "unclear_impact",
        "missing_context",
        "policy_requires_human",
        "accounting_unknown",
        "review_timeout",
        "review_unavailable",
        "review_invalid_response",
        "review_unknown",
        "review_cancelled",
    ],
)
def test_all_existing_fixed_reason_codes_are_preserved_without_raw_labels(reason: str) -> None:
    item = review_repository._model_review_item(uuid4(), CREATED, "unknown", "unknown", reason)
    assert runs.RunModelReviewItemResponse.from_record(item).reason_code == reason


@pytest.mark.parametrize("limit", [True, 0, -1, 201])
@pytest.mark.asyncio
async def test_direct_repository_invalid_page_size_never_queries_or_writes(
    store: ReviewStore, limit: int
) -> None:
    with pytest.raises(ValueError):
        await repository(store).model_reviews(TENANT, RUN, actor_id=ACTOR, limit=limit)
    assert store.statements == []


def test_http_cross_run_cursor_returns_fixed_422_without_details(store: ReviewStore) -> None:
    store.seed()
    foreign = uuid4()
    with store.engine.begin() as conn:
        conn.execute(
            store.reviews.insert(),
            {
                "review_id": foreign,
                "run_id": UUID(int=5),
                "tenant_id": UUID(int=8),
                "created_at": CREATED,
                "status": "deny",
            },
        )
    with client(store) as api:
        result = api.get(
            "/api/v1/runs/" + str(RUN) + "/model-reviews?cursor=" + str(foreign),
            headers={"Authorization": "Bearer synthetic"},
        )
    assert result.status_code == 422
    assert result.json() == {
        "error": {"code": "invalid_cursor", "message": "model review cursor is invalid"}
    }


@pytest.mark.asyncio
async def test_each_history_select_rechecks_ownership_after_initial_read(
    store: ReviewStore,
) -> None:
    store.seed()

    class ChangedOwner(SQLSession):
        owner_changed = False

        def _execute_sql(self, state: ORMExecuteState) -> Result[tuple[object, ...]]:
            result = super()._execute_sql(state)
            if not self.owner_changed:
                self.sql_connection.execute(
                    store.owners.update()
                    .where(store.owners.c.id == RUN)
                    .values(actor_id=UUID(int=9))
                )
                self.owner_changed = True
            return result

    repo = RunRepository(SQLSessions(store.engine, ChangedOwner))
    page = await repo.model_reviews(TENANT, RUN, actor_id=ACTOR)
    assert page.items == ()
    assert page.next_cursor is None


@pytest.mark.parametrize("limit", [1, 200])
def test_http_page_boundary_limits_and_default_are_not_history_total_caps(
    store: ReviewStore, limit: int
) -> None:
    store.seed(207)
    with client(store) as api:
        response = api.get(
            "/api/v1/runs/" + str(RUN) + "/model-reviews?limit=" + str(limit),
            headers={"Authorization": "Bearer synthetic"},
        )
    assert response.status_code == 200
    assert len(response.json()["items"]) == limit
    assert response.json()["next_cursor"] == str(UUID(int=1000 + limit - 1))


def test_http_requires_current_authenticated_principal_before_any_read(store: ReviewStore) -> None:
    with client(store) as api:
        response = api.get("/api/v1/runs/" + str(RUN) + "/model-reviews")
    assert response.status_code == 401
    assert store.statements == []


def test_read_dto_openapi_has_only_whitelist_and_exact_page_contract() -> None:
    app = FastAPI()
    app.include_router(runs.router)
    schema = app.openapi()
    operation = schema["paths"]["/api/v1/runs/{run_id}/model-reviews"]["get"]
    parameters = {item["name"]: item["schema"] for item in operation["parameters"]}
    assert parameters["limit"]["default"] == 50
    assert parameters["limit"]["minimum"] == 1
    assert parameters["limit"]["maximum"] == 200
    components = schema["components"]["schemas"]
    assert set(components["RunModelReviewsResponse"]["properties"]) == {"items", "next_cursor"}
    assert set(components["RunModelReviewItemResponse"]["properties"]) == {
        "id",
        "created_at",
        "status",
        "risk",
        "reason_code",
        "reviewer",
        "tool_name",
    }
    assert components["RunModelReviewItemResponse"]["additionalProperties"] is False
    assert set(components["ModelReviewStatus"]["enum"]) == {
        "pending",
        "allow",
        "deny",
        "needs_human",
        "unknown",
    }
    assert set(components["ModelReviewReasonCode"]["enum"]) == {
        "authorized_action",
        "outside_authorized_scope",
        "protected_resource",
        "untrusted_content",
        "unclear_impact",
        "missing_context",
        "policy_requires_human",
        "accounting_unknown",
        "review_timeout",
        "review_unavailable",
        "review_invalid_response",
        "review_unknown",
        "review_cancelled",
    }
