"""Output-room regressions use real estimates and private native receipts; no network."""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import replace
from uuid import UUID

import pytest

from agent_hub.models.gateway import GatewayCompletion
from agent_hub.models.gateway import ModelGateway as ConfiguredModelGateway
from agent_hub.models.registry import ModelRegistry
from agent_hub.models.types import (
    Deployment,
    ModelCapability,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TokenUsage,
    ToolCall,
)
from agent_hub.runtime.artifacts import ArtifactReference, InMemoryArtifactRepository
from agent_hub.runtime.contracts import EventKind, RunEvent, RuntimeCheckpoint, TaskContext
from agent_hub.runtime.crew import adapter
from agent_hub.runtime.provider_continuation import InMemoryProviderContinuationRepository
from tests.unit.runtime.crew.test_adapter_failure_reason import (
    RecordingHarnessToolGateway,
    _workspace_delivery_bundle_response,
)
from tests.unit.runtime.crew.test_meaningful_workspace_progress import (
    Observations,
    observe,
    offline_only,  # noqa: F401
    write,
)
from tests.unit.runtime.crew.test_tool_incomplete_recovery import subject, tool_plan
from tests.unit.runtime.post_tool_plain_action_typed_fixture import (
    FINAL,
    HANDOFF,
    EstimateOnlyIO,
    NativeGateway,
    RoomCase,
    context,
    integer,
    json_map,
    mapping,
    model_rows,
    native,
    present,
    runtime_for,
    string,
)
from tests.unit.runtime.test_plain_file_action_helper import LocalCapacityBoundary

WINDOW = 65536


def configured(window: int = WINDOW, output_max: int | None = None) -> ConfiguredModelGateway:
    return ConfiguredModelGateway(
        ModelRegistry(
            (
                Deployment(
                    id="primary",
                    logical_model="general",
                    capabilities=frozenset(ModelCapability),
                    context_window_tokens=window,
                    max_output_tokens=output_max,
                ),
            )
        ),
        LocalCapacityBoundary(),
        EstimateOnlyIO(),
        EstimateOnlyIO(),
    )


def observation(label: str, count: int) -> ModelMessage:
    return ModelMessage(
        "user",
        "UNTRUSTED_CAPABILITY_RESULTS_JSON="
        + json.dumps(
            [{"name": "read_context", "result": {"label": label, "text": '{}[],:"' * count}}],
            separators=(",", ":"),
        ),
    )


def high_input_request(gateway: ConfiguredModelGateway, output: int = 16384) -> ModelRequest:
    # Find a deterministic high-input fixture with the real production estimator.
    def request(count: int) -> ModelRequest:
        return ModelRequest(
            logical_model="general",
            allow_fallback=False,
            messages=(
                ModelMessage("system", "Keep all instructions and permissions."),
                ModelMessage("user", "Verify the existing delivery."),
                observation("older", count),
                observation("latest", 1),
            ),
            max_output_tokens=output,
        )

    low, high = 0, 12000
    while low < high:
        mid = (low + high) // 2
        if gateway.estimate_input_tokens(request(mid)) < 57000:
            low = mid + 1
        else:
            high = mid
    return request(low)


def test_fresh_policy_reserves_requested_output_using_real_estimator() -> None:
    gateway = configured()
    request = high_input_request(gateway)
    estimate = gateway.estimate_input_tokens(request)
    assert estimate < WINDOW < estimate + request.max_output_tokens
    runtime = subject(
        gateway, tool_plan(500000), InMemoryArtifactRepository(), RecordingHarnessToolGateway()
    )
    fitted = runtime._fit_context_request(request, adapter._RunState(adapter._RunToken(1)))
    assert gateway.estimate_input_tokens(fitted) + fitted.max_output_tokens <= WINDOW
    assert request.messages[2] not in fitted.messages
    assert request.messages[-1] in fitted.messages
    assert fitted.max_output_tokens == request.max_output_tokens


