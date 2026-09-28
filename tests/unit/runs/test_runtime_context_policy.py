from __future__ import annotations

from typing import cast
from uuid import uuid4

from agent_hub.domain.runs import TaskMode
from agent_hub.runs.repository import ConversationContextItem
from agent_hub.runs.service import (
    _checkpoint_progress_units,
    _conversation_history_artifact,
    _conversation_history_line_budget,
    _conversation_history_token_budget,
    _local_main_agent_auto_mode,
    _project_delivery_assessment,
    _routing_runtime_timeout_seconds,
    _runtime_timeout_seconds,
    _runtime_token_budget,
)
from agent_hub.runtime.contracts import RuntimeCheckpoint


def test_runtime_timeout_policy_uses_configured_production_window_for_dispatch() -> None:
    assert _runtime_timeout_seconds(TaskMode.DISPATCH, configured_seconds=300.0) == 300.0


def test_runtime_timeout_policy_clamps_to_runtime_contract_limit() -> None:
    assert _runtime_timeout_seconds(TaskMode.HYBRID, configured_seconds=7200.0) == 3600.0


def test_runtime_token_budget_policy_uses_configured_complex_budget() -> None:
    assert _runtime_token_budget(TaskMode.HYBRID, configured_tokens=1_000_000) == 1_000_000


def test_runtime_token_budget_policy_clamps_to_runtime_contract_limit() -> None:
    assert _runtime_token_budget(TaskMode.DISPATCH, configured_tokens=99_000_000) == 10_000_000


def test_project_delivery_assessment_routes_natural_chinese_cloud_drive_to_large_workspace() -> None:
    assessment = _project_delivery_assessment("编写一个网盘网站")

    assert assessment == {
        "project_scale": "large",
        "project_delivery": "workspace",
        "artifact_strategy": "workspace_bundle",
        "website_preview_required": True,
        "runtime_timeout_seconds": 1200.0,
        "runtime_timeout_soft_seconds": 1200.0,
        "runtime_timeout_absolute_seconds": 3600.0,
        "runtime_timeout_source": "project_scale_soft_budget",
        "critical_path_complexity_units": 3,
        "critical_path_baseline_units": 7,
    }
    assert _local_main_agent_auto_mode("编写一个网盘网站", ()) is TaskMode.HYBRID


def test_project_delivery_assessment_covers_all_four_scales() -> None:
    cases = (
        ("创建一个简单的 hello world 网站", "small", 300.0),
        ("开发一个公司官网", "medium", 600.0),
        ("开发一个包含登录、上传和分享功能的网盘网站", "large", 1200.0),
        ("构建一个超大型企业项目组合管理平台", "ultra", 1800.0),
    )

    for message, expected_scale, expected_timeout in cases:
        assessment = _project_delivery_assessment(message)
        assert assessment is not None
        assert assessment["project_scale"] == expected_scale
        assert assessment["runtime_timeout_seconds"] == expected_timeout


def test_project_delivery_assessment_ignores_non_execution_planning_questions() -> None:
    assert _project_delivery_assessment("网盘网站应该怎么规划？不要开始实现") is None


def test_project_delivery_assessment_ignores_document_about_software() -> None:
    assert _project_delivery_assessment("生成一份网站需求文档") is None
    assert _project_delivery_assessment("编写网站测试报告") is None
    assert _project_delivery_assessment("分析如何实现网站") is None


def test_project_delivery_assessment_upgrades_feature_rich_website_to_large() -> None:
    assessment = _project_delivery_assessment(
        "开发一个带登录、文件上传、分享链接、全文搜索和角色权限的文件管理网站"
    )

    assert assessment is not None
    assert assessment["project_scale"] == "large"
    assert assessment["runtime_timeout_seconds"] == 1200.0


def test_project_delivery_budget_grows_when_complexity_exceeds_declared_scale() -> None:
    assessment = _project_delivery_assessment(
        "创建一个简单网站，包含前端、后端、数据库、登录、上传、搜索、权限、部署和端到端测试"
    )

    assert assessment is not None
    assert assessment["project_scale"] == "small"
    assert assessment["runtime_timeout_soft_seconds"] == 300.0
    assert cast(int, assessment["critical_path_complexity_units"]) > cast(
        int, assessment["critical_path_baseline_units"]
    )
    assert cast(float, assessment["runtime_timeout_seconds"]) > 300.0


def test_project_runtime_budget_extends_only_when_checkpoint_made_progress() -> None:
    decision = {
        "runtime_timeout_seconds": 600.0,
        "runtime_timeout_soft_seconds": 300.0,
        "runtime_timeout_source": "project_scale_soft_budget",
        "critical_path_complexity_units": 8,
        "runtime_timeout_absolute_seconds": 900.0,
    }

    assert _routing_runtime_timeout_seconds(decision, progress_units=0) == 600.0
    assert _routing_runtime_timeout_seconds(decision, progress_units=2) == 675.0
    assert _routing_runtime_timeout_seconds(decision, progress_units=99) == 900.0


