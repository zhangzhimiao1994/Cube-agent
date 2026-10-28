from __future__ import annotations

import builtins
import hashlib
import importlib.util
import json
import runpy
import sys
from collections.abc import Callable, Iterator, Mapping
from dataclasses import FrozenInstanceError, replace
from types import MappingProxyType
from typing import Any, cast
from uuid import UUID

import pytest

from agent_hub.capabilities import project_contracts as contracts

TENANT = UUID("11111111-1111-4111-8111-111111111111")
RUN = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")


def file_digest(content: bytes, *, executable: bool = False) -> str:
    return hashlib.sha256((b"x\0" if executable else b"f\0") + content).hexdigest()


def source_files() -> dict[str, str]:
    return {
        "src/main.py": file_digest(b"print('original')\n"),
        "tests/test_main.py": file_digest(b"assert original_behavior\n"),
        "pyproject.toml": file_digest(b"[build-system]\n"),
        "src/package.json": file_digest(b"{}\n"),
        "README.md": file_digest(b"Original instructions\n"),
    }


def source_digest(files: Mapping[str, str]) -> str:
    raw = json.dumps(dict(files), sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(raw.encode("ascii")).hexdigest()


def build(**updates: Any) -> contracts.ProjectCheckContract:
    files = source_files()
    arguments: dict[str, Any] = {
        "tenant_id": TENANT, "run_id": RUN, "project_id": "project-a", "session_id": "session-a",
        "input_source_sha256": source_digest(files), "editable_paths": ("src",),
        "source_files": files,
    }
    arguments.update(updates)
    return contracts.build_project_check_contract(**arguments)


@pytest.mark.parametrize("loader", ["runpy", "independent"])
def test_native_runner_loads_the_same_contract_without_application_imports(
    monkeypatch: pytest.MonkeyPatch, loader: str,
) -> None:
    original_import = builtins.__import__

    def stdlib_only(name: str, *args: Any, **kwargs: Any) -> Any:
        assert name.split(".")[0] in sys.stdlib_module_names
        return original_import(name, *args, **kwargs)

    with monkeypatch.context() as patcher:
        patcher.setattr(builtins, "__import__", stdlib_only)
        if loader == "runpy":
            namespace = runpy.run_path(contracts.__file__)
        else:
            spec = importlib.util.spec_from_file_location("native_project_contracts", contracts.__file__)
            assert spec is not None and spec.loader is not None
            module = importlib.util.module_from_spec(spec)
            assert module.__name__ not in sys.modules
            spec.loader.exec_module(module)
            namespace = vars(module)
        factory = cast(Callable[..., contracts.ProjectCheckContract], namespace["build_project_check_contract"])
        files = source_files()
        native = factory(TENANT, RUN, "project-a", "session-a", source_digest(files), ("src",), files)
        native.require_current(files)
        assert native.to_payload() == build().to_payload()


def test_managed_input_round_trips_explicit_json_paths_without_authority() -> None:
    request = contracts.ManagedProjectInput.from_payload({"editable_paths": ["src", "docs"]})
    assert request.schema_version == 1 and request.kind == "managed_workspace"
    assert request.editable_paths == ("docs", "src")
    payload = request.to_payload()
    assert payload == {
        "schema_version": 1, "kind": "managed_workspace", "editable_paths": ["docs", "src"],
    }
    assert contracts.ManagedProjectInput.from_payload(payload) == request
    with pytest.raises(FrozenInstanceError):
        request.editable_paths = ("tests",)  # type: ignore[misc]
    paths = payload["editable_paths"]
    assert isinstance(paths, list)
    paths.append("tests")
    assert request.editable_paths == ("docs", "src")


@pytest.mark.parametrize("value", [
    {}, {"editable_paths": []}, {"editable_paths": ["src"] * 129},
    {"editable_paths": ["src"], "schema_version": True},
    {"editable_paths": ["src"], "schema_version": "1"},
    {"editable_paths": ["src"], "schema_version": 2},
    {"editable_paths": ["src"], "kind": "workspace"},
    {"editable_paths": ["src"], "install_allowed": True},
    {"editable_paths": ["src"], "host_path": "/host"},
    {"editable_paths": ["src"], "approval_id": "invented"},
    {"editable_paths": "src"}, {"editable_paths": {"src"}},
    {"editable_paths": [True]}, {"editable_paths": [1]}, {"editable_paths": [b"src"]},
    [("editable_paths", ["src"])], None, True,
])
def test_managed_input_rejects_missing_coerced_or_authority_fields(value: object) -> None:
    with pytest.raises((TypeError, ValueError)):
        contracts.ManagedProjectInput.from_payload(value)


@pytest.mark.parametrize("path", [
    "", ".", "..", "/src", "C:/src", "C:src", "//server/share", "src\\main.py",
    "src/../tests", "src/./main.py", "src//main.py", "src/", "src/a.", "src/a ",
    " src", "src\n", "src\x00", "src:stream", "src/CON", "aux.txt", "COM1.py", "NUL",
    "src/*", "src/?", "src/%61.py", "src/SHORT~1", "src/\u00e9.py", "src/\u4e2d.py",
    "src/" + "a" * 256,
])
def test_paths_reject_escape_alias_reserved_and_non_ascii(path: str) -> None:
    with pytest.raises((TypeError, ValueError)):
        contracts.ManagedProjectInput.from_payload({"editable_paths": [path]})


@pytest.mark.parametrize("paths", [
    ["src", "src"], ["src", "SRC"], ["src", "src/main.py"],
    ["src/main.py", "src"], ["Src/a.py", "src/b.py"], ["src/a", "src/A/b"],
])
def test_editable_scopes_reject_case_aliases_duplicates_and_prefix_overlap(paths: list[str]) -> None:
    with pytest.raises(ValueError):
        contracts.ManagedProjectInput.from_payload({"editable_paths": paths})


@pytest.mark.parametrize("name", [
    "package.json", "npm-shrinkwrap.json", "package-lock.json", "DELIVERY_MANIFEST.json",
    "pyproject.toml", "requirements.txt", "requirements.lock", "uv.lock", "poetry.lock",
    "setup.py", "setup.cfg", "pytest.ini", "tox.ini", "conftest.py", "AGENTS.md",
])
@pytest.mark.parametrize("prefix", ["", "src/"])
def test_control_files_cannot_be_editable_even_by_case_or_ancestor(name: str, prefix: str) -> None:
    for path in (prefix + name, prefix + name.upper(), prefix + name + "/child"):
        with pytest.raises(ValueError):
            contracts.ManagedProjectInput.from_payload({"editable_paths": [path]})


def test_builder_protects_original_checks_and_nested_control_files() -> None:
    original = source_files()
    contract = build(source_files=original)
    assert dict(contract.protected_files) == {key: value for key, value in original.items()
                                               if key != "src/main.py"}
    assert contract.is_editable("src/main.py")
    assert contract.is_editable("src/new/module.py")
    assert not contract.is_editable("src/package.json")
    assert not contract.is_editable("src/package.json/child")
    assert not contract.is_editable("src2/main.py")
    assert not contract.is_editable("SRC/main.py")
    assert not contract.is_editable("tests/test_main.py")
    original["tests/test_main.py"] = "f" * 64
    assert contract.protected_files["tests/test_main.py"] != "f" * 64
    with pytest.raises(TypeError):
        contract.protected_files["tests/test_main.py"] = "f" * 64  # type: ignore[index]
    with pytest.raises(FrozenInstanceError):
        contract.protected_sha256 = "f" * 64  # type: ignore[misc]


def test_legal_source_edits_additions_and_removal_within_scope_pass() -> None:
    contract = build()
    changed = source_files()
    del changed["src/main.py"]
    changed["src/new.py"] = file_digest(b"fixed application\n", executable=True)
    changed["DELIVERY_MANIFEST.json"] = file_digest(b"runtime metadata\n")
    contract.require_current(changed)
    del changed["DELIVERY_MANIFEST.json"]
    contract.require_current(changed)


@pytest.mark.parametrize("operation", ["modify", "delete", "delete_directory", "mode", "add_check",
                                      "add_config", "add_nested_control", "delivery_alias"])
def test_current_rejects_protected_loss_changes_and_new_glob_checks(operation: str) -> None:
    contract, changed = build(), source_files()
    if operation == "modify":
        changed["tests/test_main.py"] = file_digest(b"assert True\n")
    elif operation == "delete":
        del changed["pyproject.toml"]
    elif operation == "delete_directory":
        changed = {path: digest for path, digest in changed.items() if not path.startswith("tests/")}
    elif operation == "mode":
        changed["tests/test_main.py"] = file_digest(b"assert original_behavior\n", executable=True)
    elif operation == "add_check":
        changed["tests/test_injected.py"] = file_digest(b"skip all tests\n")
    elif operation == "add_config":
        changed["pytest.ini"] = file_digest(b"[pytest]\n")
    elif operation == "add_nested_control":
        changed["src/conftest.py"] = file_digest(b"skip tests\n")
    else:
        changed["delivery_manifest.json"] = file_digest(b"fake metadata\n")
    with pytest.raises(ValueError):
        contract.require_current(changed)


@pytest.mark.parametrize("extra", [
    {"SRC/main.py": "a" * 64}, {"tests": "a" * 64},
    {"tests/test_main.py/child": "a" * 64}, {"Tests/other.py": "a" * 64},
    {"src/new.py": "a" * 64, "src/new.py/child": "b" * 64},
    {"src/Main.py": "a" * 64}, {"../tests.py": "a" * 64},
])
def test_source_file_tree_rejects_case_and_file_directory_aliases(extra: dict[str, str]) -> None:
    changed = {**source_files(), **extra}
    with pytest.raises(ValueError):
        build(source_files=changed, input_source_sha256=source_digest(changed))
    with pytest.raises(ValueError):
        build().require_current(changed)


def test_builder_requires_exact_snapshot_hash_not_caller_claim() -> None:
    with pytest.raises(ValueError):
        build(input_source_sha256="b" * 64)


@pytest.mark.parametrize(("field", "value"), [
    ("tenant_id", str(TENANT)), ("tenant_id", True), ("run_id", str(RUN)),
    ("project_id", "../other"), ("project_id", "Project"), ("session_id", "a/b"),
    ("session_id", ""), ("input_source_sha256", "A" * 64),
    ("input_source_sha256", True), ("editable_paths", ["src"]),
    ("schema_version", True), ("schema_version", 2), ("protected_sha256", "f" * 64),
])
def test_dataclass_constructor_strictly_rejects_invalid_or_rebound_fields(
    field: str, value: object,
) -> None:
    changes: dict[str, Any] = {field: value}
    with pytest.raises((TypeError, ValueError)):
        replace(build(), **changes)


def test_canonical_binding_is_order_independent_and_scope_sensitive() -> None:
    files = source_files()
    first = build(editable_paths=("src", "docs"))
    second = build(editable_paths=("docs", "src"), source_files=dict(reversed(tuple(files.items()))))
    assert first.protected_sha256 == second.protected_sha256
    assert first.to_payload() == second.to_payload()
    for update in ({"tenant_id": RUN}, {"run_id": TENANT}, {"project_id": "project-b"},
                   {"session_id": "session-b"}, {"editable_paths": ("src",)}):
        assert build(**update).protected_sha256 != first.protected_sha256
    files["src/main.py"] = file_digest(b"different original input\n")
    assert build(source_files=files, input_source_sha256=source_digest(files)).protected_sha256 != build().protected_sha256


def test_payload_round_trip_detaches_mutable_data_and_preserves_binding() -> None:
    contract = build()
    payload = contract.to_payload()
    assert set(payload) == {
        "schema_version", "tenant_id", "run_id", "project_id", "session_id", "input_source_sha256",
        "editable_paths", "protected_files", "protected_sha256",
    }
    assert contracts.ProjectCheckContract.from_payload(json.loads(json.dumps(payload))) == contract
    assert contracts.ProjectCheckContract.from_payload(MappingProxyType(payload)) == contract
    protected = payload["protected_files"]
    assert isinstance(protected, dict)
    protected.clear()
    assert contract.protected_files
    with pytest.raises(ValueError):
        contracts.ProjectCheckContract.from_payload(payload)


@pytest.mark.parametrize(("field", "value"), [
    ("install_allowed", True), ("host_path", "/private"), ("receipt", {"status": "passed"}),
    ("schema_version", True), ("schema_version", "1"), ("schema_version", 2),
    ("tenant_id", TENANT), ("tenant_id", str(TENANT).replace("-", "")),
    ("run_id", str(RUN).upper()), ("input_source_sha256", "b" * 64),
    ("protected_files", {}), ("protected_files", {"src/main.py": "a" * 64}),
    ("protected_files", {"DELIVERY_MANIFEST.json": "a" * 64}),
    ("editable_paths", ["tests"]), ("protected_sha256", "A" * 64),
])
def test_payload_rejects_tampering_coercion_unknown_fields_and_claimed_authority(
    field: str, value: object,
) -> None:
    payload = build().to_payload()
    payload[field] = value
    with pytest.raises((TypeError, ValueError)):
        contracts.ProjectCheckContract.from_payload(payload)


class DuplicateMapping(Mapping[str, object]):
    def __init__(self, values: Mapping[str, object] | None = None) -> None:
        self._values = {"editable_paths": ["src"]} if values is None else values

    def __iter__(self) -> Iterator[str]:
        return iter((*self._values, next(iter(self._values))))

    def __len__(self) -> int:
        return len(self._values) + 1

    def __getitem__(self, key: str) -> object:
        return self._values[key]


def test_duplicate_structural_mapping_and_pair_lists_are_not_collapsed() -> None:
    with pytest.raises(ValueError):
        contracts.ManagedProjectInput.from_payload(DuplicateMapping())
    with pytest.raises((TypeError, ValueError)):
        contracts.ProjectCheckContract.from_payload([("schema_version", 1), ("schema_version", 1)])
    payload = build().to_payload()
    with pytest.raises(ValueError):
        contracts.ProjectCheckContract.from_payload(DuplicateMapping(payload))
    protected = payload["protected_files"]
    assert isinstance(protected, dict)
    payload["protected_files"] = DuplicateMapping(protected)
    with pytest.raises(ValueError):
        contracts.ProjectCheckContract.from_payload(payload)


def test_runtime_delivery_manifest_is_excluded_only_at_exact_root_spelling() -> None:
    files = source_files()
    files["DELIVERY_MANIFEST.json"] = file_digest(b"runtime owned\n")
    contract = build(source_files=files, input_source_sha256=source_digest(files))
    assert "DELIVERY_MANIFEST.json" not in contract.protected_files
    contract.require_current(source_files())
    assert not contract.is_editable("DELIVERY_MANIFEST.json")
    assert not contract.is_editable("src/DELIVERY_MANIFEST.json")


@pytest.mark.parametrize("path", ["../escape", "src\\main.py", "CON", "src/package.json", True])
def test_membership_never_expands_invalid_or_control_paths(path: object) -> None:
    assert build().is_editable(path) is False


def test_explicit_path_soft_count_and_disjoint_scopes() -> None:
    paths = [f"source_{index}" for index in range(128)]
    assert len(contracts.ManagedProjectInput.from_payload({"editable_paths": paths}).editable_paths) == 128
    expanded = contracts.ManagedProjectInput.from_payload({"editable_paths": [*paths, "source_128"]})
    assert len(expanded.editable_paths) == 129
    assert contracts.ManagedProjectInput(("src", "src2")).editable_paths == ("src", "src2")


@pytest.mark.parametrize("value", ["", "A" * 64, "a" * 63, True, 1, None, b"a" * 64])
def test_source_file_digests_are_exact_lowercase_sha_not_streams_or_coerced_values(value: object) -> None:
    files: dict[str, Any] = {**source_files(), "src/main.py": value}
    with pytest.raises(ValueError):
        build(source_files=files)
    with pytest.raises(ValueError):
        build().require_current(files)


@pytest.mark.parametrize("field", [
    "schema_version", "tenant_id", "run_id", "project_id", "session_id", "input_source_sha256",
    "editable_paths", "protected_files", "protected_sha256",
])
def test_persisted_contract_requires_every_binding_field(field: str) -> None:
    payload = build().to_payload()
    del payload[field]
    with pytest.raises(ValueError):
        contracts.ProjectCheckContract.from_payload(payload)


def test_empty_protected_set_is_preservation_only_not_an_acceptance_receipt() -> None:
    files = {"src/main.py": file_digest(b"original\n")}
    contract = build(source_files=files, input_source_sha256=source_digest(files))
    assert not contract.protected_files
    contract.require_current({"src/repaired.py": file_digest(b"repaired\n")})
    with pytest.raises(ValueError):
        contract.require_current({"tests/new_check.py": file_digest(b"assert True\n")})
    assert "status" not in contract.to_payload()


@pytest.mark.parametrize(("field", "value"), [
    ("protected_files", {"src/main.py": "a" * 64}),
    ("protected_files", {"DELIVERY_MANIFEST.json": "a" * 64}),
    ("protected_files", {"../outside.py": "a" * 64}),
    ("protected_files", {"tests": "a" * 64, "tests/check.py": "b" * 64}),
    ("protected_files", {"tests/a.py": "a" * 64, "Tests/b.py": "b" * 64}),
    ("protected_files", {"SRC/pyproject.toml": "a" * 64}),
    ("protected_files", {"src": "a" * 64}),
    ("editable_paths", ["src", "src/child"]),
    ("editable_paths", ["package.json"]),
    ("editable_paths", ["SRC/a", "src/b"]),
])
def test_rehashed_structural_forgery_is_not_rejected_only_by_digest_mismatch(
    field: str, value: object,
) -> None:
    payload = build().to_payload()
    payload[field] = value
    binding = {key: item for key, item in payload.items() if key != "protected_sha256"}
    canonical = json.dumps(binding, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    payload["protected_sha256"] = hashlib.sha256(canonical.encode("ascii")).hexdigest()
    with pytest.raises(ValueError):
        contracts.ProjectCheckContract.from_payload(payload)
