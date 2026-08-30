"""Dynamic role planning for dispatch and discussion runtimes."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType

from agent_hub.domain.runs import TaskMode
from agent_hub.runtime.role_catalog import RoleCatalog, RoleDefinition, default_role_catalog


class TaskProfile(StrEnum):
    """Coarse task profile used to choose temporary agent roles."""

    SOFTWARE = "software"
    RESEARCH = "research"
    DEPLOYMENT = "deployment"
    OPERATIONS = "operations"
    GENERAL = "general"
    UNKNOWN = "unknown"


class RolePurpose(StrEnum):
    """What a role is allowed to contribute in this run."""

    MODERATE = "moderate"
    EXPERTISE = "expertise"
    CRITIQUE = "critique"
    RISK_REVIEW = "risk_review"
    RECORD_DECISION = "record_decision"
    PLAN = "plan"
    EXECUTE = "execute"
    VERIFY = "verify"
    RELEASE = "release"


_SAFE_IDENTIFIER = re.compile(r"^[a-z0-9][a-z0-9_-]{0,127}$")
_DOTTED_BUILT_IN_TOOL_NAMES = frozenset(
    {"document.generate_docx", "presentation.generate_pptx", "project.generate_zip"}
)
_MAX_TEXT = 2_000
_DISCUSSION_SCHEMA = MappingProxyType(
    {
        "position": "approve | reject | needs_user",
        "recommended_option": "string | null",
        "confidence": "0.0-1.0",
        "claims": "string[]",
        "evidence": "string[]",
        "objections": "string[]",
        "risks": "string[]",
        "questions_for_user": "string[]",
        "verification_needed": "string[]",
    }
)
_DISPATCH_SCHEMA = MappingProxyType(
    {
        "status": "done | blocked | needs_user",
        "summary": "string",
        "evidence": "string[]",
        "risks": "string[]",
        "artifacts": "string[]",
        "verification": "string[]",
    }
)


@dataclass(frozen=True, slots=True)
class RoleAssignment:
    """Temporary role assigned by the main agent for one run."""

    id: str
    role: str
    purpose: RolePurpose
    mission: str
    must_answer: tuple[str, ...]
    allowed_tools: tuple[str, ...]
    forbidden_actions: tuple[str, ...]
    skills: tuple[str, ...]
    output_schema: Mapping[str, str]
    model: str

    def __post_init__(self) -> None:
        _require_identifier("role id", self.id)
        _require_text("role", self.role)
        if type(self.purpose) is not RolePurpose:
            raise ValueError("role purpose is invalid")
        _require_text("mission", self.mission)
        must_answer = _normalize_tuple("must_answer", self.must_answer, min_length=1)
        allowed_tools = _normalize_tool_tuple("allowed_tools", self.allowed_tools)
        forbidden_actions = _normalize_tuple(
            "forbidden_actions",
            self.forbidden_actions,
            min_length=1,
        )
        skills = _normalize_identifier_tuple("skills", self.skills)
        output_schema = MappingProxyType(
            {
                _require_schema_key(key): _require_schema_value(value)
                for key, value in self.output_schema.items()
            }
        )
        _require_identifier("model", self.model)
        object.__setattr__(self, "must_answer", must_answer)
        object.__setattr__(self, "allowed_tools", allowed_tools)
        object.__setattr__(self, "forbidden_actions", forbidden_actions)
        object.__setattr__(self, "skills", skills)
        object.__setattr__(self, "output_schema", output_schema)


@dataclass(frozen=True, slots=True)
class RolePlanningRequest:
    """Role planning input from the main agent."""

    task: str
    mode: TaskMode
    profile: TaskProfile = TaskProfile.GENERAL
    profiles: tuple[TaskProfile, ...] = ()
    high_risk: bool = False
    requested_skills: tuple[str, ...] = ()
    default_model: str = "main-agent"
    model_overrides: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require_text("task", self.task)
        if type(self.mode) is not TaskMode or self.mode is TaskMode.AUTO:
            raise ValueError("mode must be an executable task mode")
        if type(self.profile) is not TaskProfile:
            raise ValueError("task profile is invalid")
        profiles = _normalize_profiles(self.profile, self.profiles)
        if type(self.high_risk) is not bool:
            raise ValueError("high_risk must be a boolean")
        requested_skills = _normalize_identifier_tuple("requested_skills", self.requested_skills)
        _require_identifier("default_model", self.default_model)
        overrides = MappingProxyType(
            {
                _require_identifier("model override role", role_id): _require_identifier(
                    "model override model",
                    model,
                )
                for role_id, model in self.model_overrides.items()
            }
        )
        object.__setattr__(self, "requested_skills", requested_skills)
        object.__setattr__(self, "profiles", profiles)
        object.__setattr__(self, "model_overrides", overrides)


@dataclass(frozen=True, slots=True)
class RolePlan:
    """Role assignments and escalation state for one run."""

    mode: TaskMode
    profile: TaskProfile
    roles: tuple[RoleAssignment, ...]
    profiles: tuple[TaskProfile, ...] = ()
    requires_user: bool = False
    reason: str = "ready"

    def __post_init__(self) -> None:
        if type(self.mode) is not TaskMode:
            raise ValueError("mode is invalid")
        if type(self.profile) is not TaskProfile:
            raise ValueError("profile is invalid")
        profiles = _normalize_profiles(self.profile, self.profiles)
        if type(self.requires_user) is not bool:
            raise ValueError("requires_user must be a boolean")
        _require_identifier("reason", self.reason)
        roles = tuple(self.roles)
        if not all(isinstance(role, RoleAssignment) for role in roles):
            raise ValueError("roles must contain only RoleAssignment values")
        role_ids = [role.id for role in roles]
        if len(role_ids) != len(set(role_ids)):
            raise ValueError("duplicate role id")
        object.__setattr__(self, "profiles", profiles)
        object.__setattr__(self, "roles", roles)

    def role(self, role_id: str) -> RoleAssignment:
        _require_identifier("role_id", role_id)
        for role in self.roles:
            if role.id == role_id:
                return role
        raise KeyError(role_id)


class RolePlanner:
    """Build per-run roles from task profile and execution mode."""

    __slots__ = ("_role_catalog",)

    def __init__(self, role_catalog: RoleCatalog | None = None) -> None:
        self._role_catalog = role_catalog or default_role_catalog()

    def plan(self, request: RolePlanningRequest) -> RolePlan:
        if type(request) is not RolePlanningRequest:
            raise ValueError("request must be RolePlanningRequest")
        if (
            request.mode is TaskMode.HYBRID
            and request.profile is TaskProfile.UNKNOWN
            and request.high_risk
        ):
            return RolePlan(
                mode=request.mode,
                profile=request.profile,
                roles=(),
                requires_user=True,
                reason="ambiguous_high_risk_role_plan",
            )
        if request.mode is TaskMode.DISCUSS:
            role_specs = _combined_specs(
                _discussion_specs(profile, request.high_risk) for profile in request.profiles
            )
        else:
            role_specs = _combined_specs(_dispatch_specs(profile) for profile in request.profiles)
        catalog_specs = _catalog_specs_for_request(self._role_catalog, request)
        role_specs = (*role_specs, *_select_relevant_catalog_specs(request, catalog_specs))
        roles = tuple(_assignment(spec, request) for spec in role_specs)
        return RolePlan(
            mode=request.mode,
            profile=request.profile,
            profiles=request.profiles,
            roles=roles,
        )


type _RoleSpec = tuple[
    str,
    str,
    RolePurpose,
    str,
    tuple[str, ...],
    tuple[str, ...],
    tuple[str, ...],
    tuple[str, ...],
    Mapping[str, str],
]


def _discussion_specs(profile: TaskProfile, high_risk: bool) -> tuple[_RoleSpec, ...]:
    expert = {
        TaskProfile.SOFTWARE: (
            "software_architect",
            "Software Architect",
            "从系统边界、模块职责、接口和可维护性角度提出方案。",
            ("架构边界是否清晰？", "哪些模块必须隔离？", "如何验证方案可落地？"),
            ("system-design",),
        ),
        TaskProfile.RESEARCH: (
            "domain_researcher",
            "Domain Researcher",
            "从资料来源、事实完整性和缺口角度提出判断。",
            ("哪些事实需要证据？", "有哪些信息缺口？", "结论依赖哪些来源？"),
            ("research",),
        ),
        TaskProfile.DEPLOYMENT: (
            "ops_architect",
            "Ops Architect",
            "从部署拓扑、系统依赖、排障路径和回滚角度提出方案。",
            ("部署路径是什么？", "失败如何恢复？", "哪些环境差异必须处理？"),
            ("ops",),
        ),
    }.get(
        profile,
        (
            "domain_expert",
            "Domain Expert",
            "从任务专业角度提出可执行方案。",
            ("推荐什么方案？", "依据是什么？", "如何验证？"),
            (),
        ),
    )
    specs: list[_RoleSpec] = [
        (
            "moderator",
            "Moderator",
            RolePurpose.MODERATE,
            "控制讨论轮次、聚焦问题、要求各方给出证据。",
            ("当前分歧是什么？", "是否需要继续讨论？", "是否需要询问用户？"),
            (),
            ("不允许直接执行外部操作", "不允许替代 Decision Resolver 做最终裁决"),
            (),
            _DISCUSSION_SCHEMA,
        ),
        (
            expert[0],
            expert[1],
            RolePurpose.EXPERTISE,
            expert[2],
            expert[3],
            ("read_context",),
            ("不允许直接执行外部操作", "不允许忽略用户约束"),
            expert[4],
            _DISCUSSION_SCHEMA,
        ),
        (
            "skeptic",
            "Skeptic",
            RolePurpose.CRITIQUE,
            "专门寻找错误假设、反例、失败路径和过度自信判断。",
            ("这个方案哪里可能错？", "有哪些未验证假设？", "什么情况下应问用户？"),
            (),
            ("不允许直接执行外部操作", "不允许给无证据最终结论"),
            (),
            _DISCUSSION_SCHEMA,
        ),
        (
            "decision_recorder",
            "Decision Recorder",
            RolePurpose.RECORD_DECISION,
            "整理共识、分歧、证据、待验证事项和可交给决策器的结构化输入。",
            ("各方立场是什么？", "证据是什么？", "仍有哪些分歧？"),
            (),
            ("不允许直接执行外部操作", "不允许删除分歧意见"),
            (),
            _DISCUSSION_SCHEMA,
        ),
    ]
    if profile is TaskProfile.SOFTWARE:
        specs.extend(
            [
                (
                    "implementation_strategist",
                    "Implementation Strategist",
                    RolePurpose.EXPERTISE,
                    "Translate the proposed design into implementation slices and sequencing.",
                    (
                        "Which implementation path is lowest risk?",
                        "What can be built first?",
                        "What integration points are fragile?",
                    ),
                    ("read_context",),
                    ("do not execute external operations", "do not bypass tests"),
                    ("implementation",),
                    _DISCUSSION_SCHEMA,
                ),
                (
                    "test_strategist",
                    "Test Strategist",
                    RolePurpose.VERIFY,
                    "Define the tests and checks needed before the plan is trusted.",
                    (
                        "Which behavior needs tests?",
                        "What failure would prove the plan is wrong?",
                        "Which checks are required before completion?",
                    ),
                    ("read_context",),
                    ("do not execute external operations", "do not waive verification"),
                    ("test",),
                    _DISCUSSION_SCHEMA,
                ),
            ]
        )
    elif profile is TaskProfile.RESEARCH:
        specs.extend(
            [
                (
                    "source_validator",
                    "Source Validator",
                    RolePurpose.VERIFY,
                    "Check source quality, recency, conflicts, and citation gaps.",
                    (
                        "Which sources are trustworthy?",
                        "Which claims need verification?",
                        "Are there conflicting sources?",
                    ),
                    ("read_context",),
                    ("do not invent citations", "do not ignore stale evidence"),
                    ("research",),
                    _DISCUSSION_SCHEMA,
                ),
                (
                    "data_analyst",
                    "Data Analyst",
                    RolePurpose.EXPERTISE,
                    "Evaluate numbers, comparisons, assumptions, and uncertainty.",
                    (
                        "What data supports the conclusion?",
                        "What assumptions affect the result?",
                        "How large is the uncertainty?",
                    ),
                    ("read_context",),
                    ("do not fabricate data", "do not hide uncertainty"),
                    ("analysis",),
                    _DISCUSSION_SCHEMA,
                ),
                (
                    "synthesis_writer",
                    "Synthesis Writer",
                    RolePurpose.RECORD_DECISION,
                    "Turn discussion evidence into a clear recommendation and caveats.",
                    (
                        "What is the concise recommendation?",
                        "What caveats must be shown?",
                        "What next action is supported?",
                    ),
                    ("read_context",),
                    ("do not remove material caveats", "do not overstate confidence"),
                    ("writing",),
                    _DISCUSSION_SCHEMA,
                ),
            ]
        )
    elif profile is TaskProfile.DEPLOYMENT:
        specs.extend(
            [
                (
                    "dependency_resolver",
                    "Dependency Resolver",
                    RolePurpose.EXPERTISE,
                    "Identify OS, package, service, and runtime dependency constraints.",
                    (
                        "Which dependencies are required?",
                        "Which versions are risky?",
                        "What fallback exists?",
                    ),
                    ("read_context",),
                    ("do not install packages directly", "do not ignore distro differences"),
                    ("ops",),
                    _DISCUSSION_SCHEMA,
                ),
                (
                    "network_tls_engineer",
                    "Network/TLS Engineer",
                    RolePurpose.EXPERTISE,
                    "Evaluate ports, DNS, TLS, proxy, and webhook reachability.",
                    (
                        "Which endpoints must be reachable?",
                        "What TLS/DNS setup is needed?",
                        "What network failure should be tested?",
                    ),
                    ("read_context",),
                    (
                        "do not change DNS or certificates directly",
                        "do not expose private services",
                    ),
                    ("ops",),
                    _DISCUSSION_SCHEMA,
                ),
            ]
        )
    if high_risk:
        specs.insert(
            3,
            (
                "risk_officer",
                "Risk Officer",
                RolePurpose.RISK_REVIEW,
                "检查权限、安全、成本、外部状态变更和需要用户确认的边界。",
                ("是否有危险操作？", "是否需要用户授权？", "如何降低风险？"),
                (),
                ("不允许直接执行外部操作", "不允许默认批准高风险动作"),
                ("security-review",),
                _DISCUSSION_SCHEMA,
            ),
        )
    specs.extend(
        [
            (
                "cost_estimator",
                "Cost Estimator",
                RolePurpose.RISK_REVIEW,
                "Estimate model, tool, infrastructure, and retry cost before execution.",
                (
                    "What drives cost?",
                    "What can be limited?",
                    "When should user approval be required?",
                ),
                ("read_context",),
                ("do not approve spending", "do not ignore quota limits"),
                (),
                _DISCUSSION_SCHEMA,
            ),
            (
                "user_advocate",
                "User Advocate",
                RolePurpose.CRITIQUE,
                "Check whether the plan matches the user's stated intent and constraints.",
                (
                    "Does this satisfy the user request?",
                    "What assumption should be confirmed?",
                    "Is the result understandable?",
                ),
                ("read_context",),
                ("do not expand scope silently", "do not override explicit user choices"),
                (),
                _DISCUSSION_SCHEMA,
            ),
        ]
    )
    return tuple(specs)


def _dispatch_specs(profile: TaskProfile) -> tuple[_RoleSpec, ...]:
    if profile is TaskProfile.DEPLOYMENT:
        return (
            (
                "ops_planner",
                "Ops Planner",
                RolePurpose.PLAN,
                "判断 Linux 发行版、依赖、网络、权限和部署路径。",
                ("目标环境是什么？", "安装路径是什么？", "失败如何诊断？"),
                ("read_context", "run_safe_command"),
                ("delete_file", "push_to_remote", "send_external_message"),
                ("ops",),
                _DISPATCH_SCHEMA,
            ),
            (
                "installer",
                "Installer",
                RolePurpose.EXECUTE,
                "执行可回滚的安装和配置步骤。",
                ("执行了什么？", "产物在哪里？", "如何验证？"),
                ("read_context", "run_safe_command"),
                ("delete_file", "push_to_remote", "send_external_message"),
                (),
                _DISPATCH_SCHEMA,
            ),
            (
                "doctor_agent",
                "Doctor Agent",
                RolePurpose.VERIFY,
                "自动检查常见部署故障并给出修复建议。",
                ("健康检查结果是什么？", "根因是什么？", "修复是否安全？"),
                ("read_context", "run_safe_command"),
                ("delete_file", "push_to_remote", "send_external_message"),
                ("diagnostics",),
                _DISPATCH_SCHEMA,
            ),
            (
                "dependency_resolver",
                "Dependency Resolver",
                RolePurpose.PLAN,
                "Resolve package managers, runtime dependencies, service users, and distro variants.",
                (
                    "Which dependency path applies?",
                    "What is missing?",
                    "What safe remediation is available?",
                ),
                ("read_context", "run_safe_command"),
                ("delete_file", "push_to_remote", "send_external_message"),
                ("ops",),
                _DISPATCH_SCHEMA,
            ),
            (
                "network_tls_engineer",
                "Network/TLS Engineer",
                RolePurpose.VERIFY,
                "Validate ports, reverse proxy, TLS, public URL, and Feishu connectivity.",
                (
                    "Which ports must be open?",
                    "Is TLS/proxy healthy?",
                    "What connectivity check passed?",
                ),
                ("read_context", "run_safe_command"),
                ("delete_file", "push_to_remote", "send_external_message"),
                ("ops",),
                _DISPATCH_SCHEMA,
            ),
            (
                "security_reviewer",
                "Security Reviewer",
                RolePurpose.RISK_REVIEW,
                "检查端口、密钥、权限、服务用户和危险操作。",
                ("是否泄露密钥？", "权限是否最小化？", "哪些操作需确认？"),
                ("read_context",),
                ("delete_file", "push_to_remote", "send_external_message"),
                ("security-review",),
                _DISPATCH_SCHEMA,
            ),
            (
                "release_engineer",
                "Release Engineer",
                RolePurpose.RELEASE,
                "Package deployment artifacts, version metadata, service restart order, and smoke checks.",
                (
                    "What version was deployed?",
                    "What smoke check passed?",
                    "What operational note is needed?",
                ),
                ("read_context", "run_safe_command"),
                ("delete_file", "push_to_remote", "send_external_message"),
                ("release",),
                _DISPATCH_SCHEMA,
            ),
            (
                "rollback_planner",
                "Rollback Planner",
                RolePurpose.RELEASE,
                "定义失败回滚、备份恢复和升级回退路径。",
                ("如何备份？", "如何回滚？", "回滚会丢失什么？"),
                ("read_context",),
                ("delete_file", "push_to_remote", "send_external_message"),
                (),
                _DISPATCH_SCHEMA,
            ),
        )
    if profile is TaskProfile.OPERATIONS:
        return (
            (
                "incident_commander",
                "Incident Commander",
                RolePurpose.PLAN,
                "Coordinate incident triage, scope, priority, and safe next actions.",
                (
                    "What is impacted?",
                    "What is the current hypothesis?",
                    "What action is safe next?",
                ),
                ("read_context",),
                ("delete_file", "push_to_remote", "send_external_message"),
                ("ops",),
                _DISPATCH_SCHEMA,
            ),
            (
                "log_analyst",
                "Log Analyst",
                RolePurpose.EXPERTISE,
                "Inspect relevant application and service logs without exposing secrets.",
                (
                    "What errors appear in logs?",
                    "What timestamps matter?",
                    "What sensitive data must be redacted?",
                ),
                ("read_context", "run_safe_command"),
                ("delete_file", "push_to_remote", "send_external_message"),
                ("diagnostics",),
                _DISPATCH_SCHEMA,
            ),
            (
                "metrics_analyst",
                "Metrics Analyst",
                RolePurpose.EXPERTISE,
                "Review health, latency, capacity, queue, and error-rate metrics.",
                (
                    "Which metric changed?",
                    "What threshold was crossed?",
                    "What evidence supports the hypothesis?",
                ),
                ("read_context", "run_safe_command"),
                ("delete_file", "push_to_remote", "send_external_message"),
                ("diagnostics",),
                _DISPATCH_SCHEMA,
            ),
            (
                "runbook_executor",
                "Runbook Executor",
                RolePurpose.EXECUTE,
                "Execute only safe, approved runbook steps and report exact outcomes.",
                (
                    "Which runbook step ran?",
                    "What output proves it worked?",
                    "What step needs approval?",
                ),
                ("read_context", "run_safe_command"),
                ("delete_file", "push_to_remote", "send_external_message"),
                ("ops",),
                _DISPATCH_SCHEMA,
            ),
            (
                "reliability_reviewer",
                "Reliability Reviewer",
                RolePurpose.RISK_REVIEW,
                "Check blast radius, rollback safety, recurrence risk, and service impact.",
                (
                    "What is the blast radius?",
                    "Can we roll back safely?",
                    "What recurrence risk remains?",
                ),
                ("read_context",),
                ("delete_file", "push_to_remote", "send_external_message"),
                ("reliability",),
                _DISPATCH_SCHEMA,
            ),
            (
                "postmortem_writer",
                "Postmortem Writer",
                RolePurpose.RECORD_DECISION,
                "Summarize incident timeline, root cause, mitigation, and follow-up items.",
                (
                    "What happened?",
                    "What fixed it?",
                    "What follow-up is needed?",
                ),
                ("read_context",),
                ("delete_file", "push_to_remote", "send_external_message"),
                ("writing",),
                _DISPATCH_SCHEMA,
            ),
        )
    if profile is TaskProfile.SOFTWARE:
        return (
            (
                "architect",
                "Architect",
                RolePurpose.PLAN,
                "拆分代码边界、接口和验证路径。",
                ("边界是什么？", "改哪些文件？", "如何验证？"),
                ("read_context",),
                ("delete_file", "send_external_message"),
                ("system-design",),
                _DISPATCH_SCHEMA,
            ),
            (
                "implementer",
                "Implementer",
                RolePurpose.EXECUTE,
                "按计划实现代码改动。",
                ("实现了什么？", "影响范围是什么？", "如何验证？"),
                ("read_context", "edit_file", "run_safe_command", "project.generate_zip"),
                ("delete_file", "send_external_message"),
                (),
                _DISPATCH_SCHEMA,
            ),
            (
                "tester",
                "Tester",
                RolePurpose.VERIFY,
                "补测试、运行测试并报告失败证据。",
                ("测试覆盖什么？", "结果是什么？", "还有哪些风险？"),
                ("read_context", "run_safe_command"),
                ("delete_file", "send_external_message"),
                ("test",),
                _DISPATCH_SCHEMA,
            ),
            (
                "security_reviewer",
                "Security Reviewer",
                RolePurpose.RISK_REVIEW,
                "审查权限、数据破坏、密钥和外部影响。",
                ("是否有危险操作？", "是否需要用户确认？", "证据是什么？"),
                ("read_context",),
                ("delete_file", "send_external_message"),
                ("security-review",),
                _DISPATCH_SCHEMA,
            ),
        )
    return (
        (
            "planner",
            "Planner",
            RolePurpose.PLAN,
            "拆解任务、定义步骤和验收标准。",
            ("目标是什么？", "步骤是什么？", "如何验收？"),
            ("read_context",),
            ("delete_file", "send_external_message"),
            (),
            _DISPATCH_SCHEMA,
        ),
        (
            "reviewer",
            "Reviewer",
            RolePurpose.VERIFY,
            "检查结果是否满足用户目标。",
            ("是否满足目标？", "风险是什么？", "是否需要用户确认？"),
            ("read_context",),
            ("delete_file", "send_external_message"),
            (),
            _DISPATCH_SCHEMA,
        ),
    )


def _combined_specs(spec_groups: Iterable[tuple[_RoleSpec, ...]]) -> tuple[_RoleSpec, ...]:
    combined: list[_RoleSpec] = []
    seen: set[str] = set()
    for specs in spec_groups:
        for spec in specs:
            role_id = spec[0]
            if role_id in seen:
                continue
            seen.add(role_id)
            combined.append(spec)
    return tuple(combined)


def _assignment(spec: _RoleSpec, request: RolePlanningRequest) -> RoleAssignment:
    role_id, role, purpose, mission, must_answer, allowed_tools, forbidden, skills, schema = spec
    merged_skills = tuple(dict.fromkeys((*skills, *request.requested_skills)))
    if role_id == "risk_officer":
        merged_skills = tuple(
            skill for skill in merged_skills if skill in {"security-review", "compliance"}
        )
    elif role_id == "software_architect":
        merged_skills = tuple(
            skill for skill in merged_skills if skill in {"system-design", "architecture"}
        )
    model = request.model_overrides.get(role_id, request.default_model)
    return RoleAssignment(
        id=role_id,
        role=role,
        purpose=purpose,
        mission=mission,
        must_answer=must_answer,
        allowed_tools=allowed_tools,
        forbidden_actions=forbidden,
        skills=merged_skills,
        output_schema=schema,
        model=model,
    )


def _catalog_spec(role: RoleDefinition) -> _RoleSpec:
    return (
        role.id,
        role.role,
        RolePurpose(role.purpose),
        role.mission,
        role.must_answer,
        role.allowed_tools,
        role.forbidden_actions,
        role.skills,
        role.output_schema,
    )


def _catalog_specs_for_request(
    catalog: RoleCatalog,
    request: RolePlanningRequest,
) -> tuple[_RoleSpec, ...]:
    profiles = tuple(profile.value for profile in request.profiles)
    specs: list[_RoleSpec] = []
    seen: set[str] = set()
    for profile in profiles:
        for role in catalog.roles_for(
            mode=request.mode.value,
            profile=profile,
            high_risk=request.high_risk,
        ):
            if role.id in seen:
                continue
            seen.add(role.id)
            specs.append(_catalog_spec(role))
    return tuple(specs)


def _select_relevant_catalog_specs(
    request: RolePlanningRequest,
    specs: tuple[_RoleSpec, ...],
) -> tuple[_RoleSpec, ...]:
    if not specs:
        return ()
    selected = [spec for spec in specs if _role_matches_task(spec, request)]
    if selected:
        return tuple(selected)
    if request.requested_skills:
        return ()
    return tuple(spec for spec in specs if spec[0] in _BASELINE_CATALOG_ROLE_IDS)


_BASELINE_CATALOG_ROLE_IDS = frozenset({"project_manager", "quality_reviewer", "user_advocate"})

_ROLE_TRIGGER_KEYWORDS: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        "director": (
            "导演",
            "镜头",
            "分镜",
            "剧情",
            "短剧",
            "视频",
            "叙事",
            "即梦",
            "文生视频",
            "图生视频",
            "story",
            "shot",
        ),
        "copywriter": (
            "文案",
            "脚本",
            "短剧",
            "标题",
            "口播",
            "广告",
            "提示词",
            "prompt",
            "即梦",
            "slogan",
            "copy",
            "script",
        ),
        "video_editor": (
            "剪辑",
            "字幕",
            "转场",
            "视频",
            "素材",
            "节奏",
            "即梦",
            "文生视频",
            "图生视频",
            "edit",
            "caption",
        ),
        "document_writer": (
            "word",
            "docx",
            "document",
            "documents",
            "write-up",
            "memo",
            "文档",
            "word文档",
            "docx文档",
            "生成文档",
            "汇报材料",
            "复盘文档",
        ),
        "presentation_designer": (
            "powerpoint",
            "ppt",
            "pptx",
            "presentation",
            "presentations",
            "slide",
            "slides",
            "deck",
            "演示稿",
            "演示文稿",
            "幻灯片",
            "汇报ppt",
            "汇报材料",
        ),
        "multimedia_generator": (
            "generate",
            "generation",
            "image",
            "video",
            "media",
            "multimedia",
            "文生视频",
            "图生视频",
            "生成图片",
            "生成视频",
        ),
        "content_editor": ("润色", "校对", "编辑", "文案", "脚本", "改写", "polish", "edit"),
        "economic_analyst": ("经济", "市场", "需求", "定价", "宏观", "商业回报", "roi", "market"),
        "finance_analyst": ("预算", "成本", "收入", "财务", "回报", "roi", "budget", "cost"),
        "marketing_strategist": ("营销", "投放", "渠道", "增长", "转化", "用户", "campaign"),
        "product_manager": ("产品", "需求", "路线图", "优先级", "里程碑", "交付", "方案", "plan"),
        "operations_coordinator": ("交付", "清单", "排期", "运营", "协同", "执行", "handoff"),
        "legal_compliance_reviewer": (
            "法律",
            "合规",
            "版权",
            "隐私",
            "许可",
            "免责声明",
            "compliance",
        ),
        "quality_reviewer": ("审核", "验收", "质量", "检查", "校验", "verify", "quality"),
        "designer": ("设计", "视觉", "海报", "界面", "ui", "品牌", "配色", "layout"),
        "sales_advisor": ("销售", "话术", "客户", "成交", "异议", "sales"),
        "market_researcher": ("research", "market", "competitor", "opportunity", "user", "product"),
    }
)


def _role_matches_task(spec: _RoleSpec, request: RolePlanningRequest) -> bool:
    role_id, role, _purpose, mission, must_answer, _tools, _forbidden, skills, _schema = spec
    requested = set(request.requested_skills)
    if role_id == "multimedia_generator":
        return _is_multimedia_generation_request(request.task)
    if role_id == "document_writer":
        return _is_document_generation_request(request.task)
    if role_id == "presentation_designer":
        return _is_presentation_generation_request(request.task)
    if requested and requested.intersection(skills):
        return True
    task = request.task.casefold()
    haystack = " ".join(
        (role_id, role, mission, " ".join(must_answer), " ".join(skills))
    ).casefold()
    triggers = _ROLE_TRIGGER_KEYWORDS.get(role_id, ())
    if any(keyword.casefold() in task for keyword in triggers):
        return True
    if role_id == "project_manager" and any(
        keyword in task
        for keyword in (
            "plan",
            "scope",
            "milestone",
            "deadline",
            "build",
            "prototype",
            "deliver",
            "方案",
            "交付",
            "清单",
        )
    ):
        return True
    if role_id == "quality_reviewer" and any(
        keyword in task
        for keyword in (
            "build",
            "prototype",
            "deliver",
            "verify",
            "quality",
            "test",
            "prompt",
            "验收",
            "质量",
            "检查",
            "测试",
            "交付",
            "提示词",
            "可直接使用",
        )
    ):
        return True
    if role_id == "copywriter" and any(
        keyword in task
        for keyword in (
            "文案",
            "脚本",
            "标题",
            "口播",
            "短剧",
            "短视频",
            "营销",
            "广告",
            "提示词",
            "prompt",
            "即梦",
        )
    ):
        return True
    if role_id == "economic_analyst" and any(
        keyword in task for keyword in ("经济", "市场", "需求", "定价", "商业回报", "回报", "预算")
    ):
        return True
    if role_id == "finance_analyst" and any(
        keyword in task for keyword in ("预算", "成本", "收入", "财务", "回报")
    ):
        return True
    if role_id == "sales_advisor" and any(keyword in task for keyword in ("销售", "话术", "客户")):
        return True
    if role_id == "operations_coordinator" and any(
        keyword in task for keyword in ("交付", "清单", "排期", "协同", "执行")
    ):
        return True
    if role_id == "legal_compliance_reviewer" and any(
        keyword in task for keyword in ("法律", "合规", "版权", "隐私", "许可")
    ):
        return True
    # Custom catalog roles stay discoverable without hard-coding: if a role's
    # own short skill/role words appear in the task, it can participate.
    return any(
        len(token) >= 2 and token.casefold() in task
        for token in (*skills, role_id.replace("_", " "), role)
        if token.casefold() in haystack
    )


_MULTIMEDIA_GENERATION_TERMS = (
    "generate an image",
    "generate a product image",
    "generate image",
    "generate images",
    "generate a video",
    "generate video",
    "generate videos",
    "generate audio",
    "generate speech",
    "create an image",
    "create image",
    "create a video",
    "create video",
    "make a video",
    "text-to-image",
    "text to image",
    "text-to-video",
    "text to video",
    "text-to-speech",
    "text to speech",
    "image generation",
    "video generation",
    "audio generation",
    "生成图片",
    "生成一张图",
    "生成图像",
    "生成视频",
    "制作视频",
    "生成语音",
    "语音合成",
    "文生图",
    "文生视频",
    "文生语音",
)

_MULTIMEDIA_GENERATION_NEGATIONS = (
    "不需要",
    "无需",
    "不要",
    "不用",
    "暂不",
    "not need",
    "do not",
    "don't",
    "without",
    "no need",
)

_DOCUMENT_GENERATION_TERMS = (
    "generate a word",
    "generate word",
    "create a word",
    "create word",
    "make a word",
    "build a word",
    "produce a word",
    "export a word",
    "generate a docx",
    "generate docx",
    "create a docx",
    "create docx",
    "make a docx",
    "build a docx",
    "produce a docx",
    "export a docx",
    "export a document",
    "export document",
    "generate a document file",
    "create a document file",
    "make a document file",
    "build a document file",
    "produce a document file",
    "export a document file",
    "download a document file",
    "generate a word file",
    "create a word file",
    "make a word file",
    "build a word file",
    "produce a word file",
    "export a word file",
    "download a word file",
    "generate a docx file",
    "create a docx file",
    "make a docx file",
    "build a docx file",
    "produce a docx file",
    "export a docx file",
    "download a docx file",
    "生成word",
    "生成 word",
    "生成docx",
    "生成 docx",
    "创建word",
    "创建 word",
    "创建docx",
    "创建 docx",
    "制作word",
    "制作 word",
    "制作docx",
    "制作 docx",
    "生成文档文件",
    "创建文档文件",
    "制作文档文件",
    "输出文档文件",
    "导出文档文件",
    "下载文档文件",
    "生成word文件",
    "生成 word 文件",
    "创建word文件",
    "创建 word 文件",
    "制作word文件",
    "制作 word 文件",
    "输出word文件",
    "输出 word 文件",
    "导出word文件",
    "导出 word 文件",
    "下载word文件",
    "下载 word 文件",
    "生成docx文件",
    "生成 docx 文件",
    "创建docx文件",
    "创建 docx 文件",
    "制作docx文件",
    "制作 docx 文件",
    "输出docx文件",
    "输出 docx 文件",
    "导出docx文件",
    "导出 docx 文件",
    "下载docx文件",
    "下载 docx 文件",
    "输出文档",
    "输出word",
    "输出 word",
    "输出docx",
    "输出 docx",
    "导出文档",
    "导出word",
    "导出 word",
    "导出docx",
    "导出 docx",
    "下载文档",
    "下载word",
    "下载 word",
    "下载docx",
    "下载 docx",
)

_PRESENTATION_GENERATION_TERMS = (
    "generate a powerpoint",
    "generate powerpoint",
    "create a powerpoint",
    "create powerpoint",
    "make a powerpoint",
    "build a powerpoint",
    "generate a ppt",
    "generate ppt",
    "create a ppt",
    "create ppt",
    "make a ppt",
    "build a ppt",
    "generate a pptx",
    "generate pptx",
    "create a pptx",
    "create pptx",
    "make a pptx",
    "build a pptx",
    "generate a presentation",
    "generate presentation",
    "create a presentation",
    "create presentation",
    "make a presentation",
    "build a presentation",
    "export a presentation",
    "export presentation",
    "generate a slide deck",
    "create a slide deck",
    "make a slide deck",
    "build a slide deck",
    "produce a slide deck",
    "draft a slide deck",
    "export a slide deck",
    "generate slides",
    "create slides",
    "make slides",
    "build slides",
    "export slides",
    "generate a presentation file",
    "create a presentation file",
    "make a presentation file",
    "build a presentation file",
    "produce a presentation file",
    "export a presentation file",
    "download a presentation file",
    "generate a powerpoint file",
    "create a powerpoint file",
    "make a powerpoint file",
    "build a powerpoint file",
    "produce a powerpoint file",
    "export a powerpoint file",
    "download a powerpoint file",
    "generate a ppt file",
    "create a ppt file",
    "make a ppt file",
    "build a ppt file",
    "produce a ppt file",
    "export a ppt file",
    "download a ppt file",
    "generate a pptx file",
    "create a pptx file",
    "make a pptx file",
    "build a pptx file",
    "produce a pptx file",
    "export a pptx file",
    "download a pptx file",
    "生成ppt",
    "生成 ppt",
    "生成pptx",
    "生成 pptx",
    "生成演示稿",
    "生成演示文稿",
    "创建演示文稿",
    "创建幻灯片",
    "制作演示文稿",
    "制作幻灯片",
    "做ppt",
    "做 ppt",
    "做一份ppt",
    "做一份 ppt",
    "输出ppt",
    "输出 ppt",
    "导出ppt",
    "导出 ppt",
    "汇报ppt",
    "汇报 ppt",
)

_OFFICE_GLOBAL_GENERATION_NEGATIONS = (
    "do not generate",
    "don't generate",
    "dont generate",
    "not generate",
    "no need to generate",
    "do not create",
    "don't create",
    "dont create",
    "not create",
    "no need to create",
    "do not make",
    "don't make",
    "dont make",
    "not make",
    "no need to make",
    "do not build",
    "don't build",
    "dont build",
    "not build",
    "no need to build",
    "do not export",
    "don't export",
    "dont export",
    "not export",
    "no need to export",
    "不要生成",
    "不用生成",
    "无需生成",
    "不需要生成",
    "暂不生成",
    "不要创建",
    "不用创建",
    "无需创建",
    "不需要创建",
    "暂不创建",
    "不要制作",
    "不用制作",
    "无需制作",
    "不需要制作",
    "暂不制作",
    "不要输出",
    "不用输出",
    "无需输出",
    "不需要输出",
    "暂不输出",
    "不要导出",
    "不用导出",
    "无需导出",
    "不需要导出",
    "暂不导出",
    "no office file",
    "without office file",
    "no generated file",
    "without generated file",
    "不要文件",
    "不用文件",
    "无需文件",
    "不需要文件",
)

_DOCUMENT_GENERATION_NEGATIONS = (
    *_OFFICE_GLOBAL_GENERATION_NEGATIONS,
    "no docx",
    "no docx file",
    "without docx",
    "without docx file",
    "no word file",
    "without word file",
    "no word document",
    "without word document",
    "no document file",
    "without document file",
    "不用docx",
    "不用 docx",
    "不要docx",
    "不要 docx",
    "无需docx",
    "无需 docx",
    "不需要docx",
    "不需要 docx",
    "不用word",
    "不用 word",
    "不要word",
    "不要 word",
    "无需word",
    "无需 word",
    "不需要word",
    "不需要 word",
    "不用word文档",
    "不用 word 文档",
    "不要word文档",
    "不要 word 文档",
    "无需word文档",
    "无需 word 文档",
    "不需要word文档",
    "不需要 word 文档",
)

_PRESENTATION_GENERATION_NEGATIONS = (
    *_OFFICE_GLOBAL_GENERATION_NEGATIONS,
    "no ppt",
    "no ppt file",
    "without ppt",
    "without ppt file",
    "no pptx",
    "no pptx file",
    "without pptx",
    "without pptx file",
    "no powerpoint",
    "no powerpoint file",
    "without powerpoint",
    "without powerpoint file",
    "no presentation file",
    "without presentation file",
    "不用ppt",
    "不用 ppt",
    "不要ppt",
    "不要 ppt",
    "无需ppt",
    "无需 ppt",
    "不需要ppt",
    "不需要 ppt",
)


def _is_multimedia_generation_request(task: str) -> bool:
    normalized = task.casefold()
    return any(term in normalized for term in _MULTIMEDIA_GENERATION_TERMS) and not any(
        negation in normalized for negation in _MULTIMEDIA_GENERATION_NEGATIONS
    )


def _is_document_generation_request(task: str) -> bool:
    normalized = task.casefold()
    office_document_pair = (
        any(term in normalized for term in ("word", "docx"))
        and any(term in normalized for term in ("document", "文档", "文件", "材料"))
    )
    return _has_generation_terms(task, _DOCUMENT_GENERATION_TERMS, _DOCUMENT_GENERATION_NEGATIONS) or (
        office_document_pair
        and any(term in normalized for term in ("generate", "create", "make", "build", "生成", "创建", "制作"))
        and not _has_office_generation_negation(normalized, _DOCUMENT_GENERATION_NEGATIONS)
    )


def _is_presentation_generation_request(task: str) -> bool:
    return _has_generation_terms(
        task,
        _PRESENTATION_GENERATION_TERMS,
        _PRESENTATION_GENERATION_NEGATIONS,
    )


def _has_generation_terms(
    task: str,
    terms: tuple[str, ...],
    negations: tuple[str, ...],
) -> bool:
    normalized = task.casefold()
    return any(term in normalized for term in terms) and not _has_office_generation_negation(
        normalized,
        negations,
    )


def _has_generation_negation(normalized: str) -> bool:
    return any(negation in normalized for negation in _MULTIMEDIA_GENERATION_NEGATIONS)


def _has_office_generation_negation(normalized: str, negations: tuple[str, ...]) -> bool:
    return any(negation in normalized for negation in negations)


def _normalize_profiles(
    primary: TaskProfile,
    profiles: tuple[TaskProfile, ...],
) -> tuple[TaskProfile, ...]:
    raw = (primary,) if not profiles else profiles
    normalized: list[TaskProfile] = []
    for profile in raw:
        if type(profile) is not TaskProfile:
            raise ValueError("task profiles are invalid")
        if profile is TaskProfile.UNKNOWN and len(raw) > 1:
            continue
        if profile not in normalized:
            normalized.append(profile)
    if not normalized:
        normalized.append(primary)
    if TaskProfile.GENERAL not in normalized and TaskProfile.UNKNOWN not in normalized:
        normalized.append(TaskProfile.GENERAL)
    return tuple(normalized)


def _require_identifier(name: str, value: str) -> str:
    if type(value) is not str or _SAFE_IDENTIFIER.fullmatch(value) is None:
        raise ValueError(f"{name} must be a safe identifier")
    return value


def _require_text(name: str, value: str) -> None:
    if type(value) is not str or not value or value != value.strip() or len(value) > _MAX_TEXT:
        raise ValueError(f"{name} must be nonblank, unpadded, and bounded")
    if any(ord(character) < 32 for character in value):
        raise ValueError(f"{name} must not contain control characters")


def _normalize_tuple(name: str, values: tuple[str, ...], *, min_length: int = 0) -> tuple[str, ...]:
    normalized = tuple(values)
    if len(normalized) < min_length:
        raise ValueError(f"{name} has too few entries")
    for value in normalized:
        _require_text(name, value)
    return normalized


def _normalize_identifier_tuple(name: str, values: tuple[str, ...]) -> tuple[str, ...]:
    normalized = tuple(values)
    for value in normalized:
        _require_identifier(name, value)
    if len(normalized) != len(set(normalized)):
        raise ValueError(f"{name} must be unique")
    return normalized


def _normalize_tool_tuple(name: str, values: tuple[str, ...]) -> tuple[str, ...]:
    normalized = tuple(values)
    for value in normalized:
        if type(value) is not str or not _is_safe_tool_name(value):
            raise ValueError(f"{name} must contain safe tool names")
    if len(normalized) != len(set(normalized)):
        raise ValueError(f"{name} must be unique")
    return normalized


def _is_safe_tool_name(value: str) -> bool:
    return _SAFE_IDENTIFIER.fullmatch(value) is not None or value in _DOTTED_BUILT_IN_TOOL_NAMES


def _require_schema_key(value: str) -> str:
    return _require_identifier("output_schema key", value)


def _require_schema_value(value: str) -> str:
    _require_text("output_schema value", value)
    return value


__all__ = [
    "RoleAssignment",
    "RolePlan",
    "RolePlanner",
    "RolePlanningRequest",
    "RolePurpose",
    "TaskProfile",
]