class RoomGateway(NativeGateway):
    def __init__(self, *outcomes: ModelResponse | str, output_max: int | None = None) -> None:
        def read(path: str, call_id: str) -> ModelResponse:
            response = observe("workspace.read", path)
            return native(
                replace(
                    response,
                    tool_calls=tuple(replace(call, id=call_id) for call in response.tool_calls),
                )
            )

        defaults = (
            native(write()),
            read("older.txt", "older-read"),
            read("latest.txt", "latest-read"),
            "incomplete",
            "incomplete",
            native(HANDOFF),
            native(_workspace_delivery_bundle_response()),
            native(FINAL),
        )
        super().__init__(*(outcomes or defaults))
        self.estimator_gateway = configured(output_max=output_max)
        self._registry = self.estimator_gateway._registry
        self.context_window_tokens = self.estimator_gateway.context_window_tokens
        self.estimate_input_tokens = self.estimator_gateway.estimate_input_tokens
        self._estimated_input_tokens = self.estimator_gateway._estimated_input_tokens

    async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
        from agent_hub.models.gateway import GatewayRejectedOutput

        index = len(self.requests)
        try:
            return await super().complete_with_context(request)
        except GatewayRejectedOutput as error:
            assert error.evidence is not None
            used = request.max_output_tokens - 400 if index == 3 else request.max_output_tokens
            error.evidence = replace(error.evidence, usage=TokenUsage(10, used, 10 + used))
            raise


async def test_native_post_tool_growth_compacts_older_read_before_capacity_clamp() -> None:
    gateway = RoomGateway()
    gateway.outcomes = (*gateway.outcomes[:5], "incomplete", *gateway.outcomes[5:])
    harness = Observations()
    harness.files["older.txt"] = '{}[],:"' * 5000
    harness.files["latest.txt"] = "latest required observation"
    runtime = runtime_for(gateway, harness)
    runtime._plan = runtime._plan.model_copy(
        update={
            "total_token_budget": 500000,
            "agents": tuple(
                a.model_copy(update={"max_output_tokens": 8192}) for a in runtime._plan.agents
            ),
            "steps": (runtime._plan.steps[0].model_copy(update={"token_budget": 500000}),),
        }
    )
    task = context().model_copy(
        update={
            "token_budget": 500000,
            "routing_decision": {
                **context().routing_decision,
                "post_tool_finalization_growth_version": 1,
            },
        }
    )
    events = [event async for event in runtime.run(task)]
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    correction, growth = gateway.requests[4:6]
    assert growth.max_output_tokens > correction.max_output_tokens
    assert gateway.estimate_input_tokens(growth) + growth.max_output_tokens <= WINDOW
    assert growth.messages != correction.messages
    assert len(harness.calls) == 4
    checkpoint = await runtime.save_checkpoint()
    repair = mapping(mapping(checkpoint.state["structured_repairs"])["implementer_step"])
    row = mapping(mapping(checkpoint.state["models"])[string(repair["correction_key"])])
    assert row["request_sha256"] == repair["correction_request_sha256"]
    growth_row = next(
        row
        for row in model_rows(checkpoint.state["models"]).values()
        if row.get("post_tool_growth") and row["call_index"] == 5
    )
    assert growth_row["request_sha256"] == runtime._model_request_sha256(growth)
    assert mapping(growth_row["post_tool_growth"])["request_sha256"] == growth_row["request_sha256"]


@pytest.mark.parametrize("version", [0, 1])
def test_legacy_input_only_versions_do_not_gain_output_fit(version: int) -> None:
    gateway = configured()
    request = high_input_request(gateway)
    runtime = subject(
        gateway, tool_plan(500000), InMemoryArtifactRepository(), RecordingHarnessToolGateway()
    )
    state = adapter._RunState(adapter._RunToken(1), context_window_compaction_version=version)
    fitted = runtime._fit_context_request(request, state)
    assert fitted is request
    assert runtime._model_request_sha256(fitted) == runtime._model_request_sha256(request)


