"""Engineering proof retention expands with budget/progress, not a 64-row window."""

from collections.abc import Mapping
from dataclasses import replace
from decimal import Decimal
from typing import cast
from uuid import uuid4

import pytest

from agent_hub.models.gateway import GatewayCompletion
from agent_hub.models.types import ModelRequest, ModelResponse, TokenUsage, ToolCall
from agent_hub.runtime.autogen.adapter import (
    AutoGenDiscussionRuntime,
    _bounded_checkpoint_artifacts,
)
from agent_hub.runtime.autogen.engineering_delivery import EngineeringEvidenceLimits
from agent_hub.runtime.contracts import Artifact, EventKind, JsonValue, RuntimeCheckpoint
from tests.unit.runtime.autogen.test_engineering_completion import (
    Capabilities,
    Gateway,
    context,
    plan,
)


class LargeBackend(Capabilities):
    """Deterministic unit capability fixture; not a build/installation acceptance receipt."""

    async def execute(self, **kwargs: object) -> Mapping[str, JsonValue]:
        name = cast(str, kwargs["name"])
        result = self.results[name]
        assert isinstance(result, Mapping)
        if name == "workspace.write_text":
            index = len(self.calls)
            metadata: JsonValue = {"path": f"src/file-{index}.js", "sha256": "a" * 64, "size_bytes": 10}
            result = {"workspace_files": (metadata,)}
        elif name == "workspace.bundle":
            result = {**result, "workspace_files": tuple(
                {"path": f"src/file-{index}.js", "sha256": "a" * 64, "size_bytes": 10}
                for index in range(self.calls.count("workspace.write_text"))
            )}
        self.calls.append(name)
        return result


class ToolOnlyGateway(Gateway):
    async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
        if request.logical_model == "selector" or not self.actions:
            return await super().complete_with_context(request)
        self.requests.append(request)
        action = self.actions.pop(0)
        return GatewayCompletion(response=ModelResponse(text=None,
            tool_calls=(ToolCall(id=f"call_{len(self.requests)}", name=action, arguments={}),),
            usage=TokenUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
        ), deployment_id="shared", logical_model=request.logical_model,
            provider_id="test", provider_model="test/model", cost_usd=Decimal(0))


async def test_more_than_64_typed_tools_compact_checkpoint_restore_and_deliver() -> None:
    from collections.abc import AsyncGenerator

    from agent_hub.runtime.contracts import RunEvent

    backend = LargeBackend()
    configured = replace(plan(), max_turns=80, token_budget=2000, wall_time_seconds=300)
    ctx = context().model_copy(update={"token_budget": 2000, "timeout_seconds": 300.0})
    gateway = ToolOnlyGateway(["workspace.write_text"] * 72)
    runner = AutoGenDiscussionRuntime(gateway, configured, capability_gateway=backend)
    stream = cast(AsyncGenerator[RunEvent, None], runner.run(ctx))
    saved = None
    last = None
    try:
        async for event in stream:
            last = event
            if (event.checkpoint is not None
                and event.checkpoint.state.get("engineering_tool_prefix") == 72):
                saved = event.checkpoint
                break
    finally:
        await stream.aclose()
    assert saved is not None, (last.reason if last else None, len(backend.calls))
    registry = saved.state["artifact_registry"]
    assert isinstance(registry, Mapping) and len(registry) > 64
    assert len(cast(tuple[JsonValue, ...], saved.state["tool_ledger"])) == 72
    # Exercise the real strict JSON boundary, scoped repository hydration, and
    # replay cursor. No old tool is executed while restoring the compacted prefix.
    saved = RuntimeCheckpoint.from_payload(saved.to_payload())
    second = ToolOnlyGateway(["project.verify", "workspace.bundle"])
    resumed = AutoGenDiscussionRuntime(second, configured, capability_gateway=backend,
                                      artifact_repository=runner._repository)
    await resumed.restore_checkpoint(saved)
    events = [event async for event in resumed.run(ctx.model_copy(update={"checkpoint": saved}))]
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED, events[-1].reason
    assert backend.calls == ["workspace.write_text"] * 72 + ["project.verify", "workspace.bundle"]
    final = await resumed.save_checkpoint()
    unused = Gateway([])
    terminal = AutoGenDiscussionRuntime(unused, configured, capability_gateway=backend,
                                       artifact_repository=runner._repository)
    await terminal.restore_checkpoint(RuntimeCheckpoint.from_payload(final.to_payload()))
    events = [event async for event in terminal.run(ctx.model_copy(update={"checkpoint": terminal._restored}))]
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert not unused.requests and len(backend.calls) == 74


