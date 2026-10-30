"""Fixed wire protocol and actual native-mode assembly; never production evidence.

Configuration/secret metadata and provider responses are deterministic fixtures.
CapacityPool uses an owned Redis Unix socket; SDK, Gateway, worker binding,
runtime classes, policy, Root and repositories are production implementations.
No real provider delegate is constructed.
"""

from __future__ import annotations

import ast
import asyncio
import hashlib
import json
import re
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx

from agent_hub.api.routers.runs import CreateRunRequest
from agent_hub.auth.models import AuthenticatedPrincipal, Role
from agent_hub.capabilities.defaults import build_runtime_capability_stack
from agent_hub.capabilities.review_contracts import ApprovalReviewConfig
from agent_hub.config.schema import PlatformConfig
from agent_hub.db.models import RunRow
from agent_hub.domain.runs import TaskMode
from agent_hub.models.capacity import CapacityPool
from agent_hub.models.litellm_client import LiteLLMClient
from agent_hub.models.physical_admission import _call as physical_call
from agent_hub.models.physical_budget import _run_budget
from agent_hub.runs.authorization import _capture_authenticated_input, _persist_authenticated_input
from agent_hub.runs.repository import RunRepository, _database_uuid
from agent_hub.runs.workspace import workspace_selection
from agent_hub.runtime.artifacts import InMemoryArtifactRepository
from agent_hub.runtime.contracts import TaskContext
from agent_hub.runtime.defaults import (
    ConfigBackedDirectRuntime,
    ConfigBackedDiscussionRuntime,
    ConfigBackedDispatchRuntime,
    ConfigBackedHybridRuntime,
)
from agent_hub.runtime.review_assembly import (
    REQUIRED_VERIFICATION_CHECKS,
    ProviderBillingContract,
    ReviewAssemblyBroker,
    WorkerReviewVerification,
    assemble_review_capability_stack,
    catalog_fingerprint,
    runtime_source_fingerprint,
)
from tests.integration.models.test_run_admission import prepare

FACTORIES = {
    TaskMode.DIRECT: ConfigBackedDirectRuntime,
    TaskMode.DISPATCH: ConfigBackedDispatchRuntime,
    TaskMode.DISCUSS: ConfigBackedDiscussionRuntime,
    TaskMode.HYBRID: ConfigBackedHybridRuntime,
}
FINAL_TEXT = "The bounded observations support the final answer."


def submission_request(mode=TaskMode.DIRECT, *, pending=False):
    """Validate the real public schema, keeping all actor-selected route fields."""
    return CreateRunRequest(
        message=(
            "Write exactly 'Exact native write.\\n' to native.txt in this project workspace."
            if pending
            else "Compare two bounded observations and provide the final answer."
        ),
        mode=mode,
        agent_ids=("analyst", "critic"),
        conversation_id="fixture-" + uuid4().hex,
        project_id="native-review",
        workspace_session_id="fixture-session",
        sandbox_profile="workspace_write",
        permission_mode="auto_review" if pending else "default",
        requested_permissions=(
            ("workspace.read", "command.run")
            if pending
            else ("workspace.read", "workspace.write", "command.run")
        ),
        execution_backend="systemd",
        runtime_timeout_seconds=180,
    )


def submitted_input_scope(body):
    """Mirror the authenticated create boundary's scope; selection is not a grant."""
    return {
        "schema_version": 1,
        "evidence_kind": "submitted_selection_not_grant",
        "operation": "create_run",
        "conversation_id": body.conversation_id,
        "selection": body.model_dump(mode="json", exclude={"message"}),
        "resolved_selection": {
            "sandbox_profile": body.sandbox_profile,
            "permission_mode": body.permission_mode,
            "requested_permissions": list(body.requested_permissions),
            "execution_backend": body.execution_backend,
        },
    }


def capture_submission_input(row, body):
    """Capture the fixture's original input at its persisted-principal boundary."""
    return _capture_authenticated_input(
        AuthenticatedPrincipal(
            _database_uuid(row.actor_id), _database_uuid(row.tenant_id), Role(row.actor_role)
        ),
        body.message,
        submitted_input_scope(body),
    )


def build_task_context(record, binding, body, route):
    return TaskContext(
        run_id=record.id,
        tenant_id=record.tenant_id,
        actor_id=record.actor_id,
        actor_role=record.actor_role,
        execution_token=binding["lease"].token,
        mode=body.mode,
        request=body.message,
        routing_decision=route,
        timeout_seconds=150,
        token_budget=1000000,
    )


