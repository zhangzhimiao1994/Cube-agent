from __future__ import annotations

import math
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

JsonValue = object


class PolicyEffect(StrEnum):
    ALLOW = "allow"
    REQUIRE_APPROVAL = "require_approval"
    DENY = "deny"


class CapabilityRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    tenant_id: UUID
    user_id: UUID
    agent_id: str = Field(min_length=1, max_length=128, pattern=r"^[a-z0-9][a-z0-9_-]*$")
    capability: str = Field(min_length=1, max_length=128, pattern=r"^[a-z][a-z0-9_-]*$")
    operation: str = Field(min_length=1, max_length=128, pattern=r"^[a-z][a-z0-9_-]*$")
    resource: str = Field(min_length=1, max_length=512)
    arguments: dict[str, object] = Field(default_factory=dict)
    idempotency_key: str = Field(min_length=1, max_length=160)
    run_id: UUID

    @field_validator("resource", "idempotency_key")
    @classmethod
    def printable_unpadded(cls, value: str) -> str:
        if value != value.strip() or any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
            raise ValueError("value must be unpadded printable text")
        return value

    @field_validator("arguments", mode="before")
    @classmethod
    def json_arguments(cls, value: object) -> object:
        return _validate_json(value)


def _validate_json(value: object) -> object:
    if value is None or type(value) in {bool, int, str}:
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError("JSON numbers must be finite")
        return value
    if isinstance(value, list):
        return [_validate_json(item) for item in value]
    if isinstance(value, dict):
        result: dict[str, object] = {}
        for key, item in value.items():
            if type(key) is not str:
                raise ValueError("JSON object keys must be strings")
            result[key] = _validate_json(item)
        return result
    raise ValueError("arguments must be JSON-compatible")