def snapshot(*, count: int = 0, pending: bool = False) -> dict[str, object]:
    rows = [{"status": "succeeded", "artifact_id": str(uuid4()), "artifact_sha256": "a" * 64}
            for _ in range(count)]
    registry = {row["artifact_id"]: row["artifact_sha256"] for row in rows}
    if pending:
        rows.append({"status": "running"})
    return {"id": str(uuid4()), "runtime_type": "autogen", "runtime_version": "2",
            "run_id": str(uuid4()), "tenant_id": str(uuid4()), "mode": "discuss", "state_sha256": "a" * 64,
            "state": {"model_ledger": (), "tool_ledger": tuple(rows), "artifact_registry": registry}}


def limits(payload: dict[str, object], *, tokens: int = 1000, used: int = 0,
           seconds: float = 300, remaining: float = 300) -> EngineeringEvidenceLimits:
    from agent_hub.runtime.autogen.engineering_delivery import engineering_evidence_limits

    return engineering_evidence_limits(payload, token_budget=tokens, used_tokens=used,
                                        timeout_seconds=seconds, remaining_seconds=remaining,
                                        max_turns=2, tool_fanout=16)


def test_initial_soft_capacity_follows_budget_and_progress_expands_it() -> None:
    first = limits(snapshot())
    progressed = limits(snapshot(count=72, pending=True))
    assert first.initial_soft < 72 < progressed.soft_limit <= progressed.hard_limit
    small = limits(snapshot(), tokens=2)
    assert small.initial_soft < first.initial_soft


@pytest.mark.parametrize("tokens,used,seconds,remaining", [(0, 0, 300, 300), (2, 2, 300, 300),
                                                          (1000, 0, 300, 0)])
def test_insufficient_budget_does_not_admit_new_pending_evidence(
    tokens: int, used: int, seconds: float, remaining: float,
) -> None:
    with pytest.raises(ValueError):
        limits(snapshot(count=72, pending=True), tokens=tokens, used=used,
               seconds=seconds, remaining=remaining)
    # Already completed evidence is retained even when a run stopped at its budget.
    limits(snapshot(count=72), tokens=tokens, used=used, seconds=seconds, remaining=remaining)


@pytest.mark.parametrize("padding", [tuple(range(4096)), ("x" * 500_000,) * 4], ids=["nodes", "bytes"])
def test_contract_node_and_byte_fuses_are_not_relaxed(padding: object) -> None:
    payload = snapshot(count=72)
    state = cast(dict[str, object], payload["state"])
    state["padding"] = padding
    with pytest.raises(ValueError):
        limits(payload)


@pytest.mark.parametrize("padding", [tuple(range(800)), ("x" * 490_000,) * 4],
                         ids=["node-headroom", "byte-headroom"])
def test_measured_json_headroom_reduces_capacity_without_new_fixed_row_cap(padding: object) -> None:
    payload = snapshot(count=72)
    original = limits(payload)
    state = cast(dict[str, object], payload["state"])
    state["padding"] = padding
    limited = limits(payload)
    assert 72 <= limited.hard_limit < original.hard_limit
    assert limited.soft_limit <= limited.hard_limit


def test_legacy_artifact_window_is_still_64() -> None:
    artifacts = tuple(Artifact(id=uuid4(), type="text", producer="engineer", content={"text": "ok"})
                      for _ in range(80))
    assert len(_bounded_checkpoint_artifacts(artifacts)) == 64
