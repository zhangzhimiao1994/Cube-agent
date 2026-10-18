from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from collections.abc import Callable, Mapping
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest

from agent_hub.auth.models import Role
from agent_hub.capabilities.execution_diagnostics import ExecutionDiagnostic
from agent_hub.capabilities.gateway import CapabilityResult, CapabilityStatus
from agent_hub.capabilities.runtime import RuntimeCapabilityError
from agent_hub.domain.runs import TaskMode
from agent_hub.harness import HarnessToolGateway
from agent_hub.harness.types import HarnessToolCallRequest, HarnessToolCallResult
from agent_hub.models.gateway import GatewayCompletion
from agent_hub.models.types import ModelResponse, TokenUsage, ToolCall
from agent_hub.runtime.artifacts import InMemoryArtifactRepository
from agent_hub.runtime.contracts import EventKind, TaskContext
from agent_hub.runtime.crew.adapter import CrewDispatchRuntime, RuntimeExecutionError
from agent_hub.runtime.crew.plan import AgentSpec, DispatchPlan, DispatchStep
from agent_hub.skills.sandbox.base import SkillResult
from tests.unit.runtime.crew.test_adapter_failure_reason import FastFactory

TOOLS = ("project.verify", "workspace.read", "workspace.write_text")


class ProcessBackend:
    def __init__(self, root: Path, failure: str = "exit") -> None:
        self.root = root
        self.failure = failure
        self.calls: list[str] = []
        self.results: list[SkillResult] = []
        self.path = root / "main.py"
        self.path.write_text(
            "import time; time.sleep(10)" if failure == "timeout"
            else "import sys; print('FIXTURE_FAILURE', file=sys.stderr); sys.exit(3)",
            encoding="utf-8",
        )

    def is_available(self, tenant_id: Any, name: str) -> bool:
        return name in (*TOOLS, "workspace.bundle")

    def is_replay_safe(self, name: str) -> bool:
        return name == "workspace.read"

    async def execute(self, *, name: str, arguments: Mapping[str, Any], **kwargs: Any) -> Any:
        self.calls.append(name)
        if name == "project.verify":
            assert arguments == {}, "verification uses server-owned commands only"
        if name == "workspace.bundle":
            return {"artifact_id": str(uuid4()), "presentation": "final_attachment",
                    "file": {"filename": "fixture.zip"}}
        if name == "workspace.read":
            return {"path": "main.py", "text": self.path.read_text(encoding="utf-8"), "truncated": False}
        if name == "workspace.write_text":
            self.path.write_text(arguments["content"], encoding="utf-8")
            return {"path": "main.py", "summary": "written"}
        if self.failure == "backend":
            raise RuntimeError("untrusted backend exception")
        if self.failure == "unknown":
            raise RuntimeCapabilityError("skill execution failed")
        try:
            completed = await asyncio.to_thread(
                subprocess.run, [sys.executable, str(self.path)],
                capture_output=True, text=True, timeout=0.5, check=False,
            )
            result = SkillResult(
                exit_code=completed.returncode, stdout=completed.stdout,
                stderr=completed.stderr, timed_out=False,
            )
        except subprocess.TimeoutExpired:
            result = SkillResult(exit_code=None, stdout="", stderr="", timed_out=True)
        self.results.append(result)
        if result.timed_out or result.exit_code != 0:
            raise RuntimeCapabilityError(
                "skill execution timed out" if result.timed_out else "skill execution failed",
                execution_diagnostic=ExecutionDiagnostic(backend="fixture", result=result),
            )
        return {"verified": True, "stdout": result.stdout}


class Policy:
    def __init__(
        self, context: TaskContext, status: CapabilityStatus = CapabilityStatus.ALLOWED,
    ) -> None:
        self.context = context
        self.status = status
        self.calls: list[Any] = []

    async def invoke(self, request: Any, *, role: Role) -> CapabilityResult:
        assert request.tenant_id == self.context.tenant_id
        assert request.user_id == self.context.actor_id
        assert role is Role.OPERATOR
        if request.capability == "command":
            assert request.operation == "run"
            assert request.resource == "workspace/current/project-verification"
            assert request.arguments == {}
        self.calls.append(request)
        return CapabilityResult(
            status=self.status, run_id=request.run_id,
            approval_id="fixture-approval" if self.status is CapabilityStatus.WAITING_APPROVAL else None,
            reason=None if self.status is CapabilityStatus.ALLOWED else "capability denied",
        )


