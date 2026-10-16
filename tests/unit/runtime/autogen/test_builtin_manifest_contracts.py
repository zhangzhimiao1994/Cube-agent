"""The actual AutoGen facade must expose required builtin executor inputs."""
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast
from uuid import UUID

import pytest
from jsonschema import Draft202012Validator  # type: ignore[import-untyped]

from agent_hub.capabilities.runtime import RuntimeCapabilityGateway
from agent_hub.runtime.autogen.adapter import GatewayCapabilityTool
from agent_hub.runtime.contracts import JsonValue


@pytest.mark.parametrize(("name", "arguments", "required"), (
    ("calculator.evaluate", {"expression": "2 + 2"}, ("expression",)),
    ("http.read", {"url": "https://example.com/read"}, ("url",)),
    ("workspace.read", {"path": "src/main.ts"}, ("path",)),
    ("document.generate_docx", {"title": "Report", "sections": []}, ("title",)),
    ("presentation.generate_pptx", {"title": "Review", "slides": []}, ("title",)),
    ("project.generate_zip", {"title": "Project", "files": {"README.md": "Hello"}}, ("title", "files")),
))
def test_runtime_manifest_reaches_actual_autogen_facade(
    tmp_path: Path, name: str, arguments: Mapping[str, JsonValue], required: tuple[str, ...],
) -> None:
    gateway = RuntimeCapabilityGateway(skill_store_dir=tmp_path)
    manifest = gateway.capability_manifest(UUID(int=2))
    items = cast(tuple[Mapping[str, JsonValue], ...], manifest["capabilities"])
    item = next(item for item in items if item["id"] == name)
    assert "input_schema" in item
    tool = GatewayCapabilityTool(
        gateway, tenant_id=UUID(int=2), run_id=UUID(int=1), actor="implementer",
        name=name.replace(".", "_"), records=[],
        durability=cast(Any, object()), harness_tool_gateway=cast(Any, object()),
        input_schema=cast(Mapping[str, JsonValue], item["input_schema"]),
        description=cast(str | None, item.get("description")),
    )
    schema = tool.schema["parameters"]
    assert schema is not None
    validator = Draft202012Validator(json.loads(json.dumps(schema)))
    assert set(required) <= set(schema.get("required", []))
    assert validator.is_valid(dict(arguments))
    for key in required:
        assert not validator.is_valid({k: value for k, value in arguments.items() if k != key})
