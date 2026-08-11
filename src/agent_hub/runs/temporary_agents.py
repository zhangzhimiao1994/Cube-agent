"""Temporary agent gap detection backed by persisted admin resources."""

from __future__ import annotations

import re
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agent_hub.db.models import AdminResourceRow
from agent_hub.domain.runs import TaskMode
from agent_hub.runs.service import TemporaryAgentProposal


@dataclass(frozen=True, slots=True)
class _CapabilitySpec:
    capability: str
    role_id: str
    name: str
    role: str
    prompt: str
    keywords: tuple[str, ...]
    coverage: tuple[str, ...]
    skills: tuple[str, ...]


_CAPABILITIES: tuple[_CapabilitySpec, ...] = (
    _CapabilitySpec(
        capability="software_engineering",
        role_id="temp-web-engineer",
        name="Temporary Web Engineer",
        role="Web Engineer",
        prompt="把当前方案落成可交付的网页、前端实现或工程方案，并标注验证步骤。",
        keywords=("网页", "前端", "落地页", "网站", "html", "css", "react", "vue", "代码", "开发", "web"),
        coverage=("工程", "程序", "代码", "开发", "前端", "后端", "engineer", "developer", "software", "web", "react"),
        skills=("frontend", "software-engineering"),
    ),
    _CapabilitySpec(
        capability="operations",
        role_id="temp-ops-engineer",
        name="Temporary Ops Engineer",
        role="Ops Engineer",
        prompt="检查部署、服务、权限、回滚和生产运维风险，并给出可执行处理步骤。",
        keywords=("部署", "服务器", "systemd", "docker", "caddy", "nginx", "运维", "上线", "生产环境"),
        coverage=("运维", "部署", "ops", "devops", "sre", "服务器", "systemd", "docker"),
        skills=("ops",),
    ),
    _CapabilitySpec(
        capability="financial_analysis",
        role_id="temp-finance-analyst",
        name="Temporary Finance Analyst",
        role="Finance Analyst",
        prompt="补充财务、经济、成本、收益、现金流和风险假设分析。",
        keywords=("经济", "财经", "财务", "成本", "收益", "利润", "现金流", "投资", "finance", "economic"),
        coverage=("经济", "财经", "财务", "finance", "economic", "成本", "收益"),
        skills=("finance", "analysis"),
    ),
    _CapabilitySpec(
        capability="video_editing",
        role_id="temp-video-editor",
        name="Temporary Video Editor",
        role="Video Editor",
        prompt="补充剪辑结构、节奏、镜头、字幕、转场和成片交付建议。",
        keywords=("剪辑", "镜头", "成片", "短视频", "字幕", "转场", "video", "edit"),
        coverage=("剪辑", "视频", "editor", "video", "镜头", "成片"),
        skills=("editing",),
    ),
    _CapabilitySpec(
        capability="copywriting",
        role_id="temp-copywriter",
        name="Temporary Copywriter",
        role="Copywriter",
        prompt="补充标题、脚本、正文、口播、卖点和不同版本文案。",
        keywords=("文案", "脚本", "标题", "口播", "广告语", "copy", "script"),
        coverage=("文案", "脚本", "copy", "copywriter", "标题", "口播"),
        skills=("copywriting",),
    ),
    _CapabilitySpec(
        capability="compliance_review",
        role_id="temp-compliance-reviewer",
        name="Temporary Compliance Reviewer",
        role="Compliance Reviewer",
        prompt="检查法律、合规、隐私、版权、资质和外部发布风险。",
        keywords=("合规", "法律", "版权", "隐私", "资质", "合同", "legal", "compliance"),
        coverage=("合规", "法律", "legal", "compliance", "版权", "隐私"),
        skills=("compliance",),
    ),
    _CapabilitySpec(
        capability="research",
        role_id="temp-researcher",
        name="Temporary Researcher",
        role="Researcher",
        prompt="补充事实查证、资料来源、信息缺口和证据链。",
        keywords=("调研", "研究", "资料", "证据", "来源", "竞品", "research"),
        coverage=("研究", "调研", "research", "资料", "证据"),
        skills=("research",),
    ),
)


