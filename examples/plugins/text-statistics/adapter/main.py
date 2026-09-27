from __future__ import annotations

import json
import re
import sys
from typing import Any

_CAPABILITY_ID = "text.statistics"
_MAX_TEXT_LENGTH = 200_000
_WORD_PATTERN = re.compile(r"[A-Za-z0-9]+(?:['-][A-Za-z0-9]+)*|[\u4e00-\u9fff]")
_CHINESE_CHARACTER_PATTERN = re.compile(r"[\u4e00-\u9fff]")


def _statistics(request: Any) -> dict[str, bool | int]:
    if not isinstance(request, dict) or request.get("capability_id") != _CAPABILITY_ID:
        raise ValueError("unsupported capability")
    arguments = request.get("arguments")
    if not isinstance(arguments, dict) or set(arguments) != {"text"}:
        raise ValueError("invalid arguments")
    text = arguments.get("text")
    if not isinstance(text, str) or len(text) > _MAX_TEXT_LENGTH:
        raise ValueError("invalid text")
    return {
        "ok": True,
        "character_count": len(text),
        "non_whitespace_count": sum(not character.isspace() for character in text),
        "line_count": 0 if not text else text.count("\n") + 1,
        "word_count": len(_WORD_PATTERN.findall(text)),
        "chinese_character_count": len(_CHINESE_CHARACTER_PATTERN.findall(text)),
        "utf8_byte_count": len(text.encode("utf-8")),
    }


def main() -> int:
    try:
        request = json.load(sys.stdin)
        result = _statistics(request)
    except (UnicodeError, ValueError, TypeError, json.JSONDecodeError) as error:
        print(str(error), file=sys.stderr)
        return 2
    json.dump(result, sys.stdout, ensure_ascii=False, separators=(",", ":"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
