"""One native plain-note action plus explicitly local protocol checks.

Only the native selector exercises actual PG/Redis/worker/broker composition.
The without_pg nodes use explicit local storage/capacity boundaries. All paths
retain actual factory/adapter, Gateway, SDK, Root/backend and Harness classes;
provider responses are final MockTransport fixtures, never execution grants.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import sys
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Unpack
from uuid import UUID, uuid4

import httpx
import pytest
from redis.asyncio import Redis
from sqlalchemy import select

from agent_hub.api.routers.runs import CreateRunRequest
from agent_hub.auth.models import AuthenticatedPrincipal, Role
from agent_hub.capabilities.defaults import build_runtime_capability_stack
from agent_hub.config.repository import ConfigRevision
from agent_hub.db.models import RunAdmissionAttemptRow, RunReviewEventRow, RunReviewIntentRow
from agent_hub.domain.runs import RunStatus, TaskMode
from agent_hub.files.workspace import ProjectWorkspaceStore
from agent_hub.harness.types import HarnessToolCallRequest, HarnessToolCallResult
from agent_hub.models.litellm_client import LiteLLMClient
from agent_hub.models.physical_admission import _call as physical_call
from agent_hub.models.physical_budget import _BudgetAdmission
from agent_hub.models.run_admission_repository import RunAdmissionRepository
from agent_hub.models.types import Deployment
from agent_hub.runs.authorization import _capture_authenticated_input
from agent_hub.runs.repository import RunRecord
from agent_hub.runs.service import _project_delivery_assessment
from agent_hub.runs.workspace import workspace_selection
from agent_hub.runtime.artifacts import InMemoryArtifactRepository
from agent_hub.runtime.contracts import Artifact, EventKind, RunEvent, TaskContext, _freeze_object
from agent_hub.runtime.defaults import ConfigBackedDirectRuntime
from agent_hub.runtime.direct import DirectRuntime, RuntimeExecutionError
from agent_hub.runtime.review_assembly import runtime_source_fingerprint
from agent_hub.runtime.streams import closing_runtime_events
from tests.integration.capabilities.test_review_repository import (
    database_url as database_url,  # noqa: PLC0414
)
from tests.integration.models.run_admission_typed_fixture import Storage
from tests.integration.models.test_run_admission import storage as storage  # noqa: PLC0414
from tests.integration.runtime.review_actions_typed_fixture import (
    AccountingRow,
    AccountingSent,
    ConfigFixture,
    InvokeArguments,
    NoteWireCase,
    NoteWireRecord,
    PurposeAccountingSent,
    WireOwner,
    config_revision,
)
from tests.integration.runtime.test_model_review_modes import (
    capacity_is_clean,
    collect,
)
from tests.integration.runtime.test_model_review_modes import (
    own_redis as own_redis,  # noqa: PLC0414
)
from tests.integration.runtime.test_model_review_modes_helper import (
    ClosedBody,
    FixedWire,
    FixtureSecrets,
    build_case,
    fixture_platform,
    submitted_input_scope,
)
from tests.unit.harness.review_typed_fixture import present
from tests.unit.runs.project_auto_repair_typed_fixture import mapping, sequence, string
from tests.unit.runtime.test_plain_file_action_helper import (
    LocalCapacityBoundary,
    plain_note_submission,
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="module")]


@pytest.mark.parametrize("reply_kind", ["capability_calls_text", "native_tool_calls"])
async def test_plain_note_direct_protocol_blocker_without_pg(
    tmp_path: Path, reply_kind: str
) -> None:
    """Observe real factory/SDK execution, not source-text assertions or fake tools."""
    body, path, content = plain_note_submission()
    assert _project_delivery_assessment(body.message) is None
    assert (
        body.permission_mode == "auto_review"
        and "workspace.write" not in body.requested_permissions
    )
    principal = AuthenticatedPrincipal(uuid4(), uuid4(), Role.OPERATOR)
    evidence = _capture_authenticated_input(principal, body.message, submitted_input_scope(body))
    selection = workspace_selection(
        project_id=body.project_id,
        session_id=present(body.workspace_session_id),
        sandbox_profile=body.sandbox_profile,
        requested_permissions=body.requested_permissions,
        permission_mode=body.permission_mode,
    )
    route = {
        **selection.routing_payload(),
        "conversation_id": body.conversation_id,
        "execution_backend": body.execution_backend,
    }
    record = RunRecord(
        uuid4(),
        principal.tenant_id,
        principal.user_id,
        body.message,
        TaskMode.DIRECT,
        RunStatus.RUNNING,
        1,
        datetime.now(UTC),
        route,
        actor_role=principal.role,
    )

    class RecordReader:
        async def get(self, tenant: UUID, run: UUID) -> RunRecord:
            assert (tenant, run) == (record.tenant_id, record.id)
            return record

    stack = build_runtime_capability_stack(
        tenant_id=record.tenant_id,
        run_repository=RecordReader(),
        skill_store_dir=tmp_path / "skills",
        workspace_root=tmp_path,
        project_workspace_dir=tmp_path,
        artifact_repository=InMemoryArtifactRepository(),
    )
    harness_calls: list[tuple[tuple[UUID, HarnessToolCallRequest], InvokeArguments]] = []
    actual_invoke = stack.harness_tool_gateway.invoke

    async def observe_harness(
        tenant_id: UUID, request: HarnessToolCallRequest, **kwargs: Unpack[InvokeArguments]
    ) -> HarnessToolCallResult:
        call = request
        harness_calls.append(((tenant_id, call), kwargs))
        return await actual_invoke(tenant_id, call, **kwargs)

    pytest.MonkeyPatch().setattr(stack.harness_tool_gateway, "invoke", observe_harness)
    owner = WireOwner()
    wire: list[Mapping[str, object]] = []
    tool = {
        "id": "plain-note-write",
        "type": "function",
        "function": {
            "name": "workspace.write_text",
            "arguments": json.dumps({"path": path, "content": content}),
        },
    }
    text_reply = json.dumps(
        {
            "capability_calls": [
                {"name": "workspace.write_text", "arguments": {"path": path, "content": content}}
            ]
        }
    )

    async def transport(request: httpx.Request) -> httpx.Response:
        payload = mapping(json.loads(request.content))
        wire.append(payload)
        assert path in json.dumps(payload) and json.dumps(content) in body.message
        assert not payload.get("stream"), "update diagnostic for the actual streaming protocol"
        message = (
            {"role": "assistant", "content": text_reply}
            if reply_kind == "capability_calls_text"
            else {"role": "assistant", "content": None, "tool_calls": [tool]}
        )
        response = {
            "id": "plain-note-probe",
            "object": "chat.completion",
            "created": 1,
            "model": payload["model"],
            "choices": [
                {
                    "index": 0,
                    "message": message,
                    "finish_reason": "stop"
                    if reply_kind == "capability_calls_text"
                    else "tool_calls",
                }
            ],
            "usage": {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5},
        }
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            stream=ClosedBody(json.dumps(response).encode(), owner),
        )

    platform = fixture_platform()

    class Configs(ConfigFixture):
        async def get_current(self, tenant: UUID) -> ConfigRevision:
            assert tenant == record.tenant_id
            return config_revision(tenant, platform)

    capacities: list[LocalCapacityBoundary] = []

    async def capacity(tenant: UUID, deployments: Sequence[Deployment]) -> LocalCapacityBoundary:
        value = LocalCapacityBoundary()
        capacities.append(value)
        return value

    runtime = ConfigBackedDirectRuntime(
        config_service=Configs(),
        secret_service=FixtureSecrets(),
        capacity_factory=capacity,
        transport=LiteLLMClient(_network_delegate_factory=lambda: httpx.MockTransport(transport)),
        capability_gateway=stack.runtime_gateway,
        harness_tool_gateway=stack.harness_tool_gateway,
        action_capability_gateway=stack.runtime_gateway,
        action_harness_tool_gateway=stack.harness_tool_gateway,
        artifact_repository=InMemoryArtifactRepository(),
    )
    context = TaskContext(
        run_id=record.id,
        tenant_id=record.tenant_id,
        actor_id=record.actor_id,
        actor_role=record.actor_role,
        execution_token=uuid4(),
        mode=TaskMode.DIRECT,
        request=body.message,
        routing_decision=_freeze_object(route, name="routing decision"),
        timeout_seconds=30,
        token_budget=1000000,
    )
    assert type(await runtime._runtime_for(context)) is DirectRuntime
    assert evidence.message == body.message and json.loads(
        evidence.input_scope
    ) == submitted_input_scope(body)
    events: list[RunEvent] = []
    try:
        async with closing_runtime_events(runtime.run(context)) as stream:
            async for event in stream:
                events.append(event)
    except RuntimeExecutionError as error:
        assert reply_kind == "native_tool_calls" and str(error) == "model response is unsupported"
    else:
        assert reply_kind == "capability_calls_text"
        assert events[-1].kind is EventKind.RUNTIME_COMPLETED
        texts = [
            present(event.artifact).content["text"]
            for event in events
            if event.kind is EventKind.ARTIFACT_CREATED and present(event.artifact).type == "text"
        ]
        assert texts == [text_reply]  # A request printed as text is not a file action.
    assert wire and all(not payload.get("tools") for payload in wire)
    assert harness_calls == []
    assert not ProjectWorkspaceStore(tmp_path).list_files(
        record.tenant_id, present(body.project_id), present(body.workspace_session_id)
    )
    assert all(capacity.acquired == capacity.released for capacity in capacities)
    assert all(response.closed for response in owner.bodies)
    print(
        json.dumps(
            {
                "scope": "local-protocol-only",
                "reply_kind": reply_kind,
                "factory": type(runtime).__name__,
                "adapter": "DirectRuntime",
                "wire_calls": len(wire),
                "harness_calls": len(harness_calls),
                "file_written": False,
                "pg_redis_worker_broker": "not exercised",
            }
        )
    )


def _install_note_wire(
    case: NoteWireCase, body: CreateRunRequest, path: str, content: str
) -> list[Mapping[str, object]]:
    proposals: list[Mapping[str, object]] = []

    def reply(payload: Mapping[str, object]) -> str:
        call = physical_call.get()
        assert call is not None and call.attempts
        attempt = call.attempts[-1]
        admission = call.scope._admission
        assert isinstance(admission, _BudgetAdmission)
        case.wire.attempts[attempt.permit.attempt_id]["physical_transport_id"] = str(
            admission._attempts[attempt.permit.attempt_id].physical_id
        )
        schema = mapping(
            mapping(mapping(payload.get("response_format", {})).get("json_schema", {})).get(
                "schema", {}
            )
        )
        if set(mapping(schema.get("properties", {}))) == {
            "schema_version",
            "decision",
            "risk",
            "reason_code",
            "rationale",
            "action_digest",
        }:
            material = mapping(
                json.loads(string(mapping(sequence(payload["messages"])[-1])["content"]))
            )
            assert mapping(material["authenticated_authorization"])["message"] == body.message
            assert mapping(mapping(material["exact_action"])["request"])["arguments"] == {
                "path": path,
                "content": content,
            }
            return FixedWire.text_for(case.wire, payload)
        assert any(
            "PLAIN_FILE_ACTION:" in str(message.get("content", ""))
            for raw_message in sequence(payload["messages"])
            for message in (mapping(raw_message),)
        ), "actual Direct protocol is not plain-file capable"
        assert not payload.get("tools")
        proposals.append(payload)
        return json.dumps(
            {
                "workspace_batch": {"files": {path: content}, "complete": True, "continuation": ""},
                "summary": f"Saved {path}.",
            }
        )

    pytest.MonkeyPatch().setattr(case.wire, "text_for", reply)
    return proposals


def _assert_accounting_receipt(row: AccountingRow, sent: AccountingSent) -> None:
    """Compare durable accounting against the guarded wire for either purpose."""
    expected = {
        "outcome": "completed",
        "deployment": sent["deployment"],
        "attempt_id": str(row.attempt_id),
        "physical_transport_id": sent["physical_transport_id"],
        "usage": sent["usage"],
    }
    assert row.receipt == expected
    assert (
        row.receipt_sha256
        == hashlib.sha256(
            json.dumps(
                expected, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
            ).encode()
        ).hexdigest()
    )


@pytest.mark.parametrize("purpose", ["primary", "review"])
async def test_plain_note_accounting_receipt_rejects_rehashed_identity_drift(purpose: str) -> None:
    """Pure assertion check: a valid hash must not hide incorrect receipt identity."""
    attempt_id, physical_id = uuid4(), uuid4()
    sent: PurposeAccountingSent = {
        "purpose": purpose,
        "physical_transport_id": str(physical_id),
        "deployment": {
            "provider_id": "fixture",
            "deployment_id": f"fixture-{purpose}",
            "model_id": f"fixture/{purpose}",
        },
        "usage": {
            "prompt_tokens": 2,
            "completion_tokens": 3,
            "total_tokens": 5,
            "cost_usd": {"decimal": "0.000008"},
        },
    }
    receipt: dict[str, object] = {
        "outcome": "completed",
        "deployment": sent["deployment"],
        "attempt_id": str(attempt_id),
        "physical_transport_id": str(physical_id),
        "usage": sent["usage"],
    }

    def row_with(value: dict[str, object]) -> RunAdmissionAttemptRow:
        return RunAdmissionAttemptRow(
            attempt_id=attempt_id,
            purpose=purpose,
            deployment=sent["deployment"],
            physical_transport_id=physical_id,
            receipt=value,
            receipt_sha256=hashlib.sha256(
                json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
            ).hexdigest(),
        )

    _assert_accounting_receipt(row_with(receipt), sent)
    wrong_hash = row_with(receipt)
    wrong_hash.receipt_sha256 = "0" * 64
    with pytest.raises(AssertionError):
        _assert_accounting_receipt(wrong_hash, sent)
    for key, wrong in {
        "outcome": "cancelled",
        "attempt_id": str(uuid4()),
        "physical_transport_id": str(uuid4()),
        "deployment": {**sent["deployment"], "deployment_id": "not-the-wire-deployment"},
    }.items():
        with pytest.raises(AssertionError):
            _assert_accounting_receipt(row_with({**receipt, key: wrong}), sent)


async def test_plain_note_actual_review_wire_and_storage_without_pg(tmp_path: Path) -> None:
    """Actual service/SDK plus focused DB; not PG/Redis/worker acceptance."""
    from agent_hub.capabilities.execution_authority import bind_execution_authority
    from agent_hub.capabilities.model_review import ModelApprovalReviewService
    from agent_hub.config.schema import PlatformConfig
    from agent_hub.models.physical_budget import _bind_run_budget
    from agent_hub.runtime.defaults import _gateway_for_config
    from tests.integration.runtime.review_actions_typed_fixture import typed_note_case as note_case

    s = note_case(tmp_path)
    wire = FixedWire(runtime=s.runtime)
    case = NoteWireRecord(wire)
    proposals = _install_note_wire(case, s.body, s.path, s.content)
    transport = LiteLLMClient(_network_delegate_factory=lambda: httpx.MockTransport(wire))
    s.runtime._transport = transport
    current = await s.runtime._config_service.get_current(s.task.tenant_id)
    assert current is not None
    gateway, _, _ = await _gateway_for_config(
        PlatformConfig.model_validate(current.document),
        tenant_id=s.task.tenant_id,
        secret_service=s.runtime._secret_service,
        capacity_factory=s.runtime._capacity_factory,
        transport=transport,
        routing_decision=s.task.routing_decision,
    )
    s.gate.service = ModelApprovalReviewService(gateway, s.ledger)
    context = s.task.validated_internal_clone()
    with (
        bind_execution_authority(context, worker_id=s.budget.lease.worker_id),
        _bind_run_budget(s.budget),
    ):
        async with closing_runtime_events(s.runtime.run(context)) as stream:
            events = [event async for event in stream]
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert len(proposals) == wire.reviewer_calls == 1 and len(wire.calls) == 2
    assert wire.classes >= {"ActionApprovedDirectRuntime"}
    assert all(budget is s.budget for budget in wire.budgets)
    root = ProjectWorkspaceStore(tmp_path).session_root(s.task.tenant_id, "demo", "session")
    assert (root / s.path).read_bytes() == s.content.encode()
    intents = [row for row in s.db.rows if type(row) is RunReviewIntentRow]
    assert len(intents) == 1 and intents[0].consumed
    assert s.db.pool.token_charged == 10 and s.db.pool.review_token_charged == 5
    attempts = [row for row in s.db.rows if type(row) is RunAdmissionAttemptRow]
    assert {row.purpose for row in attempts} == {"primary", "review"}
    assert {row.attempt_id for row in attempts} == set(wire.attempts)
    for row in attempts:
        sent = wire.attempts[row.attempt_id]
        assert row.request_sha256 == sent["request_sha256"] and row.deployment == sent["deployment"]
        assert str(row.physical_transport_id) == sent["physical_transport_id"]
        _assert_accounting_receipt(row, sent)
    (review_attempt,) = [row for row in attempts if row.purpose == "review"]
    receipt = json.loads(present(intents[0].private_receipt))
    assert receipt["accounting_receipt_sha256"] == review_attempt.receipt_sha256
    assert receipt["evidence"]["usage"] == present(review_attempt.receipt)["usage"]
    assert receipt["evidence"]["model_identity"] == review_attempt.deployment
    assert (
        hashlib.sha256(present(intents[0].private_receipt).encode()).hexdigest()
        == intents[0].receipt_sha256
    )
    assert all(response.closed for response in wire.bodies)


def _direct_delivery_metadata(artifact: Artifact, path: str) -> dict[str, str | int]:
    text = artifact.content.get("text")
    assert artifact.type == "text" and artifact.producer == "main"
    assert isinstance(text, str) and text.strip() and not text.lstrip().startswith("{")
    assert re.search(
        r"(?<![\w/.-])" + re.escape(path) + r"(?=$|[\s\x60\"'，。,:;!?)]|\.(?:$|\s))", text
    ), "final delivery must name the exact saved filename"
    assert re.search(r"[A-Za-z\u4e00-\u9fff]{2,}", text.replace(path, ""))
    return {
        "artifact_id": str(artifact.id),
        "artifact_sha256": artifact.content_sha256,
        "text_sha256": hashlib.sha256(text.encode()).hexdigest(),
        "text_bytes": len(text.encode()),
        "filename_sha256": hashlib.sha256(path.encode()).hexdigest(),
    }


class _DirectWriteAudit:
    """Observe real writable opens of the exact target, without replacing IO."""

    def __init__(self, target: Path) -> None:
        self.target = os.path.normcase(str(target.resolve()))
        self.count, self.active = 0, True

    def __call__(self, event: str, args: tuple[object, ...]) -> None:
        if (
            self.active
            and event == "open"
            and isinstance(args[0], (str, bytes))
            and os.path.normcase(os.path.abspath(os.fsdecode(args[0]))) == self.target
            and type(args[2]) is int
            and args[2] & (os.O_WRONLY | os.O_RDWR)
        ):
            self.count += 1


@pytest.mark.parametrize("text", ["{}", "Saved other.txt.", "Saved other-note.txt.", "note.txt"])
async def test_direct_delivery_metadata_rejects_unreadable_or_wrong_filename(text: object) -> None:
    artifact = Artifact(
        id=uuid4(),
        type="text",
        producer="main",
        content=_freeze_object({"text": text}, name="artifact content"),
    )
    with pytest.raises(AssertionError):
        _direct_delivery_metadata(artifact, "note.txt")


async def test_direct_delivery_metadata_contains_only_actual_artifact_hashes() -> None:
    artifact = Artifact(
        id=uuid4(), type="text", producer="main", content={"text": "Saved note.txt."}
    )
    facts = _direct_delivery_metadata(artifact, "note.txt")
    assert facts == {
        "artifact_id": str(artifact.id),
        "artifact_sha256": artifact.content_sha256,
        "text_sha256": hashlib.sha256(b"Saved note.txt.").hexdigest(),
        "text_bytes": 15,
        "filename_sha256": hashlib.sha256(b"note.txt").hexdigest(),
    }
    assert "Saved" not in json.dumps(facts) and "note.txt" not in json.dumps(facts)


async def test_direct_write_audit_metadata_counts_only_target_writes(tmp_path: Path) -> None:
    target = tmp_path / "note.txt"
    audit = _DirectWriteAudit(target)
    sys.addaudithook(audit)
    try:
        target.write_bytes(b"first")
        assert audit.count == 1
        assert target.read_bytes() == b"first"
        (tmp_path / "other.txt").write_bytes(b"other")
        assert audit.count == 1
    finally:
        audit.active = False
    target.write_bytes(b"after")
    assert audit.count == 1


async def test_native_direct_plain_note_review_consumes_once_and_writes(
    storage: Storage,
    own_redis: Redis,
    tmp_path: Path,
    request: pytest.FixtureRequest,
    record_property: Callable[[str, object], None],
) -> None:
    """One actual PG/Redis/worker/broker/factory/Harness action, fixed wire only."""
    body, path, content = plain_note_submission()
    case = await build_case(storage, tmp_path, TaskMode.DIRECT, own_redis, body=body)
    proposals = _install_note_wire(case, body, path, content)
    source_sha = runtime_source_fingerprint()
    root = ProjectWorkspaceStore(tmp_path).session_root(
        case.context.tenant_id, present(body.project_id), present(body.workspace_session_id)
    )
    audit = _DirectWriteAudit(root / path)
    sys.addaudithook(audit)
    invoked: list[tuple[HarnessToolCallRequest, HarnessToolCallResult]] = []
    original_invoke = case.stack.harness_tool_gateway.invoke

    async def observe(
        tenant_id: UUID, request: HarnessToolCallRequest, **kwargs: Unpack[InvokeArguments]
    ) -> HarnessToolCallResult:
        call = request
        result = await original_invoke(tenant_id, call, **kwargs)
        if call.tool_name == "workspace.write_text":
            invoked.append((call, result))
        return result

    pytest.MonkeyPatch().setattr(case.stack.harness_tool_gateway, "invoke", observe)
    try:
        evidence = await case.repository.load_authenticated_input(
            case.context.tenant_id, case.context.run_id
        )
        assert evidence is not None
        assert evidence.message == body.message and json.loads(
            evidence.input_scope
        ) == submitted_input_scope(body)
        events = await asyncio.wait_for(collect(case), timeout=75)
        assert events[-1].kind is EventKind.RUNTIME_COMPLETED
        assert {"ActionApprovedDirectRuntime"} <= case.wire.classes
        assert len(proposals) == case.wire.reviewer_calls == 1
        assert len(case.wire.calls) == 2 and all(
            budget is case.wire.budgets[0] for budget in case.wire.budgets
        )
        assert not case.wire.budgets[0]._active
        root = ProjectWorkspaceStore(tmp_path).session_root(
            case.context.tenant_id, present(body.project_id), present(body.workspace_session_id)
        )
        assert (root / path).read_bytes() == content.encode()
        files = ProjectWorkspaceStore(tmp_path).list_files(
            case.context.tenant_id, present(body.project_id), present(body.workspace_session_id)
        )
        assert [item.path for item in files] == [path]
        digest = hashlib.sha256(content.encode()).hexdigest()
        assert files[0].sha256 == digest
        text_artifacts = [
            event.artifact
            for event in events
            if event.kind is EventKind.ARTIFACT_CREATED
            and event.artifact is not None
            and event.artifact.type == "text"
        ]
        assert len(text_artifacts) == 1 and path in string(text_artifacts[0].content["text"])
        assert (
            mapping(sequence(text_artifacts[0].content["workspace_files"])[0])["sha256"] == digest
        )
        final_delivery = _direct_delivery_metadata(text_artifacts[0], path)
        assert events[-1].inputs == (text_artifacts[0],)
        assert audit.count == len(invoked) == 1
        call, result = invoked[0]
        assert result.status == "succeeded" and result.failure_reason is None
        assert call.run_id == case.context.run_id
        assert dict(call.arguments) == {"path": path, "content": content}
        assert mapping(result.payload["file"])["sha256"] == digest
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
            transitions = list(
                await session.scalars(
                    select(RunReviewEventRow)
                    .where(RunReviewEventRow.run_id == case.context.run_id)
                    .order_by(RunReviewEventRow.sequence)
                )
            )
        assert len(reviews) == 1 and reviews[0].status == "allow" and reviews[0].consumed
        assert [item.kind for item in transitions] == ["prepared", "issued", "terminal", "consumed"]
        assert {row.purpose for row in attempts} == {"primary", "review"} and len(attempts) == 2
        assert {row.attempt_id for row in attempts} == set(case.wire.attempts)
        for row in attempts:
            sent = case.wire.attempts[row.attempt_id]
            assert row.state == "completed" and row.request_sha256 == sent["request_sha256"]
            assert row.purpose == sent["purpose"] and row.deployment == sent["deployment"]
            assert str(row.physical_transport_id) == sent["physical_transport_id"]
            assert row.worker_id == case.binding["lease"].worker_id
            assert row.worker_lease_token == case.context.execution_token
            _assert_accounting_receipt(row, sent)
        (review,) = reviews
        (review_attempt,) = [row for row in attempts if row.purpose == "review"]
        assert review.attempt_id == review_attempt.attempt_id
        assert review.request_sha256 == review_attempt.request_sha256
        assert review.physical_identity == review_attempt.physical_transport_id
        receipt = json.loads(present(review.private_receipt))
        assert (
            hashlib.sha256(present(review.private_receipt).encode()).hexdigest()
            == review.receipt_sha256
        )
        assert receipt["accounting_receipt_sha256"] == review_attempt.receipt_sha256
        assert receipt["evidence"]["usage"] == present(review_attempt.receipt)["usage"]
        assert receipt["evidence"]["model_identity"] == review_attempt.deployment
        assert receipt["evidence"]["verdict"]["action_digest"] == review.action_digest
        action = json.loads(review.private_action)
        assert action["request"]["arguments"] == {"path": path, "content": content}
        assert action["normalized_effect"]["target"] == path
        assert review.authorization_turn_id == evidence.turn_id
        pool = await RunAdmissionRepository(case.factory).get_pool(
            case.context.tenant_id, case.context.run_id
        )
        assert pool is not None
        assert (
            pool.authorization_turn_id == evidence.turn_id and pool.original_token_grant == 10000000
        )
        assert (
            pool.token_charged == 10
            and pool.review_token_charged == 5
            and pool.review_count_charged == 1
        )
        assert pool.review_cost_charged == Decimal("0.000008")
        record = await case.repository.get(case.context.tenant_id, case.context.run_id)
        assert record is not None and record.routing_decision is not None
        assert record.routing_decision["requested_permissions"] == ["workspace.read", "command.run"]
        assert all(response.closed for response in case.wire.bodies)
        await capacity_is_clean(case)
        assert runtime_source_fingerprint() == source_sha
        assert pool.run_id == case.context.run_id
        primary = [row for row in attempts if row.purpose == "primary"]
        assert primary and all(row.deployment != review_attempt.deployment for row in primary)
        adapter_names = case.wire.classes & {"DirectRuntime", "ActionApprovedDirectRuntime"}
        assert len(adapter_names) == 1
        record_property(
            "review_action_composition_observed",
            json.dumps(
                {
                    "mode": case.context.mode.value,
                    "phase": "review_action_composition",
                    "selector": request.node.nodeid,
                    "runtime_source_sha256": source_sha,
                    "factory": type(case.runtime).__name__,
                    "adapter": next(iter(adapter_names)),
                    "tool": call.tool_name,
                    "review_calls": case.wire.reviewer_calls,
                    "reviewed_backend_effects": audit.count,
                    "consumed_intents": sum(int(item.consumed) for item in reviews),
                    "primary_attempts": len(primary),
                    "review_attempts": len([row for row in attempts if row.purpose == "review"]),
                    "action_digest": review.action_digest,
                    "backend_write_sha256": files[0].sha256,
                    "review_receipt_sha256": review.receipt_sha256,
                    "pool_id": str(pool.run_id),
                    "run_id": str(case.context.run_id),
                },
                sort_keys=True,
            ),
        )
        record_property(
            "review_action_assertions",
            json.dumps(
                {
                    "same_original_pool": pool.run_id == case.context.run_id,
                    "same_lease": all(
                        row.worker_id == case.binding["lease"].worker_id
                        and row.worker_lease_token == case.context.execution_token
                        for row in attempts
                    ),
                    "root_bytes_match": (root / path).read_bytes() == content.encode(),
                    "result_status": result.status,
                    "final_status": events[-1].kind.value,
                    "write_count": audit.count,
                    "final_delivery": final_delivery,
                },
                sort_keys=True,
            ),
        )
    finally:
        audit.active = False
        case.wire.release.set()
        await case.runtime.cancel_run_owned(
            case.context.run_id, present(case.context.execution_token)
        )
