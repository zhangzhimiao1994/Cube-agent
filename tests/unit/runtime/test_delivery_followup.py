"""Opt-in delivery guidance must preserve legacy prompts and recovery identity."""

from __future__ import annotations

from importlib import import_module
from pathlib import Path
from uuid import UUID

import pytest

from agent_hub.domain.runs import TaskMode
from agent_hub.runtime import defaults
from agent_hub.runtime.action_direct import _binding, _check_guidance
from agent_hub.runtime.artifacts import InMemoryArtifactRepository
from agent_hub.runtime.contracts import EventKind, JsonValue, TaskContext
from agent_hub.runtime.direct import DirectRuntime, RuntimeExecutionError
from agent_hub.runtime.role_planner import RolePlanner, RolePlanningRequest, TaskProfile
from tests.contracts.test_runtime_contract import FakeGateway
from tests.unit.runtime.test_action_direct import Backend, Policy
from tests.unit.runtime.test_action_direct_recovery_regressions import pause_at_write, runtime
from tests.unit.runtime.test_direct_project_verification import Model, batch, context

POLICY = "delivery_followup_version"
MARKER = "Delivery follow-up (v1):"


def opted(ctx: TaskContext, value: JsonValue = 1) -> TaskContext:
    return ctx.model_copy(update={"routing_decision": {**ctx.routing_decision, POLICY: value}})


def prompt(subject: DirectRuntime, ctx: TaskContext) -> str:
    outcome = subject._build_prompt(ctx)
    assert outcome.error_code is None and outcome.messages is not None
    return "\n".join(str(message.content) for message in outcome.messages)


@pytest.mark.parametrize("mode", (TaskMode.DIRECT, TaskMode.DISPATCH, TaskMode.DISCUSS, TaskMode.HYBRID))
def test_guidance_is_shared_bounded_and_does_not_serialize_private_context(mode: TaskMode) -> None:
    module = import_module("agent_hub.runtime.delivery_followup")
    ctx = opted(context(mode=mode, request="PRIVATE_REQUEST_NOT_FOR_GUIDANCE"))
    text = module.delivery_followup_guidance(ctx)
    assert module.DELIVERY_FOLLOWUP_POLICY == POLICY
    assert module.DELIVERY_FOLLOWUP_VERSION == 1
    assert text.startswith(MARKER) and len(text) < 1400
    for phrase in ("what was delivered", "verification evidence", "delivered files",
                   "startup/use", "re-verification", "report results or request changes",
                   "required response schemas", "Do not invent commands, URLs, test passes",
                   "without a mandatory question", "adds no provider calls",
                   "does not authorize starting a preview"):
        assert phrase in text
    assert "PRIVATE_REQUEST_NOT_FOR_GUIDANCE" not in text
    assert str(ctx.tenant_id) not in text and str(ctx.actor_id) not in text


@pytest.mark.parametrize("value", (None, False, True, 0, 1.0, "1", 3))
def test_unsupported_or_boolean_versions_do_not_change_legacy_prompt(value: JsonValue) -> None:
    ctx = context()
    subject = DirectRuntime(FakeGateway(), logical_model="main")
    assert prompt(subject, opted(ctx, value)) == prompt(subject, ctx)


def test_direct_guidance_is_prompt_only_and_legacy_prompt_is_unchanged() -> None:
    ctx = context()
    subject = DirectRuntime(FakeGateway(), logical_model="main")
    legacy = prompt(subject, ctx)
    enabled = prompt(subject, opted(ctx))
    assert MARKER not in legacy and MARKER in enabled
    guidance = import_module("agent_hub.runtime.delivery_followup").delivery_followup_guidance(opted(ctx))
    assert enabled.replace("\n\n" + guidance, "") == legacy
    assert ctx.routing_decision.get(POLICY) is None


async def test_direct_does_not_add_model_calls_or_mutate_identity_and_budget() -> None:
    ctx = opted(TaskContext(run_id=context().run_id, tenant_id=context().tenant_id,
                            mode=TaskMode.DIRECT, request="Summarize the delivery."))
    original = ctx.to_payload()
    model = FakeGateway()
    events = [event async for event in DirectRuntime(model, logical_model="main").run(ctx)]
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert len(model.requests) == 1
    assert MARKER in "\n".join(str(message.content) for message in model.requests[0].messages)
    assert ctx.to_payload() == original


def roles(ctx: TaskContext, mode: TaskMode):
    return RolePlanner().plan(RolePlanningRequest(
        task=ctx.request, mode=mode, profile=TaskProfile.SOFTWARE, default_model="main",
    )).roles


