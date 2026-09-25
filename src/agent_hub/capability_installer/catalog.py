from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from agent_hub.api.routers.admin import PluginCapabilityRequest, PluginResourceRequest

CapabilityInstallRisk = Literal[
    "read_only",
    "workspace_write",
    "network_write",
    "account_write",
    "code_execution",
    "security_testing",
    "production_change",
]


class CapabilityCatalogEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=128, pattern=r"^[a-z0-9][a-z0-9_-]*$")
    name_cn: str = Field(min_length=1, max_length=128)
    summary_cn: str = Field(min_length=1, max_length=500)
    aliases: tuple[str, ...] = Field(default_factory=tuple, max_length=64)
    risks: tuple[CapabilityInstallRisk, ...] = Field(min_length=1, max_length=16)
    permission_summary: tuple[str, ...] = Field(min_length=1, max_length=16)
    plugin: PluginResourceRequest

    @field_validator("aliases")
    @classmethod
    def validate_aliases(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(alias.strip().lower() for alias in value if alias.strip())
        if len(set(normalized)) != len(normalized):
            raise ValueError("catalog aliases must be unique")
        return normalized


class CapabilityInstallPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    status: Literal["planned", "cancelled", "installed", "failed", "rolled_back"] = "planned"
    entry_id: str
    name_cn: str
    summary_cn: str
    query: str
    plugin_id: str
    capabilities: tuple[str, ...]
    risks: tuple[CapabilityInstallRisk, ...]
    permission_summary: tuple[str, ...]
    rollback_strategy: Literal["restore_previous_plugin_or_delete_installed_plugin"]
    requires_confirmation: bool
    plugin_request: dict[str, object]


class TrustedCapabilityCatalog:
    def __init__(self, entries: Iterable[CapabilityCatalogEntry]) -> None:
        self._entries = tuple(entries)
        ids = [entry.id for entry in self._entries]
        if len(set(ids)) != len(ids):
            raise ValueError("catalog entry ids must be unique")
        self._by_id = {entry.id: entry for entry in self._entries}

    def list(self) -> tuple[CapabilityCatalogEntry, ...]:
        return self._entries

    def get(self, entry_id: str) -> CapabilityCatalogEntry:
        try:
            return self._by_id[entry_id]
        except KeyError:
            raise KeyError("capability catalog entry not found") from None

    def resolve(self, query: str) -> tuple[CapabilityCatalogEntry, ...]:
        query_tokens = _tokens(query)
        scored: list[tuple[int, CapabilityCatalogEntry]] = []
        for entry in self._entries:
            score = _match_score(entry, query_tokens)
            if score > 0:
                scored.append((score, entry))
        return tuple(entry for _score, entry in sorted(scored, key=lambda item: (-item[0], item[1].id)))

    def plan(self, entry_id: str, *, query: str) -> CapabilityInstallPlan:
        entry = self.get(entry_id)
        payload = entry.plugin.model_dump(mode="json", exclude_none=True)
        plan_seed = json.dumps(
            {
                "entry_id": entry.id,
                "query": query.strip(),
                "plugin": payload,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        digest = hashlib.sha256(plan_seed.encode("utf-8")).hexdigest()[:16]
        return CapabilityInstallPlan(
            id=f"cap-install-{digest}",
            entry_id=entry.id,
            name_cn=entry.name_cn,
            summary_cn=entry.summary_cn,
            query=query.strip(),
            plugin_id=entry.plugin.id,
            capabilities=tuple(capability.id for capability in entry.plugin.capabilities),
            risks=entry.risks,
            permission_summary=entry.permission_summary,
            rollback_strategy="restore_previous_plugin_or_delete_installed_plugin",
            requires_confirmation=True,
            plugin_request=payload,
        )


def default_trusted_capability_entries() -> tuple[CapabilityCatalogEntry, ...]:
    return (
        CapabilityCatalogEntry(
            id="office_doc_search",
            name_cn="Office 文档搜索",
            summary_cn="读取用户授权范围内的 Office 文档索引，并按关键词返回片段和文件位置。",
            aliases=(
                "office",
                "office文档",
                "文档搜索",
                "docx",
                "xlsx",
                "pptx",
                "search_documents",
            ),
            risks=("read_only",),
            permission_summary=(
                "读取 Office 文档索引",
                "执行前仍按能力策略审批",
            ),
            plugin=PluginResourceRequest(
                id="office-doc-search",
                name="Office 文档搜索",
                description="Search authorized Office documents through a reviewed HTTP JSON connector.",
                version="1.0.0",
                endpoint_url="https://plugins.example/office-doc-search/invoke",
                domain_allowlist=["plugins.example"],
                capabilities=[
                    PluginCapabilityRequest(
                        id="office.search_documents",
                        adapter="http_json",
                        permission_class="file.read",
                        sandbox_profile="remote_connector",
                        policy_effect="require_approval",
                        replay_safe=True,
                        aliases=["office_doc_search", "search_documents"],
                    )
                ],
            ),
        ),
        CapabilityCatalogEntry(
            id="security_testing",
            name_cn="自动化安全测试",
            summary_cn="把授权目标交给外部安全测试连接器执行探测，并返回结构化发现。",
            aliases=("安全测试", "渗透", "pentest", "security", "strix"),
            risks=("security_testing", "network_write"),
            permission_summary=("发起授权安全测试流量", "高风险动作必须单次审批"),
            plugin=PluginResourceRequest(
                id="security-testing",
                name="自动化安全测试",
                description="Run approved security testing jobs through a controlled connector.",
                version="1.0.0",
                endpoint_url="https://plugins.example/security-testing/invoke",
                domain_allowlist=["plugins.example"],
                capabilities=[
                    PluginCapabilityRequest(
                        id="security.run_assessment",
                        adapter="http_json",
                        permission_class="security.testing",
                        sandbox_profile="remote_connector",
                        policy_effect="require_approval",
                        replay_safe=False,
                        aliases=["pentest", "security_assessment", "strix"],
                    )
                ],
            ),
        ),
    )


def _tokens(value: str) -> frozenset[str]:
    normalized = value.strip().lower()
    words = set(re.findall(r"[a-z0-9_.-]+", normalized))
    for phrase in ("office", "文档", "搜索", "安全", "渗透", "测试"):
        if phrase in normalized:
            words.add(phrase)
    return frozenset(words)


def _match_score(entry: CapabilityCatalogEntry, query_tokens: Iterable[str]) -> int:
    searchable = {
        entry.id,
        entry.name_cn.lower(),
        entry.summary_cn.lower(),
        entry.plugin.id,
        *(alias.lower() for alias in entry.aliases),
        *(capability.id for capability in entry.plugin.capabilities),
    }
    searchable_text = " ".join(searchable)
    score = 0
    for token in query_tokens:
        if token in searchable:
            score += 3
        elif token and token in searchable_text:
            score += 1
    return score


__all__ = [
    "CapabilityCatalogEntry",
    "CapabilityInstallPlan",
    "CapabilityInstallRisk",
    "TrustedCapabilityCatalog",
    "default_trusted_capability_entries",
]
