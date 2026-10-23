"""V2 permits useful code examples without changing deployed V1 recovery identity."""

from __future__ import annotations

import hashlib
from pathlib import Path
from uuid import UUID

import pytest

from agent_hub.domain.runs import TaskMode
from agent_hub.models.types import ModelResponse, TokenUsage
from agent_hub.runtime import defaults
from agent_hub.runtime.action_direct import _binding, _check_guidance, _guidance_identity
from agent_hub.runtime.artifacts import InMemoryArtifactRepository
from agent_hub.runtime.contracts import EventKind, JsonValue, TaskContext
from agent_hub.runtime.delivery_followup import (
    DELIVERY_FOLLOWUP_VERSION,
    delivery_followup_guidance,
)
from agent_hub.runtime.direct import DirectRuntime, RuntimeExecutionError
from tests.contracts.test_runtime_contract import FakeGateway
from tests.unit.runtime.test_action_direct import Backend, Policy
from tests.unit.runtime.test_action_direct_recovery_regressions import pause_at_write, runtime
from tests.unit.runtime.test_delivery_followup import opted, prompt, roles
from tests.unit.runtime.test_direct_project_verification import Model, batch, context

POLICY = "delivery_followup_version"
MARKER = "Delivery follow-up (v2):"
V1_TEXT = (
    "Delivery follow-up (v1): In the existing final answer or summary field, include one short, "
    "natural paragraph stating what was delivered and the limits of the actual verification evidence. "
    "Give startup/use or re-verification steps only when supported by the delivered files or observed "
    "tool results; otherwise state that the steps are not yet available. "
    "Do not invent commands, URLs, test passes, or turn failures into success. "
    "Keep required response schemas unchanged and do not paste source code. "
    "Leave the user a natural way to report results or request changes in the same project, "
    "without a mandatory question, a menu of choices, or an automatic new task. "
    "This instruction grants no tools or permissions, adds no provider calls, "
    "and does not authorize starting a preview."
)


def fixed_context() -> TaskContext:
    return context(run_id=UUID("11111111-1111-4111-8111-111111111111"),
                   tenant_id=UUID("22222222-2222-4222-8222-222222222222"),
                   actor_id=UUID("33333333-3333-4333-8333-333333333333"))


@pytest.mark.parametrize("mode", (TaskMode.DIRECT, TaskMode.DISPATCH, TaskMode.DISCUSS, TaskMode.HYBRID))
def test_v2_is_conditional_and_allows_language_tagged_code_examples(mode: TaskMode) -> None:
    ctx = opted(context(mode=mode, request="PRIVATE_REQUEST"), 2)
    before = ctx.to_payload()
    text = delivery_followup_guidance(ctx)
    assert text.startswith(MARKER) and len(text) < 1600
    for phrase in ("Only when delivering or updating an artifact or project",
                   "ordinary questions or explanations", "without a delivery template",
                   "what was delivered", "verification evidence and limits", "actual files or tool results",
                   "startup/use or re-verification", "report results or request changes",
                   "complete project source", "necessary code and command examples",
                   "language-tagged fenced code blocks", "required response schemas",
                   "Do not invent commands, URLs, test passes", "without a mandatory question",
                   "adds no provider calls", "does not authorize starting a preview"):
        assert phrase in text
    assert "do not paste source code" not in text
    assert "PRIVATE_REQUEST" not in text and str(ctx.tenant_id) not in text
    assert ctx.to_payload() == before


def test_deployed_v1_text_prompt_and_binding_remain_exact() -> None:
    ctx = opted(fixed_context(), 1)
    assert DELIVERY_FOLLOWUP_VERSION == 1
    assert delivery_followup_guidance(ctx) == V1_TEXT
    text = prompt(DirectRuntime(FakeGateway(), logical_model="main"), ctx)
    assert hashlib.sha256(text.encode()).hexdigest() == "ec1d3e483ea1ce3cfc82e7d0da7dbeb4efde85bb58242dd5c5351ec54f14c699"
    assert _binding(ctx, "main") == "e3cb90ea8f0b9d6880557855180bc7686cb42a09087d866d46d3ffc640e6287d"