class AdminResourceTemporaryAgentPolicy:
    """Ask before adding a temporary agent when saved agents do not cover a task."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def propose(
        self,
        *,
        tenant_id: UUID,
        actor_id: UUID,
        message: str,
        mode: TaskMode,
        agent_ids: tuple[str, ...],
        workflow_id: str | None,
        allow_workflow_adjustment: bool,
    ) -> TemporaryAgentProposal | None:
        del actor_id
        if mode not in {TaskMode.DISPATCH, TaskMode.HYBRID}:
            return None
        if not allow_workflow_adjustment:
            return None
        workflow = await self._payload(tenant_id, "workflow", workflow_id) if workflow_id else None
        settings = await self._payload(tenant_id, "setting", "system")
        global_allows_temporary_agents = (
            settings is not None
            and settings.get("allow_main_agent_override") is True
            and settings.get("allow_temporary_agents") is True
        )
        legacy_workflow_allows_temporary_agents = (
            settings is None
            and workflow is not None
            and workflow.get("allow_temporary_agents") is True
        )
        if not global_allows_temporary_agents and not legacy_workflow_allows_temporary_agents:
            return None
        spec = _infer_capability(message)
        if spec is None:
            return None
        selected_ids = agent_ids or _string_tuple(workflow.get("agent_ids") if workflow is not None else None)
        selected_agents = await self._agents(tenant_id, selected_ids)
        if _agents_cover(selected_agents, spec):
            return None
        policy_source = settings if global_allows_temporary_agents else workflow
        policy = str((policy_source or {}).get("temporary_agent_policy") or "").strip()
        pool_label = f"工作流 {workflow_id} 的角色池" if workflow_id else "当前角色池"
        reason = (
            f"{pool_label}缺少 {spec.capability} 能力；规则：{policy}"
            if policy
            else f"{pool_label}缺少 {spec.capability} 能力。"
        )
        return TemporaryAgentProposal(
            id=spec.role_id,
            name=spec.name,
            role=spec.role,
            prompt=spec.prompt,
            reason=reason,
            missing_capability=spec.capability,
            suggested_skills=spec.skills,
            permanentizable=True,
        )

    async def _payload(
        self,
        tenant_id: UUID,
        kind: str,
        resource_id: str,
    ) -> dict[str, object] | None:
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    select(AdminResourceRow)
                    .where(AdminResourceRow.tenant_id == tenant_id)
                    .where(AdminResourceRow.kind == kind)
                    .where(AdminResourceRow.resource_id == resource_id)
                )
            ).scalar_one_or_none()
            return None if row is None else dict(row.payload)

    async def _agents(
        self,
        tenant_id: UUID,
        agent_ids: tuple[str, ...],
    ) -> tuple[dict[str, object], ...]:
        if not agent_ids:
            return ()
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    select(AdminResourceRow)
                    .where(AdminResourceRow.tenant_id == tenant_id)
                    .where(AdminResourceRow.kind == "agent")
                    .where(AdminResourceRow.resource_id.in_(agent_ids))
                )
            ).scalars()
            return tuple(dict(row.payload) for row in rows)


def _infer_capability(message: str) -> _CapabilitySpec | None:
    normalized = message.casefold()
    for spec in _CAPABILITIES:
        if any(keyword.casefold() in normalized for keyword in spec.keywords):
            return spec
    return None


def _agents_cover(agents: tuple[dict[str, object], ...], spec: _CapabilitySpec) -> bool:
    for agent in agents:
        haystack = " ".join(
            str(agent.get(key) or "")
            for key in ("id", "name", "role", "prompt")
        ).casefold()
        haystack = re.sub(r"\s+", " ", haystack)
        if any(token.casefold() in haystack for token in spec.coverage):
            return True
        skills = agent.get("skills")
        if isinstance(skills, list) and any(
            isinstance(skill, str) and skill.casefold() in {item.casefold() for item in spec.skills}
            for skill in skills
        ):
            return True
    return False


def _string_tuple(value: object) -> tuple[str, ...]:
    if not isinstance(value, list | tuple):
        return ()
    return tuple(item for item in value if isinstance(item, str))


__all__ = ["AdminResourceTemporaryAgentPolicy"]
