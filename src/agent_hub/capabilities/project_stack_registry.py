"""Server-owned detection registry, separate from the existing execution routes."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal

from agent_hub.capabilities.go_project_recipe import (
    GoProjectRecipeError,
    parse_go_project_declaration,
)
from agent_hub.capabilities.project_stack_contracts import (
    ProjectStack,
    ProjectStackContractError,
    StackObservation,
    project_relative_path,
    require_project_stack,
    validate_project_files,
)

_MANIFEST_STACKS: Mapping[str, ProjectStack] = MappingProxyType({
    "package.json": "node", "pyproject.toml": "python", "requirements.txt": "python",
    "go.mod": "go", "pom.xml": "java", "build.gradle": "java", "build.gradle.kts": "java",
    "Cargo.toml": "rust", "CMakeLists.txt": "cmake",
})
STACK_MANIFEST_NAMES = tuple(_MANIFEST_STACKS)
STACK_MANIFEST_SUFFIXES = (".csproj",)
_CONTROL_NAMES = frozenset(name.casefold() for name in (
    *STACK_MANIFEST_NAMES,
    "package-lock.json", "npm-shrinkwrap.json", "pnpm-lock.yaml", "yarn.lock", "bun.lock",
    "bun.lockb", "requirements.lock", "uv.lock", "poetry.lock", "Pipfile", "Pipfile.lock",
    "setup.py", "setup.cfg", "pytest.ini", "tox.ini", "conftest.py",
    "go.sum", "go.work", "go.work.sum", "vendor", "go.env",
    "settings.xml", "toolchains.xml", ".mvn", "mvnw", "mvnw.cmd", "gradlew", "gradlew.bat",
    "settings.gradle", "settings.gradle.kts", "gradle.properties", "gradle",
    "Cargo.lock", "rust-toolchain", "rust-toolchain.toml", ".cargo", "build.rs",
    "global.json", "NuGet.Config", "packages.lock.json", "Directory.Build.props",
    "Directory.Build.targets", "Directory.Packages.props", "CMakePresets.json",
    "CMakeUserPresets.json", "CTestConfig.cmake", "AGENTS.md", "DELIVERY_MANIFEST.json",
))


@dataclass(frozen=True, slots=True)
class ProjectStackAdapter:
    """Detection descriptor only; no supplied callable or execution registration."""

    stack: ProjectStack
    manifest_names: tuple[str, ...]
    recipe_kind: Literal["go_stdlib_singlemodule_v1"] | None = None
    execution_available: Literal[False] = False

    def __post_init__(self) -> None:
        require_project_stack(self.stack)
        if (type(self.manifest_names) is not tuple
                or not all(type(name) is str and _MANIFEST_STACKS.get(name) == self.stack
                           for name in self.manifest_names)
                or len(set(self.manifest_names)) != len(self.manifest_names)
                or self.execution_available is not False
                or self.recipe_kind != (
                    "go_stdlib_singlemodule_v1" if self.stack == "go" else None
                )):
            raise ProjectStackContractError()


_STACK_ORDER: tuple[ProjectStack, ...] = ("node", "python", "go", "java", "rust", "dotnet", "cmake")
PROJECT_STACK_REGISTRY: Mapping[ProjectStack, ProjectStackAdapter] = MappingProxyType({
    stack: ProjectStackAdapter(
        stack=stack,
        manifest_names=tuple(name for name, owner in _MANIFEST_STACKS.items() if owner == stack),
        recipe_kind="go_stdlib_singlemodule_v1" if stack == "go" else None,
    ) for stack in _STACK_ORDER
})


def get_stack_adapter(stack: object) -> ProjectStackAdapter:
    return PROJECT_STACK_REGISTRY[require_project_stack(stack)]


def stack_for_manifest_path(path: object) -> ProjectStack | None:
    name = project_relative_path(path).rsplit("/", 1)[-1]
    if name.lower().endswith(STACK_MANIFEST_SUFFIXES):
        return "dotnet"
    return next((owner for manifest, owner in _MANIFEST_STACKS.items()
                 if name.lower() == manifest.lower()), None)


def is_stack_control_path(path: object) -> bool:
    """Classify controls even under editable parents; caller enforces preservation."""
    parts = project_relative_path(path).split("/")
    return any(
        part.casefold() in _CONTROL_NAMES
        or part.casefold().endswith((".csproj", ".cmake", "_test.go"))
        for part in parts
    )


def detect_stacks(paths: Sequence[str], *, max_files: int = 100_000) -> tuple[ProjectStack, ...]:
    """Recognize paths only, with no declaration, environment or execution claim."""
    if isinstance(paths, (str, bytes)) or not isinstance(paths, Sequence):
        raise ProjectStackContractError()
    if type(max_files) is not int or max_files <= 0 or len(paths) > max_files:
        raise ProjectStackContractError("project_stack_input_limit")
    snapshot = {project_relative_path(path): b"" for path in paths}
    if len(snapshot) != len(paths):
        raise ProjectStackContractError()
    validate_project_files(snapshot, max_files=max_files, max_total_bytes=1)
    stacks: set[ProjectStack] = set()
    for path in snapshot:
        stack = stack_for_manifest_path(path)
        if stack is not None:
            stacks.add(stack)
        name = path.rsplit("/", 1)[-1].lower()
        if name in {"go.sum", "go.work", "go.work.sum"} or name.endswith(".go"):
            stacks.add("go")
    return tuple(sorted(stacks))


def inspect_stack(files: Mapping[str, bytes]) -> StackObservation:
    """Inspect an already-read complete snapshot; never probe tools or grant execution."""
    snapshot = validate_project_files(files)
    detected = detect_stacks(tuple(snapshot))
    if not detected:
        return StackObservation(detected_stacks=(), stack=None, reason="stack_not_detected")
    if len(detected) != 1:
        return StackObservation(
            detected_stacks=detected, stack=None, reason="mixed_stack_unsupported",
        )
    selected = detected[0]
    if selected != "go":
        return StackObservation(
            detected_stacks=detected, stack=selected,
            reason="recipe_not_inspected" if selected in {"node", "python"}
            else "stack_execution_unsupported",
        )
    try:
        parse_go_project_declaration(snapshot)
    except GoProjectRecipeError as error:
        return StackObservation(detected_stacks=detected, stack="go", reason=error.code)
    return StackObservation(
        detected_stacks=detected, stack="go", declaration_supported=True,
        reason="execution_not_available",
    )
