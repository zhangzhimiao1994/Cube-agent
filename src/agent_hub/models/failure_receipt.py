"""Bounded gateway-issued failure evidence, never a model completion."""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from typing import Literal, cast
from uuid import UUID

from agent_hub.models.types import JsonValue, TokenUsage, _require_safe_identifier

MAX_GATEWAY_FAILURE_ATTEMPTS = 64
MAX_GATEWAY_FAILURE_TOKENS = 1_000_000_000
_ISSUED_RECEIPT = object()
_RECEIPT_ATTRIBUTE = "_gateway_failure_receipt"
_RECEIPT_KEYS = {
    "schema_version", "source", "call_id", "requested_logical_model", "allow_fallback",
    "disposition", "history_complete", "attempted_logical_models", "attempts",
}
_ATTEMPT_KEYS = {
    "ordinal", "provenance", "transport_state", "outcome", "status_code", "usage_status", "usage",
}
_PROVENANCE_KEYS = {"logical_model", "deployment_id", "provider_id", "provider_model"}
_USAGE_KEYS = {"prompt_tokens", "completion_tokens", "total_tokens"}


def _exact_object(value: object, keys: set[str]) -> dict[str, object]:
    if (
        type(value) is not dict or len(value) != len(keys)
        or any(type(key) is not str for key in value) or set(value) != keys
    ):
        raise ValueError("gateway failure object is invalid")
    return cast(dict[str, object], value)


def _identifier(value: object) -> None:
    if type(value) is not str:
        raise ValueError("gateway failure scope is invalid")
    _require_safe_identifier("gateway failure scope", value)


def _valid_usage(usage: object) -> bool:
    if type(usage) is not TokenUsage:
        return False
    counts = (usage.prompt_tokens, usage.completion_tokens, usage.total_tokens)
    return (
        all(type(count) is int and 0 <= count <= MAX_GATEWAY_FAILURE_TOKENS for count in counts)
        and counts[0] + counts[1] == counts[2]
    )


@dataclass(frozen=True, slots=True)
class GatewayFailureAttempt:
    ordinal: int
    logical_model: str
    deployment_id: str
    provider_id: str
    provider_model: str = field(repr=False)
    outcome: Literal["empty_response", "transport_error"]
    status_code: int | None = None
    usage_status: Literal["known", "missing", "invalid"] = "missing"
    usage: TokenUsage | None = None

    def __post_init__(self) -> None:
        self._validate()

    def _validate(self) -> None:
        if type(self.ordinal) is not int or not 1 <= self.ordinal <= MAX_GATEWAY_FAILURE_ATTEMPTS:
            raise ValueError("gateway failure ordinal is invalid")
        for value in (self.logical_model, self.deployment_id, self.provider_id):
            _identifier(value)
        if (
            type(self.provider_model) is not str
            or re.fullmatch(r"[A-Za-z0-9_./:-]{1,512}", self.provider_model) is None
            or "://" in self.provider_model
            or self.provider_model.split("/", 1)[0] != self.provider_id
            or "/" not in self.provider_model
            or not self.provider_model.split("/", 1)[1]
        ):
            raise ValueError("gateway failure provider scope is invalid")
        if type(self.outcome) is not str or self.outcome not in {"empty_response", "transport_error"}:
            raise ValueError("gateway failure outcome is invalid")
        if self.status_code is not None and (
            type(self.status_code) is not int or not 100 <= self.status_code <= 599
        ):
            raise ValueError("gateway failure status is invalid")
        if self.outcome == "empty_response" and self.status_code != 200:
            raise ValueError("gateway empty response status is invalid")
        if type(self.usage_status) is not str or self.usage_status not in {"known", "missing", "invalid"}:
            raise ValueError("gateway failure usage status is invalid")
        if self.usage_status == "known":
            if not _valid_usage(self.usage):
                raise ValueError("gateway failure usage is invalid")
        elif self.usage is not None:
            raise ValueError("gateway failure unknown usage must be absent")
        if self.outcome == "transport_error" and (self.usage_status != "missing" or self.usage is not None):
            raise ValueError("gateway transport failure cannot contain response usage")

    def to_payload(self) -> dict[str, JsonValue]:
        self._validate()
        return {
            "ordinal": self.ordinal,
            "provenance": {
                "logical_model": self.logical_model,
                "deployment_id": self.deployment_id,
                "provider_id": self.provider_id,
                "provider_model": self.provider_model,
            },
            "transport_state": "entered",
            "outcome": self.outcome,
            "status_code": self.status_code,
            "usage_status": self.usage_status,
            "usage": None if self.usage is None else {
                "prompt_tokens": self.usage.prompt_tokens,
                "completion_tokens": self.usage.completion_tokens,
                "total_tokens": self.usage.total_tokens,
            },
        }


