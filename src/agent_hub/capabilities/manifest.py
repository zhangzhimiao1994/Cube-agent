from __future__ import annotations

import re
from collections.abc import Mapping
from typing import cast

from jsonschema import (  # type: ignore[import-untyped]
    Draft3Validator,
    Draft4Validator,
    Draft6Validator,
    Draft7Validator,
    Draft201909Validator,
    Draft202012Validator,
)
from jsonschema.protocols import Validator  # type: ignore[import-untyped]
from jsonschema.validators import validator_for  # type: ignore[import-untyped]

from agent_hub.runtime.contracts import JsonValue

_SAFE_MANIFEST_NAME = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,127}$")
_SAFE_SCHEMA_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")
_SAFE_SCHEMA_STRING = re.compile(r"^[A-Za-z0-9_.:/ -]{0,256}$")
_SCHEMA_TYPE_VALUES = frozenset(
    {"array", "boolean", "integer", "number", "object", "string", "null"}
)
_SCHEMA_STRING_KEYS = frozenset({"format", "pattern"})
_SCHEMA_NUMBER_KEYS = frozenset(
    {
        "exclusiveMaximum",
        "exclusiveMinimum",
        "maxItems",
        "maxLength",
        "maximum",
        "minItems",
        "minLength",
        "minimum",
        "multipleOf",
    }
)
_MAX_SCHEMA_DEPTH = 8
_MAX_SCHEMA_PROPERTIES = 64
_MAX_SCHEMA_REQUIRED = 64
_MAX_SCHEMA_ENUM = 64
_SCHEMA_DIALECTS: Mapping[str, type[Validator]] = {
    "http://json-schema.org/draft-03/schema": Draft3Validator,
    "http://json-schema.org/draft-04/schema": Draft4Validator,
    "http://json-schema.org/draft-06/schema": Draft6Validator,
    "http://json-schema.org/draft-07/schema": Draft7Validator,
    "https://json-schema.org/draft/2019-09/schema": Draft201909Validator,
    "https://json-schema.org/draft/2020-12/schema": Draft202012Validator,
}


def project_capability_manifest_item(
    raw_item: Mapping[str, JsonValue],
    seen_ids: set[str],
    seen_names: set[str],
) -> Mapping[str, JsonValue] | None:
    item_id = raw_item.get("id")
    aliases = _manifest_aliases(raw_item.get("aliases"))
    available = raw_item.get("available")
    availability_reason = raw_item.get("availability_reason")
    if (
        not is_safe_manifest_name(item_id)
        or item_id in seen_ids
        or aliases is None
        or (availability_reason is not None and not isinstance(availability_reason, str))
    ):
        return None
    if item_id in seen_names or any(alias in seen_names for alias in aliases):
        return None
    item_id = cast(str, item_id)
    item: dict[str, JsonValue] = {
        "id": item_id,
        "kind": _string_or_default(raw_item.get("kind"), "plugin"),
        "adapter": _string_or_default(raw_item.get("adapter"), "tool_registry"),
        "permission_class": _string_or_default(
            raw_item.get("permission_class"),
            "tool.use",
        ),
        "sandbox_profile": _string_or_default(
            raw_item.get("sandbox_profile"),
            "unspecified",
        ),
        "policy_effect": _policy_effect(raw_item.get("policy_effect")),
        "available": available if isinstance(available, bool) else True,
        "availability_reason": availability_reason,
        "replay_safe": raw_item.get("replay_safe") is True,
        "aliases": aliases,
    }
    failure_codes = _manifest_failure_codes(raw_item.get("failure_codes"))
    plugin_id = raw_item.get("plugin_id")
    if item["kind"] == "plugin" and is_safe_manifest_name(plugin_id):
        item["plugin_id"] = plugin_id
    if failure_codes:
        item["failure_codes"] = failure_codes
    input_schema = raw_item.get("input_schema")
    if isinstance(input_schema, Mapping):
        projected = _project_schema(input_schema)
        if projected is not None:
            item["input_schema"] = projected
    output_schema = raw_item.get("output_schema")
    if isinstance(output_schema, Mapping):
        projected = _project_schema(output_schema)
        if projected is not None:
            item["output_schema"] = projected
    return item


def is_safe_manifest_name(value: object) -> bool:
    return isinstance(value, str) and _SAFE_MANIFEST_NAME.fullmatch(value) is not None


def _manifest_aliases(value: object) -> tuple[str, ...] | None:
    if not isinstance(value, tuple | list):
        return ()
    aliases = tuple(value)
    if not all(is_safe_manifest_name(alias) for alias in aliases):
        return None
    if len(set(aliases)) != len(aliases):
        return None
    return cast(tuple[str, ...], aliases)


def _manifest_failure_codes(value: object) -> tuple[str, ...]:
    if not isinstance(value, tuple | list):
        return ()
    codes: list[str] = []
    for item in value:
        if len(codes) >= 32:
            break
        if not is_safe_manifest_name(item):
            continue
        lowered = item.lower()
        if "token" in lowered or "secret" in lowered:
            continue
        codes.append(cast(str, item))
    return tuple(dict.fromkeys(codes))


def _string_or_default(value: object, default: str) -> str:
    if not isinstance(value, str) or not value.strip():
        return default
    return value


def _policy_effect(value: object) -> str:
    if value in {"inherit", "allow", "require_approval", "deny"}:
        return value
    return "inherit"


def _project_schema(schema: Mapping[str, JsonValue]) -> Mapping[str, JsonValue] | None:
    # Public hints may weaken the source contract; plugin execution still validates the original.
    return _project_schema_at_depth(schema, depth=0)


