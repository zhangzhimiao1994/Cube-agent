from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, cast

import pytest
from jsonschema import Draft202012Validator  # type: ignore[import-untyped]
from jsonschema.validators import validator_for  # type: ignore[import-untyped]

from agent_hub.capabilities.manifest import project_capability_manifest_item
from agent_hub.runtime.contracts import JsonValue

DRAFT3 = "http://json-schema.org/draft-03/schema#"
DRAFT4 = "http://json-schema.org/draft-04/schema#"
DRAFT7 = "http://json-schema.org/draft-07/schema#"


def _project(schema: Mapping[str, JsonValue]) -> Mapping[str, JsonValue]:
    item = project_capability_manifest_item(
        {"id": "plugin.inspect", "input_schema": schema}, set(), set(),
    )
    assert item is not None
    return item


def _accepts_original_and_projection(
    schema: Mapping[str, JsonValue], value: JsonValue,
) -> Mapping[str, JsonValue]:
    original = json.loads(json.dumps(schema))
    original_class = validator_for(original)
    original_class.check_schema(original)
    instance = json.loads(json.dumps(value))
    original_class(original).validate(instance)

    item = _project(schema)
    projected = cast(Mapping[str, JsonValue], item.get("input_schema", {}))
    payload = json.loads(json.dumps(projected))
    projected_class = validator_for(payload)
    projected_class.check_schema(payload)
    projected_class(payload).validate(instance)
    return projected


@pytest.mark.parametrize("removed", [
    "\u4e2d\u6587", "secret://credential", "sk-private", "access_token", "x" * 257,
    None, {"private": "value"}, ("private",),
])
def test_filtered_enum_is_dropped_instead_of_narrowed(removed: JsonValue) -> None:
    schema: Mapping[str, JsonValue] = {
        "type": "object",
        "properties": {"text": {"enum": ("English", removed)}},
    }
    projected = _accepts_original_and_projection(schema, {"text": removed})
    text_schema = cast(Mapping[str, JsonValue],
                       cast(Mapping[str, JsonValue], projected["properties"])["text"])
    assert "enum" not in text_schema
    assert "secret://" not in json.dumps(projected)
    assert "sk-private" not in json.dumps(projected)
    assert "access_token" not in json.dumps(projected)


def test_enum_over_budget_drops_the_whole_constraint() -> None:
    projected = _accepts_original_and_projection({"enum": tuple(range(65))}, 64)
    assert "enum" not in projected


@pytest.mark.parametrize("branch", [{"type": "integer"}, "any"])
def test_draft3_type_union_never_retains_only_supported_branches(branch: JsonValue) -> None:
    projected = _accepts_original_and_projection(
        {"$schema": DRAFT3, "type": ("string", branch)}, 7,
    )
    assert "type" not in projected


@pytest.mark.parametrize("additional", [False, {"type": "string"}])
def test_property_budget_does_not_reclassify_declared_keys_as_additional(
    additional: JsonValue,
) -> None:
    properties: dict[str, JsonValue] = {f"p{i}": {"type": "integer"} for i in range(65)}
    projected = _accepts_original_and_projection({
        "type": "object", "properties": properties, "additionalProperties": additional,
    }, {"p64": 7})
    assert "additionalProperties" not in projected
    assert cast(Mapping[str, JsonValue], projected["properties"])["p0"] == {"type": "integer"}


@pytest.mark.parametrize("name", ["\u4e2d\u6587", "secret://credential", "private key"])
@pytest.mark.parametrize("additional", [False, {"type": "string"}])
def test_filtered_property_names_cannot_activate_additional_property_restrictions(
    name: str, additional: JsonValue,
) -> None:
    projected = _accepts_original_and_projection({
        "type": "object",
        "properties": {"text": {"type": "string"}, name: {"type": "integer"}},
        "additionalProperties": additional,
    }, {name: 7})
    assert "additionalProperties" not in projected
    assert cast(Mapping[str, JsonValue], projected["properties"])["text"] == {"type": "string"}
    assert name not in json.dumps(projected, ensure_ascii=False)


@pytest.mark.parametrize("additional", [False, {"type": "string"}])
def test_filtered_subschema_cannot_activate_additional_property_restrictions(
    additional: JsonValue,
) -> None:
    projected = _accepts_original_and_projection({
        "type": "object", "$defs": {"value": {"type": "integer"}},
        "properties": {"text": {"type": "string"}, "value": {"$ref": "#/$defs/value"}},
        "additionalProperties": additional,
    }, {"value": 7})
    assert "additionalProperties" not in projected
    assert "$ref" not in json.dumps(projected)


@pytest.mark.parametrize("additional", [False, {"type": "string"}])
def test_empty_property_schema_remains_a_declared_property(additional: JsonValue) -> None:
    projected = _accepts_original_and_projection({
        "type": "object", "properties": {"anything": {}}, "additionalProperties": additional,
    }, {"anything": 7})
    assert projected["properties"] == {"anything": {}}
    assert projected["additionalProperties"] == additional


