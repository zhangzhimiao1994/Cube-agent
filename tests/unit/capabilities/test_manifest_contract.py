from __future__ import annotations

from agent_hub.capabilities.manifest import (
    is_safe_manifest_name,
    project_capability_manifest_item,
)


def test_capability_manifest_item_applies_defaults_and_preserves_schemas() -> None:
    seen_ids: set[str] = set()
    seen_names: set[str] = set()

    item = project_capability_manifest_item(
        {
            "id": "mcp.search",
            "aliases": ("search_web",),
            "available": False,
            "availability_reason": "mcp_server_timeout",
            "replay_safe": True,
            "input_schema": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
            },
            "output_schema": {"type": "object"},
        },
        seen_ids,
        seen_names,
    )

    assert item == {
        "id": "mcp.search",
        "kind": "plugin",
        "adapter": "tool_registry",
        "permission_class": "tool.use",
        "sandbox_profile": "unspecified",
        "policy_effect": "inherit",
        "available": False,
        "availability_reason": "mcp_server_timeout",
        "replay_safe": True,
        "aliases": ("search_web",),
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
        },
        "output_schema": {"type": "object"},
    }


def test_capability_manifest_item_rejects_unsafe_ids_and_alias_conflicts() -> None:
    seen_ids = {"mcp.search"}
    seen_names = {"workspace.read"}

    assert not is_safe_manifest_name("bad name")
    assert project_capability_manifest_item(
        {"id": "bad name"},
        seen_ids=set(),
        seen_names=set(),
    ) is None
    assert project_capability_manifest_item(
        {"id": "mcp.search"},
        seen_ids=seen_ids,
        seen_names=set(),
    ) is None
    assert project_capability_manifest_item(
        {"id": "mcp.read", "aliases": ("workspace.read",)},
        seen_ids=set(),
        seen_names=seen_names,
    ) is None
