from collections.abc import Mapping

import pytest
from jsonschema import (
    Draft3Validator,
    Draft4Validator,
    Draft6Validator,
    Draft7Validator,
    Draft201909Validator,
    Draft202012Validator,
)
from jsonschema.protocols import Validator

from agent_hub.plugins.schemas import known_schema_validator
from agent_hub.runtime.contracts import JsonValue


@pytest.mark.parametrize(
    ("uri", "expected"),
    [
        ("http://json-schema.org/draft-03/schema", Draft3Validator),
        ("http://json-schema.org/draft-04/schema", Draft4Validator),
        ("http://json-schema.org/draft-06/schema", Draft6Validator),
        ("http://json-schema.org/draft-07/schema", Draft7Validator),
        ("https://json-schema.org/draft/2019-09/schema", Draft201909Validator),
        ("https://json-schema.org/draft/2020-12/schema", Draft202012Validator),
    ],
)
@pytest.mark.parametrize("suffix", ["", "#"])
def test_known_dialect_keeps_exact_validator(
    uri: str, expected: type[Validator], suffix: str,
) -> None:
    schema: Mapping[str, JsonValue] = {"$schema": uri + suffix, "type": "object"}
    assert known_schema_validator(schema) is expected


@pytest.mark.parametrize(
    "schema", [{"$schema": "https://invalid.example/schema"}, {}, True, False],
)
def test_unknown_or_absent_dialect_has_no_implicit_validator(
    schema: Mapping[str, JsonValue] | bool,
) -> None:
    assert known_schema_validator(schema) is None
