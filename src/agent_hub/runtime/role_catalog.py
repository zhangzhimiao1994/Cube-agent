"""Extensible role catalog used by the runtime role planner."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType

_SAFE_IDENTIFIER = re.compile(r"^[a-z0-9][a-z0-9_-]{0,127}$")
_MAX_TEXT = 2_000


@dataclass(frozen=True, slots=True)
class RoleDefinition:
    """Reusable role template that can be selected for a run."""

    id: str
    role: str
    purpose: str
    mission: str
    must_answer: tuple[str, ...]
    allowed_tools: tuple[str, ...]
    forbidden_actions: tuple[str, ...]
    skills: tuple[str, ...]
    output_schema: Mapping[str, str]
    modes: frozenset[str]
    profiles: frozenset[str]
    high_risk_only: bool = False

    def __post_init__(self) -> None:
        _require_identifier("role id", self.id)
        _require_text("role", self.role)
        _require_identifier("purpose", self.purpose)
        _require_text("mission", self.mission)
        object.__setattr__(self, "must_answer", _normalize_texts("must_answer", self.must_answer))
        object.__setattr__(
            self,
            "allowed_tools",
            _normalize_identifiers("allowed_tools", self.allowed_tools),
        )
        object.__setattr__(
            self,
            "forbidden_actions",
            _normalize_texts("forbidden_actions", self.forbidden_actions),
        )
        object.__setattr__(self, "skills", _normalize_identifiers("skills", self.skills))
        object.__setattr__(
            self,
            "output_schema",
            MappingProxyType({
                _require_identifier("schema key", key): _require_text_value("schema value", value)
                for key, value in self.output_schema.items()
            }),
        )
        object.__setattr__(self, "modes", frozenset(_normalize_identifiers("modes", self.modes)))
        object.__setattr__(
            self,
            "profiles",
            frozenset(_normalize_identifiers("profiles", self.profiles)),
        )
        if type(self.high_risk_only) is not bool:
            raise ValueError("high_risk_only must be a boolean")


@dataclass(frozen=True, slots=True)
class RoleCatalog:
    """Immutable collection of reusable role definitions."""

    roles: tuple[RoleDefinition, ...]

    def __post_init__(self) -> None:
        roles = tuple(self.roles)
        if not all(isinstance(role, RoleDefinition) for role in roles):
            raise ValueError("roles must contain only RoleDefinition values")
        keys = [
            (mode, profile, role.id)
            for role in roles
            for mode in role.modes
            for profile in role.profiles
        ]
        if len(keys) != len(set(keys)):
            raise ValueError("duplicate role catalog entry")
        object.__setattr__(self, "roles", roles)

    def with_role(self, role: RoleDefinition) -> RoleCatalog:
        return RoleCatalog((*self.roles, role))

    def roles_for(self, *, mode: str, profile: str, high_risk: bool) -> tuple[RoleDefinition, ...]:
        _require_identifier("mode", mode)
        _require_identifier("profile", profile)
        if type(high_risk) is not bool:
            raise ValueError("high_risk must be a boolean")
        return tuple(
            role
            for role in self.roles
            if mode in role.modes
            and profile in role.profiles
            and (high_risk or not role.high_risk_only)
        )


def default_role_catalog() -> RoleCatalog:
    return RoleCatalog(_daily_work_roles())


def _daily_work_roles() -> tuple[RoleDefinition, ...]:
    discussion_schema = {
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
    dispatch_schema = {
        "status": "done | blocked | needs_user",
        "summary": "string",
        "evidence": "string[]",
        "risks": "string[]",
        "artifacts": "string[]",
        "verification": "string[]",
    }
    general_discuss = frozenset({"general"})
    general_dispatch = frozenset({"general"})
    return (
        _role("director", "Director", "expertise", "Own narrative direction, shots, rhythm, and audience intent.", ("What story are we telling?", "What should the audience feel?", "What must be cut?"), ("creative-direction",), discussion_schema, modes=("discuss",), profiles=general_discuss),
        _role("copywriter", "Copywriter", "expertise", "Generate hooks, titles, scripts, slogans, and conversion copy.", ("What is the core message?", "What hook is strongest?", "What CTA is clear?"), ("copywriting",), discussion_schema, modes=("discuss",), profiles=general_discuss),
        _role("video_editor", "Video Editor", "expertise", "Plan edit structure, pacing, transitions, captions, and asset needs.", ("What is the edit sequence?", "Where should pacing change?", "What assets are missing?"), ("editing",), discussion_schema, modes=("discuss",), profiles=general_discuss),
        _role("economic_analyst", "Economic Analyst", "expertise", "Evaluate market context, incentives, demand, pricing, and macro risks.", ("What economic force matters?", "What assumption drives ROI?", "What risk changes the decision?"), ("analysis",), discussion_schema, modes=("discuss",), profiles=general_discuss),
        _role("marketing_strategist", "Marketing Strategist", "expertise", "Define audience, channel, positioning, funnel, and campaign angle.", ("Who is the audience?", "Which channel fits?", "What conversion path is likely?"), ("marketing",), discussion_schema, modes=("discuss",), profiles=general_discuss),
        _role("product_manager", "Product Manager", "plan", "Translate goals into product scope, priorities, user value, and tradeoffs.", ("What user problem is solved?", "What is in scope?", "What tradeoff matters?"), ("product",), discussion_schema, modes=("discuss",), profiles=general_discuss),
        _role("finance_analyst", "Finance Analyst", "risk_review", "Check budget, unit economics, payback, and cash impact.", ("What is the budget?", "What return is plausible?", "What cost cap is needed?"), ("finance",), discussion_schema, modes=("discuss",), profiles=general_discuss),
        _role("legal_compliance_reviewer", "Legal/Compliance Reviewer", "risk_review", "Identify legal, policy, copyright, privacy, and claim-risk issues.", ("What claim is risky?", "What permission is needed?", "What disclaimer is needed?"), ("compliance",), discussion_schema, modes=("discuss",), profiles=general_discuss),
        _role("designer", "Designer", "expertise", "Evaluate visual hierarchy, layout, brand consistency, and deliverable clarity.", ("What visual system fits?", "What should be emphasized?", "What design risk exists?"), ("design",), discussion_schema, modes=("discuss",), profiles=general_discuss),
        _role("sales_advisor", "Sales Advisor", "expertise", "Shape objections, buyer value, sales talking points, and closing path.", ("What buyer objection matters?", "What value proof is needed?", "What sales script works?"), ("sales",), discussion_schema, modes=("discuss",), profiles=general_discuss),
        _role("project_manager", "Project Manager", "plan", "Break daily work into owners, milestones, dependencies, and acceptance checks.", ("What needs to happen?", "Who owns which output?", "What is the deadline risk?"), ("planning",), dispatch_schema, modes=("dispatch", "hybrid"), profiles=general_dispatch),
        _role("copywriter", "Copywriter", "execute", "Produce practical copy, scripts, titles, posts, and message variants.", ("What copy was produced?", "Which version is recommended?", "What needs review?"), ("copywriting",), dispatch_schema, modes=("dispatch", "hybrid"), profiles=general_dispatch),
        _role("content_editor", "Content Editor", "verify", "Polish structure, tone, readability, grammar, and consistency.", ("What was edited?", "What issue remains?", "Is the tone consistent?"), ("editing",), dispatch_schema, modes=("dispatch", "hybrid"), profiles=general_dispatch),
        _role("economic_analyst", "Economic Analyst", "expertise", "Build lightweight economic assumptions, scenarios, and decision implications.", ("What scenario is likely?", "What variable matters?", "What conclusion follows?"), ("analysis",), dispatch_schema, modes=("dispatch", "hybrid"), profiles=general_dispatch),
        _role("finance_analyst", "Finance Analyst", "risk_review", "Prepare budget, cost estimate, revenue assumptions, and ROI ranges.", ("What does it cost?", "What return is expected?", "What cap is needed?"), ("finance",), dispatch_schema, modes=("dispatch", "hybrid"), profiles=general_dispatch),
        _role("operations_coordinator", "Operations Coordinator", "execute", "Coordinate calendars, checklists, vendors, assets, and handoff status.", ("What is blocked?", "What is ready?", "What handoff is needed?"), ("operations",), dispatch_schema, modes=("dispatch", "hybrid"), profiles=general_dispatch),
        _role("legal_compliance_reviewer", "Legal/Compliance Reviewer", "risk_review", "Review risky claims, licensing, privacy, and external publication constraints.", ("What should not be published?", "What approval is required?", "What wording is safer?"), ("compliance",), dispatch_schema, modes=("dispatch", "hybrid"), profiles=general_dispatch),
        _role("quality_reviewer", "Quality Reviewer", "verify", "Check completeness, evidence, formatting, and user-request alignment.", ("Does it meet the request?", "What is missing?", "What should be fixed?"), ("quality",), dispatch_schema, modes=("dispatch", "hybrid"), profiles=general_dispatch),
    )


def _role(
    identifier: str,
    role: str,
    purpose: str,
    mission: str,
    must_answer: tuple[str, ...],
    skills: tuple[str, ...],
    output_schema: Mapping[str, str],
    *,
    modes: Iterable[str],
    profiles: Iterable[str],
) -> RoleDefinition:
    return RoleDefinition(
        id=identifier,
        role=role,
        purpose=purpose,
        mission=mission,
        must_answer=must_answer,
        allowed_tools=("read_context",),
        forbidden_actions=("do not execute external operations", "do not expand scope silently"),
        skills=skills,
        output_schema=output_schema,
        modes=frozenset(modes),
        profiles=frozenset(profiles),
    )


def _require_identifier(name: str, value: str) -> str:
    if type(value) is not str or _SAFE_IDENTIFIER.fullmatch(value) is None:
        raise ValueError(f"{name} must be a safe identifier")
    return value


def _require_text(name: str, value: str) -> None:
    if type(value) is not str or not value or value != value.strip() or len(value) > _MAX_TEXT:
        raise ValueError(f"{name} must be nonblank, unpadded, and bounded")
    if any(ord(character) < 32 for character in value):
        raise ValueError(f"{name} must not contain control characters")


def _require_text_value(name: str, value: str) -> str:
    _require_text(name, value)
    return value


def _normalize_texts(name: str, values: Iterable[str]) -> tuple[str, ...]:
    normalized = tuple(values)
    if not normalized:
        raise ValueError(f"{name} must not be empty")
    for value in normalized:
        _require_text(name, value)
    return normalized


def _normalize_identifiers(name: str, values: Iterable[str]) -> tuple[str, ...]:
    normalized = tuple(values)
    if not normalized:
        raise ValueError(f"{name} must not be empty")
    for value in normalized:
        _require_identifier(name, value)
    if len(normalized) != len(set(normalized)):
        raise ValueError(f"{name} must be unique")
    return normalized


__all__ = ["RoleCatalog", "RoleDefinition", "default_role_catalog"]
