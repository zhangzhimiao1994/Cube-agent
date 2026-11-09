"""The ordinary Python fixture must yield intent without Harness control files."""

import hashlib

import pytest
from packaging.requirements import Requirement

from agent_hub.capabilities.python_original_metadata import (
    StandardPythonIntentError,
    standard_python_intent,
)

MANIFEST = b"""
[build-system]
requires = ["setuptools>=68", "wheel"]
build-backend = "setuptools.build_meta"

[project]
name = "discuss-expense-cli"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = ["click>=8.1,<9"]

[project.scripts]
expense-report = "expense_cli.cli:main"

[tool.setuptools.packages.find]
where = ["src"]
"""
FILES = (
    "README.md", "cases/input.csv", "pyproject.toml", "src/expense_cli/__init__.py",
    "src/expense_cli/cli.py", "tests/test_contract.py",
)
PROTECTED = frozenset(FILES) - {"src/expense_cli/cli.py"}


def test_standard_manifest_preserves_requirements_and_original_binding() -> None:
    intent = standard_python_intent(MANIFEST, paths=FILES, protected=PROTECTED)
    assert intent is not None
    assert intent.project_name == "discuss-expense-cli"
    assert intent.project_version == "0.1.0"
    assert intent.import_module == "expense_cli"
    assert intent.manifest_sha256 == hashlib.sha256(MANIFEST).hexdigest()
    assert Requirement(intent.runtime_requirements[0]) == Requirement("click>=8.1,<9")
    assert set(intent.build_requirements) == {"setuptools>=68", "wheel"}
    assert intent.test_directory == "tests"
    assert intent.console_scripts == (("expense-report", "expense_cli.cli:main"),)
    assert intent.requires_python == ">=3.11"
    assert intent.supports_interpreter("3.12.8")
    assert not intent.supports_interpreter("3.10.0")


def test_tool_only_pyproject_is_not_a_python_package() -> None:
    assert standard_python_intent(
        b"[tool.ruff]\nline-length = 100\n",
        paths=("package.json", "pyproject.toml", "src/main.ts"),
        protected=frozenset({"package.json", "pyproject.toml"}),
    ) is None


@pytest.mark.parametrize(
    ("manifest", "paths", "protected", "code"),
    (
        (MANIFEST.replace(b"click>=8.1,<9", b"click @ https://example.test/click.whl"),
         FILES, PROTECTED, "python_original_unsafe_requirement"),
        (MANIFEST, FILES + ("src/second/__init__.py",), PROTECTED, "python_original_package_ambiguous"),
        (MANIFEST, FILES, PROTECTED - {"tests/test_contract.py"}, "python_original_tests_unbound"),
        (MANIFEST.replace(b"expense_cli.cli:main", b"outside.cli:main"),
         FILES, PROTECTED, "python_original_entrypoint_unsupported"),
        (MANIFEST.replace(b"setuptools.build_meta", b"custom.build"),
         FILES, PROTECTED, "python_original_build_backend_unsupported"),
    ),
)
def test_unsupported_standard_forms_fail_without_granting_authority(
    manifest: bytes, paths: tuple[str, ...], protected: frozenset[str], code: str,
) -> None:
    with pytest.raises(StandardPythonIntentError, match=code):
        standard_python_intent(manifest, paths=paths, protected=protected)
