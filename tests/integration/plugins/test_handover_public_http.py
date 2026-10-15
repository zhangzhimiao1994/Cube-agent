"""Real login, HTTP and PostgreSQL; not model generation or activation acceptance.

Redis admission is a double; the real run service has a fail-closed fixture runtime
and a queue that refuses execution. Passwords, tokens,
source authorization, current users, storage and signing use real implementations.
The shared fixture requires the explicit isolated testDB opt-in, never production.
"""

from __future__ import annotations

import json
import secrets
import sys
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import select, update

from agent_hub.app import create_app
from agent_hub.auth.models import Role
from agent_hub.auth.passwords import PasswordService
from agent_hub.auth.rate_limit import RateLimitDecision
from agent_hub.auth.service import AuthService
from agent_hub.auth.tokens import AccessTokenService
from agent_hub.capabilities.approvals import PluginApprovalIdentity, fingerprint_capability_request
from agent_hub.capabilities.types import CapabilityRequest
from agent_hub.config.service import ConfigService
from agent_hub.db.models import AdminResourceRow, RunEventRow, RunRow, UserRow
from agent_hub.domain.runs import RunStatus, TaskMode
from agent_hub.plugins.handover_grants import SourceGrant
from agent_hub.runs.repository import RunRepository
from agent_hub.runs.service import RunService
from agent_hub.runtime.contracts import EventKind, RunEvent, RuntimeCheckpoint, TaskContext
from agent_hub.runtime.registry import RuntimeRegistry
from agent_hub.settings import Settings
from tests.integration.plugins import test_handover_confirmation as sql_tests

pytestmark = sql_tests.pytestmark
sql_context = sql_tests.context


class _Admission:
    async def check(self, endpoint: str, client_ip: str) -> RateLimitDecision:
        assert endpoint == "login" and client_ip
        return RateLimitDecision(True)


async def _ready() -> None:
    return None


class _NoTasks:
    async def enqueue_run(self, run_id: UUID, *, idempotency_key: str) -> None:
        raise AssertionError("HTTP projection fixture must never enqueue execution")


class _NoExecution:
    mode = TaskMode.DISPATCH

    def run(self, context: TaskContext) -> AsyncIterator[RunEvent]:
        raise AssertionError("HTTP projection fixture must never execute")

    async def save_checkpoint(self) -> RuntimeCheckpoint:
        raise AssertionError("HTTP projection fixture must never execute")

    async def restore_checkpoint(self, checkpoint: RuntimeCheckpoint) -> None:
        raise AssertionError("HTTP projection fixture must never execute")

    async def cancel(self) -> None:
        raise AssertionError("HTTP projection fixture must never execute")


def _projection_run_service(repository: RunRepository) -> RunService:
    return RunService(
        repository, runtime_registry=RuntimeRegistry((_NoExecution(),)),
        router=None, task_queue=_NoTasks(),
    )


def _approval_mirror(run_id: UUID, approval_id: str, kind: EventKind) -> RunEvent:
    assert kind in {EventKind.APPROVAL_REQUESTED, EventKind.APPROVAL_RESOLVED}
    requested = kind is EventKind.APPROVAL_REQUESTED
    return RunEvent(
        kind=kind, sequence=99, run_id=run_id, approval_id=approval_id,
        actor="fixture-runtime", action="untrusted-runtime-mirror" if requested else None,
        reason="untrusted runtime requested" if requested else None,
        decision=None if requested else "approved",
        payload={"plugin_id": "forged", "private": "PRIVATE-RUNTIME-MIRROR"},
    )