def schema_value(schema, root=None):
    """Generate an exact fixture for the actual request's output schema."""
    root = schema if root is None else root
    if "$ref" in schema:
        target = root
        for part in schema["$ref"].removeprefix("#/").split("/"):
            target = target[part]
        return schema_value(target, root)
    if "const" in schema:
        return schema["const"]
    if "enum" in schema:
        for preferred in ("done", "approve", "low"):
            if preferred in schema["enum"]:
                return preferred
        return schema["enum"][0]
    for union in ("anyOf", "oneOf"):
        if union in schema:
            return schema_value(next(s for s in schema[union] if s.get("type") != "null"), root)
    kind = schema.get("type")
    if kind == "object" or "properties" in schema:
        return {
            key: schema_value(schema["properties"][key], root) for key in schema.get("required", ())
        }
    if kind == "array":
        return [
            schema_value(schema.get("items", {"type": "string"}), root)
            for _ in range(schema.get("minItems", 0))
        ]
    if kind == "boolean":
        return True
    if kind in ("number", "integer"):
        return schema.get("minimum", 0)
    if kind == "null":
        return None
    return FINAL_TEXT


class ClosedBody(httpx.AsyncByteStream):
    def __init__(self, content, owner):
        self.content, self.owner, self.closed = content, owner, False
        owner.bodies.append(self)

    async def __aiter__(self):
        yield self.content

    async def aclose(self):
        self.closed = True


@dataclass
class FixedWire:
    calls: list = field(default_factory=list)
    bodies: list = field(default_factory=list)
    budgets: list = field(default_factory=list)
    classes: set = field(default_factory=set)
    attempts: dict = field(default_factory=dict)
    selector_calls: int = 0
    participant_calls: int = 0
    reviewer_calls: int = 0
    runtime: object = None
    block_primary: bool = False
    entered: asyncio.Event = field(default_factory=asyncio.Event)
    release: asyncio.Event = field(default_factory=asyncio.Event)

    def native_objects(self):
        pending, seen, objects = [self.runtime], set(), []
        while pending:
            item = pending.pop()
            if item is None or id(item) in seen:
                continue
            seen.add(id(item))
            objects.append(item)
            self.classes.add(type(item).__name__)
            active = getattr(item, "_active", None)
            if isinstance(active, dict):
                pending.extend(active.values())
            for name in ("_child", "_dispatch", "_discussion", "_synthesizer"):
                pending.append(getattr(item, name, None))
        return objects

    def text_for(self, payload):
        messages = payload["messages"]
        text = "\n".join(str(message.get("content", "")) for message in messages)
        response_format = payload.get("response_format", {})
        structured = response_format.get("json_schema", {})
        schema = structured.get("schema")
        if schema and set(schema.get("properties", {})) == {
            "schema_version",
            "decision",
            "risk",
            "reason_code",
            "rationale",
            "action_digest",
        }:
            material = json.loads(messages[-1]["content"])
            self.reviewer_calls += 1
            assert not payload.get("tools")
            return json.dumps(
                {
                    "schema_version": 1,
                    "decision": "allow",
                    "risk": "low",
                    "reason_code": "authorized_action",
                    "rationale": "Matches the complete exact action.",
                    "action_digest": material["action_digest"],
                }
            )
        # AutoGen's actual SelectorGroupChat formats its available participant list.
        candidates = re.findall(r"(?:from|among)\s+(\[[^\]\n]+\])", text, re.IGNORECASE)
        if candidates and ("select" in text.lower() or "role play" in text.lower()):
            names = ast.literal_eval(candidates[-1])
            assert names and all(isinstance(name, str) for name in names)
            selected = names[self.selector_calls % len(names)]
            self.selector_calls += 1
            return selected
        if schema:
            return json.dumps(schema_value(schema))
        self.participant_calls += 1
        return ("[COMPLETE] " if self.participant_calls > 1 else "") + FINAL_TEXT

    async def __call__(self, request):
        payload = json.loads(request.content)
        self.calls.append(payload)
        self.budgets.append(_run_budget.get())
        assert self.budgets[-1] is not None, "native primary escaped original pool"
        self.native_objects()
        call = physical_call.get()
        assert call is not None and call.attempts
        attempt = call.attempts[-1]
        meta = attempt.metadata
        assert payload["model"] == meta.provider_model.rsplit("/", 1)[-1]
        self.attempts[attempt.permit.attempt_id] = {
            "request_sha256": meta.request_sha256,
            "purpose": meta.purpose,
            "physical_transport_id": (
                str(call.scope._admission._attempts[attempt.permit.attempt_id].physical_id)
                if meta.purpose == "primary"
                else None
            ),
            "deployment": {
                "provider_id": meta.provider_model.split("/", 1)[0],
                "deployment_id": meta.deployment_id,
                "model_id": meta.provider_model,
            },
            "usage": {
                "prompt_tokens": 2,
                "completion_tokens": 3,
                "total_tokens": 5,
                "cost_usd": {"decimal": "0.000008"},
            },
        }
        self.entered.set()
        if self.block_primary:
            await self.release.wait()
        text = self.text_for(payload)
        usage = {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5}
        if payload.get("stream"):
            frames = [
                {
                    "id": "fixed",
                    "object": "chat.completion.chunk",
                    "model": payload["model"],
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"role": "assistant", "content": text},
                            "finish_reason": None,
                        }
                    ],
                },
                {
                    "id": "fixed",
                    "object": "chat.completion.chunk",
                    "model": payload["model"],
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                    "usage": usage,
                },
            ]
            content = (
                "".join("data: " + json.dumps(frame) + "\n\n" for frame in frames)
                + "data: [DONE]\n\n"
            ).encode()
            return httpx.Response(
                200, headers={"content-type": "text/event-stream"}, stream=ClosedBody(content, self)
            )
        body = {
            "id": "fixed",
            "object": "chat.completion",
            "created": 1,
            "model": payload["model"],
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": text},
                    "finish_reason": "stop",
                }
            ],
            "usage": usage,
        }
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            stream=ClosedBody(json.dumps(body).encode(), self),
        )


