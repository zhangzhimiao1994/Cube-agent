from __future__ import annotations

from uuid import uuid4

from agent_hub.domain.runs import TaskMode
from agent_hub.runtime.contracts import TaskContext
from agent_hub.runtime.defaults import _producer_step_timeout, _role_step_timeout
from agent_hub.runtime.role_planner import RoleAssignment, RolePurpose


def _context(timeout_seconds: float) -> TaskContext:
    return TaskContext(
        run_id=uuid4(),
        tenant_id=uuid4(),
        actor_id=uuid4(),
        mode=TaskMode.HYBRID,
        request="Build and verify a project artifact.",
        timeout_seconds=timeout_seconds,
        token_budget=1_000_000,
    )


def _role(role_id: str, *, purpose: RolePurpose = RolePurpose.EXECUTE) -> RoleAssignment:
    return RoleAssignment(
        id=role_id,
        role=role_id.title(),
        purpose=purpose,
        mission="Complete the assigned project delivery work.",
        must_answer=("result",),
        allowed_tools=(),
        forbidden_actions=("do not request extra user input",),
        skills=(),
        output_schema={"summary": "string"},
        model="qwen",
    )


def test_producer_step_timeout_keeps_short_interaction_budget_tight() -> None:
    roles = (
        _role("architect"),
        _role("implementer"),
        _role("tester", purpose=RolePurpose.VERIFY),
        _role("security", purpose=RolePurpose.RISK_REVIEW),
    )

    assert _producer_step_timeout(_context(300), roles) == 120


def test_producer_step_timeout_reserves_fallback_room_for_project_scale_runs() -> None:
    roles = (
        _role("architect"),
        _role("implementer"),
        _role("tester", purpose=RolePurpose.VERIFY),
        _role("security", purpose=RolePurpose.RISK_REVIEW),
    )

    assert _producer_step_timeout(_context(900), roles) == 405


def test_ultra_workspace_writer_timeout_scales_to_the_dynamic_cap() -> None:
    context = TaskContext(
        run_id=uuid4(),
        tenant_id=uuid4(),
        actor_id=uuid4(),
        mode=TaskMode.DISPATCH,
        request="Build a real ultra-large project in the workspace.",
        timeout_seconds=2700,
        token_budget=1_000_000,
        routing_decision={
            "project_scale": "ultra",
            "project_delivery": "workspace",
            "artifact_strategy": "workspace_bundle",
        },
    )
    role = _role("implementer")

    timeout = _role_step_timeout(
        context,
        role,
        tools=("workspace.write_text",),
        producer_step_timeout=900,
        post_product_step_timeout=1200,
    )

    assert timeout == 1800


def test_non_project_workspace_writer_keeps_the_normal_producer_timeout() -> None:
    context = _context(1200)
    role = _role("implementer")

    timeout = _role_step_timeout(
        context,
        role,
        tools=("workspace.write_text",),
        producer_step_timeout=300,
        post_product_step_timeout=600,
    )

    assert timeout == 300