def test_provider_hard_max_is_clamped_before_unreachable_desired_fit() -> None:
    gateway = configured(output_max=8192)
    request = high_input_request(gateway)
    runtime = subject(
        gateway, tool_plan(500000), InMemoryArtifactRepository(), RecordingHarnessToolGateway()
    )
    fitted = runtime._post_tool_fitted_request(
        request, 32768, 500000, adapter._RunState(adapter._RunToken(1))
    )
    assert fitted.max_output_tokens == 8192
    assert fitted.messages == request.messages
    assert gateway.estimate_input_tokens(fitted) + fitted.max_output_tokens <= WINDOW


def test_required_latest_may_clip_output_without_dropping_required_content() -> None:
    from agent_hub.runtime.crew.context_window import fit_request

    gateway = configured()
    high = high_input_request(gateway)
    latest = replace(
        high.messages[2], content=string(high.messages[2].content).replace("older", "latest")
    )
    request = replace(high, messages=(*high.messages[:2], latest))
    fitted = fit_request(request, gateway, reserve_output=True)
    assert fitted.messages == request.messages
    assert 0 < fitted.max_output_tokens < request.max_output_tokens
    assert gateway.estimate_input_tokens(fitted) + fitted.max_output_tokens <= WINDOW
    assert fit_request(fitted, gateway, reserve_output=True) is fitted


def test_irreducible_required_input_cannot_manufacture_output_capacity() -> None:
    from agent_hub.runtime.crew.context_window import fit_request

    gateway = configured(window=100)
    request = ModelRequest(
        logical_model="general",
        allow_fallback=False,
        max_output_tokens=8192,
        messages=(
            ModelMessage("system", "REQUIRED_INSTRUCTIONS=" + "x" * 2000),
            ModelMessage("user", "task"),
        ),
    )
    with pytest.raises(ValueError, match="required model context"):
        fit_request(request, gateway, reserve_output=True)


def test_native_read_pairs_stay_atomic_and_latest_verification_stays_required() -> None:
    from agent_hub.runtime.crew.context_window import fit_request
    from agent_hub.runtime.crew.native_history import tool_result_messages

    old = native(
        ModelResponse(
            text="",
            tool_calls=(ToolCall(id="old", name="workspace.read", arguments={"path": "old"}),),
        )
    )
    verified = native(
        ModelResponse(
            text="", tool_calls=(ToolCall(id="verify", name="project.verify", arguments={}),)
        )
    )
    older = tool_result_messages(
        old, [{"name": "workspace.read", "result": {"text": '{}[],:"' * 1500}}]
    )
    latest = tool_result_messages(
        verified, [{"name": "project.verify", "result": {"status": "passed", "receipt": "a" * 64}}]
    )
    request = ModelRequest(
        logical_model="general",
        allow_fallback=False,
        max_output_tokens=4096,
        native_tool_continuation=True,
        messages=(
            ModelMessage("system", "keep role"),
            ModelMessage("user", "task"),
            *older,
            *latest,
        ),
    )
    gateway = configured(window=10000)
    fitted = fit_request(request, gateway, reserve_output=True)
    assert all(message not in fitted.messages for message in older)
    assert all(message in fitted.messages for message in latest)
    assert request.messages[:2] == fitted.messages[1:3]
    assert gateway.estimate_input_tokens(fitted) + fitted.max_output_tokens <= 10000


@pytest.mark.parametrize("name", ["project.verify", "workspace.write_text", "workspace.bundle"])
def test_required_mutating_or_verification_exchange_is_not_pruned(name: str) -> None:
    from agent_hub.runtime.crew.context_window import fit_request

    required = ModelMessage(
        "user",
        "UNTRUSTED_CAPABILITY_RESULTS_JSON="
        + json.dumps([{"name": name, "result": {"text": "required " * 100}}]),
    )
    request = ModelRequest(
        logical_model="general",
        allow_fallback=False,
        max_output_tokens=4096,
        messages=(
            ModelMessage("system", "role"),
            ModelMessage("user", "task"),
            required,
            observation("latest", 1),
        ),
    )
    fitted = fit_request(request, configured(window=1800), reserve_output=True)
    assert required in fitted.messages and request.messages[-1] in fitted.messages
    assert fitted.max_output_tokens < 4096


