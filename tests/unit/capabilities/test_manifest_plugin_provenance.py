from collections.abc import Mapping

import pytest

from agent_hub.capabilities.manifest import project_capability_manifest_item
from agent_hub.runtime.contracts import JsonValue


def _project(plugin_id: JsonValue, kind: str = "plugin") -> Mapping[str, JsonValue]:
    result = project_capability_manifest_item(
        {"id": "scoped.inspect", "kind": kind, "plugin_id": plugin_id}, set(), set()
    )
    assert result is not None
    return result


def test_public_projection_preserves_safe_plugin_provenance() -> None:
    assert _project("alpha")["plugin_id"] == "alpha"


@pytest.mark.parametrize("value", [None, "../private", "", "a" * 129, "PRIVATE TOKEN", 1])
def test_public_projection_drops_invalid_plugin_provenance(value: JsonValue) -> None:
    assert "plugin_id" not in _project(value)


def test_public_projection_does_not_add_plugin_provenance_to_builtin() -> None:
    assert "plugin_id" not in _project("alpha", kind="builtin")