def fixture_platform():
    definition = {
        "provider": "openai",
        "model": "fixture-primary",
        "credential_ref": "fixture-primary-secret",
        "quota_scope_id": "fixture-primary",
        "context_window_tokens": 131072,
        "max_output_tokens": 8192,
        "input_per_million_usd": "1",
        "output_per_million_usd": "2",
        "capabilities": ["text", "structured_output", "tool_calling"],
    }
    return PlatformConfig.model_validate(
        {
            "models": {
                "primary": {"deployments": [definition]},
                "reviewer": {
                    "deployments": [
                        {
                            **definition,
                            "model": "fixture-reviewer",
                            "credential_ref": "fixture-reviewer-secret",
                            "quota_scope_id": "fixture-reviewer",
                        }
                    ]
                },
            },
            "agents": [
                {
                    "id": name,
                    "role": name,
                    "prompt": "Compare the bounded observations and provide the final answer.",
                    "model": "primary",
                    "skills": [],
                }
                for name in ("analyst", "critic")
            ],
        }
    )


class FixtureSecrets:
    async def fingerprint(self, tenant_id, reference):
        value = await self.resolve(tenant_id, reference)
        return hashlib.sha256(f"{tenant_id}:{value}".encode()).hexdigest()

    async def resolve(self, tenant_id, reference):
        return {
            "fixture-primary-secret": "synthetic-primary-credential",
            "fixture-reviewer-secret": "synthetic-reviewer-credential",
        }[reference]


def assert_wire_receipts(attempts, wire_attempts, pool):
    """Compare each durable receipt to its actual guarded transport admission."""
    assert {row.attempt_id for row in attempts} == set(wire_attempts)
    total = 0
    for row in attempts:
        sent = wire_attempts[row.attempt_id]
        assert row.purpose == sent["purpose"] == "primary"
        assert row.request_sha256 == sent["request_sha256"]
        assert row.deployment == sent["deployment"]
        assert row.state == "completed" and row.physical_transport_id is not None
        assert str(row.physical_transport_id) == sent["physical_transport_id"]
        expected = {
            "outcome": "completed",
            "deployment": sent["deployment"],
            "attempt_id": str(row.attempt_id),
            "physical_transport_id": str(row.physical_transport_id),
            "usage": sent["usage"],
        }
        assert row.receipt == expected
        encoded = json.dumps(
            expected, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
        ).encode()
        assert row.receipt_sha256 == hashlib.sha256(encoded).hexdigest()
        assert Decimal(expected["usage"]["cost_usd"]["decimal"]) == Decimal("0.000008")
        total += expected["usage"]["total_tokens"]
    assert total == pool.token_charged == len(wire_attempts) * 5


