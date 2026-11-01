"""Actual native adapter/PG/Redis/SDK composition, with an honest routing gate.

No adapter is replaced by a Leaf/Composite. Standard fresh workspace submission
currently routes write_text as inbound, so this candidate DOES NOT claim the
unreachable model-required/Root-await/UNKNOWN-review acceptance is complete.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import os
import shutil
import tempfile
from collections.abc import AsyncGenerator, AsyncIterator, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agent_hub.auth.models import Role
from agent_hub.capabilities.types import CapabilityRequest
from agent_hub.db.base import Base
from agent_hub.db.models import (
    RunAdmissionAttemptRow,
    RunAuthenticatedInputRow,
    RunInputAuthorizationRow,
    RunReviewIntentRow,
    RunRow,
)
from agent_hub.domain.runs import TaskMode
from agent_hub.models.capacity import (
    CapacityBackendError,
    CapacityConfigurationError,
    CapacityPool,
    CredentialDescriptor,
)
from agent_hub.models.run_admission_repository import RunAdmissionRepository, _WorkerLease
from agent_hub.runs.authorization import _persist_authenticated_input, _validate_authenticated_input
from agent_hub.runs.repository import RunRepository
from agent_hub.runs.workspace import workspace_selection
from agent_hub.runtime.autogen.adapter import AutoGenDiscussionRuntime
from agent_hub.runtime.contracts import EventKind, RunEvent, TaskContext
from agent_hub.runtime.crew.adapter import CrewAIObjectFactory, CrewDispatchRuntime
from agent_hub.runtime.defaults import TenantSecretResolver, _deployments
from agent_hub.runtime.direct import DirectRuntime
from agent_hub.runtime.hybrid import HybridRuntime
from agent_hub.runtime.worker_execution import worker_runtime_events
from tests.integration.capabilities.test_review_repository import (
    database_url as database_url,  # noqa: PLC0414 - dedicated URL identity fixture
)
from tests.integration.models.run_admission_typed_fixture import Storage
from tests.integration.models.test_run_admission import (
    storage as storage,  # noqa: PLC0414 - actual PG transactions and identity checks
)
from tests.integration.runtime.review_mode_import_closure_typed_fixture import (
    NativeCase,
    TokenPool,
    WireAttempt,
    WireReceipt,
)
from tests.integration.runtime.review_mode_import_closure_typed_fixture import (
    driver_uuid as DriverUUID,
)
from tests.integration.runtime.test_model_review_modes_helper import (
    FACTORIES,
    FINAL_TEXT,
    FixedWire,
    FixtureSecrets,
    assert_wire_receipts,
    build_case,
    build_task_context,
    capture_submission_input,
    fixture_platform,
    schema_value,
    submission_request,
    submitted_input_scope,
)
from tests.unit.harness.review_typed_fixture import StreamRuntimeStub, present
from tests.unit.runs.project_auto_repair_typed_fixture import mapping

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="module")]


@pytest.fixture(autouse=True)
def owned_crewai_storage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Own native CrewAI state for this test, restoring the environment on exit."""
    monkeypatch.setenv("AGENT_HUB_CREWAI_STORAGE_DIR", str(tmp_path / "crewai"))


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def own_redis() -> AsyncGenerator[Redis, None]:
    """No TCP, production Redis, Docker, persistent data or external broker."""
    if os.name != "posix":
        raise RuntimeError(
            "native-only fixture requires Linux redis-server/AF_UNIX; collect on Windows"
        )
    executable = shutil.which("redis-server")
    if executable is None:
        raise RuntimeError("native binding must supply redis-server; this is not a skipped PASS")
    with tempfile.TemporaryDirectory(prefix="task7a-redis-", dir="/tmp") as directory:
        socket = Path(directory) / "redis.sock"
        process = await asyncio.create_subprocess_exec(
            executable,
            "--port",
            "0",
            "--unixsocket",
            str(socket),
            "--unixsocketperm",
            "700",
            "--save",
            "",
            "--appendonly",
            "no",
            "--daemonize",
            "no",
            "--maxmemory",
            "16mb",
            "--maxmemory-policy",
            "noeviction",
            "--dir",
            directory,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        client = Redis(unix_socket_path=str(socket), socket_connect_timeout=1, socket_timeout=2)
        try:
            async with asyncio.timeout(5):
                while True:
                    if process.returncode is not None:
                        raise RuntimeError("owned redis-server exited before startup")
                    try:
                        if await client.ping():
                            break
                    except (ConnectionError, OSError):
                        pass
                    # redis raises its own connection exception during socket creation.
                    except Exception as error:
                        if type(error).__module__.startswith("redis"):
                            await asyncio.sleep(0.03)
                            continue
                        raise
                    await asyncio.sleep(0.03)
            memory = await client.config_get("maxmemory")
            assert int(next(iter(memory.values()))) == 16 * 1024 * 1024
            yield client
        finally:
            try:
                await client.aclose()
            finally:
                if process.returncode is None:
                    try:
                        process.terminate()
                    except ProcessLookupError:
                        pass
                try:
                    await asyncio.wait_for(process.wait(), timeout=5)
                except TimeoutError:
                    process.kill()
                    await process.wait()
                assert process.returncode is not None


async def collect(case: NativeCase) -> list[RunEvent]:
    events = []
    async with worker_runtime_events(
        case.runtime,
        case.context,
        worker_id=case.binding["lease"].worker_id,
        review_broker=case.broker,
    ) as stream:
        async for event in stream:
            events.append(event)
    return events


async def capacity_is_clean(case: NativeCase) -> None:
    for capacity in case.capacities:
        for scope in ("fixture-primary", "fixture-reviewer"):
            assert await case.redis.zcard(capacity._keys(scope)["leases"]) == 0


async def test_native_sources_are_bound_to_current_checkout() -> None:
    """Collector/source proof only, not proof of native execution."""
    root = Path(__file__).resolve().parents[3] / "src"
    classes = (
        *FACTORIES.values(),
        DirectRuntime,
        CrewDispatchRuntime,
        AutoGenDiscussionRuntime,
        HybridRuntime,
        CrewAIObjectFactory,
    )
    observed = {}
    for cls in classes:
        source = Path(inspect.getfile(cls)).resolve()
        assert source.is_relative_to(root.resolve()), (cls, source)
        assert "Leaf" not in cls.__qualname__ and "Composite" not in cls.__qualname__
        observed[cls.__name__] = str(source)
    print(json.dumps(observed, sort_keys=True))


async def test_protocol_responder_preserves_exact_reviewer_digest() -> None:
    """Protocol fixture only: no DB/native acceptance is claimed for this node."""
    digest = "a" * 64
    keys = ("schema_version", "decision", "risk", "reason_code", "rationale", "action_digest")
    payload = {
        "messages": [{"role": "user", "content": json.dumps({"action_digest": digest})}],
        "response_format": {"json_schema": {"schema": {"properties": {key: {} for key in keys}}}},
    }
    wire = FixedWire()
    result = json.loads(wire.text_for(payload))
    assert set(result) == set(keys) and result["action_digest"] == digest
    assert result["decision"] == "allow" and wire.reviewer_calls == 1
    assert schema_value(
        {
            "type": "object",
            "properties": {"status": {"enum": ["done", "blocked"]}},
            "required": ["status"],
        }
    ) == {"status": "done"}
    pending = submission_request(pending=True)
    assert pending.conversation_id and pending.project_id and pending.workspace_session_id
    assert pending.permission_mode == "auto_review" and pending.sandbox_profile == "workspace_write"
    assert pending.requested_permissions == ("workspace.read", "command.run")
    scope = submitted_input_scope(pending)
    assert scope["conversation_id"] == pending.conversation_id
    assert "message" not in mapping(scope["selection"])


async def test_protocol_fixture_capture_accepts_driver_uuid_without_changing_authority() -> None:
    """No PG: exercise actual asyncpg UUID, ORM row and strict sealed capture."""
    actor, tenant = uuid4(), uuid4()
    row = RunRow(
        id=DriverUUID(str(uuid4())),
        tenant_id=DriverUUID(str(tenant)),
        actor_id=DriverUUID(str(actor)),
        actor_role=Role.OPERATOR.value,
        status="running",
        version=1,
        created_at=datetime.now(UTC),
    )
    lease = _WorkerLease("fixture-driver", uuid4())
    row.worker_id, row.worker_lease_token = lease.worker_id, DriverUUID(str(lease.token))

    class RecordingSession(AsyncSession):
        """Persistence protocol only, no SQL or native PG claim."""

        def __init__(self) -> None:
            super().__init__()
            self.rows: list[Base] = []

        def add(self, instance: object, _warn: bool = True) -> None:
            assert isinstance(instance, Base)
            self.rows.append(instance)

        async def flush(self, objects: Sequence[object] | None = None) -> None:
            pass

        async def get(self, entity: object, ident: object, **kwargs: object) -> None:
            model, key = entity, ident
            assert model is RunInputAuthorizationRow and key == row.id

    assert isinstance(row.actor_id, UUID) and type(row.actor_id) is not UUID
    for mode, pending in [*((mode, False) for mode in FACTORIES), (TaskMode.DIRECT, True)]:
        body = submission_request(mode, pending=pending)
        row.request, row.mode = body.message, body.mode.value
        selection = workspace_selection(
            project_id=body.project_id,
            session_id=body.workspace_session_id,
            sandbox_profile=body.sandbox_profile,
            requested_permissions=body.requested_permissions,
            permission_mode=body.permission_mode,
        )
        row.routing_decision = {
            **selection.routing_payload(),
            "conversation_id": body.conversation_id,
        }
        evidence = capture_submission_input(row, body)
        _validate_authenticated_input(evidence, tenant, actor, Role.OPERATOR, body.message)
        assert type(evidence.actor_id) is UUID and type(evidence.tenant_id) is UUID
        assert evidence.actor_id == row.actor_id and evidence.tenant_id == row.tenant_id
        assert evidence.actor_role is Role.OPERATOR
        assert evidence.message == body.message
        assert json.loads(evidence.input_scope) == submitted_input_scope(body)
        assert body.conversation_id and body.project_id and body.workspace_session_id
        session = RecordingSession()
        await _persist_authenticated_input(session, row, evidence)
        saved, link = session.rows
        assert type(saved) is RunAuthenticatedInputRow and type(link) is RunInputAuthorizationRow
        assert saved.turn_id == link.turn_id == evidence.turn_id
        assert saved.message == body.message and saved.input_scope == evidence.input_scope
        record = RunRepository._record(row)
        context = build_task_context(record, {"lease": lease}, body, row.routing_decision)
        assert context.execution_token is lease.token  # Use the driver, not the DB token.
        assert context.mode is mode and context.request == body.message
        assert context.actor_id == actor and context.actor_role is Role.OPERATOR
        assert type(context.run_id) is UUID and type(context.tenant_id) is UUID
    row.actor_role = Role.VIEWER.value
    evidence = capture_submission_input(row, submission_request())
    assert evidence.actor_role is Role.VIEWER  # No role upgrade hidden in normalization.


async def test_protocol_fixture_credentials_match_actual_capacity_contract() -> None:
    """Exercise the real capacity credential registry before any Redis/PG setup."""
    tenant = uuid4()
    secrets = FixtureSecrets()
    deployments = _deployments(fixture_platform())

    async def fingerprint(reference: str) -> str:
        return await secrets.fingerprint(tenant, reference)

    class RedisBoundary:
        calls = 0

        async def eval(self, *args: object) -> None:
            self.calls += 1
            raise OSError("protocol test stops at the Redis boundary")

    boundary = RedisBoundary()
    capacity = CapacityPool(boundary, deployments=deployments, fingerprint_resolver=fingerprint)
    registry = await capacity._resolved_credentials(deployments)
    identities: dict[str, str] = {}
    for deployment in deployments:
        value = present(registry.fingerprint_for(deployment.secret_ref))
        CredentialDescriptor(deployment.secret_ref, value)
        assert value == await secrets.fingerprint(tenant, deployment.secret_ref)
        previous = identities.setdefault(value, deployment.quota_scope_id)
        assert previous == deployment.quota_scope_id, "one credential cannot claim two quota scopes"
        assert await TenantSecretResolver(secrets, tenant).resolve(deployment.secret_ref)
        secret = await secrets.resolve(tenant, deployment.secret_ref)
        assert value == hashlib.sha256(f"{tenant}:{secret}".encode()).hexdigest()
    assert len(identities) == 2
    with pytest.raises(CapacityBackendError):
        await capacity._register_metadata(
            catalog_by_id={d.id: d for d in deployments},
            scope_policies=capacity._scope_policies,
            prior_registration_current=False,
        )
    assert boundary.calls > 0  # The actual pre-register checks reached the no-Redis boundary.
    boundary.calls = 0
    conflicting = (deployments[0], replace(deployments[1], secret_ref=deployments[0].secret_ref))
    with pytest.raises(CapacityConfigurationError, match="fingerprint quota scope conflicts"):
        await capacity._register_metadata(
            catalog_by_id={d.id: d for d in conflicting},
            scope_policies=capacity._scope_policies,
            prior_registration_current=False,
        )
    assert boundary.calls == 0


async def test_protocol_receipt_rejects_usage_drift_even_with_valid_digest() -> None:
    attempt_id, physical = uuid4(), uuid4()
    sent: WireAttempt = {
        "request_sha256": "a" * 64,
        "purpose": "primary",
        "physical_transport_id": str(physical),
        "deployment": {
            "provider_id": "openai",
            "deployment_id": "primary_1",
            "model_id": "openai/fixture-primary",
        },
        "usage": {
            "prompt_tokens": 2,
            "completion_tokens": 3,
            "total_tokens": 5,
            "cost_usd": {"decimal": "0.000008"},
        },
    }
    receipt: WireReceipt = {
        "outcome": "completed",
        "deployment": sent["deployment"],
        "attempt_id": str(attempt_id),
        "physical_transport_id": str(physical),
        "usage": sent["usage"],
    }
    row = RunAdmissionAttemptRow(
        attempt_id=attempt_id,
        physical_transport_id=physical,
        state="completed",
        purpose="primary",
        request_sha256=sent["request_sha256"],
        deployment=sent["deployment"],
        receipt=receipt,
        receipt_sha256=hashlib.sha256(
            json.dumps(receipt, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
    )
    pool = TokenPool(token_charged=5)
    assert_wire_receipts([row], {attempt_id: sent}, pool)
    row.receipt = {**receipt, "usage": {**sent["usage"], "total_tokens": 999}}
    row.receipt_sha256 = hashlib.sha256(
        json.dumps(row.receipt, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    with pytest.raises(AssertionError):
        assert_wire_receipts([row], {attempt_id: sent}, pool)


@pytest.mark.parametrize(
    "mode", [TaskMode.DIRECT, TaskMode.DISPATCH, TaskMode.DISCUSS, TaskMode.HYBRID]
)
async def test_actual_native_modes_share_original_pool_and_deliver_chat(
    storage: Storage, own_redis: Redis, tmp_path: Path, mode: TaskMode
) -> None:
    """Real native smoke: ordinary chat, zero reviewer; not model-required proof."""
    case = await build_case(storage, tmp_path, mode, own_redis)
    try:
        evidence = present(
            await case.repository.load_authenticated_input(
                case.context.tenant_id, case.context.run_id
            )
        )
        assert evidence.message == case.body.message
        assert json.loads(evidence.input_scope) == submitted_input_scope(case.body)
        assert all(
            case.context.routing_decision[key]
            for key in ("conversation_id", "project_id", "workspace_session_id")
        )
        events = await asyncio.wait_for(collect(case), timeout=75)
        assert events[-1].kind is EventKind.RUNTIME_COMPLETED, [
            (event.kind, event.reason) for event in events
        ]
        assert any(
            event.artifact is not None
            and FINAL_TEXT in json.dumps(dict(event.artifact.content), default=dict)
            for event in events
            if event.kind is EventKind.ARTIFACT_CREATED
        )
        expected = {
            TaskMode.DIRECT: {"DirectRuntime"},
            TaskMode.DISPATCH: {"CrewDispatchRuntime"},
            TaskMode.DISCUSS: {"AutoGenDiscussionRuntime"},
            TaskMode.HYBRID: {
                "HybridRuntime",
                "CrewDispatchRuntime",
                "AutoGenDiscussionRuntime",
                "DirectRuntime",
            },
        }[mode]
        assert expected <= case.wire.classes
        assert case.wire.calls and all(
            budget is case.wire.budgets[0] for budget in case.wire.budgets
        )
        assert not case.wire.budgets[0]._active
        assert case.wire.reviewer_calls == 0
        assert all(body.closed for body in case.wire.bodies)
        pool = present(
            await RunAdmissionRepository(case.factory).get_pool(
                case.context.tenant_id, case.context.run_id
            )
        )
        assert pool.original_token_grant == 10000000
        assert pool.authorization_turn_id == evidence.turn_id
        assert pool.token_charged == len(case.wire.calls) * 5
        async with case.factory() as session:
            attempts = list(
                await session.scalars(
                    select(RunAdmissionAttemptRow).where(
                        RunAdmissionAttemptRow.run_id == case.context.run_id
                    )
                )
            )
            reviews = list(
                await session.scalars(
                    select(RunReviewIntentRow).where(
                        RunReviewIntentRow.run_id == case.context.run_id
                    )
                )
            )
        assert len(attempts) == len(case.wire.calls)
        assert all(attempt.state == "completed" and attempt.receipt_sha256 for attempt in attempts)
        assert_wire_receipts(attempts, case.wire.attempts, pool)
        assert all(
            attempt.worker_lease_token == case.context.execution_token
            and attempt.worker_id == case.binding["lease"].worker_id
            for attempt in attempts
        )
        assert not reviews
        await capacity_is_clean(case)
    finally:
        case.wire.release.set()
        await case.runtime.cancel_run_owned(
            case.context.run_id, present(case.context.execution_token)
        )


async def test_standard_fresh_workspace_write_is_inbound_not_model_required(
    storage: Storage, own_redis: Redis, tmp_path: Path
) -> None:
    """Persisted standard route documents the source blocker, without deleting conversation_id."""
    case = await build_case(storage, tmp_path, TaskMode.DIRECT, own_redis)
    context = case.context
    request = CapabilityRequest(
        tenant_id=context.tenant_id,
        user_id=present(context.actor_id),
        run_id=context.run_id,
        agent_id="main_agent",
        capability="file",
        operation="write",
        resource="workspace/current",
        arguments={"path": "native.txt", "content": "Exact native write.\n"},
        idempotency_key="native-write",
    )
    policy, inbound = await case.stack.policy_gateway._current_review_policy(
        request, present(context.actor_role)
    )
    assert context.routing_decision["conversation_id"]
    assert inbound is True
    assert policy.evaluate(request, present(context.actor_role)).route == "allowed"
    assert case.wire.reviewer_calls == 0 and not case.wire.calls


async def test_actual_direct_cancel_retains_unknown_hold_and_releases_capacity(
    storage: Storage, own_redis: Redis, tmp_path: Path
) -> None:
    """Representative actual adapter failure, not UNKNOWN reviewer recovery proof."""
    case = await build_case(storage, tmp_path, TaskMode.DIRECT, own_redis)
    case.wire.block_primary = True
    task = asyncio.create_task(collect(case))
    try:
        await asyncio.wait_for(case.wire.entered.wait(), timeout=20)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        pool = present(
            await RunAdmissionRepository(case.factory).get_pool(
                case.context.tenant_id, case.context.run_id
            )
        )
        assert pool.token_charged == 131072 and pool.review_count_charged == 0
        async with case.factory() as session:
            attempts = list(
                await session.scalars(
                    select(RunAdmissionAttemptRow).where(
                        RunAdmissionAttemptRow.run_id == case.context.run_id
                    )
                )
            )
        assert len(attempts) == 1 and attempts[0].state == "unknown"
        assert not case.wire.budgets[0]._active
        await capacity_is_clean(case)
    finally:
        case.wire.release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await case.runtime.cancel_run_owned(
            case.context.run_id, present(case.context.execution_token)
        )


async def test_protocol_actual_guarded_sdk_wire_observer_matches_persisted_receipt(
    tmp_path: Path,
) -> None:
    """Real SDK/guard/DAO code, fixed HTTP and unit DB/capacity seams; no native claim."""
    import httpx

    from agent_hub.db.models import RunAdmissionAttemptRow
    from agent_hub.models.litellm_client import LiteLLMClient
    from agent_hub.runtime.defaults import _gateway_for_config
    from tests.integration.runtime.test_model_review_modes_helper import (
        FixedWire,
        assert_wire_receipts,
    )
    from tests.unit.harness.review_typed_fixture import logical_request
    from tests.unit.runtime.test_review_assembly import fixture

    f = await fixture(tmp_path)
    wire = FixedWire()
    transport = LiteLLMClient(_network_delegate_factory=lambda: httpx.MockTransport(wire))

    class Runtime(StreamRuntimeStub):
        async def run(self, context: TaskContext) -> AsyncIterator[RunEvent]:
            gateway, _, _ = await _gateway_for_config(
                f.state.platform,
                tenant_id=context.tenant_id,
                secret_service=f.secrets,
                capacity_factory=f.capacity,
                transport=transport,
            )
            await gateway.complete(logical_request())
            yield RunEvent(
                kind="runtime.wire", sequence=1, run_id=context.run_id, payload={"ok": True}
            )

    async with worker_runtime_events(
        Runtime(), f.s.task, worker_id=f.s.budget.lease.worker_id, review_broker=f.broker
    ) as events:
        await anext(events)
    attempts = [row for row in f.db.rows if type(row) is RunAdmissionAttemptRow]
    pool = present(await RunAdmissionRepository(f.db).get_pool(f.s.task.tenant_id, f.s.task.run_id))
    assert_wire_receipts(attempts, wire.attempts, pool)
    assert len(wire.calls) == 1 and all(body.closed for body in wire.bodies)