@dataclass(frozen=True, slots=True)
class GatewayFailureReceipt:
    call_id: str
    requested_logical_model: str
    allow_fallback: bool
    history_complete: bool
    attempted_logical_models: tuple[str, ...]
    attempts: tuple[GatewayFailureAttempt, ...]

    def __post_init__(self) -> None:
        self._validate()

    def _validate(self) -> None:
        if type(self.call_id) is not str:
            raise ValueError("gateway failure call identity is invalid")
        try:
            canonical = str(UUID(self.call_id))
        except (ValueError, AttributeError):
            raise ValueError("gateway failure call identity is invalid") from None
        if canonical != self.call_id:
            raise ValueError("gateway failure call identity is invalid")
        _identifier(self.requested_logical_model)
        if type(self.allow_fallback) is not bool or type(self.history_complete) is not bool:
            raise ValueError("gateway failure flags are invalid")
        if (
            type(self.attempted_logical_models) is not tuple
            or not 1 <= len(self.attempted_logical_models) <= MAX_GATEWAY_FAILURE_ATTEMPTS
            or type(self.attempts) is not tuple
            or not 1 <= len(self.attempts) <= MAX_GATEWAY_FAILURE_ATTEMPTS
        ):
            raise ValueError("gateway failure history is invalid")
        for model in self.attempted_logical_models:
            _identifier(model)
        if (
            self.attempted_logical_models[0] != self.requested_logical_model
            or len(set(self.attempted_logical_models)) != len(self.attempted_logical_models)
            or (not self.allow_fallback and self.attempted_logical_models != (self.requested_logical_model,))
        ):
            raise ValueError("gateway failure request history is inconsistent")
        previous = 0
        for attempt in self.attempts:
            if type(attempt) is not GatewayFailureAttempt:
                raise ValueError("gateway failure attempt is invalid")
            attempt._validate()
            if (
                attempt.ordinal <= previous
                or attempt.logical_model not in self.attempted_logical_models
                or (self.history_complete and attempt.ordinal != previous + 1)
                or (self.history_complete and (
                    attempt.usage_status == "invalid"
                    or (attempt.outcome == "transport_error" and attempt.status_code is None)
                ))
            ):
                raise ValueError("gateway failure attempt history is inconsistent")
            previous = attempt.ordinal

    def to_payload(self) -> dict[str, JsonValue]:
        self._validate()
        return {
            "schema_version": 1,
            "source": "model_gateway",
            "call_id": self.call_id,
            "requested_logical_model": self.requested_logical_model,
            "allow_fallback": self.allow_fallback,
            "disposition": "failed",
            "history_complete": self.history_complete,
            "attempted_logical_models": cast(JsonValue, list(self.attempted_logical_models)),
            "attempts": cast(JsonValue, [attempt.to_payload() for attempt in self.attempts]),
        }

    @classmethod
    def from_payload(cls, value: object) -> GatewayFailureReceipt:
        """Parse only the exact public JSON schema, never arbitrary object protocols."""
        try:
            raw = _exact_object(value, _RECEIPT_KEYS)
            if (
                type(raw["schema_version"]) is not int or raw["schema_version"] != 1
                or type(raw["source"]) is not str or raw["source"] != "model_gateway"
                or type(raw["disposition"]) is not str or raw["disposition"] != "failed"
            ):
                raise ValueError
            models, items = raw["attempted_logical_models"], raw["attempts"]
            if (
                type(models) is not list or not 1 <= len(models) <= MAX_GATEWAY_FAILURE_ATTEMPTS
                or type(items) is not list or not 1 <= len(items) <= MAX_GATEWAY_FAILURE_ATTEMPTS
            ):
                raise ValueError
            attempts = []
            for item in items:
                attempt = _exact_object(item, _ATTEMPT_KEYS)
                provenance = _exact_object(attempt["provenance"], _PROVENANCE_KEYS)
                if type(attempt["transport_state"]) is not str or attempt["transport_state"] != "entered":
                    raise ValueError
                usage = None
                if attempt["usage"] is not None:
                    counts = _exact_object(attempt["usage"], _USAGE_KEYS)
                    if not all(type(count) is int for count in counts.values()):
                        raise ValueError
                    usage = TokenUsage(
                        cast(int, counts["prompt_tokens"]), cast(int, counts["completion_tokens"]),
                        cast(int, counts["total_tokens"]),
                    )
                attempts.append(GatewayFailureAttempt(
                    ordinal=cast(int, attempt["ordinal"]),
                    logical_model=cast(str, provenance["logical_model"]),
                    deployment_id=cast(str, provenance["deployment_id"]),
                    provider_id=cast(str, provenance["provider_id"]),
                    provider_model=cast(str, provenance["provider_model"]),
                    outcome=cast(Literal["empty_response", "transport_error"], attempt["outcome"]),
                    status_code=cast(int | None, attempt["status_code"]),
                    usage_status=cast(Literal["known", "missing", "invalid"], attempt["usage_status"]),
                    usage=usage,
                ))
            return cls(
                call_id=cast(str, raw["call_id"]),
                requested_logical_model=cast(str, raw["requested_logical_model"]),
                allow_fallback=cast(bool, raw["allow_fallback"]),
                history_complete=cast(bool, raw["history_complete"]),
                attempted_logical_models=tuple(models), attempts=tuple(attempts),
            )
        except BaseException:  # noqa: BLE001 - do not disclose malformed evidence or hostile input.
            raise ValueError("gateway failure receipt payload is invalid") from None


def _attach_gateway_failure_receipt(error: BaseException, receipt: GatewayFailureReceipt) -> None:
    try:
        receipt._validate()
        namespace = object.__getattribute__(error, "__dict__")
        if type(namespace) is dict:
            namespace[_RECEIPT_ATTRIBUTE] = (_ISSUED_RECEIPT, receipt)
    except BaseException:  # noqa: BLE001 - evidence cannot replace a primary failure.
        return


def get_gateway_failure_receipt(error: object) -> GatewayFailureReceipt | None:
    """Read only gateway-issued evidence without invoking exception properties."""
    try:
        if not isinstance(error, BaseException) or isinstance(error, asyncio.CancelledError):
            return None
        namespace = object.__getattribute__(error, "__dict__")
        if type(namespace) is not dict:
            return None
        binding = namespace.get(_RECEIPT_ATTRIBUTE)
        if type(binding) is not tuple or len(binding) != 2 or binding[0] is not _ISSUED_RECEIPT:
            return None
        receipt = binding[1]
        if type(receipt) is not GatewayFailureReceipt:
            return None
        receipt._validate()
        return receipt
    except BaseException:  # noqa: BLE001 - hostile exception access must fail closed.
        return None
