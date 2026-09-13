from pathlib import Path
from typing import cast
from uuid import uuid4

from agent_hub.capabilities.runtime import RuntimeCapabilityGateway
from agent_hub.runtime.contracts import JsonValue


async def test_project_preflight_capability_writes_plan_and_graph_workspace_files(
    tmp_path: Path,
) -> None:
    tenant_id = uuid4()
    gateway = RuntimeCapabilityGateway(
        skill_store_dir=tmp_path / "skills",
        generated_artifact_dir=tmp_path / "generated",
        project_workspace_dir=tmp_path / "workspaces",
    )

    result = await gateway.execute(
        tenant_id=tenant_id,
        run_id=uuid4(),
        actor="architect",
        name="project.preflight_architecture",
        arguments={
            "title": "超大型 Agent 项目",
            "request": "添加超大型项目架构和构建能力，并生成对应的计划 MD 文件和浏览器链接图谱。",
            "project_id": "Mofang Agent",
            "workspace_session_id": "Conv Preflight",
        },
        idempotency_key="project-preflight",
    )

    workspace_files = result["workspace_files"]
    assert isinstance(workspace_files, tuple)
    workspace_items = cast(tuple[dict[str, JsonValue], ...], workspace_files)
    paths: set[str] = set()
    for item in workspace_items:
        path = item.get("path")
        assert isinstance(path, str)
        paths.add(path)
    assert paths == {"PROJECT_ARCHITECTURE_PLAN.md", "architecture-map.html"}
    assert result["plan_path"] == "PROJECT_ARCHITECTURE_PLAN.md"
    assert result["graph_path"] == "architecture-map.html"
    workspace_download_prefix = (
        "/api/v1/workspaces/projects/mofang-agent/sessions/conv-preflight/files/download"
    )
    plan_download_url = result["plan_download_url"]
    graph_download_url = result["graph_download_url"]
    assert isinstance(plan_download_url, str)
    assert isinstance(graph_download_url, str)
    generated_dir = (
        tmp_path
        / "workspaces"
        / str(tenant_id)
        / "projects"
        / "mofang-agent"
        / "sessions"
        / "conv-preflight"
    )
    plan = (generated_dir / "PROJECT_ARCHITECTURE_PLAN.md").read_text(encoding="utf-8")
    graph = (generated_dir / "architecture-map.html").read_text(encoding="utf-8")
    assert "## 实现阶段执行契约" in plan
    assert "## 阶段自修复闭环" in plan
    assert "`stage_repair_actions`" in plan
    assert "## 阶段验收和风险回收" in plan
    assert "阶段契约" in graph
    assert "自修复闭环" in graph
    assert "风险回收" in graph
    assert plan_download_url.endswith(
        f"{workspace_download_prefix}?path=PROJECT_ARCHITECTURE_PLAN.md"
    )
    assert graph_download_url.endswith(
        f"{workspace_download_prefix}?path=architecture-map.html"
    )
