"""Pydantic settings adapter; Task1 owns all review configuration validation.

No runtime readiness, deployment lookup, budget probe or provider I/O lives here.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    GetJsonSchemaHandler,
    PlainSerializer,
    PlainValidator,
    TypeAdapter,
)
from pydantic.json_schema import JsonSchemaValue
from pydantic_core import CoreSchema

from agent_hub.capabilities.review_contracts import ApprovalReviewConfig


def _review_config_from_payload(value: object) -> ApprovalReviewConfig:
    # Revalidate even typed inputs, and never retain a caller-owned instance.
    if isinstance(value, ApprovalReviewConfig):
        value = value.to_payload()
    return ApprovalReviewConfig.from_payload(value)


class _ReviewConfigJsonSchema:
    @classmethod
    def __get_pydantic_json_schema__(
        cls, schema: CoreSchema, handler: GetJsonSchemaHandler
    ) -> JsonSchemaValue:
        # Let the enclosing generator register the dataclass/engine references.
        result = handler(TypeAdapter(ApprovalReviewConfig).core_schema)
        handler.resolve_ref_schema(result)["additionalProperties"] = False
        return result


# A plain validator runs before Pydantic can coerce bools/numbers or drop extras.
# The dataclass supplies documentation only; it is not a second validation path.
ApprovalReviewSettings = Annotated[
    ApprovalReviewConfig,
    PlainValidator(_review_config_from_payload),
    PlainSerializer(ApprovalReviewConfig.to_payload, return_type=dict[str, object]),
    _ReviewConfigJsonSchema,
]


class ApprovalReviewReadiness(BaseModel):
    """Fail-closed projection until Task6B supplies trusted assembled runtime state."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["not_ready"] = "not_ready"
    reason_code: Literal["rules_only", "runtime_not_ready"]
