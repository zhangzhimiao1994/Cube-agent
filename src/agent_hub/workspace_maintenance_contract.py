"""Versioned existing-workspace completion hints, never execution authority."""
from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence

from agent_hub.runtime.role_intent import role_planning_intent_view

WORKSPACE_MAINTENANCE_CONTRACT_MARKER = "Existing workspace maintenance contract:"
WORKSPACE_MAINTENANCE_CONTRACT_GUIDANCE = (
    WORKSPACE_MAINTENANCE_CONTRACT_MARKER + "\n"
    "Repair only the necessary existing production files; do not initialize or rebuild the project. "
    "Preserve tests and assertions, package files, config files, and dependencies. "
    "Use the existing authorized workspace tools; this contract grants no new permissions. "
    "Read only what is needed for the repair, then use workspace.write_text for the changed files. "
    "After a successful fresh write, call workspace.bundle with presentation=final_attachment. "
    "Do not claim completion until the new bundle succeeds. "
    "Do not paste the full project source into chat or treat old verification as proof of new files."
)
_NON_COMMAND = re.compile(
    r"```.*?(?:```|\Z)|~~~.*?(?:~~~|\Z)|`[^`]*(?:`|\Z)|"
    r'"(?:\\.|[^"\\])*(?:"|\Z)|(?<!\w)\'(?:\\.|[^\'\\])*(?:\'|\Z)|'
    r"\u201c[^\u201d]*(?:\u201d|\Z)|\u300c[^\u300d]*(?:\u300d|\Z)|"
    r"\u300e[^\u300f]*(?:\u300f|\Z)|^\s*>[^\n]*",
    re.MULTILINE | re.DOTALL,
)
_CLAUSE = re.compile(r"([\n\r;!?\u3002\uff1b\uff01\uff1f]|\.(?=\s|$))")
_ACTION = re.compile(
    r"\b(?:fix|repair|modify|update|patch|maintain|edit)\b|"
    r"\u4fee\u6539|\u4fee\u590d|\u4fee\u8865|\u66f4\u65b0|\u7ef4\u62a4|\u8c03\u6574", re.IGNORECASE,
)
_EXISTING = re.compile(
    r"\b(?:existing|current|retained)\s+(?:\w+\s+){0,4}(?:project|workspace|files?|application|code|sources?)\b|"
    r"(?:\u73b0\u6709|\u5df2\u6709|\u5f53\u524d|\u539f\u6709)(?:\u9879\u76ee|\u5de5\u7a0b|\u5de5\u4f5c\u533a|\u6587\u4ef6|\u5e94\u7528|\u4ee3\u7801|\u6e90\u6587\u4ef6)", re.IGNORECASE,
)
_DELIVERY = re.compile(
    r"\b(?:deliver|return|package|produce|attach|generate)\b.{0,100}\b(?:zip|bundle)\b|"
    r"\bworkspace\.bundle\b|\bbundle\s+(?:it|the|these|existing|current)\b|"
    r"(?:\u4ea4\u4ed8|\u6253\u5305|\u751f\u6210).{0,80}(?:zip|bundle|\u538b\u7f29\u5305)", re.IGNORECASE,
)
_META = re.compile(
    r"\b(?:how\s+to|explain|describe|diagnose|review|example|diagnostic|analysis|read.only)\b|"
    r"\u5982\u4f55|\u600e\u4e48|\u8bf4\u660e|\u89e3\u91ca|\u8bca\u65ad|\u793a\u4f8b|\u53ea\u5206\u6790|\u65e0\u9700|\u4e0d\u5fc5", re.IGNORECASE,
)
_ADVICE = re.compile(r"\b(?:how|whether)\b|\u5982\u4f55|\u600e\u4e48|\u662f\u5426", re.IGNORECASE)
_CONDITIONAL = re.compile(
    r"\b(?:if|unless|suppose|supposing|assuming|provided|maybe|perhaps|might|may|would|could|should|hypothetically)\b|"
    r"\bin\s+case\b|\u5982\u679c|\u5047\u5982|\u5047\u8bbe|\u5018\u82e5|\u8981\u662f|\u4e5f\u8bb8|\u53ef\u80fd|\u8003\u8651",
    re.IGNORECASE,
)
_COMMAND_PREFIX = re.compile(
    r"\s*(?:(?:please|kindly|now|then|this\s+time|call)\s+|"
    r"(?:\u8bf7\u4f60|\u8bf7|\u9ebb\u70e6|\u73b0\u5728|\u8fd9\u6b21|\u672c\u6b21|\u7136\u540e)\s*)*",
    re.IGNORECASE,
)
_ACTION_V2 = re.compile(
    _ACTION.pattern + r"|(?<![A-Za-z0-9_./-])workspace\.write_text(?![A-Za-z0-9_/-]|\.[A-Za-z0-9_])",
    re.IGNORECASE,
)
_COMMAND_PREFIX_V2 = re.compile(
    r"\s*(?:(?:please|kindly|now|then|this\s+time|call|use)\s+|"
    r"(?:\u8bf7\u4f60|\u8bf7|\u9ebb\u70e6|\u73b0\u5728|\u8fd9\u6b21|\u672c\u6b21|\u7136\u540e|\u4f7f\u7528|\u7528)\s*)*",
    re.IGNORECASE,
)
_TOKEN_START_V2 = r"(?<![A-Za-z0-9_./-])"
_TOKEN_END_V2 = r"(?![A-Za-z0-9_/-]|\.[A-Za-z0-9_])"
_BUNDLE_V2 = _TOKEN_START_V2 + r"workspace\.bundle" + _TOKEN_END_V2
_PACKAGE_V2 = _TOKEN_START_V2 + r"(?:zip|bundle)" + _TOKEN_END_V2
_DELIVERY_V2 = re.compile(
    r"\b(?:deliver|return|package|produce|attach|generate)\b.{0,100}?(?:" + _BUNDLE_V2 + "|" + _PACKAGE_V2 + ")|"
    + _BUNDLE_V2 + r"|\bbundle\s+(?:it|the|these|existing|current)\b|"
    r"(?:\u4ea4\u4ed8|\u6253\u5305|\u751f\u6210).{0,80}?(?:" + _BUNDLE_V2 + "|" + _PACKAGE_V2 + r"|\u538b\u7f29\u5305)",
    re.IGNORECASE,
)
_REFERENCE_V2 = re.compile(
    r"\b(?:as|for)\s+(?:(?:an?|the)\s+)?(?:example|illustration|reference)s?\b|"
    r"\b(?:for\s+documentation\s+only|not\s+for\s+execution|not\s+as\s+(?:an?\s+)?(?:instruction|command))\b|"
    r"(?:\u4f5c\u4e3a|\u7528\u4f5c|\u4ec5\u4f5c|\u4ec5\u7528\u4e8e).{0,40}(?:\u793a\u4f8b|\u53c2\u8003|\u8bf4\u660e)",
    re.IGNORECASE,
)
_FOLLOWUP_V2 = re.compile(
    r"(?:\b(?:then|and)\s+|,\s*)(?:explain|describe|review|summarize)\b|"
    r"(?:\u7136\u540e|\u518d)(?:\u89e3\u91ca|\u8bf4\u660e|\u603b\u7ed3)", re.IGNORECASE,
)
_EXECUTION_VETO_V2 = re.compile(
    r"^\s*(?:please\s+)?(?:never|do\s+not|don['\u2019]t|must\s+not|should\s+not)\s+"
    r"(?:execute|run|invoke|call|use)\s*(?:(?:(?:any|the|these)\s+)?tools?\b|"
    + _TOKEN_START_V2 + r"workspace\.(?:write_text|bundle)" + _TOKEN_END_V2 + r"|$)|"
    r"^\s*(?:\u8bf7)?(?:\u4e0d\u8981|\u7981\u6b62|\u4e0d\u5f97|\u4e0d\u5141\u8bb8|\u5207\u52ff)"
    r"(?:\u6267\u884c|\u8fd0\u884c|\u8c03\u7528|\u4f7f\u7528)\s*(?:(?:\u4efb\u4f55|\u8fd9\u4e9b)?\u5de5\u5177|"
    + _TOKEN_START_V2 + r"workspace\.(?:write_text|bundle)" + _TOKEN_END_V2 + r"|$)",
    re.IGNORECASE,
)
_DATA_V2 = re.compile(r"(?<!\w)UNTRUSTED_DATA_JSON\s*=", re.ASCII)
_PARAGRAPH_V2 = re.compile(r"\n[ \t]*\n")
_NEW_PROJECT = re.compile(
    r"\b(?:build|create|generate|initialize|scaffold)\b.{0,40}\b(?:new|entire|whole)\b.{0,30}\b(?:project|application|workspace)\b|"
    r"\u65b0\u5efa\u9879\u76ee|\u4ece\u96f6|\u5b8c\u6574\u91cd\u5199", re.IGNORECASE,
)


