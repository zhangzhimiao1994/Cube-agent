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
        "shutdown",
        "disable",
        "format",
    )
    destructive_targets: tuple[str, ...] = (
        "production",
        "prod",
        "database",
        "db",
        "data",
        "table",
        "file",
        "disk",
        "server",
        "security",
        "control",
        "controls",
        "record",
        "records",
        "customer",
        "customers",
        "drive",
        "volume",
        "device",
        "partition",
        "filesystem",
    )
    financial_actions: tuple[str, ...] = (
        "transfer",
        "send",
        "refund",
        "pay",
        "wire",
    )
    financial_targets: tuple[str, ...] = (
        "money",
        "fund",
        "funds",
        "payment",
        "payments",
        "customer",
        "supplier",
        "vendor",
        "account",
        "recipient",
        "payee",
        "beneficiary",
        "receiver",
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
    )
    sensitive_targets: tuple[str, ...] = (
        "permission",
        "credential",
        "secret",
        "api key",
        "api",
        "key",
        "token",
        "password",
        "admin",
    )
    external_actions: tuple[str, ...] = (
        "publish",
        "deploy",
    )
    external_targets: tuple[str, ...] = (
        "external",
        "public",
        "production",
        "prod",
    )
    irreversible_markers: tuple[str, ...] = (
        "irreversible",
        "permanent",
        "cannot undo",
    )

    def is_high_risk(self, task_text: str) -> bool:
        normalized = unicodedata.normalize("NFKC", task_text).casefold()
        return any(self._clause_is_high_risk(clause) for clause in self._clauses(normalized))

    @staticmethod
    def _clauses(normalized: str) -> tuple[str, ...]:
        boundary = re.compile(
            r"(?:[;\n!?。！？]+|\.(?=\s+|$)|,\s*(?=(?:and\s+)?then\b)|"
            r"\b(?:and\s+then|and\s+afterwards|then|afterwards|after\s+that|next)\b|"
            r"(?:然后|随后|接着|之后|并且|说明后|解释后))",
            re.IGNORECASE,
        )
        return tuple(part.strip(" ,") for part in boundary.split(normalized) if part.strip(" ,"))

    def _clause_is_high_risk(self, clause: str) -> bool:
        if self._has_recursive_force_remove(clause):
            return True

        english_tokens = frozenset(re.findall(r"[a-z0-9]+", clause))

        def has(values: tuple[str, ...]) -> bool:
            return not english_tokens.isdisjoint(values)

        english_read_only = (
            re.match(r"^\s*(?:explain|what\s+is|what\s+are|describe|how\s+does)\b", clause)
            is not None
        )
        chinese_read_only = clause.startswith(("解释", "什么是", "介绍", "说明"))
        if english_read_only or chinese_read_only:
            return False

        destructive_action = has(self.destructive_actions) or (
            "shut" in english_tokens and "down" in english_tokens
        )
        currency_amount = (
            re.search(
                r"(?:[$€£¥]\s*\d+(?:[.,]\d+)?)|"
                r"(?:\b\d+(?:[.,]\d+)?\s*(?:usd|eur|gbp|cny|rmb|jpy)\b)",
                clause,
            )
            is not None
        )
        sensitive_targets = tuple(target for target in self.sensitive_targets if target != "api")
        if (
            (destructive_action and has(self.destructive_targets))
            or (has(self.financial_actions) and (has(self.financial_targets) or currency_amount))
            or (has(self.sensitive_actions) and has(sensitive_targets))
            or (has(self.external_actions) and has(self.external_targets))
            or any(marker in clause for marker in self.irreversible_markers)
        ):
            return True

        chinese_destructive_actions = (
            "删除",
            "清空",
            "销毁",
            "截断",
            "擦除",
            "移除",
            "关闭",
            "停机",
            "禁用",
            "格式化",
        )
        chinese_destructive_targets = (
            "生产",
            "正式",
            "数据库",
            "数据",
            "表",
            "文件",
            "磁盘",
            "服务器",
            "安全",
            "控制",
            "客户记录",
            "全部客户",
        )
        chinese_sensitive_actions = (
            "修改",
            "重置",
            "轮换",
            "撤销",
            "授予",
            "泄露",
            "导出",
            "删除",
        )
        chinese_sensitive_targets = (
            "权限",
            "凭证",
            "密钥",
            "秘钥",
            "令牌",
            "密码",
            "授权",
        )
        chinese_financial = ("转账", "付款", "支付", "退款", "汇款", "打款")
        chinese_external_actions = ("发布", "部署", "上线", "外发", "推送")
        chinese_external_targets = ("外部", "公开", "生产", "正式")

        def contains_any(values: tuple[str, ...]) -> bool:
            return any(value in clause for value in values)

        return (
            contains_any(chinese_financial)
            or contains_any(("不可逆", "无法撤销", "永久"))
            or (
                contains_any(chinese_destructive_actions)
                and contains_any(chinese_destructive_targets)
            )
            or (contains_any(chinese_sensitive_actions) and contains_any(chinese_sensitive_targets))
            or (contains_any(chinese_external_actions) and contains_any(chinese_external_targets))
        )

    @staticmethod
    def _has_recursive_force_remove(normalized: str) -> bool:
        """Detect a dangerous rm form as text only; this never parses or executes a shell."""
        command = re.compile(
            r"(?:^|[;&|])\s*(?:sudo\s+)?rm\s+"
            r"(?P<flags>(?:(?:-[a-z]+|--[a-z][a-z-]*)\s+)+)"
            r"(?:--\s+)?(?P<target>[^\s;&|]+)",
            re.IGNORECASE,
        )
        for match in command.finditer(normalized):
            flags = match.group("flags").split()
            short_letters = "".join(flag[1:] for flag in flags if re.fullmatch(r"-[a-z]+", flag))
            recursive = "r" in short_letters or "--recursive" in flags
            force = "f" in short_letters or "--force" in flags
            if recursive and force and match.group("target"):
                return True
        return False


def validate_task_text(task_text: object) -> str:
    if type(task_text) is not str:
        raise TypeError("task text must be a string")
    if not task_text or len(task_text) > MAX_TASK_TEXT:
        raise ValueError("task text must be nonempty and bounded")
    for character in task_text:
        if character in "\t\n\r":
            continue
        category = unicodedata.category(character)
        if category.startswith("C"):
            raise ValueError("task text contains an invalid character")
    return task_text


def normalize_task_text(task_text: str) -> str:
    """Normalize allowed whitespace after exact command parsing and before risk/model use."""
    return (
        task_text.replace("\r\n", "\n").replace("\r", "\n").replace("\n", " ; ").replace("\t", " ")
    )


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


def assess_rules(task_text: str, *, risk_policy: RiskRulePolicy | None = None) -> RuleResult:
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
