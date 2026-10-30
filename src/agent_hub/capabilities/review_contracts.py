"""Pure, private review wire contracts, without I/O or authorization decisions.

Review is not a sandbox permission grant. Allowed in-bound actions need no
preliminary blanket tool consent. Never replay/reset consumed FAILED/UNKNOWN.
The server context loader owns source/effect completeness and caller trust;
encoding a Role, UUID or model configuration does not establish that trust.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field, fields
from decimal import Decimal, DecimalException
from typing import Literal, cast
from uuid import UUID

from agent_hub.auth.models import Role
from agent_hub.capabilities.types import CapabilityRequest

type ReviewEngine = Literal["rules", "rules_then_model"]
type ReviewDecision = Literal["allow", "deny", "needs_human"]
type ReviewRisk = Literal["low", "medium", "high", "unknown"]

_MAX_INT64 = 2**63 - 1
_ALIAS = re.compile(r"[a-z0-9][a-z0-9_-]{0,127}\Z")
_SHA256 = re.compile(r"[a-f0-9]{64}\Z")
_MONEY = re.compile(r"-?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?\Z")
_REASONS = frozenset(
    {
        "authorized_action",
        "outside_authorized_scope",
        "protected_resource",
        "untrusted_content",
        "unclear_impact",
        "missing_context",
        "policy_requires_human",
    }
)
_CREDENTIAL = re.compile(
    r"(?i)(?:\b(?:api[_ -]?key|access[_ -]?token|refresh[_ -]?token|"
    r"token|(?:client[_ -]?)?secret|password|passwd|authorization)\s*[:=]|\bbearer\s+\S+|"
    r"\bsk-[a-z0-9_-]+|\bAKIA[0-9A-Z]{16}\b|-----BEGIN[^-]*PRIVATE KEY|"
    r"\b(?:github_pat_|gh[pousr]_|xox[baprs]-)[a-z0-9_-]+|"
    r"\beyJ[a-z0-9_-]+\.[a-z0-9_-]+\.[a-z0-9_-]+|"
    r"[a-z][a-z0-9+.-]*://[^\s/:]+:[^\s/@]+@)"
)
_VERDICT_KEYS = frozenset(
    {
        "schema_version",
        "decision",
        "risk",
        "reason_code",
        "rationale",
        "action_digest",
    }
)
_UUID_FIELDS = frozenset({"tenant_id", "user_id", "run_id"})


def _bounded_int(name: str, value: object, minimum: int, maximum: int) -> None:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"invalid review {name}")


def _require_digest(value: object) -> None:
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        raise ValueError("invalid review digest")


def _money(value: object) -> Decimal:
    if type(value) is not str or _MONEY.fullmatch(value) is None:
        raise ValueError("invalid review cost ceiling")
    try:
        amount = Decimal(value)
        exponent = amount.as_tuple().exponent
        if (
            not amount.is_finite()
            or amount < 0
            or amount > 1_000_000
            or not isinstance(exponent, int)
            or exponent < -6
        ):
            raise ValueError("invalid review cost ceiling")
    except DecimalException:
        raise ValueError("invalid review cost ceiling") from None
    return amount


@dataclass(frozen=True, slots=True)
class ApprovalReviewConfig:
    """Storage limits only; effective runtime/model/caller ceilings apply later."""

    schema_version: int = 1
    engine: ReviewEngine = "rules"
    reviewer_logical_model: str | None = None
    require_distinct_model: bool = False
    timeout_seconds: float = 15
    max_input_tokens: int = 4096
    max_output_tokens: int = 512
    max_reviews_per_run: int = 8
    max_transport_attempts_per_review: int = 1
    max_cost_usd_per_run: str = "0.00"
    denial_breaker_consecutive: int = 3
    denial_breaker_denials: int = 10
    denial_breaker_window: int = 50

    def __post_init__(self) -> None:
        _bounded_int("schema version", self.schema_version, 1, 1)
        if type(self.engine) is not str or self.engine not in ("rules", "rules_then_model"):
            raise ValueError("invalid review engine")
        alias = self.reviewer_logical_model
        if alias is not None and (type(alias) is not str or _ALIAS.fullmatch(alias) is None):
            raise ValueError("invalid review model alias")
        if type(self.require_distinct_model) is not bool:
            raise ValueError("invalid review distinct model flag")
        if (
            type(self.timeout_seconds) not in (int, float)
            or not 1 <= self.timeout_seconds <= 60
            or not math.isfinite(self.timeout_seconds)
        ):
            raise ValueError("invalid review timeout")
        for name, value in (
            ("input token limit", self.max_input_tokens),
            ("output token limit", self.max_output_tokens),
        ):
            _bounded_int(name, value, 1, 1_000_000)
        for name, value in (
            ("review count", self.max_reviews_per_run),
            ("breaker consecutive", self.denial_breaker_consecutive),
            ("breaker denials", self.denial_breaker_denials),
            ("breaker window", self.denial_breaker_window),
        ):
            _bounded_int(name, value, 1, _MAX_INT64)
        _bounded_int("transport attempts", self.max_transport_attempts_per_review, 1, 1)
        if not (
            self.denial_breaker_consecutive
            <= self.denial_breaker_denials
            <= self.denial_breaker_window
        ):
            raise ValueError("invalid review denial breaker ordering")
        ceiling = _money(self.max_cost_usd_per_run)
        if self.engine == "rules_then_model" and (alias is None or ceiling <= 0):
            raise ValueError("model review requires an alias and positive cost ceiling")

    @classmethod
    def from_payload(cls, payload: object) -> ApprovalReviewConfig:
        defaults = cls().to_payload()
        if payload is None:
            payload = {}
        if not isinstance(payload, Mapping) or any(
            type(key) is not str or key not in defaults for key in payload
        ):
            raise ValueError("invalid review configuration fields")
        values = {**defaults, **payload}
        return cls(
            schema_version=cast(int, values["schema_version"]),
            engine=cast(ReviewEngine, values["engine"]),
            reviewer_logical_model=cast(str | None, values["reviewer_logical_model"]),
            require_distinct_model=cast(bool, values["require_distinct_model"]),
            timeout_seconds=cast(float, values["timeout_seconds"]),
            max_input_tokens=cast(int, values["max_input_tokens"]),
            max_output_tokens=cast(int, values["max_output_tokens"]),
            max_reviews_per_run=cast(int, values["max_reviews_per_run"]),
            max_transport_attempts_per_review=cast(
                int, values["max_transport_attempts_per_review"]
            ),
            max_cost_usd_per_run=cast(str, values["max_cost_usd_per_run"]),
            denial_breaker_consecutive=cast(int, values["denial_breaker_consecutive"]),
            denial_breaker_denials=cast(int, values["denial_breaker_denials"]),
            denial_breaker_window=cast(int, values["denial_breaker_window"]),
        )

    def to_payload(self) -> dict[str, object]:
        return {item.name: getattr(self, item.name) for item in fields(self)}


def _json_copy(value: object, active: set[int]) -> object:
    if value is None or type(value) in (bool, int):
        return value
    if type(value) is str:
        value.encode("utf-8")
        return value
    if type(value) is float and math.isfinite(value):
        return value
    if isinstance(value, Mapping | list | tuple):
        identity = id(value)
        if identity in active:
            raise ValueError("invalid review JSON")
        active.add(identity)
        try:
            if isinstance(value, Mapping):
                result: dict[str, object] = {}
                for key, item in value.items():
                    if type(key) is not str:
                        raise ValueError("invalid review JSON")
                    key.encode("utf-8")
                    result[key] = _json_copy(item, active)
                return result
            return [_json_copy(item, active) for item in value]
        finally:
            active.remove(identity)
    raise ValueError("invalid review JSON")


def _canonical(value: object) -> bytes:
    try:
        return json.dumps(
            _json_copy(value, set()),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (ValueError, TypeError, UnicodeError, RecursionError, OverflowError):
        raise ValueError("invalid review JSON") from None


@dataclass(frozen=True, slots=True)
class FrozenReviewAction:
    """Immutable private bytes, with fresh copies at every mutable boundary."""

    action_digest: str
    effect_digest: str
    _payload_bytes: bytes = field(repr=False)

    def to_payload(self) -> dict[str, object]:
        payload = cast(dict[str, object], json.loads(self._payload_bytes))
        return {**payload, "action_digest": self.action_digest, "effect_digest": self.effect_digest}

    def request(self) -> CapabilityRequest:
        payload = cast(dict[str, object], self.to_payload()["request"])
        for name in _UUID_FIELDS:
            payload[name] = UUID(cast(str, payload[name]))
        return CapabilityRequest.model_validate(payload)


def freeze_review_action(
    request: CapabilityRequest,
    *,
    role: Role,
    authorization_turn_id: UUID,
    authorization_sha256: str,
    policy_sha256: str,
    config_sha256: str,
    source_binding: Mapping[str, object],
    normalized_effect: Mapping[str, object],
) -> FrozenReviewAction:
    """Snapshot synchronously; callers must do this before their first await.

    Source/effect mappings are opaque server-supplied bindings. This codec does
    not infer root fields, eligibility, authentication or effect completeness.
    """
    if (
        type(request) is not CapabilityRequest
        or type(role) is not Role
        or type(authorization_turn_id) is not UUID
        or not isinstance(source_binding, Mapping)
        or not isinstance(normalized_effect, Mapping)
    ):
        raise ValueError("invalid review action context")
    for digest in (authorization_sha256, policy_sha256, config_sha256):
        _require_digest(digest)
    try:
        request_payload: dict[str, object] = {}
        for name in CapabilityRequest.model_fields:
            value = getattr(request, name)
            if name in _UUID_FIELDS:
                if type(value) is not UUID:
                    raise ValueError("invalid review request identity")
                value = str(value)
            request_payload[name] = value
        payload: dict[str, object] = {
            "schema_version": 1,
            "request": request_payload,
            "role": role.value,
            "authorization_turn_id": str(authorization_turn_id),
            "authorization_sha256": authorization_sha256,
            "policy_sha256": policy_sha256,
            "config_sha256": config_sha256,
            "source_binding": source_binding,
            "normalized_effect": normalized_effect,
        }
        # Both digests and request validation use this one isolated snapshot.
        snapshot_bytes = _canonical(payload)
        snapshot = cast(dict[str, object], json.loads(snapshot_bytes))
        frozen_request = cast(dict[str, object], snapshot["request"])
        typed_request = dict(frozen_request)
        for name in _UUID_FIELDS:
            typed_request[name] = UUID(cast(str, typed_request[name]))
        CapabilityRequest.model_validate(typed_request)
        effect_bytes = _canonical(
            {
                "tenant_id": frozen_request["tenant_id"],
                "user_id": frozen_request["user_id"],
                "authorization_turn_id": snapshot["authorization_turn_id"],
                "normalized_effect": snapshot["normalized_effect"],
            }
        )
    except (ValueError, TypeError, UnicodeError, RecursionError, OverflowError):
        raise ValueError("invalid review action JSON") from None
    return FrozenReviewAction(
        action_digest=hashlib.sha256(snapshot_bytes).hexdigest(),
        effect_digest=hashlib.sha256(effect_bytes).hexdigest(),
        _payload_bytes=snapshot_bytes,
    )


@dataclass(frozen=True, slots=True)
class ModelReviewVerdict:
    schema_version: int
    decision: ReviewDecision
    risk: ReviewRisk
    reason_code: str
    rationale: str = field(repr=False)
    action_digest: str

    def __post_init__(self) -> None:
        _bounded_int("verdict schema version", self.schema_version, 1, 1)
        if type(self.decision) is not str or self.decision not in ("allow", "deny", "needs_human"):
            raise ValueError("invalid review decision")
        if type(self.risk) is not str or self.risk not in ("low", "medium", "high", "unknown"):
            raise ValueError("invalid review risk")
        if self.decision == "allow" and self.risk in ("high", "unknown"):
            raise ValueError("invalid review allow risk")
        if type(self.reason_code) is not str or self.reason_code not in _REASONS:
            raise ValueError("invalid review reason code")
        _require_digest(self.action_digest)
        if (
            type(self.rationale) is not str
            or not 1 <= len(self.rationale) <= 240
            or self.rationale != self.rationale.strip()
            or not self.rationale.isprintable()
            or _CREDENTIAL.search(self.rationale) is not None
        ):
            raise ValueError("invalid private review rationale")
        try:
            self.rationale.encode("utf-8")
        except UnicodeError:
            raise ValueError("invalid private review rationale") from None

    def to_payload(self) -> dict[str, object]:
        """Private wire shape; public summaries must use reason_code only."""
        return {item.name: getattr(self, item.name) for item in fields(self)}


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate review response field")
        result[key] = value
    return result


def _reject_constant(value: str) -> object:
    raise ValueError("invalid review response number")


def parse_model_review(text: str, *, expected_action_digest: str) -> ModelReviewVerdict:
    """Strict whole-response JSON decoder; no raw response appears in errors."""
    _require_digest(expected_action_digest)
    if type(text) is not str:
        raise ValueError("invalid review response text")
    try:
        if len(text.encode("utf-8")) > 8192:
            raise ValueError("review response exceeds byte limit")
        payload = json.loads(
            text, object_pairs_hook=_unique_object, parse_constant=_reject_constant
        )
        if type(payload) is not dict or payload.keys() != _VERDICT_KEYS:
            raise ValueError("invalid review response fields")
        if payload["action_digest"] != expected_action_digest:
            raise ValueError("review response digest mismatch")
        return ModelReviewVerdict(
            schema_version=payload["schema_version"],
            decision=payload["decision"],
            risk=payload["risk"],
            reason_code=payload["reason_code"],
            rationale=payload["rationale"],
            action_digest=payload["action_digest"],
        )
    except (ValueError, TypeError, UnicodeError, RecursionError, OverflowError):
        raise ValueError("invalid model review response") from None
