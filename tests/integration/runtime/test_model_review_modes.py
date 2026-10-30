"""Actual native adapter/PG/Redis/SDK composition, with an honest routing gate.

No adapter is replaced by a Leaf/Composite. Standard fresh workspace submission
currently routes write_text as inbound, so this candidate DOES NOT claim the
unreachable model-required/Root-await/UNKNOWN-review acceptance is complete.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import shutil
import tempfile
from pathlib import Path

import pytest
import pytest_asyncio
from redis.asyncio import Redis
from sqlalchemy import select

from agent_hub.capabilities.types import CapabilityRequest
from agent_hub.db.models import RunAdmissionAttemptRow, RunReviewIntentRow
from agent_hub.domain.runs import TaskMode
from agent_hub.models.run_admission_repository import RunAdmissionRepository
from agent_hub.runtime.autogen.adapter import AutoGenDiscussionRuntime
from agent_hub.runtime.contracts import EventKind
from agent_hub.runtime.crew.adapter import CrewAIObjectFactory, CrewDispatchRuntime
from agent_hub.runtime.direct import DirectRuntime
from agent_hub.runtime.hybrid import HybridRuntime
from agent_hub.runtime.worker_execution import worker_runtime_events
from tests.integration.capabilities.test_review_repository import (
    database_url as database_url,  # noqa: PLC0414 - dedicated URL identity fixture
)
from tests.integration.models.test_run_admission import (
    storage as storage,  # noqa: PLC0414 - actual PG transactions and identity checks
)
from tests.integration.runtime.test_model_review_modes_helper import (
    FACTORIES,
    FINAL_TEXT,
    FixedWire,
    build_case,
    schema_value,
    submission_request,
    submitted_input_scope,
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="module")]


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def own_redis():
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


async def collect(case):
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


async def capacity_is_clean(case):
    for capacity in case.capacities:
        for scope in ("fixture-primary", "fixture-reviewer"):
            assert await case.redis.zcard(capacity._keys(scope)["leases"]) == 0


async def test_native_sources_are_bound_to_current_checkout():
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


async def test_protocol_responder_preserves_exact_reviewer_digest():
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
    assert "message" not in scope["selection"]


@pytest.mark.parametrize(
    "mode", [TaskMode.DIRECT, TaskMode.DISPATCH, TaskMode.DISCUSS, TaskMode.HYBRID]
)
async def test_actual_native_modes_share_original_pool_and_deliver_chat(
    storage, own_redis, tmp_path, mode
):
    """Real native smoke: ordinary chat, zero reviewer; not model-required proof."""
    case = await build_case(storage, tmp_path, mode, own_redis)
    try:
        evidence = await case.repository.load_authenticated_input(
            case.context.tenant_id, case.context.run_id
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
        pool = await RunAdmissionRepository(case.factory).get_pool(
            case.context.tenant_id, case.context.run_id
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
        assert all(
            attempt.worker_lease_token == case.context.execution_token
            and attempt.worker_id == case.binding["lease"].worker_id
            for attempt in attempts
        )
        assert not reviews
        await capacity_is_clean(case)
    finally:
        case.wire.release.set()
        await case.runtime.cancel_run_owned(case.context.run_id, case.context.execution_token)


async def test_standard_fresh_workspace_write_is_inbound_not_model_required(
    storage, own_redis, tmp_path
):
    """Persisted standard route documents the source blocker, without deleting conversation_id."""
    case = await build_case(storage, tmp_path, TaskMode.DIRECT, own_redis)
    context = case.context
    request = CapabilityRequest(
        tenant_id=context.tenant_id,
        user_id=context.actor_id,
        run_id=context.run_id,
        agent_id="main_agent",
        capability="file",
        operation="write",
        resource="workspace/current",
        arguments={"path": "native.txt", "content": "Exact native write.\n"},
        idempotency_key="native-write",
    )
    policy, inbound = await case.stack.policy_gateway._current_review_policy(
        request, context.actor_role
    )
    assert context.routing_decision["conversation_id"]
    assert inbound is True
    assert policy.evaluate(request, context.actor_role).route == "allowed"
    assert case.wire.reviewer_calls == 0 and not case.wire.calls


async def test_actual_direct_cancel_retains_unknown_hold_and_releases_capacity(
    storage, own_redis, tmp_path
):
    """Representative actual adapter failure, not UNKNOWN reviewer recovery proof."""
    case = await build_case(storage, tmp_path, TaskMode.DIRECT, own_redis)
    case.wire.block_primary = True
    task = asyncio.create_task(collect(case))
    try:
        await asyncio.wait_for(case.wire.entered.wait(), timeout=20)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        pool = await RunAdmissionRepository(case.factory).get_pool(
            case.context.tenant_id, case.context.run_id
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
        await case.runtime.cancel_run_owned(case.context.run_id, case.context.execution_token)