async def build_case(storage, tmp_path, mode, redis_client, *, pending=False, body=None):
    """Seed authenticated PG facts; use the real worker's original driver lease."""
    factory = storage[0]
    _, binding, _ = await prepare(storage, open_now=False)
    repository = RunRepository(factory)
    config = ApprovalReviewConfig(
        engine="rules_then_model",
        reviewer_logical_model="reviewer",
        max_input_tokens=1000000,
        max_output_tokens=512,
        max_cost_usd_per_run="100.00",
    )
    platform = fixture_platform()

    class Configs:
        async def get_current(self, tenant_id):
            assert tenant_id == binding["tenant_id"]
            return SimpleNamespace(version=1, document=platform.model_dump(mode="json"))

    capacities = []
    namespace = "task7a:" + uuid4().hex + ":"

    async def capacity(tenant_id, deployments):
        async def fingerprint(reference):
            return await FixtureSecrets().fingerprint(tenant_id, reference)

        controller = CapacityPool(
            redis_client,
            deployments=deployments,
            fingerprint_resolver=fingerprint,
            key_prefix=namespace,
        )
        capacities.append(controller)
        return controller

    async def current_config(tenant_id):
        return config

    wire = FixedWire()
    transport = LiteLLMClient(_network_delegate_factory=lambda: httpx.MockTransport(wire))
    broker = ReviewAssemblyBroker(
        config_service=Configs(),
        config_getter=current_config,
        run_repository=repository,
        session_factory=factory,
        secret_service=FixtureSecrets(),
        capacity_factory=capacity,
        transport=transport,
        # Protocol fixtures ONLY: not an ops-installed file or a production record.
        billing_contracts=tuple(
            ProviderBillingContract(
                binding["tenant_id"],
                catalog_fingerprint(platform),
                name,
                "fixture://native-mode-protocol",
            )
            for name in ("primary_1", "reviewer_1")
        ),
        verification=WorkerReviewVerification(
            frozenset(FACTORIES),
            "fixture://not-production-verification",
            source_sha256=runtime_source_fingerprint(),
            checks=REQUIRED_VERIFICATION_CHECKS,
        ),
    )
    profile = await broker.submission_profile(binding["tenant_id"], timeout_seconds=180)
    assert "approval_review_profile" in profile, "protocol fixture failed to enroll fresh profile"
    body = submission_request(mode, pending=pending) if body is None else body
    if not isinstance(body, CreateRunRequest) or body.mode is not mode:
        raise ValueError("native body must match the requested runtime mode")
    selection = workspace_selection(
        project_id=body.project_id,
        session_id=body.workspace_session_id,
        sandbox_profile=body.sandbox_profile,
        requested_permissions=body.requested_permissions,
        permission_mode=body.permission_mode,
    )
    route = {
        **selection.routing_payload(),
        "conversation_id": body.conversation_id,
        "execution_backend": body.execution_backend,
        "selected_agent_ids": list(body.agent_ids),
        "runtime_token_absolute_tokens": 10000000,
        "runtime_plan_token_budget": 10000000,
        "runtime_timeout_seconds": 180,
        **profile,
    }
    message = body.message
    async with factory() as session, session.begin():
        row = await session.get(RunRow, binding["run_id"])
        row.request, row.mode, row.routing_decision = message, mode.value, route
        evidence = capture_submission_input(row, body)
        await _persist_authenticated_input(session, row, evidence)
    record = await repository.get(binding["tenant_id"], binding["run_id"])
    context = build_task_context(record, binding, body, route)
    artifacts = InMemoryArtifactRepository()
    stack = assemble_review_capability_stack(
        broker,
        build_runtime_capability_stack,
        tenant_id=context.tenant_id,
        run_repository=repository,
        skill_store_dir=tmp_path / "skills",
        workspace_root=tmp_path,
        generated_artifact_dir=tmp_path / "generated",
        project_workspace_dir=tmp_path,
        artifact_repository=artifacts,
    )
    runtime = FACTORIES[mode](
        config_service=Configs(),
        secret_service=FixtureSecrets(),
        capacity_factory=capacity,
        transport=transport,
        capability_gateway=stack.runtime_gateway,
        harness_tool_gateway=stack.harness_tool_gateway,
        artifact_repository=artifacts,
        **(
            {
                "action_capability_gateway": stack.runtime_gateway,
                "action_harness_tool_gateway": stack.harness_tool_gateway,
            }
            if mode is TaskMode.DIRECT
            else {}
        ),
    )
    wire.runtime = runtime
    return SimpleNamespace(
        factory=factory,
        repository=repository,
        broker=broker,
        context=context,
        binding=binding,
        wire=wire,
        runtime=runtime,
        stack=stack,
        capacities=capacities,
        root=Path(tmp_path),
        config=config,
        redis=redis_client,
        body=body,
    )
