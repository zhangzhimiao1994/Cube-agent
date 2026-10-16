from __future__ import annotations

import importlib
import json
from types import ModuleType

import pytest
from jsonschema import Draft202012Validator  # type: ignore[import-untyped]

from agent_hub.capabilities.tools.project_zip import project_zip_input_schema
from agent_hub.capabilities.tools.workspace_read import (
    read_tool_description,
    read_tool_input_schema,
)


def _contracts() -> ModuleType:
    name = "agent_hub.capabilities.tools.builtin_contracts"
    assert importlib.util.find_spec(name) is not None, "shared builtin contracts are missing"
    return importlib.import_module(name)


def _validator(name: str) -> Draft202012Validator:
    schema = _contracts().builtin_tool_input_schema(name)
    assert schema is not None
    schema = json.loads(json.dumps(schema))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


@pytest.mark.parametrize(
    ("name", "field", "value"),
    [
        ("calculator.evaluate", "expression", "1 + 2"),
        ("calculator", "expression", "1 + 2"),
        ("calculator_evaluate", "expression", "1 + 2"),
        ("http.read", "url", "https://example.invalid/document"),
    ],
)
def test_required_nonblank_arguments_are_exposed(name: str, field: str, value: str) -> None:
    validator = _validator(name)
    assert validator.is_valid({field: value})
    assert not validator.is_valid({})
    for invalid in (None, 4, "", " \t\n"):
        assert not validator.is_valid({field: invalid})
    assert not validator.is_valid({field: value, "unexpected": True})
    description = _contracts().builtin_tool_description(name)
    assert isinstance(description, str) and field in description.lower()


@pytest.mark.parametrize("name", ["document.generate_docx", "presentation.generate_pptx"])
def test_document_schema_preserves_crew_title_and_optional_fields(name: str) -> None:
    assert _contracts().builtin_tool_input_schema(name) == {
        "type": "object",
        "additionalProperties": True,
        "required": ("title",),
        "properties": {
            "title": {"type": "string", "minLength": 1, "maxLength": 200},
            "filename": {"type": "string", "minLength": 1, "maxLength": 200},
            "presentation": {"type": "string", "enum": ("step_detail", "final_attachment")},
        },
    }
    validator = _validator(name)
    assert not validator.is_valid({})
    assert not validator.is_valid({"title": ""})
    assert not validator.is_valid({"title": 42})
    assert not validator.is_valid({"title": "a" * 201})
    assert validator.is_valid({"title": "Report"})
    assert validator.is_valid({
        "title": "Report", "filename": "report.docx", "presentation": "final_attachment",
        "sections": [{"heading": "Summary", "body": "Result"}],
        "slides": [{"title": "Summary"}],
    })
    assert not validator.is_valid({"title": "Report", "filename": ""})
    assert not validator.is_valid({"title": "Report", "filename": "a" * 201})
    assert not validator.is_valid({"title": "Report", "presentation": "unknown"})
    assert validator.is_valid({"title": "Report", "presentation": "step_detail"})
    assert "title" in _contracts().builtin_tool_description(name).lower()


def test_project_zip_uses_canonical_schema_and_requires_title_files() -> None:
    assert _contracts().builtin_tool_input_schema("project.generate_zip") == project_zip_input_schema()
    validator = _validator("project.generate_zip")
    assert validator.is_valid({"title": "Project", "files": {"main.py": "print(1)"}})
    assert validator.is_valid({"title": "Project", "files": [{"main.py": "print(1)"}]})
    assert not validator.is_valid({"title": "Project"})
    assert not validator.is_valid({"files": {"main.py": "print(1)"}})
    assert not validator.is_valid({"title": "Project", "files": {}})
    description = _contracts().builtin_tool_description("project.generate_zip").lower()
    assert "files" in description and "title" in description


@pytest.mark.parametrize("name", ["workspace.read", "workspace_read", "read_context"])
def test_read_contracts_delegate_without_losing_file_context_distinction(name: str) -> None:
    contracts = _contracts()
    assert contracts.builtin_tool_input_schema(name) == read_tool_input_schema(name)
    assert contracts.builtin_tool_description(name) == read_tool_description(name)
    validator = _validator(name)
    if name == "read_context":
        assert validator.is_valid({})
        assert validator.is_valid({"query": "architecture"})
        assert validator.is_valid({"query": None})
        assert validator.is_valid({"text": "architecture"})
        assert validator.is_valid({"query": None, "text": "architecture", "path": "src/main.py"})
    else:
        assert not validator.is_valid({})
        assert not validator.is_valid({"query": "architecture"})
    assert validator.is_valid({"path": "src/main.py"})


@pytest.mark.parametrize(
    "name", ["http_read", "project.preflight_architecture", "plugin.example", "unknown", ""],
)
def test_unknown_names_and_nonexistent_http_alias_do_not_gain_contracts(name: str) -> None:
    assert _contracts().builtin_tool_input_schema(name) is None
    assert _contracts().builtin_tool_description(name) is None


def test_calculator_aliases_share_contract_and_description() -> None:
    contracts = _contracts()
    for alias in ("calculator", "calculator_evaluate"):
        assert contracts.builtin_tool_input_schema(alias) == contracts.builtin_tool_input_schema(
            "calculator.evaluate"
        )
        assert contracts.builtin_tool_description(alias) == contracts.builtin_tool_description(
            "calculator.evaluate"
        )
