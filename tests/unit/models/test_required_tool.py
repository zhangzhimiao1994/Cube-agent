from dataclasses import replace
from typing import Any

import pytest

from agent_hub.models.types import ModelCapability, ModelMessage, ModelRequest, ToolDefinition


def tool(name: str = "project_verify") -> ToolDefinition:
    return ToolDefinition(name, "Verify project", {"type": "object"})


def request(**updates: Any) -> ModelRequest:
    return ModelRequest(**{
        "logical_model": "primary",
        "messages": (ModelMessage("user", "Verify project"),),
        "required_capabilities": frozenset({ModelCapability.TOOL_CALLING}),
        "tools": (tool(),),
        **updates,
    })


@pytest.mark.parametrize("name", ["project_verify", "Verify-2", "a" * 64])
def test_required_tool_accepts_exact_external_name(name: str) -> None:
    req = request(tools=(tool(name),), required_tool=name)
    assert req.required_tool == name
    assert replace(req, max_output_tokens=100).required_tool == name


@pytest.mark.parametrize("name", [
    "", " ", True, False, 1, (), {}, "project.verify", "project/verify",
    " project_verify", "project_verify ", "project_verify\n", "a" * 65,
    "unknown", "PROJECT_VERIFY",
])
def test_required_tool_rejects_invalid_or_unregistered_names(name: object) -> None:
    with pytest.raises(ValueError, match="required_tool"):
        request(required_tool=name)


@pytest.mark.parametrize("name", ["project.verify", "project_verify\n", "a" * 65, True])
def test_required_tool_rejects_forged_definition_names(name: object) -> None:
    forged = tool()
    object.__setattr__(forged, "name", name)
    with pytest.raises(ValueError, match="required_tool"):
        request(tools=(forged,), required_tool=name)


def test_required_tool_rejects_a_string_subclass_with_forged_equality() -> None:
    class ForgedName(str):
        def __eq__(self, other: object) -> bool:
            return True

    with pytest.raises(ValueError, match="required_tool"):
        request(required_tool=ForgedName("unknown"))


@pytest.mark.parametrize("tools", [(), (tool(), tool())])
def test_required_tool_requires_exactly_one_definition(tools: tuple[ToolDefinition, ...]) -> None:
    with pytest.raises(ValueError, match="required_tool"):
        request(tools=tools, required_tool="project_verify")


def test_required_tool_requires_explicit_tool_calling_capability() -> None:
    with pytest.raises(ValueError, match="tool_calling"):
        request(required_capabilities=frozenset(), required_tool="project_verify")


def test_required_tool_defaults_to_none_and_does_not_change_legacy_tool_validation() -> None:
    assert request().required_tool is None
    assert request(required_tool=None).required_tool is None
    assert request(tools=(tool(), tool())).required_tool is None
