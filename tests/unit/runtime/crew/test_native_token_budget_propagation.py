"""Zero-provider proof through the installed default CrewAI async generation."""

from __future__ import annotations

import asyncio
import http.client
import json
import socket
import sys
import threading
import urllib.request
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path
from types import FrameType
from typing import cast

import pytest

from agent_hub.domain.runs import TaskMode
from agent_hub.models.gateway import GatewayCompletion
from agent_hub.models.types import ModelRequest
from agent_hub.runtime.artifacts import InMemoryArtifactRepository
from agent_hub.runtime.contracts import EventKind, ExecutionRuntime, RuntimeCheckpoint, TaskContext
from agent_hub.runtime.crew import adapter
from agent_hub.runtime.crew.adapter import CrewDispatchRuntime
from agent_hub.runtime.defaults import (
    _dispatch_role_payload,
    _dispatch_step_payload,
    _PlannedRuntime,
)
from agent_hub.runtime.hybrid import HybridRuntime
from agent_hub.runtime.token_budget import current_token_budget
from tests.unit.runtime.crew.test_default_step_token_envelope import generated_plan
from tests.unit.runtime.test_adaptive_token_budget_propagation import (
    ProgressUsageGateway,
    adaptive_context,
    consume,
)
from tests.unit.runtime.test_hybrid import MultiArtifactRuntime, artifact


@pytest.fixture(autouse=True)
def offline_only(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    _function_scoped_runner: asyncio.Runner,
) -> Iterator[list[str]]:
    # Initialize Windows' event-loop socketpair before blocking every network entry.
    _function_scoped_runner.get_loop()
    attempts: list[str] = []

    def denied(*args: object, **kwargs: object) -> None:
        attempts.append("blocked")
        raise AssertionError("native budget fixture must not access network or providers")

    local = threading.local()
    original_pair = socket.socketpair

    def local_pair(
        family: int | None = None,
        type: int = socket.SOCK_STREAM,
        proto: int = 0,
    ) -> tuple[socket.socket, socket.socket]:
        local.creating_pair = True
        try:
            if family is None:
                return original_pair(type=type, proto=proto)
            return original_pair(family, type, proto)
        finally:
            local.creating_pair = False

    def guard(original: Callable[..., object]) -> Callable[..., object]:
        def guarded(*args: object, **kwargs: object) -> object:
            if getattr(local, "creating_pair", False):
                return original(*args, **kwargs)
            denied(*args, **kwargs)
            return None

        return guarded

    for name in (
        "CREWAI_DISABLE_TELEMETRY",
        "CREWAI_DISABLE_TRACKING",
        "CREWAI_TESTING",
        "OTEL_SDK_DISABLED",
    ):
        monkeypatch.setenv(name, "true")
    monkeypatch.setenv("CREWAI_TRACING_ENABLED", "false")
    monkeypatch.setenv("AGENT_HUB_CREWAI_STORAGE_DIR", str(tmp_path / "owned-crew"))
    monkeypatch.setattr(socket, "socketpair", local_pair)
    for name in ("connect", "connect_ex", "bind", "sendto"):
        original = cast(Callable[..., object], getattr(socket.socket, name))
        monkeypatch.setattr(socket.socket, name, guard(original))
    monkeypatch.setattr(socket, "create_connection", denied)
    monkeypatch.setattr(socket, "getaddrinfo", denied)
    monkeypatch.setattr(urllib.request, "urlopen", denied)
    monkeypatch.setattr(urllib.request.OpenerDirector, "open", denied)
    monkeypatch.setattr(http.client.HTTPConnection, "connect", denied)
    monkeypatch.setattr(http.client.HTTPSConnection, "connect", denied)
    yield attempts
    assert attempts == []


class NativeProgressGateway(ProgressUsageGateway):
    def __init__(self, task: TaskContext, granted: asyncio.Event) -> None:
        super().__init__(task, granted)
        self.bridge_limits: list[int] = []
        self.receipts: list[GatewayCompletion] = []

    async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
        if self.requests:
            await asyncio.wait_for(self.progress_granted.wait(), timeout=3)
        names: set[str] = set()
        bridge_context: TaskContext | None = None
        frame: FrameType | None = sys._getframe()
        try:
            while frame is not None:
                names.add(frame.f_code.co_qualname)
                if frame.f_code.co_name == "_complete_gateway_messages":
                    candidate = frame.f_locals.get("context")
                    assert isinstance(candidate, TaskContext)
                    bridge_context = candidate
                frame = frame.f_back
        finally:
            del frame
        assert "Crew.akickoff" in names
        assert any(name.endswith("GatewayOnlyLLM.acall") for name in names)
        assert any(name.endswith("StepBridge.complete") for name in names)
        assert bridge_context is not None
        assert bridge_context is not self.task
        assert (bridge_context.run_id, bridge_context.tenant_id, bridge_context.mode) == (
            self.task.run_id,
            self.task.tenant_id,
            TaskMode.DISPATCH,
        )
        assert adapter._CREWAI_TRACE_DISABLED.get() is True
        assert adapter._CREWAI_TELEMETRY_DISABLED.get() is True
        if len(self.requests) < 3:
            assert request.response_schema is not None
            assert request.response_schema.schema["type"] == "object"
        else:
            assert request.response_schema is None
        assert request.tools == ()
        self.bridge_limits.append(current_token_budget(bridge_context))
        receipt = await super().complete_with_context(request)
        if request.response_schema is not None:
            payload = json.loads(receipt.response.text or "null")
            assert isinstance(payload, dict) and isinstance(payload.get("summary"), str)
        self.receipts.append(receipt)
        return receipt


