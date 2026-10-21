from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING

from .project_zip import PROJECT_ZIP_TOOL_NAME, project_zip_input_schema
from .workspace_read import read_tool_description, read_tool_input_schema

if TYPE_CHECKING:
    from agent_hub.runtime.contracts import JsonValue


def builtin_tool_input_schema(name: str) -> Mapping[str, JsonValue] | None:
    """Return model-owned builtin arguments, without granting execution permissions."""
    read_schema = read_tool_input_schema(name)
    if read_schema is not None:
        return read_schema
    if name in {"project.inspect", "project.verify"}:
        return {"type": "object", "additionalProperties": False, "properties": {}}
    if name == PROJECT_ZIP_TOOL_NAME:
        return project_zip_input_schema()
    if name in {"calculator.evaluate", "calculator", "calculator_evaluate", "http.read"}:
        field = "url" if name == "http.read" else "expression"
        return {
            "type": "object",
            "additionalProperties": False,
            "required": (field,),
            "properties": {
                field: {"type": "string", "minLength": 1, "pattern": r"\S"},
            },
        }
    if name in {"document.generate_docx", "presentation.generate_pptx"}:
        return {
            "type": "object",
            "additionalProperties": True,
            "required": ("title",),
            "properties": {
                "title": {"type": "string", "minLength": 1, "maxLength": 200},
                "filename": {"type": "string", "minLength": 1, "maxLength": 200},
                "presentation": {
                    "type": "string",
                    "enum": ("step_detail", "final_attachment"),
                },
            },
        }
    return None


def builtin_tool_description(name: str) -> str | None:
    """Describe builtin inputs; authorization remains the executor's responsibility."""
    read_description = read_tool_description(name)
    if read_description is not None:
        return read_description
    if name == "project.verify":
        return (
            "Install approved dependencies, then build and test in isolation. No arguments. "
            "Requires command/install authorization. Diagnose failures, repair source, then rerun before delivery. "
            "New projects first write ACCEPTANCE.json: schema_version=1, stack=node|python, implementation-only "
            "editable_paths, test_files, criteria=[{id:AC-001,description,tests:[exact test IDs]}]. "
            "Use node:test leaf titles or unittest IDs such as test_app.Case.test_feature. "
            "Every named test must pass; skip/todo/echo are not success. First verification freezes standards, "
            "tests and controls; repair implementation without weakening them. "
            "Python: inspect the actual python_build_profile catalog, use src layout, pinned pyproject.toml and "
            "hashed requirements.lock. Never invent wheel hashes or treat configured_not_probed as installed."
        )
    if name == "project.inspect":
        return (
            "Inspect current run's project manifests, declared dependencies, runtime constraints "
            "and build/test script presence without executing code. Takes no arguments. "
            "Does not install dependencies or prove execution environment readiness. "
            "python_build_profile reports the Root-configured interpreter and pinned artifact candidates, without "
            "granting installation. For python_offline_wheel_v1 use setuptools.build_meta, src package-dir, "
            "[tool.setuptools.packages.find] where=[src], and [tool.agent-hub.python-project] "
            "recipe=python_offline_wheel_v1, import-module=<actual package>, test-directory=tests. "
            "Select needed direct/transitive dependency pins from the actual catalog and write their SHA lock entries."
        )
    if name in {"calculator.evaluate", "calculator", "calculator_evaluate"}:
        return "Evaluate a calculator expression supplied as the required nonblank expression string."
    if name == "http.read":
        return (
            "Fetch an HTTP resource using the required nonblank url string, subject to the "
            "runtime's network access restrictions. Returns response text, not workspace files."
        )
    if name == "document.generate_docx":
        return "Generate a downloadable DOCX document from the required title and section content."
    if name == "presentation.generate_pptx":
        return "Generate a downloadable PPTX presentation from the required title and slide content."
    if name == PROJECT_ZIP_TOOL_NAME:
        return (
            "Generate a downloadable project ZIP using required title and files. Supply files as "
            "relative paths mapped to UTF-8 text content, or an array of single-path objects."
        )
    return None
