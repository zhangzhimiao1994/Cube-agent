import pytest

from agent_hub.domain.runs import TaskMode
from agent_hub.models.types import ModelCapability
from agent_hub.runtime.role_catalog import RoleDefinition, default_role_catalog
from agent_hub.runtime.role_planner import (
    RoleAssignment,
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


def test_multimedia_generation_dispatch_adds_dedicated_executor_role() -> None:
    plan = RolePlanner().plan(
        RolePlanningRequest(
            task="Generate a product image and then generate a short video from the approved plan.",
            mode=TaskMode.DISPATCH,
            profile=TaskProfile.GENERAL,
            default_model="general-model",
        )
    )

    executor = plan.role("multimedia_generator")

    assert executor.purpose is RolePurpose.EXECUTE
    assert "generate_multimedia" in executor.allowed_tools
    assert "submit_video_to_text_only_model" in executor.forbidden_actions


def test_multimedia_generator_is_not_selected_for_non_generation_tasks() -> None:
    plan = RolePlanner().plan(
        RolePlanningRequest(
            task="请做一个产品发布方案，分析是否需要图片、视频或语音素材，但暂不生成视频。",
            mode=TaskMode.DISPATCH,
            profile=TaskProfile.GENERAL,
            default_model="general-model",
        )
    )

    role_ids = {role.id for role in plan.roles}

    assert "multimedia_generator" not in role_ids


def test_docx_generation_dispatch_selects_document_writer_tool_role() -> None:
    plan = RolePlanner().plan(
        RolePlanningRequest(
            task="请生成一份 Word 项目复盘文档，包含摘要、风险和下一步。",
            mode=TaskMode.DISPATCH,
            profile=TaskProfile.GENERAL,
            default_model="general-model",
        )
    )

    writer = plan.role("document_writer")

    assert writer.purpose is RolePurpose.EXECUTE
    assert "document.generate_docx" in writer.allowed_tools
    assert "tool_calling" in " ".join((*writer.must_answer, writer.mission))


def test_pptx_generation_dispatch_selects_presentation_designer_tool_role() -> None:
    plan = RolePlanner().plan(
        RolePlanningRequest(
            task="Build a PowerPoint deck using the technical-blueprint template.",
            mode=TaskMode.DISPATCH,
            profile=TaskProfile.GENERAL,
            default_model="general-model",
        )
    )

    designer = plan.role("presentation_designer")

    assert designer.purpose is RolePurpose.EXECUTE
    assert "presentation.generate_pptx" in designer.allowed_tools
    assert "consulting-clean" in designer.mission
    assert "technical-blueprint" in designer.mission
    assert "dark-launch" in designer.mission


def test_office_generation_allows_local_exclusions_without_disabling_file_generation() -> None:
    docx_plan = RolePlanner().plan(
        RolePlanningRequest(
            task="Generate a DOCX report without appendix.",
            mode=TaskMode.DISPATCH,
            profile=TaskProfile.GENERAL,
            default_model="general-model",
        )
    )
    pptx_plan = RolePlanner().plan(
        RolePlanningRequest(
            task="Create a PPTX deck without speaker notes.",
            mode=TaskMode.DISPATCH,
            profile=TaskProfile.GENERAL,
            default_model="general-model",
        )
    )

    assert docx_plan.role("document_writer")
    assert pptx_plan.role("presentation_designer")


@pytest.mark.parametrize(
    ("task", "absent_role_id"),
    (
        ("Please review this Word document for clarity.", "document_writer"),
        ("Discuss the slide deck outline.", "presentation_designer"),
        ("Please review this PowerPoint deck for clarity.", "presentation_designer"),
        ("请审查这个 Word 文档的逻辑。", "document_writer"),
        ("请讨论这个 docx 文档的结构。", "document_writer"),
        ("Please review this Word file for clarity.", "document_writer"),
        ("Please review this DOCX file for clarity.", "document_writer"),
        ("Please review this PPT file for clarity.", "presentation_designer"),
        ("Draft a document in Markdown, no DOCX file.", "document_writer"),
        ("Write a report about market trends.", "document_writer"),
        ("Draft a presentation outline, no PPTX file.", "presentation_designer"),
    ),
)
def test_existing_office_content_review_does_not_select_generation_roles(
    task: str,
    absent_role_id: str,
) -> None:
    plan = RolePlanner().plan(
        RolePlanningRequest(
            task=task,
            mode=TaskMode.DISPATCH,
            profile=TaskProfile.GENERAL,
            default_model="general-model",
        )
    )

    assert absent_role_id not in {role.id for role in plan.roles}


@pytest.mark.parametrize(
    ("task", "expected_role_id", "absent_role_id"),
    (
        ("Create a PPTX deck, no DOCX file needed.", "presentation_designer", "document_writer"),
        ("Generate a DOCX report, no PPTX file needed.", "document_writer", "presentation_designer"),
        ("Create a PowerPoint file, no Word document.", "presentation_designer", "document_writer"),
    ),
)
def test_office_generation_negations_are_scoped_to_the_requested_file_type(
    task: str,
    expected_role_id: str,
    absent_role_id: str,
) -> None:
    plan = RolePlanner().plan(
        RolePlanningRequest(
            task=task,
            mode=TaskMode.DISPATCH,
            profile=TaskProfile.GENERAL,
            default_model="general-model",
        )
    )
    role_ids = {role.id for role in plan.roles}

    assert expected_role_id in role_ids
    assert absent_role_id not in role_ids


def test_office_generation_does_not_add_model_capabilities() -> None:
    assert "docx_generation" not in {capability.value for capability in ModelCapability}
    assert "pptx_generation" not in {capability.value for capability in ModelCapability}


def test_dotted_tool_names_are_limited_to_known_built_ins() -> None:
    RoleDefinition(
        id="custom_document_writer",
        role="Custom Document Writer",
        purpose="execute",
        mission="Generate a document artifact.",
        must_answer=("What file was generated?",),
        allowed_tools=("read_context", "document.generate_docx"),
        forbidden_actions=("do not claim success without an artifact",),
        skills=("writing",),
        output_schema={"summary": "string"},
        modes=frozenset({"dispatch"}),
        profiles=frozenset({"general"}),
    )
    RoleAssignment(
        id="project_builder",
        role="Project Builder",
        purpose=RolePurpose.EXECUTE,
        mission="Generate a downloadable project archive.",
        must_answer=("What archive was generated?",),
        allowed_tools=("read_context", "project.generate_zip"),
        forbidden_actions=("do not claim success without an artifact",),
        skills=("test",),
        output_schema={"summary": "string"},
        model="general",
    )

    with pytest.raises(ValueError, match="safe tool names"):
        RoleDefinition(
            id="custom_web_role",
            role="Custom Web Role",
            purpose="execute",
            mission="Use an unknown dotted tool.",
            must_answer=("What happened?",),
            allowed_tools=("read_context", "web.search"),
            forbidden_actions=("do not bypass review",),
            skills=("research",),
            output_schema={"summary": "string"},
            modes=frozenset({"dispatch"}),
            profiles=frozenset({"general"}),
        )

    with pytest.raises(ValueError, match="safe tool names"):
        RoleAssignment(
            id="unsafe_tool_role",
            role="Unsafe Tool Role",
            purpose=RolePurpose.EXECUTE,
            mission="Use an unknown dotted tool.",
            must_answer=("What happened?",),
            allowed_tools=("read_context", "web.search"),
            forbidden_actions=("do not bypass review",),
            skills=("research",),
            output_schema={"summary": "string"},
            model="general",
        )


def test_general_generation_plan_does_not_select_quality_reviewer_by_default() -> None:
    plan = RolePlanner().plan(
        RolePlanningRequest(
            task="给我生成一个中秋晚会的方案",
            mode=TaskMode.DISPATCH,
            profile=TaskProfile.GENERAL,
            default_model="general-model",
        )
    )

    role_ids = {role.id for role in plan.roles}

    assert "product_manager" in role_ids
    assert "project_manager" in role_ids
    assert "quality_reviewer" not in role_ids


def test_explicit_quality_check_still_selects_quality_reviewer() -> None:
    plan = RolePlanner().plan(
        RolePlanningRequest(
            task="请对这个活动方案做质量检查和验收，确认是否可交付。",
            mode=TaskMode.DISPATCH,
            profile=TaskProfile.GENERAL,
            default_model="general-model",
        )
    )

    role_ids = {role.id for role in plan.roles}

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