@dataclass(repr=False)
class Context:
    sql: sql_tests.Context
    client: httpx.AsyncClient
    creator_headers: dict[str, str]
    installer_headers: dict[str, str]
    other_headers: dict[str, str]

    @property
    def prefix(self) -> str:
        return f"/api/v1/runs/{self.sql.run_id}/plugin-handovers"

    async def plan(self, *, workspace: bool = False) -> dict[str, object]:
        source: dict[str, object] = {"kind": "artifact", "artifact_id": str(self.sql.artifact_id)}
        if workspace:
            source = {
                "kind": "workspace",
                "project_id": "project",
                "session_id": "session",
                "paths": sorted((*sql_tests.MEMBERS, "plugin.json")),
            }
        response = await self.client.post(
            self.prefix,
            headers={**self.creator_headers, "Idempotency-Key": "public-source-plan"},
            json={
                "source": source,
                "expires_at": (self.sql.clock[0] + timedelta(hours=1)).isoformat(),
            },
        )
        assert response.status_code == 200
        result: dict[str, object] = response.json()
        assert result["creator_id"] == str(self.sql.creator.user_id)
        assert result["installer_id"] is None and result["status"] == "awaiting_signature"
        return result

    async def confirm(
        self, plan: dict[str, object], *, creator: bool = False, key: str = sql_tests.KEY_ID
    ) -> httpx.Response:
        return await self.client.post(
            f"{self.prefix}/{plan['handover_id']}/confirm",
            headers={
                **(self.creator_headers if creator else self.installer_headers),
                "Idempotency-Key": "public-confirmation",
                "Content-Type": "application/zip",
            },
            params={
                "expected_version": "1",
                "plan_hash": str(plan["plan_hash"]),
                "confirm": "true",
                "key_id": key,
            },
            content=self.sql.archive,
        )


@pytest.fixture
async def context(sql_context: sql_tests.Context, tmp_path: Path) -> AsyncIterator[Context]:
    c = sql_context
    passwords = PasswordService()
    password = secrets.token_urlsafe(32)
    hashed = await passwords.hash_async(password)
    other_id = uuid4()
    async with c.factory() as session, session.begin():
        await session.execute(
            update(UserRow)
            .where(UserRow.tenant_id == c.creator.tenant_id)
            .values(password_hash=hashed)
        )
        session.add(
            UserRow(
                id=other_id,
                tenant_id=c.creator.tenant_id,
                username="other-operator",
                role="operator",
                disabled=False,
                password_hash=hashed,
            )
        )
    auth = AuthService(
        c.factory, c.creator.tenant_id, passwords, AccessTokenService(secrets.token_bytes(32))
    )
    settings = Settings.model_validate(
        {
            "bootstrap_tenant_id": c.creator.tenant_id,
            "project_workspace_dir": c.package_root.parent / "workspaces",
            "generated_artifact_dir": c.package_root.parent / "generated",
            "plugin_package_store_dir": c.package_root,
            "capability_environment_root_dir": tmp_path / "capabilities",
            "scheduler_enabled": False,
        }
    )
    application = create_app(
        settings=settings,
        session_factory=c.factory,
        auth_service=auth,
        config_service=ConfigService(c.factory),
        user_admin_service=object(),
        run_service=_projection_run_service(RunRepository(c.factory)),
        rate_limiter=_Admission(),
        database_probe=_ready,
        redis_probe=_ready,
    )
    async with application.router.lifespan_context(application):
        assert application.state.plugin_handover_services is not None
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=application), base_url="http://isolated.test"
        ) as client:
            headers: list[dict[str, str]] = []
            for username in ("creator", "installer", "other-operator"):
                response = await client.post(
                    "/api/v1/auth/login",
                    json={
                        "tenant_id": str(c.creator.tenant_id),
                        "username": username,
                        "password": password,
                    },
                )
                assert response.status_code == 200
                headers.append({"Authorization": "Bearer " + response.json()["access_token"]})
            del password, hashed, response
            yield Context(c, client, *headers)
            headers.clear()


