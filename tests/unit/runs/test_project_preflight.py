from agent_hub.project_preflight import build_project_preflight_files


def test_project_preflight_generates_architecture_plan_and_graph() -> None:
    files = build_project_preflight_files(
        title="超大型 Agent 项目",
        request=(
            "添加超大型项目架构和构建能力，可以拆解需求、搭建架构、"
            "完成整个项目构建，并交付最终生产结果。"
        ),
    )

    assert set(files) == {"PROJECT_ARCHITECTURE_PLAN.md", "architecture-map.html"}
    plan = files["PROJECT_ARCHITECTURE_PLAN.md"].decode()
    graph = files["architecture-map.html"].decode()
    assert "# 超大型 Agent 项目" in plan
    assert "## 架构方向" in plan
    assert "## 约束和技能规则读取" in plan
    assert "## 阶段计划" in plan
    assert "## 实现阶段执行契约" in plan
    assert "## 阶段自修复闭环" in plan
    assert "`stage_repair_actions`" in plan
    assert "## 验收矩阵" in plan
    assert "## 阶段验收和风险回收" in plan
    assert "## 审批口径" in plan
    assert "## 生产结果" in plan
    assert "需求拆解" in graph
    assert "约束读取" in graph
    assert "架构方向" in graph
    assert "计划 MD" in graph
    assert "阶段契约" in graph
    assert "实现阶段" in graph
    assert "验收测试" in graph
    assert "风险回收" in graph
    assert "自修复闭环" in graph
    assert "生产部署" in graph
    assert "自恢复" in graph
