"""Preflight artifacts for large project architecture and build requests."""

from __future__ import annotations

import html
import re

_MAX_TITLE_CHARS = 96
_MAX_REQUEST_CHARS = 1200


def build_project_preflight_files(*, title: str, request: str) -> dict[str, bytes]:
    safe_title = _safe_text(
        title,
        default="Project Architecture Preflight",
        max_chars=_MAX_TITLE_CHARS,
    )
    safe_request = _safe_text(
        request,
        default="No request text provided.",
        max_chars=_MAX_REQUEST_CHARS,
    )
    plan = _architecture_plan_markdown(title=safe_title, request=safe_request)
    graph = _architecture_graph_html(title=safe_title, request=safe_request)
    return {
        "PROJECT_ARCHITECTURE_PLAN.md": plan.encode("utf-8"),
        "architecture-map.html": graph.encode("utf-8"),
    }


def _architecture_plan_markdown(*, title: str, request: str) -> str:
    return "\n".join(
        (
            f"# {title}",
            "",
            "## 原始需求",
            request,
            "",
            "## 约束和技能规则读取",
            "- 先读取项目级约束、AGENTS/HANDOFF、现有计划和相关技能规则。",
            "- 将约束转成执行口径：权限边界、测试要求、部署口径、交互确认和禁止事项。",
            "- 后续阶段如果约束冲突，以最新用户要求和项目级规则为准，并记录可追踪决策。",
            "",
            "## 架构方向",
            "- 先建立需求域、能力域、数据域、运行域和交付域的边界。",
            "- 将超大型目标拆成可独立验收的项目包，避免一个长任务吞掉所有风险。",
            "- 每个项目包必须有接口、状态、错误恢复、权限和测试验收定义。",
            "",
            "## 阶段计划",
            "- 需求澄清和约束读取。",
            "- 架构蓝图和风险图谱。",
            "- 里程碑拆解、工作包排序和验收矩阵。",
            "- 分阶段实现、回归测试、压力测试和生产验证。",
            "",
            "## 实现阶段执行契约",
            "- 每个实现阶段必须声明输入依赖、输出产物、修改范围、回滚方式和验收证据。",
            "- 子 Agent/实现角色必须按阶段返回 `stage_status`、`verification_evidence` 和 `remaining_risks`。",
            "- Review/验收角色必须等待实现阶段产物完成后，对照验收矩阵返回 `acceptance_review`。",
            "- 如果阶段内出现错误，先诊断根因并修复；只有无法继续时才标记阻塞和需要的外部输入。",
            "",
            "## 验收矩阵",
            "- 功能验收：关键流程稳定可控，不以降级掩盖错误。",
            "- 交互验收：普通聊天、长任务、计划任务、多模式切换不能误触发。",
            "- 恢复验收：模型失败、工具失败、上下文压缩、部署失败要能诊断和修复。",
            "- 生产验收：实机、压力、权限、发布包、回滚和公网访问检查。",
            "",
            "## 阶段验收和风险回收",
            "- 每个阶段结束前先运行该阶段最小验证，再运行受影响主流程验证。",
            "- 风险必须归属到阶段、能力域、触发条件和修复动作，不能只写笼统提醒。",
            "- 最终汇总必须说明计划产物、图谱产物、阶段状态、验证证据、剩余风险和下一步。",
            "",
            "## 审批口径",
            "- 架构方向和阶段计划先形成可审阅产物，再进入大规模实现。",
            "- 高风险写操作、部署和权限扩大必须保留明确审批或既有授权记录。",
            "",
            "## 生产结果",
            "- 最终交付可运行系统、源码、测试证据、部署结果、已知风险和下一步维护口径。",
            "",
        )
    )


def _architecture_graph_html(*, title: str, request: str) -> str:
    escaped_title = html.escape(title)
    escaped_request = html.escape(request)
    nodes = (
        "需求拆解",
        "约束读取",
        "架构方向",
        "计划 MD",
        "浏览器图谱",
        "阶段契约",
        "实现阶段",
        "验收测试",
        "风险回收",
        "生产部署",
        "自恢复",
    )
    node_html = "\n".join(f"<li>{html.escape(node)}</li>" for node in nodes)
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{escaped_title} Architecture Map</title>
  <style>
    body {{
      font-family: system-ui, sans-serif;
      margin: 32px;
      background: #0f172a;
      color: #e5e7eb;
    }}
    main {{ max-width: 920px; margin: 0 auto; }}
    ul {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
      gap: 12px;
      padding: 0;
    }}
    li {{
      list-style: none;
      border: 1px solid #38bdf8;
      border-radius: 8px;
      padding: 14px;
      background: #111827;
    }}
    p {{ color: #cbd5e1; line-height: 1.7; }}
  </style>
</head>
<body>
  <main>
    <h1>{escaped_title}</h1>
    <p>{escaped_request}</p>
    <ul>
{node_html}
    </ul>
  </main>
</body>
</html>
"""


def _safe_text(value: str, *, default: str, max_chars: int) -> str:
    if not isinstance(value, str):
        return default
    text = re.sub(r"\s+", " ", value).strip()
    return text[:max_chars] or default


__all__ = ["build_project_preflight_files"]