@pytest.mark.parametrize("workspace", [False, True], ids=["artifact", "workspace"])
async def test_real_login_source_confirmation_is_pending_not_execution(
    context: Context, workspace: bool
) -> None:
    c = context
    plan = await c.plan(workspace=workspace)
    response = await c.confirm(plan)
    assert response.status_code == 200
    pending = response.json()
    assert pending["status"] == "pending" and pending["version"] == 2
    assert pending["creator_id"] == str(c.sql.creator.user_id)
    assert pending["installer_id"] == str(c.sql.installer.user_id)
    async with c.sql.factory() as session:
        row = await session.scalar(
            select(AdminResourceRow).where(
                AdminResourceRow.tenant_id == c.sql.creator.tenant_id,
                AdminResourceRow.kind == "plugin",
                AdminResourceRow.resource_id == sql_tests.PLUGIN_ID,
            )
        )
        assert row is not None
        assert row.payload["enabled"] is False and row.payload["status"] == "disabled"
        package = row.payload["package_metadata"]
        assert isinstance(package, dict)
        assert package["approval_state"] == "pending"
        assert package["activation_state"] == "blocked_pending_approval"
        receipt_row = await session.scalar(
            select(AdminResourceRow).where(
                AdminResourceRow.tenant_id == c.sql.creator.tenant_id,
                AdminResourceRow.kind == "plugin_handover",
                AdminResourceRow.resource_id == pending["handover_id"],
            )
        )
        assert receipt_row is not None
        assert receipt_row.payload["receipt_id"] == pending["receipt_id"]
        assert receipt_row.payload["audit_id"] == pending["audit_id"]
        audit = await session.scalar(
            select(AdminResourceRow).where(
                AdminResourceRow.tenant_id == c.sql.creator.tenant_id,
                AdminResourceRow.kind == "audit",
                AdminResourceRow.resource_id == pending["audit_id"],
            )
        )
        assert audit is not None and audit.payload["action"] == "plugin.generated_handover.pending"
        details = audit.payload["details"]
        assert isinstance(details, dict)
        assert details["receipt_id"] == pending["receipt_id"]
        assert details["creator_id"] == str(c.sql.creator.user_id)
        assert details["installer_id"] == str(c.sql.installer.user_id)
        grant_row = await session.scalar(
            select(AdminResourceRow).where(
                AdminResourceRow.tenant_id == c.sql.creator.tenant_id,
                AdminResourceRow.kind == "plugin_handover",
                AdminResourceRow.resource_id == receipt_row.payload["source_grant_id"],
            )
        )
        assert grant_row is not None
        grant = SourceGrant.model_validate_json(json.dumps(grant_row.payload))
        assert grant.state == "consumed" and grant.receipt_id == pending["receipt_id"]
        assert grant.binding.plan_id == pending["handover_id"]
        assert grant.consumed_by == c.sql.installer.user_id
    c.sql.assert_package()
    ledger = c.sql.package_root / ".stage-ownership" / str(c.sql.creator.tenant_id)
    records = [json.loads(path.read_bytes()) for path in sorted(ledger.rglob("*.json"))]
    assert records and records[0]["state"] == "creating"
    assert records[-1]["state"] == "published"
    states = [record["state"] for record in records]
    assert states.index("ready") < states.index("rename_attempted") < states.index("published")
    assert all(record["directory_durability"] == (sys.platform != "win32") for record in records)
    assert all(record["tenant"] == str(c.sql.creator.tenant_id) for record in records)
    assert all(record["plugin_id"] == sql_tests.PLUGIN_ID for record in records)
    assert all(type(record["created_at"]) is str for record in records)
    assert all(type(record["recorded_at"]) is str for record in records)
    created_at = datetime.fromisoformat(records[0]["created_at"])
    recorded_at = [datetime.fromisoformat(record["recorded_at"]) for record in records]
    assert created_at.tzinfo == UTC and all(stamp.tzinfo == UTC for stamp in recorded_at)
    assert all(record["created_at"] == records[0]["created_at"] for record in records)
    assert created_at <= recorded_at[0] and recorded_at == sorted(recorded_at)
    assert records[-1]["details"]["target"] == c.sql.canonical.name
    assert not any(record["state"] == "confirmed_aborted" for record in records)
    for path in ledger.rglob("*.json"):
        assert sql_tests.CODE not in path.read_bytes()
    historical = await c.client.get(f"{c.prefix}/{plan['handover_id']}", headers=c.creator_headers)
    assert historical.status_code == 200 and historical.json() == pending
    # This is real SQL/public identity projection, not execution of the disabled package.
    repository = RunRepository(c.sql.factory)
    async with c.sql.factory() as session, session.begin():
        await session.execute(
            update(RunRow)
            .where(
                RunRow.tenant_id == c.sql.creator.tenant_id,
                RunRow.id == c.sql.run_id,
            )
            .values(status=RunStatus.RUNNING.value, mode=TaskMode.DISPATCH.value,
                    worker_lease_token=uuid4())
        )
    request = CapabilityRequest(
        tenant_id=c.sql.creator.tenant_id,
        user_id=c.sql.creator.user_id,
        run_id=c.sql.run_id,
        agent_id="main",
        capability="acceptance",
        operation="verify",
        resource="fixture-only",
        arguments={"private": "PRIVATE-MUST-NOT-BE-PROJECTED"},
        idempotency_key="projection-only",
    )
    fingerprint = fingerprint_capability_request(request)
    approval_id = "projection-" + uuid4().hex
    waiting = await repository.begin_capability_approval(
        request.tenant_id,
        request.run_id,
        actor_id=request.user_id,
        approval_id=approval_id,
        approval_fingerprint=fingerprint,
        plugin_identity=PluginApprovalIdentity(sql_tests.PLUGIN_ID, "acceptance.verify"),
    )
    endpoint = f"/api/v1/runs/{request.run_id}/capability-approvals"
    projected = await c.client.get(endpoint, headers=c.creator_headers)
    assert projected.status_code == 200
    assert projected.json()["ready"] is False
    items = projected.json()["items"]
    assert len(items) == 1 and items[0]["kind"] == "approval.requested"
    assert items[0]["approval_id"] == approval_id and items[0]["run_id"] == str(request.run_id)
    assert items[0]["payload"] == {
        "plugin_id": sql_tests.PLUGIN_ID,
        "capability_id": "acceptance.verify",
        "run_id": str(request.run_id),
        "version": waiting.version,
    }
    assert "PRIVATE" not in projected.text and fingerprint not in projected.text
    repeated = await c.client.get(endpoint, headers=c.creator_headers)
    assert repeated.status_code == 200 and repeated.json() == projected.json()
    async with c.sql.factory() as session, session.begin():
        checkpoint = RuntimeCheckpoint(
            id=uuid4(), runtime_type="crew", runtime_version="11",
            run_id=request.run_id, tenant_id=request.tenant_id, mode=TaskMode.DISPATCH,
            state={"tools": {"fixture-key": {
                "status": "waiting_approval", "approval_id": approval_id,
            }}},
        )
        await repository._persist_checkpoint(
            session, request.tenant_id, request.run_id,
            RunEvent(kind=EventKind.CHECKPOINT_SAVED, sequence=98, run_id=request.run_id,
                     checkpoint=checkpoint),
        )
    projected = await c.client.get(endpoint, headers=c.creator_headers)
    assert projected.status_code == 200 and projected.json()["ready"] is True
    assert projected.json()["items"] == items
    async with c.sql.factory() as session, session.begin():
        for kind in (EventKind.APPROVAL_REQUESTED, EventKind.APPROVAL_RESOLVED):
            mirror = _approval_mirror(request.run_id, approval_id, kind)
            await repository._persist_approval(session, request.tenant_id, request.run_id, mirror)
        session.add(
            RunEventRow(
                id=uuid4(),
                tenant_id=request.tenant_id,
                run_id=request.run_id,
                sequence=99,
                kind=EventKind.STEP_STARTED.value,
                payload={
                    "kind": EventKind.STEP_STARTED.value,
                    "sequence": 99,
                    "run_id": str(request.run_id),
                    "step_id": "fixture-step",
                },
            )
        )
    after_worker = await c.client.get(endpoint, headers=c.creator_headers)
    assert after_worker.status_code == 200 and after_worker.json()["items"] == items
    assert after_worker.json()["ready"] is False
    worker_history = await c.client.get(
        f"/api/v1/runs/{request.run_id}/events",
        headers=c.creator_headers,
    )
    assert worker_history.status_code == 200
    assert [event["sequence"] for event in worker_history.json()["items"]] == [99]
    foreign = await c.client.get(endpoint, headers=c.other_headers)
    assert foreign.status_code == 404
    async with c.sql.factory() as session, session.begin():
        await repository._persist_checkpoint(
            session, request.tenant_id, request.run_id,
            RunEvent(kind=EventKind.CHECKPOINT_SAVED, sequence=100, run_id=request.run_id,
                     checkpoint=checkpoint.model_copy(update={"id": uuid4()})),
        )
    recovered = await c.client.get(endpoint, headers=c.creator_headers)
    assert recovered.status_code == 200 and recovered.json()["ready"] is True
    assert recovered.json()["items"] == items
    # Resolve through the actual repository transaction without queueing/model execution.
    resolved = await repository.resolve_capability_approval(
        request.tenant_id,
        request.run_id,
        RunStatus.CANCELLED,
        approval_id=approval_id,
        approval_fingerprint=fingerprint,
    )
    history = await c.client.get(endpoint, headers=c.creator_headers)
    assert history.status_code == 200
    assert history.json()["ready"] is False
    approved_items = history.json()["items"]
    assert approved_items[0] == items[0]
    assert approved_items[1]["kind"] == "approval.resolved"
    assert approved_items[1]["approval_id"] == approval_id
    assert approved_items[1]["sequence"] == resolved.version > items[0]["sequence"]


