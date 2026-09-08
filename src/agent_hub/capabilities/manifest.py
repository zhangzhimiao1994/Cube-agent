from __future__ import annotations

import re
from collections.abc import Mapping
from typing import cast

from agent_hub.runtime.contracts import JsonValue

_SAFE_MANIFEST_NAME = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,127}$")


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
        item["input_schema"] = cast(JsonValue, input_schema)
    output_schema = raw_item.get("output_schema")
    if isinstance(output_schema, Mapping):
        item["output_schema"] = cast(JsonValue, output_schema)
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


__all__ = ["is_safe_manifest_name", "project_capability_manifest_item"]