def test_role_token_room_is_never_spent_past_the_remaining_envelope() -> None:
    gateway = configured()
    request = high_input_request(gateway)
    runtime = subject(
        gateway, tool_plan(500000), InMemoryArtifactRepository(), RecordingHarnessToolGateway()
    )
    fitted = runtime._post_tool_fitted_request(
        request, 32768, 15000, adapter._RunState(adapter._RunToken(1))
    )
    assert gateway.estimate_input_tokens(fitted) + fitted.max_output_tokens <= 15000
    assert fitted.max_output_tokens < 15000


def test_canonical_projection_uses_final_clipped_cap_and_stable_timeout_hash() -> None:
    gateway = configured(output_max=10000)
    request = high_input_request(gateway)
    runtime = subject(
        gateway, tool_plan(500000), InMemoryArtifactRepository(), RecordingHarnessToolGateway()
    )
    state = adapter._RunState(adapter._RunToken(1))
    fitted = runtime._post_tool_fitted_request(request, 32768, 500000, state)
    canonical = runtime._fit_context_request(
        replace(request, max_output_tokens=fitted.max_output_tokens), state
    )
    assert fitted == canonical
    retimed = runtime._post_tool_fitted_request(
        replace(request, timeout_seconds=13.123), 32768, 500000, state
    )
    assert runtime._model_request_sha256(fitted) == runtime._model_request_sha256(retimed)


async def room_case(
    monkeypatch: pytest.MonkeyPatch,
    version: int = 2,
    *,
    older_count: int | None = None,
    growths: int = 2,
    output_max: int | None = None,
) -> RoomCase:
    original_fit = adapter.CrewDispatchRuntime._fit_context_request

    def policy_fit(
        self: adapter.CrewDispatchRuntime, request: ModelRequest, state: adapter._RunState
    ) -> ModelRequest:
        state.context_window_compaction_version = version
        return original_fit(self, request, state)

    # A genuine old-policy receipt fixture, not a route upgrade or a forged hash.
    with monkeypatch.context() as policy:
        if version != 2:
            policy.setattr(adapter.CrewDispatchRuntime, "_fit_context_request", policy_fit)
        gateway = RoomGateway(output_max=output_max)
        if growths == 2:
            gateway.outcomes = (*gateway.outcomes[:5], "incomplete", *gateway.outcomes[5:])
        harness = Observations()
        harness.files["older.txt"] = '{}[],:"' * (
            older_count if older_count is not None else (5000 if version == 2 else 5)
        )
        harness.files["latest.txt"] = "latest required observation"
        runtime = runtime_for(gateway, harness)
        runtime._plan = runtime._plan.model_copy(
            update={
                "total_token_budget": 500000,
                "agents": tuple(
                    a.model_copy(update={"max_output_tokens": 8192}) for a in runtime._plan.agents
                ),
                "steps": (runtime._plan.steps[0].model_copy(update={"token_budget": 500000}),),
            }
        )
        task = context()
        task = task.model_copy(
            update={
                "token_budget": 500000,
                "routing_decision": {
                    **task.routing_decision,
                    "post_tool_finalization_growth_version": 1,
                },
            }
        )
        events = [event async for event in runtime.run(task)]
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    return runtime, task, gateway, harness, events


def checkpoint_at(events: Sequence[RunEvent], phase: str) -> RuntimeCheckpoint:
    wanted = {
        "source": (3, "rejected"),
        "root_prepared": (4, "prepared"),
        "root_running": (4, "running"),
        "root_rejected": (4, "rejected"),
        "growth_prepared": (5, "prepared"),
        "growth_running": (5, "running"),
        "growth_rejected": (5, "rejected"),
        "growth_succeeded": (6, "succeeded"),
        "closed": (7, "succeeded"),
    }
    for event in events:
        cp = event.checkpoint
        if cp is None:
            continue
        if phase == "completed":
            if cp.state["phase"] == "completed":
                return RuntimeCheckpoint.from_payload(cp.to_payload())
            continue
        rows = sorted(
            model_rows(cp.state["models"]).values(), key=lambda row: integer(row["call_index"])
        )
        index, status = wanted[phase]
        if rows[-1]["call_index"] == index and rows[-1]["status"] == status:
            if phase == "closed" and not (
                len(mapping(cp.state["tools"])) == 4
                and all(
                    row["status"] == "succeeded" for row in model_rows(cp.state["tools"]).values()
                )
            ):
                continue
            return RuntimeCheckpoint.from_payload(cp.to_payload())
    raise AssertionError("missing native boundary: " + phase)