def _project_schema_at_depth(
    schema: Mapping[str, JsonValue],
    *,
    depth: int,
    dialect: type[Validator] = Draft202012Validator,
) -> Mapping[str, JsonValue] | None:
    if depth > _MAX_SCHEMA_DEPTH:
        return None
    if "$schema" in schema:
        dialect_uri = schema["$schema"]
        if not isinstance(dialect_uri, str):
            return None
        expected = _SCHEMA_DIALECTS.get(dialect_uri.removesuffix("#"))
        if expected is None:
            return None
        selected: type[Validator] | None = validator_for(schema, default=None)
        if selected is not expected:
            return None
        dialect = expected
    # Older dialects ignore $ref siblings. Never turn those siblings into new constraints.
    if {"$ref", "$dynamicRef", "$recursiveRef"}.intersection(schema):
        return None
    projected: dict[str, JsonValue] = {}
    if "$schema" in schema:
        projected["$schema"] = schema["$schema"]
    schema_type = _schema_type(schema.get("type"))
    if schema_type is not None:
        projected["type"] = schema_type
    raw_properties = schema.get("properties", {})
    properties = _schema_properties(raw_properties, depth=depth, dialect=dialect)
    if properties:
        projected["properties"] = properties
    items = schema.get("items")
    # Omitting positional schemas must not make a tail-only rule apply to every array item.
    if isinstance(items, Mapping) and not schema.get("prefixItems"):
        projected_items = _project_schema_at_depth(items, depth=depth + 1, dialect=dialect)
        if projected_items is not None:
            projected["items"] = projected_items
    required = _schema_string_tuple(schema.get("required"), max_items=_MAX_SCHEMA_REQUIRED)
    if required:
        projected["required"] = required
    enum = _schema_enum(schema.get("enum"))
    if enum:
        projected["enum"] = enum
    const = _schema_const(schema.get("const"))
    if const is not None:
        projected["const"] = const
    additional_properties = schema.get("additionalProperties")
    # Dropped declared/pattern keys must not be reclassified as restricted additional keys.
    if (
        isinstance(raw_properties, Mapping)
        and len(properties or {}) == len(raw_properties)
        and not schema.get("patternProperties")
    ):
        if isinstance(additional_properties, bool):
            projected["additionalProperties"] = additional_properties
        elif isinstance(additional_properties, Mapping):
            projected_additional = _project_schema_at_depth(
                additional_properties, depth=depth + 1, dialect=dialect,
            )
            if projected_additional is not None:
                projected["additionalProperties"] = projected_additional
    for key in _SCHEMA_STRING_KEYS:
        value = _safe_schema_string(schema.get(key))
        if value is not None:
            projected[key] = value
    for key in _SCHEMA_NUMBER_KEYS:
        number_value = schema.get(key)
        if isinstance(number_value, int | float) and not isinstance(number_value, bool):
            projected[key] = number_value
    # A keyword ignored by the source dialect must not gain force in the default dialect.
    return {
        key: value for key, value in projected.items()
        if key == "$schema" or key in dialect.VALIDATORS
    }


def _schema_type(value: JsonValue | None) -> str | tuple[str, ...] | None:
    if isinstance(value, str):
        return value if value in _SCHEMA_TYPE_VALUES else None
    if isinstance(value, tuple | list):
        result = tuple(item for item in value if isinstance(item, str) and item in _SCHEMA_TYPE_VALUES)
        if result and len(result) == len(value) and len(result) == len(set(result)):
            return result
    return None


def _schema_properties(
    value: JsonValue | None, *, depth: int, dialect: type[Validator],
) -> Mapping[str, JsonValue] | None:
    if not isinstance(value, Mapping):
        return None
    projected: dict[str, JsonValue] = {}
    for raw_name, raw_schema in value.items():
        if len(projected) >= _MAX_SCHEMA_PROPERTIES:
            break
        if not isinstance(raw_name, str) or _SAFE_SCHEMA_NAME.fullmatch(raw_name) is None:
            continue
        if not isinstance(raw_schema, Mapping):
            continue
        nested = _project_schema_at_depth(raw_schema, depth=depth + 1, dialect=dialect)
        if nested is not None:
            projected[raw_name] = nested
    return projected or None


def _schema_string_tuple(value: JsonValue | None, *, max_items: int) -> tuple[str, ...]:
    if not isinstance(value, tuple | list):
        return ()
    result: list[str] = []
    for item in value:
        if len(result) >= max_items:
            break
        safe = _safe_schema_string(item)
        if safe is not None:
            result.append(safe)
    return tuple(dict.fromkeys(result))


def _schema_enum(value: JsonValue | None) -> tuple[JsonValue, ...]:
    if not isinstance(value, tuple | list) or len(value) > _MAX_SCHEMA_ENUM:
        return ()
    result: list[JsonValue] = []
    for item in value:
        safe = _schema_const(item)
        if safe is None:
            return ()
        result.append(safe)
    return tuple(result)


def _schema_const(value: JsonValue | None) -> JsonValue | None:
    if value is None or isinstance(value, bool | int | float):
        return value
    return _safe_schema_string(value)


def _safe_schema_string(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    if _SAFE_SCHEMA_STRING.fullmatch(value) is None:
        return None
    lowered = value.lower()
    if "secret://" in lowered or "sk-" in lowered or "token" in lowered:
        return None
    return value


__all__ = ["is_safe_manifest_name", "project_capability_manifest_item"]