@pytest.mark.parametrize("mode", (TaskMode.DISPATCH, TaskMode.HYBRID))
def test_dispatch_final_synthesizer_gets_guidance_without_changing_other_steps(mode: TaskMode) -> None:
    ctx = context(mode=mode)
    selected = roles(ctx, TaskMode.DISPATCH)
    legacy = defaults._dispatch_plan(selected, ctx)
    enabled = defaults._dispatch_plan(selected, opted(ctx))
    assert len(enabled.steps) == len(legacy.steps) and enabled.agents == legacy.agents
    assert enabled.steps[:-1] == legacy.steps[:-1]
    final, old_final = enabled.steps[-1], legacy.steps[-1]
    assert final.final_synthesizer and MARKER in final.task and MARKER not in old_final.task
    guidance = import_module("agent_hub.runtime.delivery_followup").delivery_followup_guidance(opted(ctx))
    assert final.task.replace(guidance + " ", "") == old_final.task
    assert (final.tools, final.token_budget, final.timeout_seconds, final.cost_budget_usd) == (
        old_final.tools, old_final.token_budget, old_final.timeout_seconds, old_final.cost_budget_usd)


@pytest.mark.parametrize("mode", (TaskMode.DISCUSS, TaskMode.HYBRID))
def test_discussion_participant_goals_get_guidance_without_new_tools_or_models(mode: TaskMode) -> None:
    ctx = context(mode=mode)
    selected = roles(ctx, TaskMode.DISCUSS)
    legacy = defaults._discussion_plan(selected, "main", ctx)
    enabled = defaults._discussion_plan(selected, "main", opted(ctx))
    assert len(enabled.participants) == len(legacy.participants)
    for actual, old in zip(enabled.participants, legacy.participants, strict=True):
        assert MARKER in actual.goal and MARKER not in old.goal
        assert (actual.id, actual.logical_model, actual.allowed_tools, actual.max_output_tokens) == (
            old.id, old.logical_model, old.allowed_tools, old.max_output_tokens)
    assert (enabled.token_budget, enabled.wall_time_seconds, enabled.max_turns) == (
        legacy.token_budget, legacy.wall_time_seconds, legacy.max_turns)


def test_legacy_action_binding_stays_byte_identical_without_new_flag() -> None:
    ctx = context(run_id=UUID("11111111-1111-4111-8111-111111111111"),
                  tenant_id=UUID("22222222-2222-4222-8222-222222222222"),
                  actor_id=UUID("33333333-3333-4333-8333-333333333333"))
    assert _binding(ctx, "main") == "838f8540fa274e8a6b492f0a0e980568ce8bd44c542af4f98ab12ef60ff8a58e"
    assert _binding(opted(ctx), "main") != _binding(ctx, "main")


@pytest.mark.parametrize("value", (None, False, True, 0, 1.0, "1", 3))
def test_action_direct_rejects_present_invalid_version_before_io(value: JsonValue) -> None:
    with pytest.raises(RuntimeExecutionError, match="guidance"):
        _check_guidance(opted(context(), value))


@pytest.mark.parametrize("mutation", ("unchanged", "removed", "added"))
async def test_action_approval_cold_restore_binds_guidance_without_an_extra_model_call(
    tmp_path: Path, mutation: str,
) -> None:
    ctx = context() if mutation == "added" else opted(context())
    policy, repository = Policy(), InMemoryArtifactRepository()
    model = Model(batch({"README.md": "The delivered project instructions."}))
    first = Backend(tmp_path, ctx, repository)
    checkpoint = await pause_at_write(runtime(first, policy, repository, model), ctx)
    assert first.calls == [] and len(model.requests) == 1
    text = "\n".join(str(message.content) for message in model.requests[0].messages)
    assert (MARKER in text) is (mutation != "added")
    policy.allowed.update({"write", "create"})
    route = dict(ctx.routing_decision)
    if mutation == "removed":
        route.pop(POLICY)
    elif mutation == "added":
        route[POLICY] = 1
    current = ctx.model_copy(update={"checkpoint": checkpoint, "routing_decision": route})
    cold = Backend(tmp_path, current, repository)
    resumed = runtime(cold, policy, repository, model)
    await resumed.restore_checkpoint(checkpoint)
    if mutation != "unchanged":
        with pytest.raises(RuntimeExecutionError, match="context changed"):
            _ = [event async for event in resumed.run(current)]
        assert cold.calls == [] and not cold.root.exists()
    else:
        events = [event async for event in resumed.run(current)]
        assert events[-1].kind is EventKind.RUNTIME_COMPLETED
        assert cold.calls.count("workspace.write_text") == 2
        assert (cold.root / "README.md").read_text() == "The delivered project instructions."
        assert (cold.root / "DELIVERY_MANIFEST.json").is_file()
        assert cold.calls.count("workspace.bundle") == 1
    assert len(model.requests) == 1