async def prefix_stores(
    runtime: adapter.CrewDispatchRuntime,
    task: TaskContext,
    cp: RuntimeCheckpoint,
) -> tuple[InMemoryArtifactRepository, InMemoryProviderContinuationRepository]:
    artifacts = InMemoryArtifactRepository()
    refs = tuple(
        ArtifactReference(UUID(key), string(digest))
        for key, digest in mapping(cp.state["artifact_registry"]).items()
    )
    for artifact in await runtime._artifact_repository.get_many(task.tenant_id, task.run_id, refs):
        await artifacts.put(task.tenant_id, task.run_id, artifact)
    private = InMemoryProviderContinuationRepository()
    for row in model_rows(cp.state["models"]).values():
        reference = row.get("provider_continuation")
        if reference is not None:
            reference = json_map(reference)
            snapshot = await present(runtime._provider_continuation_repository).get(
                task.tenant_id,
                task.run_id,
                UUID(string(reference["id"])),
                string(reference["sha256"]),
            )
            await private.put(snapshot)
    return artifacts, private


@pytest.mark.parametrize("version", [0, 1, 2])
@pytest.mark.parametrize(
    "phase",
    [
        "source",
        "root_prepared",
        "root_running",
        "root_rejected",
        "growth_prepared",
        "growth_running",
        "growth_rejected",
        "growth_succeeded",
        "closed",
        "completed",
    ],
)
async def test_cold_receipts_keep_policy_cap_hash_usage_and_no_tool_replay(
    monkeypatch: pytest.MonkeyPatch,
    version: int,
    phase: str,
) -> None:
    runtime, task, original, harness, events = await room_case(monkeypatch, version)
    cp = checkpoint_at(events, phase)
    before = cp.to_payload()
    artifacts, private = await prefix_stores(runtime, task, cp)
    final = await runtime.save_checkpoint()
    outcomes = {
        "source": (
            "incomplete",
            "incomplete",
            native(HANDOFF),
            native(_workspace_delivery_bundle_response()),
            native(FINAL),
        ),
        "root_prepared": (
            "incomplete",
            "incomplete",
            native(HANDOFF),
            native(_workspace_delivery_bundle_response()),
            native(FINAL),
        ),
        "root_running": (),
        "root_rejected": (
            "incomplete",
            native(HANDOFF),
            native(_workspace_delivery_bundle_response()),
            native(FINAL),
        ),
        "growth_prepared": (
            "incomplete",
            native(HANDOFF),
            native(_workspace_delivery_bundle_response()),
            native(FINAL),
        ),
        "growth_running": (),
        "growth_rejected": (
            native(HANDOFF),
            native(_workspace_delivery_bundle_response()),
            native(FINAL),
        ),
        "growth_succeeded": (native(_workspace_delivery_bundle_response()), native(FINAL)),
        "closed": (native(FINAL),),
        "completed": (),
    }[phase]
    gateway = RoomGateway(*outcomes)
    fresh_harness = Observations()
    cold = runtime_for(gateway, fresh_harness, private, artifacts)
    cold._plan = runtime._plan
    await cold.restore_checkpoint(cp)
    restored_task = task.model_copy(update={"checkpoint": cp})
    if phase.endswith("running"):
        with pytest.raises(adapter.RuntimeExecutionError, match="outcome requires confirmation"):
            _ = [event async for event in cold.run(restored_task)]
        assert gateway.requests == []
        assert (await cold.save_checkpoint()).to_payload() == before
    else:
        observed = [event async for event in cold.run(restored_task)]
        assert observed[-1].kind is EventKind.RUNTIME_COMPLETED
        assert len(gateway.requests) == len(outcomes)
        saved = await cold.save_checkpoint()
        assert saved.state.get("context_window_compaction_version", 0) == version
        assert saved.state["usage"] == final.state["usage"]
        assert saved.state["step_usage"] == final.state["step_usage"]
        for key, row in model_rows(cp.state["models"]).items():
            if string(row["status"]) in {"rejected", "succeeded"}:
                assert mapping(saved.state["models"])[key] == row
        for key, value in mapping(cp.state["rejected_outputs"]).items():
            assert mapping(saved.state["rejected_outputs"])[key] == value
        if phase in {"root_prepared", "growth_prepared"}:
            index = 4 if phase == "root_prepared" else 5
            assert cold._model_request_sha256(gateway.requests[0]) == runtime._model_request_sha256(
                original.requests[index]
            )
    expected_tools = 0 if phase in {"root_running", "growth_running", "closed", "completed"} else 1
    assert len(fresh_harness.calls) == expected_tools
    assert all(call.tool_name == "workspace.bundle" for call in fresh_harness.calls)
    assert len(harness.calls) == 4
    assert cp.to_payload() == before


