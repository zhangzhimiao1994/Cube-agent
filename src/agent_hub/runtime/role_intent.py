"""Versioned hints for role selection only, never execution or authorization."""
from __future__ import annotations

import json
import re
from collections.abc import Mapping

_DATA = re.compile(r"(?<!\w)UNTRUSTED_DATA_JSON\s*=", re.ASCII)
_PROHIBITION = re.compile(
    r"(?:(?<![a-z0-9_])(?:never|do\s+not|don['\u2019]t|must\s+not|should\s+not)\b"
    r"(?!\s+(?:forget|fail)\b)|(?:\u4e0d\u8981|\u7981\u6b62|\u4e0d\u5f97(?!\u4e0d)|\u4e0d\u5141\u8bb8|\u5207\u52ff)"
    r"(?!\u5fd8\u8bb0|\u5fd8\u4e86))"
    r".*?(?=$|[\n\r;!?\u3002\uff1b\uff01\uff1f]|\.(?=\s|$)|"
    r",\s*just\b|\b(?:but|however|instead)\b|\u4f46\u662f|\u4f46|\u7136\u800c)",
    re.IGNORECASE | re.ASCII | re.DOTALL,
)


def _version(value: object) -> int:
    if type(value) is not int or value not in (0, 1):
        raise ValueError("unsupported role planning intent version")
    return value


def role_planning_intent_version(routing_decision: Mapping[str, object]) -> int:
    """Missing policy is legacy; never infer a new policy from request contents."""
    return _version(routing_decision.get("role_planning_intent_version", 0))


def _reject_constant(_: str) -> object:
    raise ValueError("diagnostic DATA must be JSON")


def role_planning_intent_view(task: object, *, version: int = 0) -> str:
    text = str(task)
    if _version(version) == 0:
        return text
    decoder = json.JSONDecoder(parse_constant=_reject_constant)
    parts: list[str] = []
    position = 0
    while match := _DATA.search(text, position):
        parts.append(text[position:match.start()])
        start = match.end()
        while start < len(text) and text[start].isspace():
            start += 1
        try:
            value, end = decoder.raw_decode(text, start)
            if not isinstance(value, dict | list):
                break
        except (ValueError, RecursionError):
            # A malformed DATA tail cannot become positive planning intent.
            break
        parts.append(" ")
        position = end
    else:
        parts.append(text[position:])
    return _PROHIBITION.sub(" ", "".join(parts))