@pytest.mark.parametrize("additional", [False, {"type": "string"}])
def test_omitted_pattern_properties_cannot_activate_additional_property_restrictions(
    additional: JsonValue,
) -> None:
    projected = _accepts_original_and_projection({
        "type": "object", "properties": {"text": {"type": "string"}},
        "patternProperties": {"^private_": {"type": "integer"}},
        "additionalProperties": additional,
    }, {"private_id": 7})
    assert "additionalProperties" not in projected
    assert "patternProperties" not in projected
    assert projected["properties"] == {"text": {"type": "string"}}


def test_depth_budget_does_not_turn_omitted_children_into_forbidden_properties() -> None:
    schema: Mapping[str, JsonValue] = {"type": "integer"}
    value: JsonValue = 7
    for _ in range(10):
        schema = {"properties": {"child": schema}, "additionalProperties": False}
        value = {"child": value}
    _accepts_original_and_projection(schema, value)


@pytest.mark.parametrize("draft", [DRAFT3, DRAFT4])
def test_legacy_annotations_do_not_become_modern_constraints(draft: str) -> None:
    projected = _accepts_original_and_projection({
        "$schema": draft, "type": "object",
        "properties": {"text": {"type": "string", "const": "fixed"}},
    }, {"text": "different"})
    assert "const" not in json.dumps(projected)


def test_official_draft4_declaration_and_ignored_const_keep_source_semantics() -> None:
    projected = _accepts_original_and_projection({
        "$schema": DRAFT4, "type": "object", "properties": {"text": {"const": "English"}},
    }, {"text": "\u4e2d\u6587"})
    assert projected["$schema"] == DRAFT4


@pytest.mark.parametrize("outer,inner", [
    ("https://json-schema.org/draft/2020-12/schema", DRAFT4),
    (DRAFT4, "https://json-schema.org/draft/2020-12/schema"),
])
def test_nested_official_dialect_switch_is_preserved(outer: str, inner: str) -> None:
    value = "\u4e2d\u6587" if inner == DRAFT4 else "English"
    projected = _accepts_original_and_projection({
        "$schema": outer, "type": "object",
        "properties": {"text": {"$schema": inner, "const": "English"}},
    }, {"text": value})
    assert projected["$schema"] == outer
    text_schema = cast(Mapping[str, JsonValue],
                       cast(Mapping[str, JsonValue], projected["properties"])["text"])
    assert text_schema["$schema"] == inner
    if inner != DRAFT4:
        payload = json.loads(json.dumps(projected))
        assert not validator_for(payload)(payload).is_valid({"text": "\u4e2d\u6587"})


def test_legacy_ref_siblings_do_not_become_constraints_after_reference_is_removed() -> None:
    projected = _accepts_original_and_projection({
        "$schema": DRAFT7, "definitions": {"value": {"type": "integer"}},
        "$ref": "#/definitions/value", "type": "string",
    }, 7)
    assert not projected


def test_prefix_items_are_not_subject_to_projected_tail_item_constraints() -> None:
    projected = _accepts_original_and_projection({
        "type": "array", "prefixItems": ({"type": "string"},), "items": {"type": "integer"},
    }, ("head", 7))
    assert "items" not in projected


def test_unknown_dialect_does_not_claim_default_dialect_validation() -> None:
    item = _project({
        "$schema": "https://unregistered.invalid/private", "type": "string", "const": "private",
    })
    assert "input_schema" not in item
    assert "unregistered.invalid" not in json.dumps(item)
    assert "private" not in json.dumps(item)


def test_complete_safe_constraints_remain_effective_and_annotations_stay_private() -> None:
    schema: Mapping[str, JsonValue] = {
        "type": "object", "required": ("text", "count"),
        "properties": {
            "text": {"type": "string", "enum": ("English", "French")},
            "count": {"type": ("integer", "null"), "minimum": 0},
            "stable": {"const": "fixed"},
        },
        "additionalProperties": False,
        "description": "secret://credential", "default": {"text": "sk-private"},
    }
    projected = _accepts_original_and_projection(schema, {"text": "English", "count": None})
    payload: Any = json.loads(json.dumps(projected))
    assert payload == {
        "type": "object", "required": ["text", "count"],
        "properties": {
            "text": {"type": "string", "enum": ["English", "French"]},
            "count": {"type": ["integer", "null"], "minimum": 0},
            "stable": {"const": "fixed"},
        },
        "additionalProperties": False,
    }
    validator = Draft202012Validator(payload)
    for invalid in ({"text": "German", "count": 1}, {"text": "English", "count": "1"},
                    {"text": "English"}, {"text": "English", "count": 1, "extra": 1},
                    {"text": "English", "count": -1},
                    {"text": "English", "count": 1, "stable": "changed"}):
        assert not validator.is_valid(invalid)
    assert "secret://credential" not in json.dumps(projected)
    assert "sk-private" not in json.dumps(projected)
