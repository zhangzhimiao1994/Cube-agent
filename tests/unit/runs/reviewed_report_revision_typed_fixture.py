"""Shape guards at the reviewed-revision JSON boundary, retaining mutable aliases."""

from collections.abc import Mapping
from typing import TypeGuard


def _is_mapping(value: object) -> TypeGuard[Mapping[str, object]]:
    return isinstance(value, Mapping) and all(isinstance(key, str) for key in value)


def _is_object(value: object) -> TypeGuard[dict[str, object]]:
    return isinstance(value, dict) and all(isinstance(key, str) for key in value)


def _is_array(value: object) -> TypeGuard[list[object]]:
    return isinstance(value, list)


def read_map(value: object) -> Mapping[str, object]:
    assert _is_mapping(value)
    return value


def object_map(value: object) -> dict[str, object]:
    assert _is_object(value)
    return value


def object_array(value: object) -> list[object]:
    assert _is_array(value)
    return value


def read_rows(value: object) -> dict[str, Mapping[str, object]]:
    return {key: read_map(row) for key, row in read_map(value).items()}


def mutable_rows(value: object) -> dict[str, dict[str, object]]:
    return {key: object_map(row) for key, row in object_map(value).items()}


def string_value(value: object) -> str:
    assert isinstance(value, str)
    return value


def integer_value(value: object) -> int:
    assert isinstance(value, int) and not isinstance(value, bool)
    return value
