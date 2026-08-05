from __future__ import annotations

import re
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
_HIGH_RISK = re.compile(
    r"\b(delete|drop|destroy|wipe|shutdown|production|prod|transfer funds?|send money|"
    r"publish|deploy|revoke|disable security)\b",
    re.IGNORECASE,
)
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


def assess_rules(task_text: str) -> RuleResult:
    high_risk = _HIGH_RISK.search(task_text) is not None
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
