from __future__ import annotations

import re
from collections.abc import Mapping
from typing import cast

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


def _string_or_default(value: object, default: str) -> str:
    if not isinstance(value, str) or not value.strip():
        return default
    return value


def _policy_effect(value: object) -> str:
    if value in {"inherit", "allow", "require_approval", "deny"}:
        return value
    return "inherit"


def _project_schema(schema: Mapping[str, JsonValue]) -> Mapping[str, JsonValue] | None:
    return _project_schema_at_depth(schema, depth=0)


def _project_schema_at_depth(
    schema: Mapping[str, JsonValue],
    *,
    depth: int,
) -> Mapping[str, JsonValue] | None:
    if depth > _MAX_SCHEMA_DEPTH:
        return None
    projected: dict[str, JsonValue] = {}
    schema_type = _schema_type(schema.get("type"))
    if schema_type is not None:
        projected["type"] = schema_type
    properties = _schema_properties(schema.get("properties"), depth=depth)
    if properties:
        projected["properties"] = properties
    items = schema.get("items")
    if isinstance(items, Mapping):
        projected_items = _project_schema_at_depth(items, depth=depth + 1)
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
    if isinstance(additional_properties, bool):
        projected["additionalProperties"] = additional_properties
    elif isinstance(additional_properties, Mapping):
        projected_additional = _project_schema_at_depth(
            additional_properties,
            depth=depth + 1,
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
    return projected or None


def _schema_type(value: JsonValue | None) -> str | tuple[str, ...] | None:
    if isinstance(value, str):
        return value if value in _SCHEMA_TYPE_VALUES else None
    if isinstance(value, tuple | list):
        result = tuple(item for item in value if isinstance(item, str) and item in _SCHEMA_TYPE_VALUES)
        if result and len(result) == len(set(result)):
            return result
    return None


def _schema_properties(value: JsonValue | None, *, depth: int) -> Mapping[str, JsonValue] | None:
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
        nested = _project_schema_at_depth(raw_schema, depth=depth + 1)
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
    if not isinstance(value, tuple | list):
        return ()
    result: list[JsonValue] = []
    for item in value:
        if len(result) >= _MAX_SCHEMA_ENUM:
            break
        safe = _schema_const(item)
        if safe is not None:
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