@pytest.mark.parametrize("version", [0, 1])
async def test_old_policy_high_input_does_not_silently_gain_output_room(
    monkeypatch: pytest.MonkeyPatch,
    version: int,
) -> None:
    # Same valid readonly/native input and saturated first growth: old0/1
    # cannot compact for its desired next cap and must retain the old failure.
    with pytest.raises(
        adapter.RuntimeExecutionError, match="post-tool finalization growth exhausted"
    ):
        await room_case(monkeypatch, version, older_count=5000)


@pytest.mark.parametrize("phase", ["prepared", "running", "succeeded"])
async def test_saved_provider_clipped_growth_cap_is_exact_on_restore(
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
) -> None:
    runtime, task, original, _, events = await room_case(monkeypatch, growths=1, output_max=10000)
    cp = next(
        event.checkpoint
        for event in events
        if event.checkpoint is not None
        and any(
            row["call_index"] == 5 and row["status"] == phase
            for row in model_rows(event.checkpoint.state["models"]).values()
        )
        and max(
            integer(row["call_index"])
            for row in model_rows(event.checkpoint.state["models"]).values()
        )
        == 5
    )
    assert cp is not None
    cp = RuntimeCheckpoint.from_payload(cp.to_payload())
    before = cp.to_payload()
    row = next(row for row in model_rows(cp.state["models"]).values() if row["call_index"] == 5)
    assert row["reserved_max_output_tokens"] == 10000
    artifacts, private = await prefix_stores(runtime, task, cp)
    outcomes = {
        "prepared": (native(HANDOFF), native(_workspace_delivery_bundle_response()), native(FINAL)),
        "running": (),
        "succeeded": (native(_workspace_delivery_bundle_response()), native(FINAL)),
    }[phase]
    gateway = RoomGateway(*outcomes, output_max=10000)
    harness = Observations()
    cold = runtime_for(gateway, harness, private, artifacts)
    cold._plan = runtime._plan
    await cold.restore_checkpoint(cp)
    restored = task.model_copy(update={"checkpoint": cp})
    if phase == "running":
        with pytest.raises(adapter.RuntimeExecutionError, match="outcome requires confirmation"):
            _ = [event async for event in cold.run(restored)]
        assert gateway.requests == [] and harness.calls == []
    else:
        result = [event async for event in cold.run(restored)]
        assert result[-1].kind is EventKind.RUNTIME_COMPLETED
        assert len(gateway.requests) == len(outcomes)
        if phase == "prepared":
            assert gateway.requests[0].max_output_tokens == 10000
            assert cold._model_request_sha256(gateway.requests[0]) == row["request_sha256"]
            assert row["request_sha256"] == runtime._model_request_sha256(original.requests[5])
        assert (await cold.save_checkpoint()).state["usage"] == (
            await runtime.save_checkpoint()
        ).state["usage"]
        assert len(harness.calls) == 1 and harness.calls[0].tool_name == "workspace.bundle"
    assert cp.to_payload() == before