@pytest.mark.parametrize("operation", ["read", "cancel"])
async def test_other_authenticated_operator_cannot_see_or_cancel_plan(
    context: Context, operation: str
) -> None:
    c = context
    plan = await c.plan()
    before = await c.sql.rows()
    url = f"{c.prefix}/{plan['handover_id']}"
    if operation == "read":
        response = await c.client.get(url, headers=c.other_headers)
    else:
        response = await c.client.post(
            url + "/cancel",
            headers=c.other_headers,
            json={"expected_version": 1, "plan_hash": plan["plan_hash"]},
        )
    assert response.status_code == 404
    assert await c.sql.rows() == before


@pytest.mark.parametrize("operation", ["read", "cancel", "confirm"])
async def test_disabled_user_stale_real_login_token_is_not_authority(
    context: Context, operation: str
) -> None:
    c = context
    plan = await c.plan()
    actor = c.sql.installer if operation == "confirm" else c.sql.creator
    async with c.sql.factory() as session, session.begin():
        await session.execute(
            update(UserRow).where(UserRow.id == actor.user_id).values(disabled=True)
        )
    before = await c.sql.rows()
    url = f"{c.prefix}/{plan['handover_id']}"
    if operation == "read":
        response = await c.client.get(url, headers=c.creator_headers)
    elif operation == "cancel":
        response = await c.client.post(
            url + "/cancel",
            headers=c.creator_headers,
            json={"expected_version": 1, "plan_hash": plan["plan_hash"]},
        )
    else:
        response = await c.confirm(plan)
    assert response.status_code == 403
    assert await c.sql.rows() == before
    assert not c.sql.canonical.exists()


