"""Go v1 parses a conservative declaration subset without running Go."""

import hashlib
import importlib
import importlib.util
from dataclasses import FrozenInstanceError, replace
from typing import Any

import pytest

MANIFEST = b"module example.org/billing\n\ngo 1.23.2\n"


@pytest.fixture
def recipe() -> Any:
    name = "agent_hub.capabilities.go_project_recipe"
    assert importlib.util.find_spec(name) is not None, "Go declaration parser missing"
    return importlib.import_module(name)


def test_declaration_binds_raw_controls_without_selecting_toolchain(recipe: Any) -> None:
    result = recipe.parse_go_project_declaration({"go.mod": MANIFEST, "go.sum": b""})
    assert result.module_path == "example.org/billing"
    assert result.go_version == "1.23.2"
    assert result.toolchain is None
    assert result.kind == "go_stdlib_singlemodule_v1"
    assert result.manifest_sha256 == hashlib.sha256(MANIFEST).hexdigest()
    assert result.sum_sha256 == hashlib.sha256(b"").hexdigest()
    assert result.external_modules == ()
    assert result.control_paths == ("go.mod", "go.sum")
    assert result.to_payload()["dependency_policy"] == "stdlib_only"
    assert recipe.GoProjectDeclaration.from_payload(result.to_payload()) == result
    with pytest.raises(FrozenInstanceError):
        result.go_version = "1.24"


def test_semantic_constraints_preserve_raw_digest_and_toolchain_is_only_data(recipe: Any) -> None:
    text = b'// declaration\nmodule "example.org/billing" // no command\ngo 1.23.2\n'
    first = recipe.parse_go_project_declaration({"go.mod": text})
    second = recipe.parse_go_project_declaration({"go.mod": text.replace(b"\n", b"\r\n")})
    assert first.module_path == second.module_path
    assert first.manifest_sha256 != second.manifest_sha256
    assert first.sha256 != second.sha256
    assert first.sum_sha256 is None
    pinned = recipe.parse_go_project_declaration({
        "go.mod": MANIFEST + b"toolchain go1.24.1\n",
    })
    assert pinned.toolchain == "go1.24.1"
    assert "environment_ready" not in pinned.to_payload()


@pytest.mark.parametrize("manifest", [
    b"", b"\xff", b"module example.org/billing\n", b"go 1.23\n",
    MANIFEST + b"module example.org/other\n", MANIFEST + b"go 1.24\n",
    MANIFEST + b"toolchain go1.24.1\ntoolchain go1.24.2\n",
    MANIFEST + b"garbage anything\n", MANIFEST + b"go 1.23; go 1.24\n",
    b"module ../outside\ngo 1.23\n", b"module /absolute\ngo 1.23\n",
    b"module https://example.org/billing\ngo 1.23\n",
    b"module example.org//billing\ngo 1.23\n",
    b"module example.org/billing\ngo 01.23\n",
    MANIFEST + b"/* unterminated", MANIFEST + b"\x00",
    pytest.param(MANIFEST + b"/*" * 10_000, id="many_unterminated_comments"),
])
def test_malformed_duplicate_and_missing_constraints_are_invalid(recipe: Any, manifest: bytes) -> None:
    with pytest.raises(recipe.GoProjectRecipeError) as error:
        recipe.parse_go_project_declaration({"go.mod": manifest})
    assert error.value.code == "go_declaration_invalid"


@pytest.mark.parametrize("extra,code", [
    (b"require example.org/dep v1.0.0\n", "go_external_modules_unsupported"),
    (b"require (\n)\n", "go_external_modules_unsupported"),
    (b"exclude example.org/dep v1.0.0\n", "go_external_modules_unsupported"),
    (b"replace example.org/dep => ../dep\n", "go_replace_unsupported"),
    (b"replace (\n)\n", "go_replace_unsupported"),
    (b"toolchain auto\n", "go_toolchain_unsupported"),
    (b"toolchain go1.24.1+auto\n", "go_toolchain_unsupported"),
    (b"toolchain ../../go\n", "go_toolchain_unsupported"),
    (b"godebug default=go1.23\n", "go_directive_unsupported"),
    (b"tool example.org/tool\n", "go_directive_unsupported"),
])
def test_out_of_recipe_directives_have_explicit_unsupported_reasons(
    recipe: Any, extra: bytes, code: str,
) -> None:
    with pytest.raises(recipe.GoProjectRecipeError) as error:
        recipe.parse_go_project_declaration({"go.mod": MANIFEST + extra})
    assert error.value.code == code