def test_absent_flag_keeps_legacy_binding_and_no_followup_guidance() -> None:
    ctx = fixed_context()
    assert delivery_followup_guidance(ctx) == ""
    assert _binding(ctx, "main") == "838f8540fa274e8a6b492f0a0e980568ce8bd44c542af4f98ab12ef60ff8a58e"


@pytest.mark.parametrize("version", (None, False, True, 0, 1.0, 2.0, "2", 3))
def test_only_exact_supported_integers_can_select_and_persist_guidance(version: JsonValue) -> None:
    ctx = opted(context(), version)
    assert delivery_followup_guidance(ctx) == ""
    with pytest.raises(RuntimeExecutionError, match="guidance"):
        _check_guidance(ctx)


@pytest.mark.parametrize("saved_version,current_version", ((1, 1), (2, 2), (1, 2), (2, 1)))
async def test_cold_action_recovery_keeps_actual_version_and_does_not_replay_models(
    tmp_path: Path, saved_version: int, current_version: int,
) -> None:
    ctx = opted(context(), saved_version)
    policy, repository = Policy(), InMemoryArtifactRepository()
    model = Model(batch({"README.md": "Actual project usage instructions."}))
    first = Backend(tmp_path, ctx, repository)
    checkpoint = await pause_at_write(runtime(first, policy, repository, model), ctx)
    identity = _guidance_identity(ctx.routing_decision)
    assert type(identity[POLICY]) is int and identity[POLICY] == saved_version
    assert not first.calls and len(model.requests) == 1
    policy.allowed.update({"write", "create"})
    current = opted(ctx, current_version).model_copy(update={"checkpoint": checkpoint})
    cold = Backend(tmp_path, current, repository)
    resumed = runtime(cold, policy, repository, model)
    await resumed.restore_checkpoint(checkpoint)
    if saved_version != current_version:
        with pytest.raises(RuntimeExecutionError, match="context changed"):
            _ = [event async for event in resumed.run(current)]
        assert not cold.calls and not cold.root.exists()
    else:
        events = [event async for event in resumed.run(current)]
        assert events[-1].kind is EventKind.RUNTIME_COMPLETED
        assert cold.calls.count("workspace.bundle") == 1
    assert len(model.requests) == 1


async def test_v2_ordinary_answer_preserves_code_blocks_without_an_extra_model_call() -> None:
    ctx = opted(TaskContext(run_id=fixed_context().run_id, tenant_id=fixed_context().tenant_id,
        mode=TaskMode.DIRECT, request="Explain closures with a small JavaScript example."), 2)
    answer = "A closure retains its lexical environment.\n\n```javascript\nconst make = x => () => x;\n```"
    model = FakeGateway(ModelResponse(text=answer, usage=TokenUsage(10, 20, 30)))
    events = [event async for event in DirectRuntime(model, logical_model="main").run(ctx)]
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED and len(model.requests) == 1
    assert any(event.artifact is not None and event.artifact.content.get("text") == answer for event in events)
    rendered = "\n".join(str(message.content) for message in model.requests[0].messages)
    assert MARKER in rendered and V1_TEXT not in rendered


@pytest.mark.parametrize("mode", (TaskMode.DISPATCH, TaskMode.HYBRID))
def test_v2_uses_existing_dispatch_and_discussion_guidance_paths(mode: TaskMode) -> None:
    ctx = opted(context(mode=mode), 2)
    dispatch = defaults._dispatch_plan(roles(ctx, TaskMode.DISPATCH), ctx)
    discussion = defaults._discussion_plan(roles(ctx, TaskMode.DISCUSS), "main", ctx)
    assert MARKER in dispatch.steps[-1].task
    assert all(MARKER in participant.goal for participant in discussion.participants)
    assert "Delivery follow-up (v1):" not in dispatch.steps[-1].task
