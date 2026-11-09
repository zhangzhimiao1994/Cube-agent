"""Read the intent of a conventional original Python project without granting IO."""

from __future__ import annotations

import hashlib
import keyword
import re
import tomllib
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from typing import cast

from packaging.requirements import InvalidRequirement, Requirement
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version

_MAX_MANIFEST_BYTES = 256 * 1024
_MAX_REQUIREMENTS = 64
_MAX_SOURCE_PATHS = 8_192
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_DISTRIBUTION = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9._-]{0,126}[A-Za-z0-9])?")
_PATH = re.compile(r"[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*")


class StandardPythonIntentError(ValueError):
    """An unsupported or ambiguous shape, never an approval or retry decision."""


@dataclass(frozen=True, slots=True)
class StandardPythonIntent:
    project_name: str
    project_version: str
    import_module: str
    requires_python: str
    manifest_sha256: str
    build_requirements: tuple[str, ...]
    runtime_requirements: tuple[str, ...]
    console_scripts: tuple[tuple[str, str], ...]
    test_directory: str = "tests"

    def supports_interpreter(self, version: str) -> bool:
        try:
            return SpecifierSet(self.requires_python).contains(Version(version))
        except (InvalidSpecifier, InvalidVersion, TypeError):
            return False


def _table(value: object, code: str) -> Mapping[str, object]:
    if not isinstance(value, dict) or any(type(key) is not str for key in value):
        raise StandardPythonIntentError(code)
    return cast(Mapping[str, object], value)


def _identifier(value: str) -> bool:
    return _IDENTIFIER.fullmatch(value) is not None and not keyword.iskeyword(value)


def _requirements(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) > _MAX_REQUIREMENTS:
        raise StandardPythonIntentError("python_original_unsafe_requirement")
    requirements: dict[str, str] = {}
    for raw in value:
        if type(raw) is not str or not raw or len(raw) > 512:
            raise StandardPythonIntentError("python_original_unsafe_requirement")
        try:
            item = Requirement(raw)
        except (InvalidRequirement, RecursionError):
            raise StandardPythonIntentError("python_original_unsafe_requirement") from None
        name = canonicalize_name(item.name)
        if (item.url is not None or name in requirements or _DISTRIBUTION.fullmatch(name) is None
                or any(spec.operator == "===" for spec in item.specifier)
                or re.search(r"\b(?:platform_release|platform_version)\b", str(item.marker or ""))):
            raise StandardPythonIntentError("python_original_unsafe_requirement")
        requirements[name] = str(item)
    return tuple(requirements[name] for name in sorted(requirements))


def _package_and_tests(paths: Collection[str], protected: Collection[str]) -> str:
    if len(paths) > _MAX_SOURCE_PATHS or any(
        type(path) is not str or len(path) > 512 or not _PATH.fullmatch(path)
        or any(part in {".", ".."} for part in path.split("/"))
        for path in paths
    ):
        raise StandardPythonIntentError("python_original_source_scope_unsupported")
    sources = frozenset(paths)
    if {"setup.py", "setup.cfg"} & sources:
        raise StandardPythonIntentError("python_original_build_backend_unsupported")
    packages = {
        parts[1] for path in sources
        if len(parts := path.split("/")) == 3 and parts[0] == "src" and parts[-1] == "__init__.py"
    }
    if len(packages) != 1:
        raise StandardPythonIntentError("python_original_package_ambiguous")
    module = next(iter(packages))
    if not _identifier(module):
        raise StandardPythonIntentError("python_original_package_ambiguous")
    test_files = {path for path in sources if path.startswith("tests/")}
    if not test_files <= set(protected) or "pyproject.toml" not in protected:
        raise StandardPythonIntentError("python_original_tests_unbound")
    tests = [
        path.split("/") for path in test_files
        if path.rsplit("/", 1)[-1].startswith("test") and path.endswith(".py")
    ]
    if not tests or any(
        not _identifier(parts[-1][:-3]) or any(not _identifier(part) for part in parts[1:-1])
        or any("/".join(parts[:index]) + "/__init__.py" not in sources for index in range(2, len(parts)))
        for parts in tests
    ):
        raise StandardPythonIntentError("python_original_tests_unbound")
    return module


