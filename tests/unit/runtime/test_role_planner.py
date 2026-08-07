from agent_hub.domain.runs import TaskMode
from agent_hub.runtime.role_planner import (
    RolePlanner,
    RolePlanningRequest,
    RolePurpose,
    TaskProfile,
)


def test_discussion_software_task_gets_dynamic_constrained_discussion_roles() -> None:
    plan = RolePlanner().plan(
        RolePlanningRequest(
            task="设计一个 Linux 上可部署的多 Agent 系统",
            mode=TaskMode.DISCUSS,
            profile=TaskProfile.SOFTWARE,
            high_risk=True,
            requested_skills=("system-design", "security-review"),
            default_model="main-agent",
        )
    )

    role_ids = {role.id for role in plan.roles}

    assert plan.requires_user is False
    assert plan.mode is TaskMode.DISCUSS
    assert {
        "moderator",
        "software_architect",
        "skeptic",
        "risk_officer",
        "decision_recorder",
    }.issubset(role_ids)
    assert all(role.model == "main-agent" for role in plan.roles)
    assert all(role.output_schema for role in plan.roles)
    assert plan.role("skeptic").purpose is RolePurpose.CRITIQUE
    assert "不允许直接执行外部操作" in plan.role("skeptic").forbidden_actions
    assert "system-design" in plan.role("software_architect").skills
    assert "security-review" in plan.role("risk_officer").skills


def test_dispatch_deployment_task_gets_execution_roles_not_discussion_only_roles() -> None:
    plan = RolePlanner().plan(
        RolePlanningRequest(
            task="在新 Linux 云服务器上一键部署并自动排障",
            mode=TaskMode.DISPATCH,
            profile=TaskProfile.DEPLOYMENT,
            default_model="ops-model",
        )
    )

    role_ids = {role.id for role in plan.roles}

    assert plan.mode is TaskMode.DISPATCH
    assert {
        "ops_planner",
        "installer",
        "doctor_agent",
        "security_reviewer",
        "rollback_planner",
    }.issubset(role_ids)
    assert "moderator" not in role_ids
    assert plan.role("installer").purpose is RolePurpose.EXECUTE
    assert "run_safe_command" in plan.role("installer").allowed_tools
    assert "delete_file" in plan.role("installer").forbidden_actions


def test_unknown_high_risk_role_plan_asks_user_instead_of_guessing() -> None:
    plan = RolePlanner().plan(
        RolePlanningRequest(
            task="帮我处理这个事情，可能会影响外部系统",
            mode=TaskMode.HYBRID,
            profile=TaskProfile.UNKNOWN,
            high_risk=True,
        )
    )

    assert plan.requires_user is True
    assert plan.reason == "ambiguous_high_risk_role_plan"
    assert plan.roles == ()


def test_specialist_model_overrides_only_matching_roles() -> None:
    plan = RolePlanner().plan(
        RolePlanningRequest(
            task="写代码并审查",
            mode=TaskMode.DISPATCH,
            profile=TaskProfile.SOFTWARE,
            default_model="general",
            model_overrides={"tester": "cheap-model", "security_reviewer": "reasoner"},
        )
    )

    assert plan.role("implementer").model == "general"
    assert plan.role("tester").model == "cheap-model"
    assert plan.role("security_reviewer").model == "reasoner"
