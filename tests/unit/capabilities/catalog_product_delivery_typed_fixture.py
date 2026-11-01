"""Checked test boundaries; real catalog validators, services and file store remain.

Malformed catalog values must reach the original constructor/parser unchanged,
so only those two hostile-data seams use a callable object plus DTO return checks.
"""
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, TypedDict
from uuid import UUID

import pytest

from agent_hub.capabilities import project_inspection as inspection
from agent_hub.capabilities.generated_project_service import GeneratedProjectContext
from agent_hub.capabilities.project_verification import ProjectVerificationService
from agent_hub.capabilities.python_generated_service import PythonGeneratedProjectService
from agent_hub.capabilities.python_project_service import PythonProjectService
from agent_hub.files.generated import GeneratedFileStore
from agent_hub.runtime.contracts import JsonValue
from agent_hub.skills.sandbox.project import ProjectInvocation
from tests.unit.capabilities import test_python_generated_service as generated
from tests.unit.capabilities import test_python_project_service as original


def parse_query(value: object) -> inspection.PythonCatalogQuery | None:
    parser: object = inspection.parse_python_catalog_query
    assert callable(parser)
    result: object = parser(value)
    assert result is None or isinstance(result, inspection.PythonCatalogQuery)
    return result


def construct_query(fields: Mapping[str, object]) -> inspection.PythonCatalogQuery:
    constructor: object = inspection.PythonCatalogQuery
    assert callable(constructor)
    result: object = constructor(**fields)
    assert isinstance(result, inspection.PythonCatalogQuery)
    return result


@dataclass
class ProductLive:
    kind: Literal['original', 'generated']
    owner: ProjectVerificationService
    service: PythonProjectService | PythonGeneratedProjectService
    context: GeneratedProjectContext | None
    repo: original.Repository | generated.Approvals
    executor: original.PythonExecutor
    base: ProjectInvocation
    source: Path
    store: GeneratedFileStore
    monkeypatch: pytest.MonkeyPatch
    root: Path


class RestoreArgs(TypedDict):
    kind: Literal['python_original', 'python_generated']
    tenant_id: UUID
    run_id: UUID
    actor: str
    payload: Mapping[str, JsonValue]
    source_sha256: str
    execution_id: str