@pytest.mark.parametrize("path,data,code", [
    ("go.work", b"", "go_workspace_unsupported"),
    ("go.work.sum", b"", "go_workspace_unsupported"),
    ("nested/go.mod", MANIFEST, "go_multiple_modules_unsupported"),
    ("nested/go.work", b"", "go_workspace_unsupported"),
    ("go.sum", b"example.org/dep v1.0.0 h1:abc\n", "go_external_modules_unsupported"),
    ("vendor/modules.txt", b"", "go_external_modules_unsupported"),
    ("main.go", b'package main\nimport "C"\n', "go_cgo_unsupported"),
    ("main.go", b'package main\nimport (\n_ "C"\n)\n', "go_cgo_unsupported"),
    ("main.go", b'package main\nimport `C`\n', "go_cgo_unsupported"),
    ("main.go", b'package main\nimport "runtime/cgo"\n', "go_cgo_unsupported"),
    ("main.go", b'package main\nimport "\\x43"\n', "go_cgo_unsupported"),
    ("main.go", b'package main\nimport "\\103"\n', "go_cgo_unsupported"),
    ("main.go", b'package main\nimport "\\u0043"\n', "go_cgo_unsupported"),
    ("main.go", b'package main\n// #cgo LDFLAGS: -lm\n', "go_cgo_unsupported"),
    ("main.go", b'//go:build cgo\n\npackage main\n', "go_cgo_unsupported"),
    ("main.c", b"", "go_native_source_unsupported"),
    ("main.syso", b"", "go_native_source_unsupported"),
    ("bridge.f90", b"", "go_native_source_unsupported"),
    ("bridge.swigcxx", b"", "go_native_source_unsupported"),
    ("main.go", b'package main\nimport "example.org/dep"\n', "go_external_modules_unsupported"),
    ("main.go", b'package main\nimport "../dep"\n', "go_external_modules_unsupported"),
])
def test_snapshot_intents_block_unsupported_recipe(
    recipe: Any, path: str, data: bytes, code: str,
) -> None:
    with pytest.raises(recipe.GoProjectRecipeError) as error:
        recipe.parse_go_project_declaration({"go.mod": MANIFEST, path: data})
    assert error.value.code == code


def test_comments_strings_and_own_module_are_not_external_imports(recipe: Any) -> None:
    result = recipe.parse_go_project_declaration({
        "go.mod": MANIFEST,
        "main.go": b'''package main
// import "C"
/* import "example.org/external" */
import (
    "fmt"
    billing "example.org/billing/internal/domain"
)
var example = `import "C"`
''',
    })
    assert result.external_modules == ()


@pytest.mark.parametrize("source", [
    b"package main\n/*" * 2_000,
    b'package main\nimport "unterminated',
], ids=["unclosed-comment", "unclosed-import"])
def test_unterminated_source_tokens_are_not_silently_skipped(recipe: Any, source: bytes) -> None:
    with pytest.raises(recipe.GoProjectRecipeError) as error:
        recipe.parse_go_project_declaration({"go.mod": MANIFEST, "main.go": source})
    assert error.value.code == "go_declaration_invalid"


@pytest.mark.parametrize("kwargs", [
    {"max_control_bytes": 2}, {"max_control_bytes": True}, {"max_files": 0},
    {"max_files": 1}, {"max_source_bytes": 2},
])
def test_input_budgets_cannot_be_bypassed(recipe: Any, kwargs: dict[str, object]) -> None:
    with pytest.raises(recipe.GoProjectRecipeError) as error:
        recipe.parse_go_project_declaration({"go.mod": MANIFEST, "main.go": b"package main"}, **kwargs)
    assert error.value.code == "go_declaration_limit"


def test_missing_root_manifest_is_distinct(recipe: Any) -> None:
    with pytest.raises(recipe.GoProjectRecipeError) as error:
        recipe.parse_go_project_declaration({"main.go": b"package main"})
    assert error.value.code == "go_manifest_missing"


@pytest.mark.parametrize("changes", [
    {"schema_version": True}, {"environment_ready": True}, {"external_modules": ["x"]},
    {"manifest_sha256": "a"}, {"control_paths": ["go.mod", "../outside"]},
    {"dependency_policy": "download"}, {"toolchain": "auto"}, {"go_version": "latest"},
])
def test_declaration_payload_cannot_smuggle_authority_or_new_policy(
    recipe: Any, changes: dict[str, object],
) -> None:
    original = recipe.parse_go_project_declaration({"go.mod": MANIFEST})
    with pytest.raises(recipe.GoProjectRecipeError):
        recipe.GoProjectDeclaration.from_payload({**original.to_payload(), **changes})
    with pytest.raises(recipe.GoProjectRecipeError):
        replace(original, external_modules=("example.org/dep",))