def _entrypoints(value: object, module: str, sources: Collection[str]) -> tuple[tuple[str, str], ...]:
    code = "python_original_entrypoint_unsupported"
    items = _table(value, code)
    if len(items) > 16:
        raise StandardPythonIntentError(code)
    result = []
    for name, target in sorted(items.items()):
        if _DISTRIBUTION.fullmatch(name) is None or type(target) is not str or len(target) > 255:
            raise StandardPythonIntentError(code)
        target_module, sep, function = target.partition(":")
        parts = target_module.split(".")
        if not sep or parts[0] != module or not all(_identifier(part) for part in (*parts, function)):
            raise StandardPythonIntentError(code)
        path = "src/" + "/".join(parts)
        if path + ".py" not in sources and path + "/__init__.py" not in sources:
            raise StandardPythonIntentError(code)
        result.append((name, target))
    return tuple(result)


def standard_python_intent(
    manifest: bytes, *, paths: Collection[str], protected: Collection[str],
) -> StandardPythonIntent | None:
    """Classify bounded setuptools PEP 621 metadata, with no writes or dependency IO.

    Tool-only pyproject files and the explicit native recipe use other paths.
    The result describes what to plan; only trusted resolution can supply a lock.
    """
    if type(manifest) is not bytes or not 0 < len(manifest) <= _MAX_MANIFEST_BYTES:
        raise StandardPythonIntentError("python_original_metadata_limit")
    try:
        data = tomllib.loads(manifest.decode("utf-8-sig"))
    except (UnicodeError, tomllib.TOMLDecodeError, RecursionError):
        raise StandardPythonIntentError("python_original_metadata_invalid") from None
    if "project" not in data and "build-system" not in data:
        return None
    tools = _table(data.get("tool", {}), "python_original_metadata_invalid")
    agent = tools.get("agent-hub")
    if isinstance(agent, dict) and "python-project" in agent:
        return None
    code = "python_original_build_backend_unsupported"
    build = _table(data.get("build-system"), code)
    if set(build) != {"build-backend", "requires"} or build.get("build-backend") != "setuptools.build_meta":
        raise StandardPythonIntentError(code)
    setuptools = _table(tools.get("setuptools", {}), code)
    if (set(setuptools) - {"packages", "package-dir"}
            or setuptools.get("package-dir", {"": "src"}) != {"": "src"}
            or setuptools.get("packages", {"find": {"where": ["src"]}}) != {"find": {"where": ["src"]}}):
        raise StandardPythonIntentError(code)
    project = _table(data.get("project"), "python_original_metadata_invalid")
    if (project.get("dynamic", []) != [] or project.get("optional-dependencies", {}) != {}
            or "dependency-groups" in data or {"gui-scripts", "entry-points"} & project.keys()
            or {"poetry", "pdm", "uv"} & tools.keys()):
        raise StandardPythonIntentError("python_original_metadata_dynamic")
    name, version, python = project.get("name"), project.get("version"), project.get("requires-python", "")
    if (type(name) is not str or _DISTRIBUTION.fullmatch(name) is None
            or type(version) is not str or not 0 < len(version) <= 64
            or type(python) is not str or len(python) > 128):
        raise StandardPythonIntentError("python_original_metadata_invalid")
    try:
        project_version = str(Version(version))
        requires_python = str(SpecifierSet(python))
    except (InvalidSpecifier, InvalidVersion):
        raise StandardPythonIntentError("python_original_metadata_invalid") from None
    module = _package_and_tests(paths, protected)
    build_requires = _requirements(build.get("requires"))
    if not {"setuptools", "wheel"} <= {canonicalize_name(Requirement(value).name) for value in build_requires}:
        raise StandardPythonIntentError(code)
    return StandardPythonIntent(
        project_name=canonicalize_name(name),
        project_version=project_version,
        import_module=module,
        requires_python=requires_python,
        manifest_sha256=hashlib.sha256(manifest).hexdigest(),
        build_requirements=build_requires,
        runtime_requirements=_requirements(project.get("dependencies", [])),
        console_scripts=_entrypoints(project.get("scripts", {}), module, paths),
    )
