from agent_hub.domain.runs import TaskMode
from agent_hub.runtime.role_catalog import RoleDefinition, default_role_catalog
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
        "implementation_strategist",
        "test_strategist",
        "skeptic",
        "risk_officer",
        "cost_estimator",
        "user_advocate",
        "decision_recorder",
    }.issubset(role_ids)
    assert len(plan.roles) >= 8
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
        "dependency_resolver",
        "network_tls_engineer",
        "release_engineer",
        "security_reviewer",
        "rollback_planner",
    }.issubset(role_ids)
    assert len(plan.roles) >= 8
    assert "moderator" not in role_ids
    assert plan.role("installer").purpose is RolePurpose.EXECUTE
    assert "run_safe_command" in plan.role("installer").allowed_tools
    assert "delete_file" in plan.role("installer").forbidden_actions


def test_research_discussion_includes_data_source_and_writer_roles() -> None:
    plan = RolePlanner().plan(
        RolePlanningRequest(
            task="调研一个市场并输出可执行建议",
            mode=TaskMode.DISCUSS,
            profile=TaskProfile.RESEARCH,
            default_model="research-model",
        )
    )

    role_ids = {role.id for role in plan.roles}

    assert {
        "moderator",
        "domain_researcher",
        "source_validator",
        "data_analyst",
        "synthesis_writer",
        "skeptic",
        "decision_recorder",
    }.issubset(role_ids)
    assert len(plan.roles) >= 7


def test_operations_dispatch_includes_monitoring_and_incident_roles() -> None:
    plan = RolePlanner().plan(
        RolePlanningRequest(
            task="排查线上系统异常并给出修复步骤",
            mode=TaskMode.DISPATCH,
            profile=TaskProfile.OPERATIONS,
            default_model="ops-model",
        )
    )

    role_ids = {role.id for role in plan.roles}

    assert {
        "incident_commander",
        "log_analyst",
        "metrics_analyst",
        "runbook_executor",
        "reliability_reviewer",
        "postmortem_writer",
    }.issubset(role_ids)
    assert len(plan.roles) >= 6


def test_general_discussion_includes_daily_work_creative_business_and_review_roles() -> None:
    plan = RolePlanner().plan(
        RolePlanningRequest(
            task="做一个短视频营销方案，同时评估预算和商业回报",
            mode=TaskMode.DISCUSS,
            profile=TaskProfile.GENERAL,
            default_model="general-model",
        )
    )

    role_ids = {role.id for role in plan.roles}

    assert {
        "director",
        "copywriter",
        "video_editor",
        "economic_analyst",
        "marketing_strategist",
        "product_manager",
        "finance_analyst",
    }.issubset(role_ids)
    assert "legal_compliance_reviewer" not in role_ids
    assert "sales_advisor" not in role_ids
    assert len(plan.roles) >= 12


def test_general_dispatch_includes_daily_execution_roles() -> None:
    plan = RolePlanner().plan(
        RolePlanningRequest(
            task="生成活动文案、预算测算、销售话术和交付清单",
            mode=TaskMode.DISPATCH,
            profile=TaskProfile.GENERAL,
            default_model="general-model",
        )
    )

    role_ids = {role.id for role in plan.roles}

    assert {
        "project_manager",
        "copywriter",
        "content_editor",
        "economic_analyst",
        "finance_analyst",
        "sales_advisor",
        "operations_coordinator",
        "quality_reviewer",
    }.issubset(role_ids)
    assert "legal_compliance_reviewer" not in role_ids
    assert len(plan.roles) >= 10


def test_video_prompt_dispatch_selects_creative_prompt_roles() -> None:
    plan = RolePlanner().plan(
        RolePlanningRequest(
            task="我想用即梦来生成 AI 视频，给我生成一段可直接使用的提示词。",
            mode=TaskMode.DISPATCH,
            profile=TaskProfile.GENERAL,
            default_model="general-model",
        )
    )

    role_ids = {role.id for role in plan.roles}

    assert "copywriter" in role_ids
    assert "director" in role_ids
    assert "video_editor" in role_ids
    assert "quality_reviewer" in role_ids


def test_role_catalog_can_be_extended_without_changing_role_planner_code() -> None:
    catalog = default_role_catalog().with_role(
        RoleDefinition(
            id="custom_hr_advisor",
            role="Custom HR Advisor",
            purpose="expertise",
            mission="Review hiring, team, incentive, and org design questions.",
            must_answer=("What people risk exists?",),
            allowed_tools=("read_context",),
            forbidden_actions=("do not contact candidates",),
            skills=("hr",),
            output_schema={"summary": "string", "risks": "string[]"},
            modes=frozenset({"discuss"}),
            profiles=frozenset({"general"}),
        )
    )

    plan = RolePlanner(role_catalog=catalog).plan(
        RolePlanningRequest(
            task="讨论团队招聘方案",
            mode=TaskMode.DISCUSS,
            profile=TaskProfile.GENERAL,
            requested_skills=("hr",),
        )
    )

    assert plan.role("custom_hr_advisor").role == "Custom HR Advisor"
    assert plan.role("custom_hr_advisor").skills == ("hr",)


def test_cross_domain_dispatch_can_combine_research_product_and_software_roles() -> None:
    plan = RolePlanner().plan(
        RolePlanningRequest(
            task="Research a product opportunity, define scope, and build a small web prototype.",
            mode=TaskMode.DISPATCH,
            profile=TaskProfile.SOFTWARE,
            profiles=(TaskProfile.SOFTWARE, TaskProfile.RESEARCH, TaskProfile.GENERAL),
            default_model="main-agent",
        )
    )

    role_ids = {role.id for role in plan.roles}

    assert "architect" in role_ids
    assert "implementer" in role_ids
    assert "product_manager" in role_ids
    assert "project_manager" in role_ids
    assert "quality_reviewer" in role_ids


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
