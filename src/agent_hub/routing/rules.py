from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from agent_hub.domain.runs import TaskMode
from agent_hub.routing.types import RiskLevel

MAX_TASK_TEXT = 16_000

_COMMANDS = {
    "/direct": TaskMode.DIRECT,
    "/dispatch": TaskMode.DISPATCH,
    "/discuss": TaskMode.DISCUSS,
    "/hybrid": TaskMode.HYBRID,
    "/auto": TaskMode.AUTO,
}
_DIRECT = re.compile(
    r"^(what|who|when|where|define|explain|calculate|compute|check status|search for|find)\b|"
    r"^(hi|hello|thanks|thank you)[.!? ]*$",
    re.IGNORECASE,
)
_DISPATCH = re.compile(
    r"\b(run|use|execute)\s+(the\s+)?(fixed|published|named)?\s*workflow\b",
    re.IGNORECASE,
)
_DISCUSS = re.compile(
    r"\b(multiple|several|different)\s+agents?\s+(debate|discuss)|"
    r"\bdebate\s+(this|the)|\breach\s+(a\s+)?consensus\b",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class ExplicitCommand:
    mode: TaskMode | None
    task_text: str
    invalid: bool = False


@dataclass(frozen=True, slots=True)
class RuleResult:
    mode: TaskMode | None
    reason: str | None
    risk: RiskLevel
    requires_approval: bool
    conflict: bool = False


@dataclass(frozen=True, slots=True)
class RiskRulePolicy:
    """Conservative multilingual detector; normalization is never used for commands."""

    destructive_actions: tuple[str, ...] = (
        "delete",
        "drop",
        "truncate",
        "destroy",
        "wipe",
        "erase",
        "remove",
        "删除",
        "清空",
        "销毁",
        "截断",
        "擦除",
        "移除",
    )
    destructive_targets: tuple[str, ...] = (
        "production",
        "prod",
        "database",
        "db",
        "data",
        "table",
        "file",
        "生产",
        "正式",
        "数据库",
        "数据",
        "表",
        "文件",
    )
    financial_actions: tuple[str, ...] = (
        "transfer",
        "payment",
        "refund",
        "pay",
        "wire",
        "转账",
        "付款",
        "支付",
        "退款",
        "汇款",
    )
    sensitive_actions: tuple[str, ...] = (
        "change",
        "reset",
        "rotate",
        "revoke",
        "grant",
        "expose",
        "export",
        "delete",
        "修改",
        "重置",
        "轮换",
        "撤销",
        "授予",
        "泄露",
        "导出",
        "删除",
    )
    sensitive_targets: tuple[str, ...] = (
        "permission",
        "credential",
        "secret",
        "api key",
        "token",
        "password",
        "权限",
        "凭证",
        "密钥",
        "秘钥",
        "令牌",
        "密码",
        "授权",
    )
    external_actions: tuple[str, ...] = (
        "publish",
        "deploy",
        "send external",
        "发布",
        "部署",
        "上线",
        "外发",
        "推送到外部",
    )
    irreversible_markers: tuple[str, ...] = (
        "irreversible",
        "permanent",
        "cannot undo",
        "不可逆",
        "无法撤销",
        "永久",
    )

    def is_high_risk(self, task_text: str) -> bool:
        normalized = unicodedata.normalize("NFKC", task_text).casefold()
        separated = re.sub(r"[\s_\-./:]+", " ", normalized)
        searchable = f" {separated} "

        def contains_any(values: tuple[str, ...]) -> bool:
            return any(value in searchable or value in normalized for value in values)

        return (
            contains_any(self.financial_actions)
            or contains_any(self.external_actions)
            or contains_any(self.irreversible_markers)
            or (
                contains_any(self.destructive_actions)
                and contains_any(self.destructive_targets)
            )
            or (contains_any(self.sensitive_actions) and contains_any(self.sensitive_targets))
        )


def validate_task_text(task_text: object) -> str:
    if type(task_text) is not str:
        raise TypeError("task text must be a string")
    if not task_text or len(task_text) > MAX_TASK_TEXT:
        raise ValueError("task text must be nonempty and bounded")
    if "\x00" in task_text:
        raise ValueError("task text contains an invalid character")
    return task_text


def parse_explicit_command(task_text: str) -> ExplicitCommand:
    """Allow ASCII space/tab padding only; commands must be the complete first token."""
    leading_trimmed = task_text.lstrip(" \t")
    if not leading_trimmed:
        return ExplicitCommand(mode=None, task_text="", invalid=False)
    token, separator, remainder = leading_trimmed.partition(" ")
    if "\t" in token:
        token, tab, tab_remainder = token.partition("\t")
        separator = tab
        remainder = tab_remainder + ((" " + remainder) if remainder else "")
    mode = _COMMANDS.get(token)
    if mode is None:
        return ExplicitCommand(mode=None, task_text=task_text, invalid=False)
    body = remainder.lstrip(" \t") if separator else ""
    if not body or body.startswith("-") or len(body) > MAX_TASK_TEXT:
        return ExplicitCommand(mode=None, task_text="", invalid=True)
    return ExplicitCommand(mode=mode, task_text=body)


def assess_rules(
    task_text: str, *, risk_policy: RiskRulePolicy | None = None
) -> RuleResult:
    high_risk = (risk_policy or RiskRulePolicy()).is_high_risk(task_text)
    signals = {
        TaskMode.DIRECT: _DIRECT.search(task_text) is not None,
        TaskMode.DISPATCH: _DISPATCH.search(task_text) is not None,
        TaskMode.DISCUSS: _DISCUSS.search(task_text) is not None,
    }
    matched = tuple(mode for mode, present in signals.items() if present)
    risk = RiskLevel.HIGH if high_risk else RiskLevel.LOW
    if high_risk:
        return RuleResult(None, "high_risk_requires_choice", risk, True)
    if len(matched) > 1:
        return RuleResult(None, "conflicting_deterministic_rules", risk, False, True)
    if len(matched) == 1:
        return RuleResult(matched[0], "deterministic_rule", risk, False)
    return RuleResult(None, None, risk, False)
