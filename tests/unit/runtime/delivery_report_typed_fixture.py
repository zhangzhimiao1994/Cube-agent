"""Typed delivery-report JSON views; mutation helpers retain the original objects."""

from tests.unit.runs.project_auto_repair_typed_fixture import (
    json_map,
    mapping,
    sequence,
    string,
)

__all__ = ["json_map", "mapping", "sequence", "string"]


def mutable(value: object) -> dict[str, object]:
    """Narrow a mutable payload without copying or normalizing corrupt fields."""
    assert isinstance(value, dict)
    assert all(isinstance(key, str) for key in value)
    return value
