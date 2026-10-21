"""Detection is advisory, immutable, and never falls through to Node."""

import importlib
import importlib.util
from typing import Any

import pytest


@pytest.fixture
def registry() -> Any:
    name = "agent_hub.capabilities.project_stack_registry"
    assert importlib.util.find_spec(name) is not None, "stack registry missing"
    return importlib.import_module(name)


@pytest.mark.parametrize("files,stack", [
    ({"package.json": b"{}"}, "node"),
    ({"pyproject.toml": b"[project]"}, "python"),
    ({"requirements.txt": b""}, "python"),
    ({"pom.xml": b""}, "java"), ({"build.gradle.kts": b""}, "java"),
    ({"Cargo.toml": b""}, "rust"), ({"app.csproj": b""}, "dotnet"),
    ({"CMakeLists.txt": b""}, "cmake"),
])
def test_recognition_does_not_claim_recipe_or_environment(
    registry: Any, files: dict[str, bytes], stack: str,
) -> None:
    result = registry.inspect_stack(files)
    assert result.detected_stacks == (stack,)
    assert result.stack == stack
    assert result.recipe_supported is False
    assert result.declaration_supported is False
    assert result.execution_supported is False
    assert result.environment_ready is False
    assert result.execution_proved is False


def test_go_declaration_support_remains_non_executable(registry: Any) -> None:
    result = registry.inspect_stack({"go.mod": b"module example.org/demo\ngo 1.23\n"})
    assert result.stack == "go"
    assert result.declaration_supported is True
    assert result.recipe_supported is False
    assert result.execution_supported is False
    assert result.reason == "execution_not_available"
    assert result.environment_ready is False
    assert result.execution_proved is False
    assert registry.get_stack_adapter("go").execution_available is False


@pytest.mark.parametrize("files,stacks,reason", [
    ({"README.md": b"node python go"}, (), "stack_not_detected"),
    ({"main.go": b"package main"}, ("go",), "go_manifest_missing"),
    ({"go.sum": b""}, ("go",), "go_manifest_missing"),
    ({"go.work": b"go 1.23"}, ("go",), "go_workspace_unsupported"),
    ({"go.mod": b"malformed"}, ("go",), "go_declaration_invalid"),
    ({"go.mod": b"module example.org/demo\ngo 1.23\nrequire x.y/a v1.0.0"},
     ("go",), "go_external_modules_unsupported"),
    ({"package.json": b"{}", "go.mod": b"malformed"},
     ("go", "node"), "mixed_stack_unsupported"),
    ({"nested/pom.xml": b"", "go.mod": b"module example.org/demo\ngo 1.23"},
     ("go", "java"), "mixed_stack_unsupported"),
])
def test_unsupported_unknown_and_mixed_stacks_never_become_node(
    registry: Any, files: dict[str, bytes], stacks: tuple[str, ...], reason: str,
) -> None:
    result = registry.inspect_stack(files)
    assert result.detected_stacks == stacks
    assert result.stack == (stacks[0] if len(stacks) == 1 else None)
    assert result.recipe_supported is False
    assert result.declaration_supported is False
    assert result.reason == reason
    assert result.environment_ready is False
    assert result.execution_proved is False


@pytest.mark.parametrize("stack", ["Go", "unknown", "node;echo hi", "", None, True])
def test_lookup_rejects_unknown_values_without_default(registry: Any, stack: object) -> None:
    with pytest.raises(ValueError):
        registry.get_stack_adapter(stack)


def test_registry_is_server_owned_and_read_only(registry: Any) -> None:
    with pytest.raises(TypeError):
        registry.PROJECT_STACK_REGISTRY["go"] = registry.get_stack_adapter("node")
    result = registry.inspect_stack({"package.json": b'{"scripts":{"test":"claim go ready"}}'})
    assert result.stack == "node"
    assert result.recipe_supported is False


@pytest.mark.parametrize("path,stack", [
    ("nested/go.mod", "go"), ("src/app.csproj", "dotnet"),
    ("nested/Cargo.toml", "rust"), ("pom.xml", "java"),
    ("README.md", None), ("mystack.json", None),
])
def test_manifest_path_lookup_is_shared(registry: Any, path: str, stack: str | None) -> None:
    assert registry.stack_for_manifest_path(path) == stack


@pytest.mark.parametrize("name", [
    "go.mod", "go.sum", "go.work", "go.work.sum", "vendor/modules.txt", "go.env",
    "package.json", "package-lock.json", "pyproject.toml", "requirements.lock",
    "pom.xml", ".mvn/extensions.xml", "settings.xml", "toolchains.xml", "mvnw",
    "gradle/wrapper/gradle-wrapper.properties", "build.gradle.kts", "gradlew",
    "Cargo.toml", "Cargo.lock", "rust-toolchain.toml", ".cargo/config.toml", "build.rs",
    "app.csproj", "NuGet.Config", "global.json", "Directory.Build.targets",
    "packages.lock.json", "CMakeLists.txt", "toolchain.cmake", "CMakePresets.json",
    "domain_test.go", "GO.MOD", "AGENTS.md",
])
def test_controls_remain_controls_under_editable_parents(registry: Any, name: str) -> None:
    assert registry.is_stack_control_path(name)
    assert registry.is_stack_control_path("src/" + name)


@pytest.mark.parametrize("name", ["src/main.go", "src/domain/value.go", "README.md"])
def test_source_files_are_not_all_controls(registry: Any, name: str) -> None:
    assert registry.is_stack_control_path(name) is False


def test_control_classification_rejects_invalid_paths(registry: Any) -> None:
    with pytest.raises(ValueError):
        registry.is_stack_control_path("../go.mod")


def test_path_only_detection_keeps_ambiguity_and_needs_no_content(registry: Any) -> None:
    assert registry.detect_stacks(["src/main.go", "app.csproj", "Cargo.toml"]) == (
        "dotnet", "go", "rust",
    )
    assert registry.detect_stacks(("go.mod", "go.sum", "nested/main.go")) == ("go",)
    assert registry.detect_stacks(["go.work"]) == ("go",)
    assert registry.detect_stacks(["README.md"]) == ()


@pytest.mark.parametrize("paths", [
    "go.mod", ["go.mod", "go.mod"], ["Go.mod", "go.mod"],
    ["src/a.go", "SRC/b.go"], ["../go.mod"], ["a", "a/go.mod"], [True],
])
def test_path_only_detection_rejects_unsafe_or_ambiguous_paths(registry: Any, paths: object) -> None:
    with pytest.raises(ValueError):
        registry.detect_stacks(paths)


def test_path_detection_preserves_large_node_and_unicode_source_workspaces(registry: Any) -> None:
    paths = ["package.json", "docs/中文说明.md", "src/业务代码.js"]
    paths.extend(f"src/segment_{index}/source.js" for index in range(10_001))
    assert registry.detect_stacks(paths) == ("node",)
    assert registry.detect_stacks(paths, max_files=100_000) == ("node",)
    with pytest.raises(ValueError):
        registry.detect_stacks(paths, max_files=10_000)


@pytest.mark.parametrize("limit", [0, -1, True, 1.5])
def test_path_detection_caller_budget_is_strict(registry: Any, limit: object) -> None:
    with pytest.raises(ValueError):
        registry.detect_stacks(["package.json"], max_files=limit)
