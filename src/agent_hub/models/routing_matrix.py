"""Deterministic role-to-model routing matrix."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

from agent_hub.config.schema import LogicalModelDefinition, PlatformConfig
from agent_hub.models.profiles import infer_model_traits
from agent_hub.models.types import (
    ModelCapability,
    effective_capabilities_for_api_base,
)

_SOFTWARE_TASK_KEYWORDS = (
    "code",
    "代码",
    "源码",
    "项目源码",
    "python",
    "javascript",
    "typescript",
    "node",
    "react",
    "vue",
    "main.py",
    ".py",
    ".js",
    ".ts",
    "zip",
    "压缩包",
    "可下载",
    "download",
    "网页",
    "web",
    "前端",
    "后端",
    "api",
    "github",
    "test",
    "测试",
)
_CREATIVE_TASK_KEYWORDS = (
    "文案",
    "脚本",
    "短剧",
    "视频",
    "导演",
    "剪辑",
    "prompt",
    "提示词",
    "creative",
    "story",
)
_ANALYSIS_TASK_KEYWORDS = (
    "分析",
    "调研",
    "研究",
    "经济",
    "金融",
    "市场",
    "竞品",
    "风险",
    "review",
    "audit",
)
_VISION_TASK_KEYWORDS = ("图片", "识图", "视觉", "image", "vision")
_COMPLIANCE_TASK_KEYWORDS = ("合规", "法律", "隐私", "版权", "资质", "compliance")


@dataclass(frozen=True, slots=True)
class RoleModelRoutingRequest:
    task: object
    role_id: str
    role: str
    purpose: str
    mission: str
    skills: tuple[str, ...]
    must_answer: tuple[str, ...]
    allowed_tools: tuple[str, ...]
    preferred_model: str
    default_model: str
    required_capabilities: frozenset[ModelCapability] = field(default_factory=frozenset)


@dataclass(frozen=True, slots=True)
class RoleModelCandidate:
    logical_model: str
    score: int
    eligible: bool
    selected: bool
    traits: frozenset[str]
    reasons: tuple[str, ...]

    def as_payload(self) -> Mapping[str, object]:
        return MappingProxyType(
            {
                "logical_model": self.logical_model,
                "score": self.score,
                "eligible": self.eligible,
                "selected": self.selected,
                "traits": tuple(sorted(self.traits)),
                "reasons": self.reasons,
            }
        )


def rank_role_models(
    request: RoleModelRoutingRequest,
    config: PlatformConfig,
) -> tuple[RoleModelCandidate, ...]:
    """Rank configured logical models for one role with explainable reasons."""

    if not config.models:
        return ()
    text = role_routing_text(request)
    preferred = (
        request.preferred_model
        if request.preferred_model in config.models and request.preferred_model != request.default_model
        else ""
    )
    candidates: list[RoleModelCandidate] = []
    for logical_model, definition in config.models.items():
        score = 0
        reasons: list[str] = []
        haystack = _logical_model_haystack(logical_model, definition)
        if logical_model == preferred:
            score += 12
            reasons.append("preference:role_model")
        if logical_model == request.default_model:
            score += 1
            reasons.append("preference:default_model")
        capacity_bonus = min(
            8,
            sum(deployment.max_concurrency for deployment in definition.deployments) // 2,
        )
        if capacity_bonus:
            score += capacity_bonus
            reasons.append(f"capacity:configured:{capacity_bonus}")
        required_capabilities = _required_capabilities_for_request(request)
        unsupported = _unsupported_required_capabilities(definition, required_capabilities)
        eligible = not unsupported
        if request.allowed_tools and ModelCapability.TOOL_CALLING not in unsupported:
            reasons.append("capability:tool_role_supported")
        elif request.allowed_tools and ModelCapability.TOOL_CALLING in unsupported:
            reasons.append("capability:tool_role_unsupported_messages_endpoint")
        if unsupported:
            score -= 1000
            for capability in sorted(unsupported, key=lambda item: item.value):
                reasons.append(f"capability:missing:{capability.value}")
        traits = _model_traits(logical_model, definition)
        score += _task_characteristic_score(text, traits, reasons)
        score += _domain_keyword_score(text, haystack, reasons)
        candidates.append(
            RoleModelCandidate(
                logical_model=logical_model,
                score=score,
                eligible=eligible,
                selected=False,
                traits=traits,
                reasons=tuple(reasons),
            )
        )
    candidates.sort(key=lambda item: (item.score, -len(item.logical_model), item.logical_model), reverse=True)
    return tuple(
        RoleModelCandidate(
            logical_model=candidate.logical_model,
            score=candidate.score,
            eligible=candidate.eligible,
            selected=index == 0,
            traits=candidate.traits,
            reasons=candidate.reasons,
        )
        for index, candidate in enumerate(candidates)
    )


def role_routing_text(request: RoleModelRoutingRequest) -> str:
    return " ".join(
        (
            str(request.task),
            request.role_id,
            request.role,
            request.purpose,
            request.mission,
            " ".join(request.skills),
            " ".join(request.must_answer),
        )
    ).lower()


def task_characteristics(text: str) -> frozenset[str]:
    characteristics: set[str] = set()
    if any(keyword in text for keyword in ("语音", "录音", "音频", "听写", "转写", "speech", "audio", "voice")):
        characteristics.add("audio")
    if any(keyword in text for keyword in ("图片", "识图", "视觉", "图像", "截图", "image", "vision")):
        characteristics.add("vision")
    if any(keyword in text for keyword in _SOFTWARE_TASK_KEYWORDS):
        characteristics.add("code")
    if any(keyword in text for keyword in ("质量", "审查", "复核", "验收", "评审", "review", "audit")):
        characteristics.add("review")
    if any(keyword in text for keyword in _ANALYSIS_TASK_KEYWORDS):
        characteristics.add("analysis")
    if any(keyword in text for keyword in _CREATIVE_TASK_KEYWORDS):
        characteristics.add("creative")
    if any("\u4e00" <= char <= "\u9fff" for char in text):
        characteristics.add("chinese")
    if not characteristics:
        characteristics.add("general")
    return frozenset(characteristics)


def _model_traits(
    logical_model: str,
    definition: LogicalModelDefinition,
) -> frozenset[str]:
    return infer_model_traits(
        logical_model=logical_model,
        deployments=(
            (deployment.provider, deployment.model, deployment.capabilities)
            for deployment in definition.deployments
        ),
    )


def _task_characteristic_score(
    text: str,
    characteristics: frozenset[str],
    reasons: list[str],
) -> int:
    requested = task_characteristics(text)
    score = 0
    for characteristic in sorted(requested):
        reasons.append(f"task:{characteristic}")
    matched_traits = requested & characteristics
    for characteristic in sorted(matched_traits):
        reasons.append(f"model_trait:{characteristic}")
    if "audio" in requested:
        score += 36 if "audio" in characteristics else -18
    if "vision" in requested:
        score += 36 if "vision" in characteristics else -18
    if "code" in requested:
        if "code" in characteristics:
            score += 18
        if "tool_calling" in characteristics or "tool" in characteristics:
            score += 8
    if "review" in requested:
        if "review" in characteristics:
            score += 30
        if "reasoning" in characteristics:
            score += 8
        if "structured" in characteristics or "structured_output" in characteristics:
            score += 6
    if "analysis" in requested:
        if "analysis" in characteristics:
            score += 18
        if "reasoning" in characteristics:
            score += 8
        if "synthesis" in characteristics:
            score += 4
        if "structured" in characteristics or "structured_output" in characteristics:
            score += 8
    if "creative" in requested and ("creative" in characteristics or "writing" in characteristics):
        score += 18
    if "chinese" in requested and "chinese" in characteristics:
        score += 5
    if "general" in requested and ("general" in characteristics or "text" in characteristics):
        score += 4
    return score


def _domain_keyword_score(text: str, haystack: str, reasons: list[str]) -> int:
    score = 0
    if any(keyword in text for keyword in _SOFTWARE_TASK_KEYWORDS):
        if any(keyword in haystack for keyword in ("coder", "code", "qwen", "program")):
            score += 30
            reasons.append("domain:software_model_match")
        if "tool_calling" in haystack:
            score += 4
            reasons.append("capability:tool_calling_declared")
    if any(keyword in text for keyword in _CREATIVE_TASK_KEYWORDS):
        if any(keyword in haystack for keyword in ("creative", "kimi", "qwen", "deepseek", "chat", "text")):
            score += 24
            reasons.append("domain:creative_model_match")
        if any(keyword in haystack for keyword in ("creative", "kimi", "story")):
            score += 10
            reasons.append("domain:creative_specialist")
        if any(keyword in haystack for keyword in ("coder", "code")):
            score -= 4
            reasons.append("penalty:creative_on_code_model")
    if any(keyword in text for keyword in _ANALYSIS_TASK_KEYWORDS):
        if any(keyword in haystack for keyword in ("analyst", "analysis", "reason", "max", "sonnet", "claude", "deepseek", "qwen", "glm")):
            score += 22
            reasons.append("domain:analysis_model_match")
        if "structured_output" in haystack:
            score += 6
            reasons.append("capability:structured_output_declared")
    if any(keyword in text for keyword in _VISION_TASK_KEYWORDS) and "vision" in haystack:
        score += 28
        reasons.append("domain:vision_model_match")
    if any(keyword in text for keyword in _COMPLIANCE_TASK_KEYWORDS) and any(
        keyword in haystack for keyword in ("analyst", "review", "sonnet", "claude", "max")
    ):
        score += 18
        reasons.append("domain:compliance_model_match")
    return score


def _logical_model_haystack(logical_model: str, definition: LogicalModelDefinition) -> str:
    return " ".join(
        (
            logical_model,
            " ".join(
                " ".join(
                    (
                        deployment.provider,
                        deployment.model,
                        " ".join(sorted(deployment.capabilities)),
                    )
                )
                for deployment in definition.deployments
            ),
        )
    ).lower()


def _logical_model_supports_tool_roles(definition: LogicalModelDefinition) -> bool:
    return any(
        ModelCapability.TOOL_CALLING
        in effective_capabilities_for_api_base(
            (ModelCapability(capability) for capability in deployment.capabilities),
            deployment.api_base,
        )
        for deployment in definition.deployments
    )


def _required_capabilities_for_request(
    request: RoleModelRoutingRequest,
) -> frozenset[ModelCapability]:
    required = set(request.required_capabilities)
    required.add(ModelCapability.TEXT)
    if request.allowed_tools:
        required.add(ModelCapability.TOOL_CALLING)
    return frozenset(required)


def _unsupported_required_capabilities(
    definition: LogicalModelDefinition,
    required: frozenset[ModelCapability],
) -> frozenset[ModelCapability]:
    if not required:
        return frozenset()
    for deployment in definition.deployments:
        capabilities = effective_capabilities_for_api_base(
            (ModelCapability(capability) for capability in deployment.capabilities),
            deployment.api_base,
        )
        if not required.issubset(capabilities):
            continue
        return frozenset()
    declared = frozenset(
        effective_capability
        for deployment in definition.deployments
        for effective_capability in effective_capabilities_for_api_base(
            (ModelCapability(capability) for capability in deployment.capabilities),
            deployment.api_base,
        )
    )
    unsupported = set(required - declared)
    if not unsupported:
        unsupported.update(required)
    if (
        ModelCapability.TOOL_CALLING in required
        and not _logical_model_supports_tool_roles(definition)
    ):
        unsupported.add(ModelCapability.TOOL_CALLING)
    return frozenset(unsupported)


__all__ = [
    "RoleModelCandidate",
    "RoleModelRoutingRequest",
    "rank_role_models",
    "role_routing_text",
    "task_characteristics",
]