def test_operator_runtime_limit_is_not_extended_by_project_progress() -> None:
    decision = {
        "runtime_timeout_seconds": 720.0,
        "runtime_timeout_source": "operator",
        "runtime_timeout_soft_seconds": 300.0,
    }

    assert _routing_runtime_timeout_seconds(decision, progress_units=10) == 720.0


def test_checkpoint_input_artifacts_do_not_extend_runtime_budget() -> None:
    run_id = uuid4()
    tenant_id = uuid4()
    input_id = uuid4()
    output_id = uuid4()
    checkpoint = RuntimeCheckpoint(
        id=uuid4(),
        runtime_type="crew",
        runtime_version="1",
        run_id=run_id,
        tenant_id=tenant_id,
        mode=TaskMode.DISPATCH,
        state={
            "input_refs": (
                {"id": str(input_id), "sha256": "a" * 64},
            ),
            "artifact_registry": {
                str(input_id): "a" * 64,
                str(output_id): "b" * 64,
            },
            "completed": (),
        },
    )

    assert _checkpoint_progress_units(checkpoint) == 1


def test_conversation_history_budget_uses_main_agent_context_window() -> None:
    assert (
        _conversation_history_token_budget(
            runtime_token_budget=1_000_000,
            main_agent_context_window_tokens=4096,
        )
        == 1024
    )


def test_conversation_history_budget_reserves_request_and_output_tokens() -> None:
    assert (
        _conversation_history_token_budget(
            runtime_token_budget=2000,
            main_agent_context_window_tokens=4096,
            current_request_tokens=1000,
            reserved_output_tokens=800,
        )
        == 200
    )


def test_conversation_history_line_watermark_grows_with_context_budget() -> None:
    assert _conversation_history_line_budget(160) == 18
    assert _conversation_history_line_budget(12_000) == 125


def test_conversation_history_stays_full_when_inside_budget() -> None:
    artifact = _conversation_history_artifact(
        conversation_id="conv-short",
        current_request="continue",
        context_items=(
            ConversationContextItem(
                run_id=uuid4(),
                request="first request",
                artifacts=(
                    {
                        "producer": "main_agent",
                        "content": {"text": "first answer"},
                    },
                ),
            ),
        ),
        history_token_budget=4096,
    )

    assert artifact is not None
    assert artifact.producer == "conversation_history"
    assert artifact.content["context_policy"] == "full_history"
    text = artifact.content["text"]
    assert isinstance(text, str)
    assert "first request" in text
    assert "first answer" in text


def test_conversation_history_full_artifact_identity_is_deterministic() -> None:
    item = ConversationContextItem(
        run_id=uuid4(),
        request="first request",
        artifacts=(
            {
                "producer": "main_agent",
                "content": {"text": "first answer"},
            },
        ),
    )

    first = _conversation_history_artifact(
        conversation_id="conv-stable",
        current_request="continue",
        context_items=(item,),
        history_token_budget=4096,
    )
    second = _conversation_history_artifact(
        conversation_id="conv-stable",
        current_request="continue",
        context_items=(item,),
        history_token_budget=4096,
    )

    assert first is not None and second is not None
    assert first.id == second.id
    assert first.content_sha256 == second.content_sha256


def test_conversation_history_is_auto_compacted_when_over_model_budget() -> None:
    old_noise = "old implementation detail " * 2000
    latest_decision = "latest important conclusion: use framework-level context compression"

    artifact = _conversation_history_artifact(
        conversation_id="conv-long",
        current_request="continue the work",
        context_items=(
            ConversationContextItem(
                run_id=uuid4(),
                request=old_noise,
                artifacts=(
                    {
                        "producer": "main_agent",
                        "content": {"text": old_noise},
                    },
                ),
            ),
            ConversationContextItem(
                run_id=uuid4(),
                request="what was the final decision?",
                artifacts=(
                    {
                        "producer": "main_agent",
                        "content": {"text": latest_decision},
                    },
                ),
            ),
        ),
        history_token_budget=256,
    )

    assert artifact is not None
    assert artifact.producer == "conversation_history_compacted"
    assert artifact.content["context_policy"] == "auto_compacted"
    original_tokens = artifact.content["original_estimated_tokens"]
    history_budget = artifact.content["history_token_budget"]
    text = artifact.content["text"]
    assert type(original_tokens) is int
    assert type(history_budget) is int
    assert isinstance(text, str)
    assert original_tokens > history_budget
    assert latest_decision in text