class RepairModel:
    def __init__(self, *, autogen: bool = False) -> None:
        self.requests: list[Any] = []
        self.autogen = autogen
        self.stage = 0
        self.bundle = False

    async def complete_with_context(self, request: Any) -> GatewayCompletion:
        self.requests.append(request)
        if self.autogen and request.max_output_tokens == 512:
            response = ModelResponse(text="implementer", usage=TokenUsage(1, 1, 2))
        else:
            stage = self.stage
            self.stage += 1
            if stage > 0:
                observations = [
                    message.content for message in request.messages
                    if message.role in {"user", "tool"} and isinstance(message.content, str)
                ]
                assert any("execution_diagnostic" in text for text in observations)
                assert any('"failed"' in text or '\\"failed\\"' in text for text in observations)
                if self.autogen:
                    assert any('"is_error":true' in text for text in observations)
                assert all(
                    "execution_diagnostic" not in message.content
                    for message in request.messages
                    if message.role == "system" and isinstance(message.content, str)
                )
            names: tuple[str, ...] = (
                "project.verify", "workspace.read", "workspace.write_text", "project.verify",
            )
            if self.bundle:
                names = (*names, "workspace.bundle")
            response = ModelResponse(
                text="[COMPLETE] Verified repaired fixture." if stage == len(names) else None,
                tool_calls=() if stage == len(names) else (ToolCall(
                    id=f"fixture-{stage}", name=names[stage],
                    arguments={"path": "main.py", "content": "print('verified')"}
                    if stage == 2 else {"path": "main.py"} if stage == 1 else {},
                ),),
                usage=TokenUsage(1, 1, 2),
            )
        return GatewayCompletion(
            response=response, deployment_id="fixture", logical_model=request.logical_model,
            provider_id="fixture", provider_model="fixture/model", cost_usd=Decimal(0),
        )


class PausingModel(RepairModel):
    async def complete_with_context(self, request: Any) -> GatewayCompletion:
        if self.stage:
            await asyncio.Event().wait()
        return await super().complete_with_context(request)


def context(mode: TaskMode = TaskMode.DISPATCH) -> TaskContext:
    return TaskContext(
        run_id=uuid4(), tenant_id=uuid4(), mode=mode, request="Repair and verify the fixture.",
        actor_id=uuid4(), actor_role=Role.OPERATOR, token_budget=10_000,
        routing_decision={"sandbox_profile": "workspace_write"},
    )


def crew(
    model: Any, backend: ProcessBackend, policy: Policy,
    repository: Any = None, *, incremental: bool = False,
) -> Any:
    tools = (*TOOLS, "workspace.bundle") if incremental else TOOLS
    plan = DispatchPlan(
        agents=(AgentSpec(
            id="implementer", role="Implementer", goal="Repair", logical_model="general",
            allowed_tools=tools,
        ),),
        steps=(DispatchStep(
            id="repair", agent="implementer",
            task="Project workspace delivery contract: repair and verify." if incremental else "Repair and verify.",
            tools=tools,
            final_synthesizer=True, token_budget=10_000,
        ),), allowed_tools=tools, total_token_budget=10_000,
    )
    return CrewDispatchRuntime(
        model, plan, capability_gateway=backend,
        harness_tool_gateway=HarnessToolGateway(backend, policy_gateway=policy),
        crew_factory=FastFactory(), artifact_repository=repository,
    )


@pytest.mark.parametrize("failure", ("exit", "timeout"))
async def test_process_failure_reaches_same_model_read_repair_and_reverify(
    tmp_path: Path, failure: str,
) -> None:
    ctx = context()
    backend, model = ProcessBackend(tmp_path, failure), RepairModel()
    policy = Policy(ctx)
    runner = crew(model, backend, policy)
    events = [event async for event in runner.run(ctx)]
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert backend.calls == ["project.verify", "workspace.read", "workspace.write_text", "project.verify"]
    assert len(model.requests) == 5
    assert len(policy.calls) == 4
    assert backend.results[-1].exit_code == 0
    failed = [event for event in events if event.kind is EventKind.TOOL_FAILED]
    assert len(failed) == 1 and failed[0].payload.get("artifact_id") is not None
    assert not any(event.kind is EventKind.TOOL_COMPLETED and event.tool_call_id == failed[0].tool_call_id for event in events)
    saved = await runner.save_checkpoint()
    assert [row["status"] for row in saved.state["tools"].values()].count("failed") == 1
    assert saved.state["usage"]["tokens"] == 10


@pytest.mark.parametrize("failure", ("backend", "unknown", "denied", "waiting"))
async def test_untyped_or_policy_failure_stays_terminal(tmp_path: Path, failure: str) -> None:
    ctx = context()
    backend, model = ProcessBackend(tmp_path, failure), RepairModel()
    policy = Policy(ctx, {
        "denied": CapabilityStatus.DENIED, "waiting": CapabilityStatus.WAITING_APPROVAL,
    }.get(failure, CapabilityStatus.ALLOWED))
    runner = crew(model, backend, policy)
    with pytest.raises(RuntimeExecutionError):
        _ = [event async for event in runner.run(ctx)]
    assert len(model.requests) == 1
    assert "workspace.write_text" not in backend.calls


