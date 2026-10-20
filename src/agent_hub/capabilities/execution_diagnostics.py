from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Mapping
from dataclasses import InitVar, dataclass, field
from io import StringIO

from agent_hub.observability.logging import SENSITIVE_KEY_PATTERN, SENSITIVE_VALUE_PATTERN
from agent_hub.runtime.contracts import JsonValue
from agent_hub.skills.sandbox.base import SkillResult

_OUTPUT_LIMIT_BYTES = 4096
_TRUNCATION_MARKER = "\n[truncated]"
_SAFE_BACKEND = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_PRIVATE_KEY_BLOCK = re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----", re.IGNORECASE)
_CREDENTIALED_URL = re.compile(r"\b[a-z][a-z0-9+.-]*://[^\s/?#@]+@", re.IGNORECASE)
_MULTILINE_VALUE_START = re.compile(r"[:=]\s*(?:[>|][+-]?\d*)?\s*$|\\\s*$")
_ANSI_ESCAPE = re.compile(
    r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)|[@-_])"
)


@dataclass(frozen=True, slots=True)
class _OutputEvidence:
    text: str
    sanitized_bytes: int
    sha256: str
    truncated: bool
    redacted_lines: int

    def metadata(self) -> Mapping[str, JsonValue]:
        return {
            "sanitized_bytes": self.sanitized_bytes,
            "returned_bytes": len(self.text.encode("utf-8")),
            "sha256": self.sha256,
            "truncated": self.truncated,
            "redacted": self.redacted_lines > 0,
            "redacted_lines": self.redacted_lines,
        }


def _detection_view(value: str, *, strip_ansi: bool = True) -> str:
    normalized = unicodedata.normalize("NFKC", value)
    if strip_ansi:
        normalized = normalized.replace("\x9b", "\x1b[").replace("\x9d", "\x1b]")
        normalized = _ANSI_ESCAPE.sub("", normalized.replace("\x9c", "\x1b\\"))
    return "".join(
        character for character in normalized
        if character == "\n" or unicodedata.category(character) not in {"Cc", "Cf"}
    )


def _capture_output(value: str, *, retain_tail: bool = False) -> _OutputEvidence:
    safe_lines: list[str] = []
    redacted_lines = 0
    # Split only at LF so other control characters cannot split a sensitive key before detection.
    lines = list(StringIO(value))
    # Also inspect escape contents: the original text is retained, including any OSC payload.
    detection_lines = [
        (_detection_view(line), _detection_view(line, strip_ansi=False)) for line in lines
    ]
    detection_views = (_detection_view(value), _detection_view(value, strip_ansi=False))
    # Structured or unfinished sensitive values may span lines; do not guess their boundaries.
    redact_stream = (
        any(
            _PRIVATE_KEY_BLOCK.search(view) or _CREDENTIALED_URL.search(view)
            for view in detection_views
        )
        or any(
            (SENSITIVE_KEY_PATTERN.search(line) or SENSITIVE_VALUE_PATTERN.search(line))
            and (
                any(marker in line for marker in ('"', "'", "{", "}", "[", "]"))
                or _MULTILINE_VALUE_START.search(line)
            )
            for views in detection_lines
            for line in views
        )
    )
    # Scan whole lines before clipping so a sensitive suffix cannot leave its prefix exposed.
    for line, views in zip(lines, detection_lines, strict=True):
        if (
            redact_stream
            or any(
                SENSITIVE_KEY_PATTERN.search(view) or SENSITIVE_VALUE_PATTERN.search(view)
                for view in views
            )
        ):
            redacted_lines += 1
        else:
            safe_lines.append(line)
    sanitized = "".join(safe_lines).encode("utf-8", errors="replace")
    truncated = len(sanitized) > _OUTPUT_LIMIT_BYTES
    if truncated and retain_tail:
        marker = _TRUNCATION_MARKER + "\n"
        available = _OUTPUT_LIMIT_BYTES - len(marker.encode("utf-8"))
        head = sanitized[:(available + 1) // 2].decode("utf-8", errors="ignore")
        # Give UTF-8 prefix slack to the suffix so only its boundary can lose
        # up to three bytes. Metadata still hashes the entire sanitized stream.
        tail_limit = available - len(head.encode("utf-8"))
        tail = sanitized[-tail_limit:].decode("utf-8", errors="ignore")
        text = head + marker + tail
    elif truncated:
        prefix_limit = _OUTPUT_LIMIT_BYTES - len(_TRUNCATION_MARKER.encode("utf-8"))
        text = sanitized[:prefix_limit].decode("utf-8", errors="ignore") + _TRUNCATION_MARKER
    else:
        text = sanitized.decode("utf-8")
    return _OutputEvidence(
        text=text,
        sanitized_bytes=len(sanitized),
        sha256=hashlib.sha256(sanitized).hexdigest(),
        truncated=truncated,
        redacted_lines=redacted_lines,
    )


@dataclass(frozen=True, slots=True)
class ExecutionDiagnostic:
    """Bounded, sanitized evidence from a completed sandbox result, never an exception."""

    backend: str
    result: InitVar[SkillResult]
    retain_tail: InitVar[bool] = False
    exit_code: int | None = field(init=False)
    timed_out: bool = field(init=False)
    _stdout: _OutputEvidence = field(init=False, repr=False)
    _stderr: _OutputEvidence = field(init=False, repr=False)

    def __post_init__(self, result: SkillResult, retain_tail: bool) -> None:
        if type(retain_tail) is not bool:
            raise TypeError("execution diagnostic tail selection must be boolean")
        if type(result) is not SkillResult:
            raise TypeError("execution diagnostic requires a SkillResult")
        if result.exit_code is None and not result.timed_out:
            raise ValueError("execution diagnostic requires a completed result")
        backend = self.backend
        if (
            type(backend) is not str
            or _SAFE_BACKEND.fullmatch(backend) is None
            or SENSITIVE_KEY_PATTERN.search(backend)
            or SENSITIVE_VALUE_PATTERN.search(backend)
        ):
            backend = "unknown"
        object.__setattr__(self, "backend", backend)
        object.__setattr__(self, "exit_code", result.exit_code)
        object.__setattr__(self, "timed_out", result.timed_out)
        object.__setattr__(self, "_stdout", _capture_output(result.stdout, retain_tail=retain_tail))
        object.__setattr__(self, "_stderr", _capture_output(result.stderr, retain_tail=retain_tail))

    def to_payload(self) -> Mapping[str, JsonValue]:
        return {
            "schema_version": 1,
            "backend": self.backend,
            "exit_code": self.exit_code,
            "timed_out": self.timed_out,
            "stdout": self._stdout.text,
            "stderr": self._stderr.text,
            "stdout_metadata": self._stdout.metadata(),
            "stderr_metadata": self._stderr.metadata(),
        }
