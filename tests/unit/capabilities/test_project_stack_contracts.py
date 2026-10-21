"""New observations cannot confer environment or execution authority."""

import importlib
import importlib.util
from dataclasses import FrozenInstanceError, replace
from typing import Any

import pytest


@pytest.mark.parametrize("module", [
    "project_stack_contracts", "project_stack_registry", "go_project_recipe",
])
def test_additive_contract_modules_exist(module: str) -> None:
    assert importlib.util.find_spec(f"agent_hub.capabilities.{module}") is not None


@pytest.fixture
def contracts() -> Any:
    name = "agent_hub.capabilities.project_stack_contracts"
    assert importlib.util.find_spec(name) is not None, "stack contracts missing"
    return importlib.import_module(name)


def test_observation_separates_declaration_from_execution(contracts: Any) -> None:
    observed = contracts.StackObservation(
        detected_stacks=("go",), stack="go", declaration_supported=True,
        reason="execution_not_available",
    )
    assert observed.detected is True
    assert observed.environment_ready is False
    assert observed.execution_proved is False
    assert observed.recipe_supported is False
    assert observed.execution_supported is False
    with pytest.raises(FrozenInstanceError):
        observed.stack = "node"


@pytest.mark.parametrize("changes", [
    {"stack": "Go"}, {"detected_stacks": ("go", "go")},
    {"detected_stacks": ["go"]}, {"stack": "node"},
    {"recipe_supported": 1}, {"recipe_supported": True}, {"execution_supported": True},
    {"declaration_supported": 1}, {"environment_ready": True}, {"execution_proved": True},
    {"environment_ready": 0}, {"execution_proved": 0}, {"reason": "arbitrary reason"},
    {"detected_stacks": (), "recipe_supported": True},
])
def test_invalid_or_authority_claiming_observations_fail_closed(
    contracts: Any, changes: dict[str, object],
) -> None:
    observed = contracts.StackObservation(
        detected_stacks=("go",), stack="go", reason="recipe_not_inspected",
    )
    with pytest.raises(contracts.ProjectStackContractError):
        replace(observed, **changes)


@pytest.mark.parametrize("files", [
    {"../go.mod": b""}, {"/go.mod": b""}, {"a//go.mod": b""},
    {"a/./go.mod": b""}, {"a\\go.mod": b""}, {"C:go.mod": b""},
    {"a/GO.mod": b"", "a/go.mod": b""}, {"a": b"", "a/go.mod": b""},
    {"src/a.go": "not bytes"}, {True: b""}, {"src/NUL": b""},
    {"src./main.go": b""}, {"A/a.go": b"", "a/b.go": b""},
])
def test_snapshot_paths_and_bytes_are_strict(contracts: Any, files: object) -> None:
    with pytest.raises(contracts.ProjectStackContractError):
        contracts.validate_project_files(files)


def test_snapshot_limits_and_copy_are_explicit(contracts: Any) -> None:
    files = {"go.mod": b"module example.org/demo\n"}
    snapshot = contracts.validate_project_files(files)
    files.clear()
    assert snapshot == {"go.mod": b"module example.org/demo\n"}
    for kwargs in ({"max_files": True}, {"max_total_bytes": 0}, {"max_total_bytes": 2}):
        with pytest.raises(contracts.ProjectStackContractError):
            contracts.validate_project_files(snapshot, **kwargs)