def workspace_maintenance_contract_version(routing_decision: Mapping[str, object]) -> int:
    value = routing_decision.get("workspace_maintenance_contract_version", 0)
    if type(value) is not int or value not in (0, 1):
        raise ValueError("unsupported workspace maintenance contract version")
    return value


def _intent_version(value: object) -> int:
    if type(value) is not int or value not in (1, 2):
        raise ValueError("unsupported workspace maintenance intent version")
    return value


def _reject_data_constant(value: str) -> object:
    raise ValueError("diagnostic DATA must be JSON")


def _v2_intent_text(task: object) -> str:
    """Remove quoted/DATA content while retaining prohibitions for scoped checks."""
    text = str(task)
    decoder = json.JSONDecoder(parse_constant=_reject_data_constant)
    parts: list[str] = []
    position = 0
    while match := _DATA_V2.search(text, position):
        parts.append(text[position:match.start()])
        start = match.end()
        while start < len(text) and text[start].isspace():
            start += 1
        try:
            value, end = decoder.raw_decode(text, start)
            if not isinstance(value, dict | list):
                break
        except (ValueError, RecursionError):
            break
        parts.append(" ")
        position = end
    else:
        parts.append(text[position:])
    return _NON_COMMAND.sub(" ", "".join(parts)).replace("\r\n", "\n").replace("\r", "\n")


