from __future__ import annotations

from agent_hub.capabilities.tools.registry import ToolRegistry, create_builtin_tool_registry


def test_registry_registers_builtin_tool_names() -> None:
    registry = create_builtin_tool_registry()

    assert registry.names() == ("calculator.evaluate", "http.read", "workspace.read")


def test_registry_exposes_schemas_without_executor_callables() -> None:
    registry = ToolRegistry()
    registry.register("sample.tool", object())

    projected = registry.schemas()

    assert projected == ({"name": "sample.tool"},)