def test_conversation_history_compaction_preserves_origin_goal_anchor() -> None:
    first_goal = "初始目标：完成 Agent Hub，并且所有高风险操作都必须审批。"
    items = [
        ConversationContextItem(
            run_id=uuid4(),
            request=first_goal,
            artifacts=(
                {
                    "producer": "main_agent",
                    "content": {"text": "长期约束：服务器增量部署，GitHub 全量推送。"},
                },
            ),
        )
    ]
    items.extend(
        ConversationContextItem(
            run_id=uuid4(),
            request=f"中间讨论 {index} " * 30,
            artifacts=(
                {
                    "producer": "main_agent",
                    "content": {"text": f"中间结果 {index} " * 30},
                },
            ),
        )
        for index in range(12)
    )
    latest_decision = "最新结论：上下文压缩属于对话框架，不属于进化模块。"
    items.append(
        ConversationContextItem(
            run_id=uuid4(),
            request="确认长期记忆归属",
            artifacts=(
                {
                    "producer": "main_agent",
                    "content": {"text": latest_decision},
                },
            ),
        )
    )

    artifact = _conversation_history_artifact(
        conversation_id="conv-framework-memory",
        current_request="继续当前任务",
        context_items=tuple(items),
        history_token_budget=128,
    )

    assert artifact is not None
    assert artifact.content["context_policy"] == "auto_compacted"
    text = artifact.content["text"]
    assert isinstance(text, str)
    assert first_goal in text
    assert "服务器增量部署" in text
    assert latest_decision in text

def test_conversation_history_compaction_preserves_latest_request_without_artifacts() -> None:
    first_goal = "初始目标：完成 Agent Hub，并且所有高风险操作都必须审批。"
    latest_decision = "最新结论：上下文压缩属于对话框架，不属于进化模块。"
    items = [
        ConversationContextItem(run_id=uuid4(), request=first_goal, artifacts=()),
    ]
    items.extend(
        ConversationContextItem(
            run_id=uuid4(),
            request=f"中间讨论 {index} " * 300,
            artifacts=(),
        )
        for index in range(4)
    )
    items.append(
        ConversationContextItem(run_id=uuid4(), request=latest_decision, artifacts=())
    )

    artifact = _conversation_history_artifact(
        conversation_id="conv-framework-memory-requests-only",
        current_request="继续当前任务",
        context_items=tuple(items),
        history_token_budget=128,
    )

    assert artifact is not None
    assert artifact.content["context_policy"] == "auto_compacted"
    text = artifact.content["text"]
    assert isinstance(text, str)
    assert first_goal in text
    assert latest_decision in text


def test_conversation_history_compaction_recalls_relevant_middle_turn() -> None:
    middle_decision = "中段关键结论：数据库迁移必须采用蓝绿双写方案。"
    items = [
        ConversationContextItem(run_id=uuid4(), request="初始目标：完成迁移", artifacts=()),
    ]
    items.extend(
        ConversationContextItem(
            run_id=uuid4(),
            request=middle_decision if index == 8 else f"普通讨论 {index} " * 120,
            artifacts=(),
        )
        for index in range(18)
    )
    items.append(
        ConversationContextItem(run_id=uuid4(), request="最近一轮：检查发布清单", artifacts=())
    )

    artifact = _conversation_history_artifact(
        conversation_id="conv-middle-recall",
        current_request="继续数据库迁移的蓝绿双写方案",
        context_items=tuple(items),
        history_token_budget=160,
    )

    assert artifact is not None
    text = artifact.content["text"]
    assert isinstance(text, str)
    assert "蓝绿双写" in text
    assert "最近一轮" in text


def test_conversation_history_relevance_uses_final_artifact_from_each_turn() -> None:
    final_decision = "最终结论：支付回调必须用幂等键去重。"
    artifacts = cast(
        tuple[dict[str, object], ...],
        tuple(
            {
                "producer": f"worker_{index}",
                "content": {"text": f"中间过程 {index}"},
            }
            for index in range(6)
        )
        + (
            {
                "producer": "main_agent",
                "content": {"text": final_decision},
            },
        ),
    )
    items = (
        ConversationContextItem(run_id=uuid4(), request="初始目标：完成支付系统", artifacts=()),
        ConversationContextItem(run_id=uuid4(), request="实现回调处理", artifacts=artifacts),
        ConversationContextItem(run_id=uuid4(), request="最近一轮：检查发布", artifacts=()),
    )

    artifact = _conversation_history_artifact(
        conversation_id="conv-final-artifact",
        current_request="继续支付回调幂等键方案",
        context_items=items,
        history_token_budget=160,
    )

    assert artifact is not None
    text = artifact.content["text"]
    assert isinstance(text, str)
    assert "幂等键去重" in text
