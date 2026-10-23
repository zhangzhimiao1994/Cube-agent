"""Pure Go CLI business expectations, without IO, execution or authority.

Limits are allocated by the caller. Stream and argv budgets sum UTF-8 bytes
across the entire suite, including stdin and both expected output streams.
JSON budgets cover canonical ASCII-escaped JSON and, when parsing, raw UTF-8.
Case order and every literal argument/stream byte participate in the digest.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Self, cast

_ERROR = "go_business_invalid"
_CASE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")
_CRITERION_ID = re.compile(r"AC-(?:[0-9]{3}|[1-9][0-9]{3,})")
_CASE_KEYS = {"id", "criterion_id", "argv", "stdin", "expected_stdout", "expected_stderr",
              "expected_exit"}
_JSON = json.JSONEncoder(sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def _need(value: object) -> None:
    if not value:
        raise ValueError(_ERROR)


def _text(value: object) -> str:
    _need(type(value) is str)
    text = cast(str, value)
    try:
        text.encode("utf-8")
    except UnicodeError:
        raise ValueError(_ERROR) from None
    return text


def _object(value: object, keys: set[str]) -> dict[str, object]:
    _need(type(value) is dict)
    item = cast(dict[str, object], value)
    _need(set(item) == keys)
    return item


def _array(value: object) -> tuple[object, ...]:
    _need(type(value) is list)
    return tuple(cast(list[object], value))


def _unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        _need(key not in result)
        result[key] = value
    return result


def _nonfinite(value: str) -> object:
    raise ValueError(_ERROR)


def _remaining_utf8(remaining: int, text: str) -> int:
    _need(len(text) <= remaining)
    remaining -= len(text.encode("utf-8"))
    _need(remaining >= 0)
    return remaining


@dataclass(frozen=True, slots=True)
class GoBusinessLimits:
    """Positive integer budgets, with no implicit case or byte allocation."""

    max_cases: int
    max_json_bytes: int
    max_stream_bytes: int
    max_argv_bytes: int

    def __post_init__(self) -> None:
        _need(all(type(value) is int and value > 0 for value in (
            self.max_cases, self.max_json_bytes, self.max_stream_bytes, self.max_argv_bytes,
        )))


@dataclass(frozen=True, slots=True)
class GoBusinessCase:
    id: str
    criterion_id: str
    argv: tuple[str, ...]
    stdin: str
    expected_stdout: str
    expected_stderr: str
    expected_exit: int

    def __post_init__(self) -> None:
        _need(_CASE_ID.fullmatch(_text(self.id)) is not None)
        _need(_CRITERION_ID.fullmatch(_text(self.criterion_id)) is not None
              and self.criterion_id != "AC-000")
        _need(type(self.argv) is tuple)
        for argument in self.argv:
            _need("\x00" not in _text(argument))
        for stream in (self.stdin, self.expected_stdout, self.expected_stderr):
            _text(stream)
        _need(type(self.expected_exit) is int and 0 <= self.expected_exit <= 255)

    def to_payload(self) -> dict[str, object]:
        return {
            "id": self.id, "criterion_id": self.criterion_id, "argv": list(self.argv),
            "stdin": self.stdin, "expected_stdout": self.expected_stdout,
            "expected_stderr": self.expected_stderr, "expected_exit": self.expected_exit,
        }

    @classmethod
    def from_payload(cls, value: object) -> Self:
        item = _object(value, _CASE_KEYS)
        return cls(
            id=cast(str, item["id"]), criterion_id=cast(str, item["criterion_id"]),
            argv=cast(tuple[str, ...], _array(item["argv"])), stdin=cast(str, item["stdin"]),
            expected_stdout=cast(str, item["expected_stdout"]),
            expected_stderr=cast(str, item["expected_stderr"]),
            expected_exit=cast(int, item["expected_exit"]),
        )


@dataclass(frozen=True, slots=True)
class GoBusinessSuite:
    cases: tuple[GoBusinessCase, ...]
    schema_version: int = 1
    kind: str = "go_cli_business_v1"

    def __post_init__(self) -> None:
        _need(type(self.schema_version) is int and self.schema_version == 1
              and type(self.kind) is str and self.kind == "go_cli_business_v1"
              and type(self.cases) is tuple)
        _need(all(type(case) is GoBusinessCase for case in self.cases))
        _need(len({case.id for case in self.cases}) == len(self.cases)
              and any(case.expected_exit == 0 for case in self.cases)
              and any(case.expected_exit != 0 for case in self.cases))

    def to_payload(self) -> dict[str, object]:
        return {"schema_version": self.schema_version, "kind": self.kind,
                "cases": [case.to_payload() for case in self.cases]}

    @property
    def sha256(self) -> str:
        digest = hashlib.sha256()
        for chunk in _JSON.iterencode(self.to_payload()):
            digest.update(chunk.encode("ascii"))
        return digest.hexdigest()

    @property
    def criterion_ids(self) -> tuple[str, ...]:
        return tuple(sorted({case.criterion_id for case in self.cases}))

    def require_limits(self, limits: GoBusinessLimits) -> None:
        """Reject an over-budget suite without truncating any expectation."""
        _need(type(limits) is GoBusinessLimits)
        _need(len(self.cases) <= limits.max_cases)
        stream_remaining, argv_remaining = limits.max_stream_bytes, limits.max_argv_bytes
        for case in self.cases:
            for stream in (case.stdin, case.expected_stdout, case.expected_stderr):
                stream_remaining = _remaining_utf8(stream_remaining, stream)
            for argument in case.argv:
                argv_remaining = _remaining_utf8(argv_remaining, argument)
        json_remaining = limits.max_json_bytes
        for chunk in _JSON.iterencode(self.to_payload()):
            # ensure_ascii=True makes each character exactly one serialized byte.
            json_remaining -= len(chunk)
            _need(json_remaining >= 0)

    @classmethod
    def from_payload(cls, value: object, *, limits: GoBusinessLimits | None = None) -> Self:
        """Validate data; callers omitting limits must call require_limits before use."""
        item = _object(value, {"schema_version", "kind", "cases"})
        if limits is not None:
            _need(type(limits) is GoBusinessLimits)
            _need(type(item["cases"]) is list
                  and len(cast(list[object], item["cases"])) <= limits.max_cases)
        result = cls(
            cases=tuple(GoBusinessCase.from_payload(row) for row in _array(item["cases"])),
            schema_version=cast(int, item["schema_version"]), kind=cast(str, item["kind"]),
        )
        if limits is not None:
            result.require_limits(limits)
        return result

    @classmethod
    def from_json(cls, raw: bytes | str, *, limits: GoBusinessLimits) -> Self:
        try:
            _need(type(limits) is GoBusinessLimits)
            _need(type(raw) in {bytes, str} and 0 < len(raw) <= limits.max_json_bytes)
            if type(raw) is bytes:
                text = raw.decode("utf-8")
            else:
                text = cast(str, raw)
                _remaining_utf8(limits.max_json_bytes, text)
            return cls.from_payload(json.loads(text, object_pairs_hook=_unique,
                                               parse_constant=_nonfinite), limits=limits)
        except (ValueError, TypeError, KeyError, OverflowError, RecursionError):
            raise ValueError(_ERROR) from None
