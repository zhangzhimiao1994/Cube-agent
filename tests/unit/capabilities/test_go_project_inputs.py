"""Original Go recipe binding; fake SDK identities are not native measurements."""

from __future__ import annotations

import hashlib
import importlib
import importlib.util
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pytest

from agent_hub.capabilities.project_baselines import ProjectBaselineReference, StoredProjectBaseline
from agent_hub.capabilities.project_contracts import build_project_check_contract
from agent_hub.skills.sandbox.toolchain_catalog import ToolchainPlanScope
from agent_hub.skills.sandbox.toolchain_protocol import ToolchainReference

MODULE = "agent_hub.capabilities.go_project_inputs"


def encoded(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def test_separate_go_execution_inputs_exist() -> None:
    assert importlib.util.find_spec(MODULE) is not None


@pytest.fixture
def api() -> Any:
    if importlib.util.find_spec(MODULE) is None:
        pytest.skip("Go inputs missing; existence test is RED")
    return importlib.import_module(MODULE)


def case(
    tmp_path: Path,
    *,
    editable: tuple[str, ...] = ("main.go",),
    changes: dict[str, bytes] | None = None,
) -> Any:
    raw = {
        "go.mod": b"module example.org/demo\ngo 1.27\n",
        "main.go": b"package main\nfunc main() {}\n",
        "main_test.go": b'package main\nimport "testing"\nfunc TestMainLogic(t *testing.T) {}\n',
    }
    raw.update(changes or {})
    files = {name: hashlib.sha256(b"f\0" + data).hexdigest() for name, data in raw.items()}
    source = hashlib.sha256(encoded(files)).hexdigest()
    contract = build_project_check_contract(
        UUID(int=1), UUID(int=3), "demo", "session", source, editable, files
    )
    envelope = {"schema_version": 1, "contract": contract.to_payload(), "source_files": files}
    reference = ProjectBaselineReference(
        hashlib.sha256(encoded(envelope)).hexdigest(), source, len(files)
    )
    for name, data in raw.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    stored = StoredProjectBaseline(reference, contract, tmp_path, files)
    scope = ToolchainPlanScope(
        tenant_id=UUID(int=1),
        user_id=UUID(int=2),
        run_id=UUID(int=3),
        project_id="demo",
        session_id="session",
        actor_id="main_agent",
        actor_role="operator",
        source_sha256=source,
        contract_sha256=reference.contract_sha256,
    )
    sdk = ToolchainReference(
        artifact_sha256="a" * 64,
        archive_sha256="b" * 64,
        tree_sha256="c" * 64,
        binary_sha256="d" * 64,
        gofmt_sha256="e" * 64,
        total_bytes=1234,
        file_count=100,
        version="1.27.3",
        platform="linux_amd64",
    )
    return SimpleNamespace(raw=raw, files=files, stored=stored, scope=scope, sdk=sdk)


def bind(api: Any, value: Any) -> Any:
    return api.bind_go_project_inputs(
        stored=value.stored,
        scope=value.scope,
        toolchain=value.sdk,
        source_limit_bytes=1024**2,
        file_limit=100,
    )


def test_real_envelope_and_snapshot_bind_to_exact_recipe(api: Any, tmp_path: Path) -> None:
    value = case(tmp_path)
    inputs = bind(api, value)
    assert inputs.original_reference.contract_sha256 != inputs.contract.protected_sha256
    assert inputs.scope == value.scope and inputs.toolchain == value.sdk
    assert api.GoProjectInputs.from_payload(inputs.to_payload()) == inputs
    assert inputs.to_payload()["kind"] == "go_stdlib_cli_v1"
    assert inputs.to_payload()["dependency_policy"] == "stdlib_only"
    assert inputs.to_payload()["external_dependencies"] == []
    assert inputs.to_payload()["build_target"] == "."
    assert inputs.to_payload()["test_target"] == "./..."
    with pytest.raises(TypeError):
        inputs.source_files["main.go"] = "0" * 64


@pytest.mark.parametrize(
    "field,changed",
    [
        ("tenant_id", UUID(int=10)),
        ("run_id", UUID(int=10)),
        ("source_sha256", "f" * 64),
        ("contract_sha256", "f" * 64),
        ("project_id", "other"),
        ("session_id", "other"),
        ("actor_id", "other"),
        ("actor_role", "viewer"),
    ],
)
def test_scope_cannot_rebind_original(
    api: Any, tmp_path: Path, field: str, changed: object
) -> None:
    value = case(tmp_path)
    value.scope = value.scope.model_copy(update={field: changed})
    with pytest.raises(ValueError):
        bind(api, value)


@pytest.mark.parametrize("editable", [("go.mod",), ("main_test.go",), ("tests", "main.go")])
def test_go_controls_and_tests_must_be_protected(
    api: Any, tmp_path: Path, editable: tuple[str, ...]
) -> None:
    value = case(tmp_path, editable=editable, changes={"tests/logic_test.go": b"package logic\n"})
    with pytest.raises(ValueError, match="go_project_protected_input_changed"):
        bind(api, value)


def test_current_source_can_repair_code_but_not_tests_or_controls(api: Any, tmp_path: Path) -> None:
    value = case(tmp_path, editable=("src",), changes={"src/main.go": b"package main\n"})
    inputs = bind(api, value)
    changed = dict(value.files) | {"src/main.go": "1" * 64, "src/new.go": "2" * 64}
    inputs.require_current(changed)
    for bad in (
        changed | {"main_test.go": "3" * 64},
        changed | {"src/added_test.go": "3" * 64},
        changed | {"src/go.mod": "3" * 64},
        changed | {"src/setup.py": "3" * 64},
    ):
        with pytest.raises(ValueError):
            inputs.require_current(bad)


def test_candidate_must_fit_its_own_approved_file_budget(api: Any, tmp_path: Path) -> None:
    value = case(tmp_path, editable=("src",), changes={"src/main.go": b"package main\n"})
    inputs = replace(bind(api, value), file_limit=len(value.files))
    inputs.require_current(value.files)
    with pytest.raises(ValueError):
        inputs.require_current(value.files | {"src/extra.go": "1" * 64})


@pytest.mark.parametrize('path', ['native.s', 'native.S', 'native.syso', 'native.SYSO',
                                 'extra.c', 'extra.cpp', 'extra.h', 'extra.swigcxx'])
def test_candidate_cannot_add_native_build_inputs_even_inside_editable_paths(
    api: Any, tmp_path: Path, path: str,
) -> None:
    value = case(tmp_path, editable=('main.go', 'native'), changes={})
    inputs = bind(api, value)
    with pytest.raises(ValueError, match='go_native_source_unsupported'):
        inputs.require_current(value.files | {'native/' + path: '1' * 64})


def test_input_constructor_rejects_native_paths_even_with_forged_declaration(
    api: Any, tmp_path: Path,
) -> None:
    clean = bind(api, case(tmp_path / 'clean'))
    value = case(tmp_path / 'native', changes={'native.syso': b'object'},
                 editable=('main.go', 'native.syso'))
    with pytest.raises(ValueError, match='go_native_source_unsupported'):
        api.GoProjectInputs(scope=value.scope, contract=value.stored.contract,
            original_reference=value.stored.reference, source_files=value.files,
            declaration=clean.declaration, toolchain=value.sdk,
            source_limit_bytes=1024**2, file_limit=100)


@pytest.mark.parametrize('malformed', [float('nan'), object()])
def test_approval_match_rejects_noncanonical_arguments_without_throwing(
    api: Any, tmp_path: Path, malformed: object,
) -> None:
    inputs = bind(api, case(tmp_path))
    request = inputs.approval_request(user_id=UUID(int=2), agent_id='main_agent',
                                      idempotency_key='go-check')
    request = request.model_copy(update={'arguments': request.arguments | {'extra': malformed}})
    assert inputs.matches_approval_request(request) is False


@pytest.mark.parametrize(
    "changed",
    [
        {"go.mod": b"module example.org/demo\ngo 1.28\n"},
        {"go.mod": b"module example.org/demo\ngo 1.27\ntoolchain go1.27.4\n"},
        {"go.mod": b"module example.org/demo\ngo 1.27\nrequire example.org/dep v1.0.0\n"},
        {"other/go.mod": b"module example.org/other\ngo 1.27\n"},
        {"main.go": b'package main\nimport "C"\nfunc main(){}\n'},
        {"package.json": b"{}"},
    ],
)
def test_incompatible_sdk_or_unsupported_source_is_not_an_execution_recipe(
    api: Any,
    tmp_path: Path,
    changed: dict[str, bytes],
) -> None:
    with pytest.raises(ValueError):
        bind(api, case(tmp_path, changes=changed))


def test_snapshot_mutation_and_wrong_baseline_hash_are_rejected(api: Any, tmp_path: Path) -> None:
    value = case(tmp_path)
    (tmp_path / "main.go").write_bytes(b"changed")
    with pytest.raises(ValueError):
        bind(api, value)
    (tmp_path / "main.go").write_bytes(value.raw["main.go"])
    ref = replace(value.stored.reference, contract_sha256=value.stored.contract.protected_sha256)
    value.scope = value.scope.model_copy(update={"contract_sha256": ref.contract_sha256})
    value.stored = replace(value.stored, reference=ref)
    with pytest.raises(ValueError):
        bind(api, value)


def test_recipe_approval_is_separate_from_sdk_supply_and_user_bound(
    api: Any, tmp_path: Path
) -> None:
    value = case(tmp_path)
    inputs = bind(api, value)
    request = inputs.approval_request(
        user_id=UUID(int=2), agent_id="main_agent", idempotency_key="go-check"
    )
    assert request.resource == "workspace/current/project-verification/go/" + inputs.sha256
    assert inputs.matches_approval_request(request)
    for change in (
        {"user_id": UUID(int=4)},
        {"agent_id": "other"},
        {"resource": "workspace/current/project-inputs/toolchain/" + inputs.sha256},
        {"arguments": request.arguments | {"install_allowed": True}},
    ):
        assert not inputs.matches_approval_request(request.model_copy(update=change))
    with pytest.raises(ValueError):
        inputs.approval_request(user_id=UUID(int=4), agent_id="main_agent", idempotency_key="no")


def test_payload_cannot_change_policy_or_digests(api: Any, tmp_path: Path) -> None:
    inputs = bind(api, case(tmp_path))
    for change in (
        {"schema_version": True},
        {"kind": "python_offline_wheel_v1"},
        {"build_target": "../other"},
        {"test_target": "-run=None"},
        {"install_allowed": True},
        {"recipe_sha256": "a" * 64},
        {"external_dependencies": ["unsafe"]},
        {"dependency_policy": "online"},
        {"environment_input_sha256": "f" * 64},
    ):
        with pytest.raises(ValueError):
            api.GoProjectInputs.from_payload(inputs.to_payload() | change)


def acceptance_payload() -> dict[str, Any]:
    return {
        "schema_version": 2, "stack": "go", "editable_paths": ["main.go"],
        "test_files": ["main_test.go"],
        "criteria": [{"id": "AC-001", "description": "CLI result",
                      "tests": ["example.org/demo::TestMainLogic"]}],
        "business": {"schema_version": 1, "kind": "go_cli_business_v1", "cases": [
            {"id": "ok", "criterion_id": "AC-001", "argv": [], "stdin": "",
             "expected_stdout": "ok\n", "expected_stderr": "", "expected_exit": 0},
            {"id": "bad", "criterion_id": "AC-001", "argv": ["bad"], "stdin": "",
             "expected_stdout": "", "expected_stderr": "bad\n", "expected_exit": 2},
        ]},
    }


def test_original_acceptance_is_bound_in_versioned_recipe(api: Any, tmp_path: Path) -> None:
    payload = acceptance_payload()
    value = case(tmp_path, changes={"ACCEPTANCE.json": encoded(payload)})
    inputs = bind(api, value)
    assert inputs.acceptance.to_payload() == payload
    assert inputs.business == inputs.acceptance.business
    assert inputs.to_payload()["schema_version"] == 2
    assert inputs.to_payload()["kind"] == "go_stdlib_cli_business_v1"
    assert inputs.to_payload()["acceptance"] == payload
    assert api.GoProjectInputs.from_payload(inputs.to_payload()) == inputs
    assert inputs.matches_approval_request(inputs.approval_request(
        user_id=UUID(int=2), agent_id="main_agent", idempotency_key="business"))
    with pytest.raises(ValueError):
        replace(inputs, acceptance=None)
    changed = dict(value.files) | {"main.go": "1" * 64}
    inputs.require_current(changed)
    with pytest.raises(ValueError):
        inputs.require_current(changed | {"ACCEPTANCE.json": "2" * 64})
    with pytest.raises(ValueError):
        inputs.require_current(changed | {"nested/ACCEPTANCE.json": "2" * 64})


def test_legacy_payload_and_recipe_are_byte_compatible(api: Any, tmp_path: Path) -> None:
    inputs = bind(api, case(tmp_path))
    assert inputs.acceptance is None and inputs.business is None
    payload = inputs.to_payload()
    assert "acceptance" not in payload
    assert set(payload) == {"schema_version", "kind", "scope", "contract", "original_reference",
        "source_files", "declaration", "toolchain", "source_limit_bytes", "file_limit",
        "dependency_policy", "external_dependencies", "build_target", "test_target",
        "recipe_sha256", "environment_input_sha256"}
    binding = {key: value for key, value in payload.items()
               if key not in {"recipe_sha256", "environment_input_sha256"}}
    assert inputs.sha256 == hashlib.sha256(encoded(binding)).hexdigest()
    assert payload["schema_version"] == 1 and payload["kind"] == "go_stdlib_cli_v1"


@pytest.mark.parametrize("change", [
    {"schema_version": 1}, {"stack": "node"}, {"unknown": True},
    {"editable_paths": ["other.go"]}, {"test_files": ["absent_test.go"]},
    {"criteria": [{"id": "AC-001", "description": "outside module",
                   "tests": ["example.org/demonstration::TestMainLogic"]}]},
])
def test_bad_original_manifest_cannot_fall_back_to_diagnostics(
    api: Any, tmp_path: Path, change: dict[str, object],
) -> None:
    with pytest.raises(ValueError):
        bind(api, case(tmp_path, changes={"ACCEPTANCE.json": encoded(acceptance_payload() | change)}))


def test_manifest_must_be_protected_and_cover_every_original_test(api: Any, tmp_path: Path) -> None:
    raw = encoded(acceptance_payload())
    with pytest.raises(ValueError):
        bind(api, case(tmp_path / "unprotected", editable=("main.go", "ACCEPTANCE.json"),
                       changes={"ACCEPTANCE.json": raw}))
    with pytest.raises(ValueError):
        bind(api, case(tmp_path / "unlisted", changes={"ACCEPTANCE.json": raw,
                       "extra_test.go": b"package main\n"}))


def test_payload_cannot_downgrade_or_detach_business_manifest(api: Any, tmp_path: Path) -> None:
    inputs = bind(api, case(tmp_path, changes={"ACCEPTANCE.json": encoded(acceptance_payload())}))
    payload = inputs.to_payload()
    for changed in (
        payload | {"schema_version": 1}, payload | {"kind": "go_stdlib_cli_v1"},
        payload | {"acceptance": None}, payload | {"approved": True},
        {key: value for key, value in payload.items() if key != "acceptance"},
    ):
        with pytest.raises(ValueError):
            api.GoProjectInputs.from_payload(changed)


def test_sealed_manifest_mutation_is_rejected(api: Any, tmp_path: Path) -> None:
    value = case(tmp_path, changes={"ACCEPTANCE.json": encoded(acceptance_payload())})
    (tmp_path / "ACCEPTANCE.json").write_bytes(b"{}")
    with pytest.raises(ValueError):
        bind(api, value)


def test_explicit_caller_budget_is_enforced_for_bind_and_payload(api: Any, tmp_path: Path) -> None:
    from agent_hub.capabilities.go_project_business import GoBusinessLimits

    value = case(tmp_path, changes={"ACCEPTANCE.json": encoded(acceptance_payload())})
    inputs = bind(api, value)
    limits = GoBusinessLimits(max_cases=1, max_json_bytes=1024**2,
                              max_stream_bytes=1024**2, max_argv_bytes=1024**2)
    with pytest.raises(ValueError):
        api.bind_go_project_inputs(stored=value.stored, scope=value.scope, toolchain=value.sdk,
                                   source_limit_bytes=1024**2, file_limit=100, limits=limits)
    with pytest.raises(ValueError):
        api.GoProjectInputs.from_payload(inputs.to_payload(), limits=limits)


def test_recipe_binds_all_expectations_and_case_order_without_attesting_raw_bytes(
    api: Any, tmp_path: Path,
) -> None:
    value = case(tmp_path, changes={"ACCEPTANCE.json": encoded(acceptance_payload())})
    inputs = bind(api, value)
    request = inputs.approval_request(user_id=UUID(int=2), agent_id="main_agent",
                                      idempotency_key="original")
    for manifest in (
        replace(inputs.acceptance, criteria=(replace(inputs.acceptance.criteria[0],
                                                     description="different"),)),
        replace(inputs.acceptance, business=replace(inputs.business, cases=tuple(
            reversed(inputs.business.cases)))),
        replace(inputs.acceptance, business=replace(inputs.business, cases=(
            replace(inputs.business.cases[0], expected_stdout="different"),
            inputs.business.cases[1]))),
    ):
        changed = replace(inputs, acceptance=manifest)
        assert changed.sha256 != inputs.sha256
        assert changed.environment_input_sha256 == inputs.environment_input_sha256
        assert not changed.matches_approval_request(request)
        with pytest.raises(ValueError):
            api.GoProjectInputs.from_payload(inputs.to_payload() | {"acceptance": manifest.to_payload()})
        # A self-consistent payload validates data, not authenticity of the raw
        # original: Root must independently rebuild the sealed recipe below.
        assert api.GoProjectInputs.from_payload(changed.to_payload()) == changed
        assert bind(api, value).sha256 == inputs.sha256


def test_named_subpackage_can_bind_but_must_stay_inside_the_module(api: Any, tmp_path: Path) -> None:
    payload = acceptance_payload()
    payload["criteria"][0]["tests"] = ["example.org/demo/logic::TestLogic"]
    payload["test_files"].append("logic/logic_test.go")
    value = case(tmp_path, changes={"ACCEPTANCE.json": encoded(payload),
                                    "logic/logic_test.go": b"package logic\n"})
    inputs = bind(api, value)
    assert inputs.acceptance.test_ids == ("example.org/demo/logic::TestLogic",)
