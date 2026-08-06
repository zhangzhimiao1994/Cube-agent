from __future__ import annotations

from dataclasses import dataclass
from html import escape

from agent_hub.knowledge.retrieval import KnowledgeHit
from agent_hub.memory.types import MemoryLayer, MemoryRecord


@dataclass(frozen=True, slots=True)
class ContextSection:
    name: str
    content: str
    priority: int
    estimated_tokens: int
    protected: bool = False


@dataclass(frozen=True, slots=True)
class BuiltContext:
    sections: tuple[ContextSection, ...]
    estimated_tokens: int


@dataclass(frozen=True, slots=True)
class ContextBuildInput:
    system_policy: str
    current_user_request: str
    approvals: tuple[str, ...] = ()
    active_plan: str | None = None
    checkpoint: str | None = None
    memories: tuple[MemoryRecord, ...] = ()
    knowledge_hits: tuple[KnowledgeHit, ...] = ()
    recent_transcript: tuple[str, ...] = ()
    compacted_summary: str | None = None
    current_constraints: tuple[str, ...] = ()
    unresolved_approvals: tuple[str, ...] = ()


class ContextBuilder:
    def build(self, value: ContextBuildInput, *, token_budget: int) -> BuiltContext:
        if token_budget < 1:
            raise ValueError("token budget must be positive")
        sections = _candidate_sections(value)
        selected: list[ContextSection] = [section for section in sections if section.protected]
        selected_tokens = sum(section.estimated_tokens for section in selected)
        if selected_tokens > token_budget:
            selected = _trim_protected(selected, token_budget)
            selected_tokens = sum(section.estimated_tokens for section in selected)
        for section in (section for section in sections if not section.protected):
            if selected_tokens + section.estimated_tokens <= token_budget:
                selected.append(section)
                selected_tokens += section.estimated_tokens
        selected.sort(key=lambda section: (section.priority, section.name))
        return BuiltContext(sections=tuple(selected), estimated_tokens=selected_tokens)


def _candidate_sections(value: ContextBuildInput) -> list[ContextSection]:
    sections = [
        _section("system_policy", value.system_policy, 10, protected=True),
        _section("current_user_request", value.current_user_request, 20, protected=True),
    ]
    sections.extend(_section("unresolved_approval", item, 30, protected=True) for item in value.unresolved_approvals)
    sections.extend(_section("current_constraint", item, 31, protected=True) for item in value.current_constraints)
    sections.extend(_section("approval", item, 40, protected=True) for item in value.approvals)
    if value.active_plan:
        sections.append(_section("active_plan", value.active_plan, 50))
    if value.checkpoint:
        sections.append(_section("checkpoint", value.checkpoint, 51))
    sections.extend(
        _section("core_memory", memory.text, 60)
        for memory in value.memories
        if memory.layer is MemoryLayer.CORE
    )
    sections.extend(
        _section("episodic_memory", memory.text, 70)
        for memory in value.memories
        if memory.layer is MemoryLayer.EPISODIC
    )
    sections.extend(
        _section(
            "knowledge_hit",
            _render_knowledge_hit(hit)
            + (" [PROMPT_INJECTION_RISK]" if hit.prompt_injection_risk else ""),
            80,
        )
        for hit in value.knowledge_hits
    )
    sections.extend(_section("recent_transcript", item, 90) for item in value.recent_transcript)
    if value.compacted_summary:
        sections.append(_section("compacted_summary", value.compacted_summary, 100))
    return sections


def _section(name: str, content: str, priority: int, *, protected: bool = False) -> ContextSection:
    return ContextSection(
        name=name,
        content=content,
        priority=priority,
        estimated_tokens=estimate_tokens(content),
        protected=protected,
    )


def estimate_tokens(text: str) -> int:
    return max(1, (len(text) + 3) // 4)


def _render_knowledge_hit(hit: KnowledgeHit) -> str:
    source_uri = escape(hit.source_uri, quote=True)
    version = escape(hit.version, quote=True)
    text = escape(hit.text, quote=False)
    return (
        f'<UNTRUSTED_KNOWLEDGE source="{source_uri}" version="{version}">\n'
        f"{text}\n"
        "</UNTRUSTED_KNOWLEDGE>"
    )


def _trim_protected(sections: list[ContextSection], budget: int) -> list[ContextSection]:
    result: list[ContextSection] = []
    remaining = budget
    for section in sorted(
        sections,
        key=lambda item: (0 if item.name == "current_user_request" else 1, item.priority, item.name),
    ):
        if remaining <= 0:
            break
        if section.estimated_tokens <= remaining:
            result.append(section)
            remaining -= section.estimated_tokens
            continue
        content = section.content[: remaining * 4]
        trimmed = _section(section.name, content, section.priority, protected=True)
        result.append(trimmed)
        remaining -= trimmed.estimated_tokens
    return result