async def test_demoted_installer_stale_token_cannot_confirm(context: Context) -> None:
    c = context
    plan = await c.plan()
    async with c.sql.factory() as session, session.begin():
        await session.execute(
            update(UserRow)
            .where(UserRow.id == c.sql.installer.user_id)
            .values(role=Role.OPERATOR.value)
        )
    before = await c.sql.rows()
    assert (await c.confirm(plan)).status_code == 403
    assert await c.sql.rows() == before and not c.sql.canonical.exists()


async def test_creator_can_cancel_and_read_historical_receipt(context: Context) -> None:
    c = context
    plan = await c.plan()
    response = await c.client.post(
        f"{c.prefix}/{plan['handover_id']}/cancel",
        headers=c.creator_headers,
        json={"expected_version": 1, "plan_hash": plan["plan_hash"]},
    )
    assert response.status_code == 200 and response.json()["status"] == "cancelled"
    historical = await c.client.get(f"{c.prefix}/{plan['handover_id']}", headers=c.creator_headers)
    assert historical.status_code == 200 and historical.json() == response.json()
    assert (await c.confirm(plan)).status_code == 409
    assert not c.sql.canonical.exists()


async def test_creator_cannot_install_and_unknown_key_cannot_be_trusted(
    context: Context,
) -> None:
    c = context
    plan = await c.plan()
    before = await c.sql.rows()
    assert (await c.confirm(plan, creator=True)).status_code == 403
    assert (await c.confirm(plan, key="not-a-trusted-key")).status_code == 503
    assert await c.sql.rows() == before and not c.sql.canonical.exists()


async def test_wrong_run_and_unknown_plan_do_not_disclose_receipt(context: Context) -> None:
    c = context
    plan = await c.plan()
    wrong_run = await c.client.get(
        f"/api/v1/runs/{uuid4()}/plugin-handovers/{plan['handover_id']}",
        headers=c.creator_headers,
    )
    unknown = await c.client.get(f"{c.prefix}/gh-{'0' * 64}", headers=c.creator_headers)
    assert wrong_run.status_code == unknown.status_code == 404
    assert wrong_run.json() == unknown.json()