async def test_failed_observation_checkpoint_resumes_without_replaying_process(tmp_path: Path) -> None:
    ctx = context()
    backend, model = ProcessBackend(tmp_path), PausingModel()
    policy, repository = Policy(ctx), InMemoryArtifactRepository()
    runner = crew(model, backend, policy, repository)
    saved = None
    async for event in runner.run(ctx):
        if event.checkpoint is not None and any(
            row.get("status") == "failed" and row.get("artifact_id") is not None
            for row in event.checkpoint.state["tools"].values()
        ):
            saved = event.checkpoint
            break
    await runner.cancel()
    assert saved is not None, "failed observation must be durable before model continuation"
    resumed_model = RepairModel()
    # The first model completion must replay from its ledger, not call the provider again.
    resumed_model.requests.append(model.requests[0])
    resumed_model.stage = 1
    resumed = crew(resumed_model, backend, policy, repository)
    await resumed.restore_checkpoint(saved)
    events = [event async for event in resumed.run(ctx.model_copy(update={"checkpoint": saved}))]
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert backend.calls == ["project.verify", "workspace.read", "workspace.write_text", "project.verify"]


class ReceiptHarness:
    """Mutate only receipts, keeping the real authorization and execution path."""
    def __init__(
        self, backend: ProcessBackend, policy: Policy,
        mutation: Callable[[HarnessToolCallResult], HarnessToolCallResult] | None = None,
    ) -> None:
        self.gateway = HarnessToolGateway(backend, policy_gateway=policy)
        self.mutation = mutation

    async def invoke(
        self, tenant_id: UUID, request: HarnessToolCallRequest, **kwargs: Any,
    ) -> HarnessToolCallResult:
        result = await self.gateway.invoke(tenant_id, request, **kwargs)
        if result.status == "failed" and self.mutation is not None:
            return self.mutation(result)
        return result


def malformed_receipt(result: HarnessToolCallResult, defect: str) -> HarnessToolCallResult:
    payload = json.loads(json.dumps(dict(result.payload), default=dict))
    diagnostic = payload["execution_diagnostic"]
    overrides: dict[str, Any] = {}
    if defect == "missing":
        del payload["execution_diagnostic"]
    elif defect == "shape":
        payload["execution_diagnostic"] = "not a typed receipt"
    elif defect == "success":
        diagnostic["exit_code"] = 0
    elif defect == "unknown":
        diagnostic["exit_code"] = None
    elif defect == "bool_exit":
        diagnostic["exit_code"] = True
    elif defect == "version":
        diagnostic["schema_version"] = 2
    elif defect == "timeout_type":
        diagnostic["timed_out"] = "true"
    elif defect == "metadata":
        diagnostic["stderr_metadata"]["returned_bytes"] = 1
    elif defect == "approval":
        payload["approval_id"] = "requires-review"
    elif defect == "binding":
        overrides["call_id"] = "other-call"
    elif defect == "truncated":
        overrides["truncated"] = True
    else:
        raise AssertionError(defect)
    return replace(result, payload=payload, **overrides)


DEFECTS = ("missing", "shape", "success", "unknown", "bool_exit", "version", "timeout_type",
           "metadata", "approval", "binding", "truncated")


@pytest.mark.parametrize("defect", DEFECTS)
async def test_invalid_failure_evidence_cannot_open_continuation(
    tmp_path: Path, defect: str,
) -> None:
    ctx = context()
    backend, model, policy = ProcessBackend(tmp_path), RepairModel(), Policy(ctx)
    runner = crew(model, backend, policy)
    runner._tool_gateway = ReceiptHarness(backend, policy, lambda result: malformed_receipt(result, defect))
    with pytest.raises(RuntimeExecutionError):
        _ = [event async for event in runner.run(ctx)]
    assert len(model.requests) == 1 and backend.calls == ["project.verify"]


async def test_native_tool_history_carries_failed_observation_through_repair(tmp_path: Path) -> None:
    from agent_hub.models.native_messages import NativeAssistantMessage, NativeToolCall
    from agent_hub.runtime.provider_continuation import InMemoryProviderContinuationRepository

    class NativeModel(RepairModel):
        async def complete_with_context(self, request: Any) -> GatewayCompletion:
            completion = await super().complete_with_context(request)
            response = completion.response
            if response.tool_calls:
                response = replace(response, native_assistant=NativeAssistantMessage(
                    "fixture", response.text,
                    tuple(NativeToolCall(call.id, call.name.replace(".", "_"), json.dumps(dict(call.arguments)))
                          for call in response.tool_calls),
                ))
            return replace(completion, response=response)

    ctx = context()
    ctx = ctx.model_copy(update={"routing_decision": {
        **ctx.routing_decision, "project_id": "fixture-project", "workspace_session_id": "fixture-session",
    }})
    backend, model = ProcessBackend(tmp_path), NativeModel()
    model.bundle = True
    runner = crew(model, backend, Policy(ctx), incremental=True)
    runner._provider_continuation_repository = InMemoryProviderContinuationRepository()
    events = [event async for event in runner.run(ctx)]
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert any(message.role == "tool" and "execution_diagnostic" in message.content
               for message in model.requests[1].messages)
    assert backend.calls == ["project.verify", "workspace.read", "workspace.write_text", "project.verify", "workspace.bundle"]
