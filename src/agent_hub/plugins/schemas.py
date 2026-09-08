from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast

from jsonschema import SchemaError  # type: ignore[import-untyped]
from jsonschema.protocols import Validator  # type: ignore[import-untyped]
from jsonschema.validators import validator_for  # type: ignore[import-untyped]

from agent_hub.runtime.contracts import JsonValue, _mutable_json


class PluginSchemaError(ValueError):
    """Raised when plugin-provided JSON Schema is unsafe or invalid."""


def plugin_schema_validator(
    *,
    schema: Mapping[str, JsonValue] | None,
    invalid_schema_message: str,
    require_object_schema: bool = False,
) -> Validator | None:
    if schema is None:
        return None
    schema_payload = cast(Any, _mutable_json(cast(JsonValue, schema)))
    if require_object_schema:
        schema_type = schema_payload.get("type")
        if schema_type is not None and schema_type != "object":
            raise PluginSchemaError(invalid_schema_message)
    if schema_contains_reference(schema_payload):
        raise PluginSchemaError(invalid_schema_message)
    validator_class: type[Validator] | None = None
    try:
        validator_class = validator_for(schema_payload)
        validator_class.check_schema(schema_payload)
    except SchemaError:
        raise PluginSchemaError(invalid_schema_message) from None
    if validator_class is None:
        raise PluginSchemaError(invalid_schema_message)
    return validator_class(schema_payload)


def schema_contains_reference(value: object) -> bool:
    if isinstance(value, Mapping):
        if _SCHEMA_REFERENCE_KEYWORDS.intersection(value):
            return True
        return any(schema_contains_reference(item) for item in value.values())
    if isinstance(value, list):
        return any(schema_contains_reference(item) for item in value)
    return False


_SCHEMA_REFERENCE_KEYWORDS = frozenset(("$ref", "$dynamicRef", "$recursiveRef"))