def _child_runtime(
    crew: CrewDispatchRuntime,
    mode: TaskMode,
    repository: InMemoryArtifactRepository,
) -> ExecutionRuntime:
    if mode is TaskMode.DISPATCH:
        return crew
    return HybridRuntime(
        crew,
        MultiArtifactRuntime(TaskMode.DISCUSS, (artifact("discussion", "owned discussion"),)),
        MultiArtifactRuntime(TaskMode.DIRECT, (artifact("synthesis", "owned synthesis"),)),
        artifact_repository=repository,
    )


@pytest.mark.parametrize("mode", (TaskMode.DISPATCH, TaskMode.HYBRID))
async def test_default_crew_async_bridge_uses_progress_grant_and_restores_without_rebill(
    mode: TaskMode,
    offline_only: list[str],
) -> None:
    task = adaptive_context(1_500_000, mode)
    plan = generated_plan(task)
    granted = asyncio.Event()
    gateway = NativeProgressGateway(task, granted)
    repository = InMemoryArtifactRepository()
    crew = CrewDispatchRuntime(gateway, plan, artifact_repository=repository)
    assert type(crew._factory) is adapter.CrewAIObjectFactory
    child = _child_runtime(crew, mode, repository)
    runtime = _PlannedRuntime(
        child,
        mode=mode,
        main_agent_model="architect",
        roles=_dispatch_role_payload(plan),
        steps=_dispatch_step_payload(plan),
    )
    async with asyncio.timeout(30):
        events, failure = await consume(runtime, task, progress_granted=granted)
    assert failure is None
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert not any(event.kind is EventKind.RUNTIME_FAILED for event in events)
    assert task.token_budget > 1_500_007
    assert len(gateway.requests) == len(gateway.receipts) == 4
    assert gateway.bridge_limits[0] == 1_500_000
    assert all(limit > 1_500_007 for limit in gateway.bridge_limits[1:])
    assert gateway.granted_at_calls[1] > 1_500_007
    checkpoint = RuntimeCheckpoint.from_payload((await crew.save_checkpoint()).to_payload())
    assert checkpoint.state["terminal"] is True
    assert checkpoint.state["phase"] == "completed"
    assert checkpoint.state["usage"] == {"tokens": 1_500_007, "cost_usd": "0"}
    step_usage = checkpoint.state["step_usage"]
    assert isinstance(step_usage, Mapping)
    assert step_usage["implementer_step"] == {"tokens": 1_500_001, "cost_usd": "0"}
    assert all(receipt.provider_id == "owned-provider" for receipt in gateway.receipts)
    assert all(receipt.response.usage is not None for receipt in gateway.receipts)
    completed = [
        event.artifact
        for event in events
        if event.kind is EventKind.ARTIFACT_CREATED
        and event.artifact is not None
        and event.artifact.type == "model_response"
    ]
    assert len(completed) == 4
    for item, receipt in zip(completed, gateway.receipts, strict=True):
        assert item is not None and item.provenance is not None
        assert item.provenance.provider_id == receipt.provider_id
        assert item.provenance.provider_model == receipt.provider_model
        assert item.provenance.logical_model == receipt.logical_model
        assert item.provenance.deployment_id == receipt.deployment_id

    outer_checkpoint = RuntimeCheckpoint.from_payload((await child.save_checkpoint()).to_payload())
    restored_gateway = NativeProgressGateway(task, granted)
    restored_crew = CrewDispatchRuntime(restored_gateway, plan, artifact_repository=repository)
    restored = _child_runtime(restored_crew, mode, repository)
    await restored.restore_checkpoint(outer_checkpoint)
    restored_task = task.model_copy(update={"checkpoint": outer_checkpoint})
    async with asyncio.timeout(30):
        restored_events, restored_failure = await consume(restored, restored_task)
    assert restored_failure is None
    assert [event.kind for event in restored_events] == [EventKind.RUNTIME_COMPLETED]
    assert restored_gateway.requests == []
    assert restored_gateway.receipts == []
    assert outer_checkpoint.state_sha256 == outer_checkpoint.recompute_state_sha256()
    assert checkpoint.state["usage"] == {"tokens": 1_500_007, "cost_usd": "0"}
    assert offline_only == []