def _is_workspace_maintenance_request_v2(task: object) -> bool:
    text = _v2_intent_text(task)
    if _NEW_PROJECT.search(role_planning_intent_view(text, version=1)):
        return False
    modifies_existing = delivers = False
    for paragraph in _PARAGRAPH_V2.split(text):
        conditional_scope = False
        clauses = _CLAUSE.split(paragraph)
        for index in range(0, len(clauses), 2):
            clause = clauses[index]
            ending = clauses[index + 1] if index + 1 < len(clauses) else ""
            head = clause
            followup, delivery = _FOLLOWUP_V2.search(clause), _DELIVERY_V2.search(clause)
            # Only detach explanation after an already complete delivery command.
            if followup is not None and delivery is not None and delivery.end() <= followup.start():
                head = clause[:followup.start()]
            if _CONDITIONAL.search(head):
                conditional_scope |= ending in {"\n", "\r"}
                continue
            if conditional_scope or ending in {"?", "\uff1f"} or _ADVICE.search(head):
                continue
            if _REFERENCE_V2.search(head):
                continue
            if _EXECUTION_VETO_V2.search(head):
                return False
            head = role_planning_intent_view(head, version=1)
            action, delivery, meta = _ACTION_V2.search(head), _DELIVERY_V2.search(head), _META.search(head)
            commands = [match for match in (action, delivery) if match is not None]
            if not commands:
                continue
            command_start = min(match.start() for match in commands)
            if (_COMMAND_PREFIX_V2.fullmatch(head[:command_start]) is None
                    or (meta is not None and meta.start() < command_start)):
                continue
            modifies_existing |= action is not None and _EXISTING.search(head) is not None
            delivers |= delivery is not None
    return modifies_existing and delivers


def is_workspace_maintenance_request(task: object, *, version: int = 1) -> bool:
    """Recognize positive maintenance and delivery outside diagnostic/quoted text."""
    version = _intent_version(version)
    if version == 2:
        return _is_workspace_maintenance_request_v2(task)
    text = _NON_COMMAND.sub(" ", role_planning_intent_view(task, version=1))
    if _NEW_PROJECT.search(text):
        return False
    modifies_existing = delivers = False
    clauses = _CLAUSE.split(text)
    for index in range(0, len(clauses), 2):
        clause = clauses[index]
        ending = clauses[index + 1] if index + 1 < len(clauses) else ""
        if ending in {"?", "\uff1f"} or _ADVICE.search(clause) or _CONDITIONAL.search(clause):
            continue
        action, meta = _ACTION.search(clause), _META.search(clause)
        delivery = _DELIVERY.search(clause)
        commands = [match for match in (action, delivery) if match is not None]
        if not commands:
            continue
        command_start = min(match.start() for match in commands)
        if (_COMMAND_PREFIX.fullmatch(clause[:command_start]) is None
                or (meta is not None and meta.start() < command_start)):
            continue
        modifies_existing |= action is not None and _EXISTING.search(clause) is not None
        delivers |= delivery is not None
    return modifies_existing and delivers


def workspace_maintenance_contract_active(
    task: object, routing_decision: Mapping[str, object], tools: Sequence[str],
) -> bool:
    intent_version = _intent_version(routing_decision.get("workspace_maintenance_intent_version", 1))
    if workspace_maintenance_contract_version(routing_decision) == 0:
        return False
    return (
        routing_decision.get("workspace_project_explicit") is True
        and all(isinstance(value := routing_decision.get(key), str) and bool(value.strip())
                for key in ("project_id", "workspace_session_id"))
        and {"workspace.write_text", "workspace.bundle"}.issubset(tools)
        and is_workspace_maintenance_request(task, version=intent_version)
    )
