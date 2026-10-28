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


_PROHIBITION_V2 = re.compile(
    r"(?:(?<![a-z0-9_])(?:no|never|without|do\s+not|will\s+not|don['\u2019]t|must\s+not|should\s+not)\b"
    r"(?!\s+(?:forget|fail)\b)|(?:\u4e0d\u8981|\u65e0\u9700|\u4e0d\u9700\u8981|\u4e0d\u7528|\u4e0d\u5fc5|"
    r"\u7981\u6b62|\u4e0d\u5f97(?!\u4e0d)|\u4e0d\u5141\u8bb8|\u5207\u52ff)"
    r"(?!\u5fd8\u8bb0|\u5fd8\u4e86))"
    r".*?(?=$|[;!?\u3002\uff1b\uff01\uff1f]|\.(?=\s|$)|\n\s*\n|"
    r"(?<!,)(?<!\uff0c)\n\s*(?=(?:deploy|install|build|implement|fix|review|research)\b)|"
    r",\s*and\s+(?=deploy\b[^.;!?\n]{0,120}\b(?:remote|production)\b)|"
    r",\s*just\b|\b(?:but|however|instead)\b|\u4f46\u662f|\u4f46|\u7136\u800c|"
    r"[,\uff0c]\s*(?:(?:must|please|then)\b|\u53ea|\u4ec5|\u5fc5\u987b|\u9700\u8981|\u8bf7|"
    r"deploy\b(?=[^.;!?]{0,120}\b(?:remote|production)\b)))",
    re.IGNORECASE | re.ASCII | re.DOTALL,
)


_PROHIBITION_V3 = re.compile(
    r"(?:(?<![a-z0-9_])(?:no|never|without|cannot|can['\u2019]t|do\s+not|will\s+not|"
    r"don['\u2019]t|must\s+not|should\s+not)\b"
    r"(?!\s+(?:forget|fail|not|only)\b)|(?:不要|无需|不需要|不用|不必|禁止|不得|不允许|切勿|"
    r"不能|不可|(?<!不能)(?<!不得)(?<!不要)(?<!不可)(?<!不需要)(?<!不用)(?<!不必)(?<!无需)"
    r"不(?:修改|改动|更改|启动|运行|执行|部署|安装|增加|新增|扩大|调整))"
    r"(?!不|忘记|忘了|只|仅))"
    r".*?(?=$|[;!?。；！？]|\.(?=\s|$)|\n\s*\n|"
    r"(?<!,)(?<!，)\n\s*(?=(?:deploy|install|build|implement|fix|review|research)\b)|"
    r",\s*and\s+(?=deploy\b[^.;!?\n]{0,120}\b(?:remote|production)\b)|"
    r",\s*just\b|\b(?:but|however|instead)\b|但是|但|然而|而是|"
    r"[,，]\s*(?:(?:must|please|then)\b|只|仅|必须|需要|请|"
    r"deploy\b(?=[^.;!?]{0,120}\b(?:remote|production)\b)))",
    re.IGNORECASE | re.ASCII | re.DOTALL,
)

_DEPLOY_ACTION = re.compile(
    r"(?<![a-z0-9_])deploy(?:s|ed|ing)?\b|\bdeployment\s+(?:to|on|of)\b|\u90e8\u7f72",
    re.IGNORECASE | re.ASCII,
)
_APPLICATION_INSTALL = re.compile(
    r"\binstall(?:ing)?\s+(?:(?:the|an?|our|this|existing)\s+)?"
    r"(?:application|app|service|system|server|daemon)\b"
    r"(?!['\u2019]s\b|\s+(?:(?:development|production|dev|runtime|optional)\s+)?(?:dependencies|packages)\b)|"
    r"\u5b89\u88c5(?:\u5e94\u7528|\u8f6f\u4ef6|\u7cfb\u7edf|\u670d\u52a1|\u8fd0\u884c\u73af\u5883|\u5de5\u5177\u94fe)"
    r"(?!\u7684?(?:\u5f00\u53d1|\u751f\u4ea7|\u8fd0\u884c\u65f6|\u53ef\u9009)?(?:\u4f9d\u8d56|\u5305))",
    re.IGNORECASE | re.ASCII,
)
_HOST_INSTALL = re.compile(
    r"\binstall(?:ing)?\b[^.;!?\n]{0,160}\b(?:on|onto|to)\s+"
    r"(?:(?:the|a|remote|production|linux|windows)\s+){0,4}(?:server|host|machine)\b",
    re.IGNORECASE | re.ASCII,
)


def _version(value: object) -> int:
    if type(value) is not int or value not in (0, 1, 2, 3):
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
    prohibition = (_PROHIBITION_V3 if version == 3 else
        _PROHIBITION_V2 if version == 2 else _PROHIBITION)
    return prohibition.sub(" ", "".join(parts))


def deployment_intent(view: str) -> bool:
    """V2 planning hint only; dependency installers and server nouns are not actions."""
    if _DEPLOY_ACTION.search(view) is not None or _HOST_INSTALL.search(view) is not None:
        return True
    for match in _APPLICATION_INSTALL.finditer(view):
        prefix = view[:match.start()].rstrip().rsplit(None, 1)
        if prefix and prefix[-1].casefold() in {'npm', 'pnpm', 'pip', 'pip3', 'yarn'}:
            continue
        return True
    return False
