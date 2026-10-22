"""Go v2 acceptance is inert data; AST matching and execution belong to Root."""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

import pytest

from agent_hub.capabilities.go_project_acceptance import (
    GoAcceptanceCriterion,
    GoAcceptanceManifest,
    parse_go_acceptance_manifest,
)
from agent_hub.capabilities.go_project_business import (
    GoBusinessCase,
    GoBusinessLimits,
    GoBusinessSuite,
)


def limits(**changes: int) -> GoBusinessLimits:
    return GoBusinessLimits(**({
        "max_cases": 2000, "max_json_bytes": 1024**2,
        "max_stream_bytes": 1024**2, "max_argv_bytes": 1024**2,
    } | changes))


def manifest() -> GoAcceptanceManifest:
    return GoAcceptanceManifest(
        editable_paths=("main.go",), test_files=("main_test.go",),
        criteria=(GoAcceptanceCriterion("AC-001", "Print a result and reject invalid input",
                                        ("example.org/demo::TestMainLogic",)),),
        business=GoBusinessSuite((
            GoBusinessCase("success", "AC-001", (), "", "ok\n", "", 0),
            GoBusinessCase("invalid", "AC-001", ("--invalid",), "", "", "invalid\n", 2),
        )),
    )


def test_v2_manifest_roundtrip_and_canonical_digest() -> None:
    value = manifest()
    payload = value.to_payload()
    assert payload["schema_version"] == 2 and payload["stack"] == "go"
    assert GoAcceptanceManifest.from_payload(payload, limits=limits()) == value
    assert GoAcceptanceManifest.from_json(json.dumps(payload).encode(), limits=limits()) == value
    assert parse_go_acceptance_manifest(json.dumps(payload).encode(), limits=limits()) == value
    assert value.test_ids == ("example.org/demo::TestMainLogic",)
    assert len(value.sha256) == 64
    assert replace(value, criteria=(replace(value.criteria[0], description="changed"),)).sha256 != value.sha256
    assert replace(value, business=GoBusinessSuite(tuple(reversed(value.business.cases)))).sha256 != value.sha256


@pytest.mark.parametrize("raw", [
    b'{"schema_version":2,"schema_version":2}', b'{"x":NaN}',
    b'{"x":Infinity}', b'{"x":-Infinity}', b'{"x":1e999}', b'\xff',
    b'\xef\xbb\xbf{}', b'{} trailing', b'[]', b'{}',
])
def test_invalid_json_is_rejected(raw: bytes) -> None:
    with pytest.raises(ValueError):
        GoAcceptanceManifest.from_json(raw, limits=limits())


def test_nested_duplicate_and_escaped_surrogate_are_rejected() -> None:
    raw = json.dumps(manifest().to_payload()).encode()
    for changed in (
        raw.replace(b'"expected_exit": 0', b'"expected_exit": 0, "expected_exit": 0'),
        raw.replace(b'"description": "Print a result and reject invalid input"',
                    b'"description": "\\ud800"'),
    ):
        with pytest.raises(ValueError):
            GoAcceptanceManifest.from_json(changed, limits=limits())


@pytest.mark.parametrize("change", [
    {"schema_version": 1}, {"schema_version": True}, {"stack": "node"},
    {"extra": False}, {"criteria": []}, {"test_files": []}, {"editable_paths": []},
])
def test_fixed_manifest_fields(change: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        GoAcceptanceManifest.from_payload(manifest().to_payload() | change, limits=limits())


@pytest.mark.parametrize("test_id", [
    "TestMainLogic", "example.org/demo:TestMainLogic", "example.org/demo::TestMain",
    "example.org/demo::Testlower", "example.org/demo::TestLogic/subtest",
    "../demo::TestLogic", "example.org//demo::TestLogic", "example.org/demo::",
    "example.org/demo::TestLogic::TestOther", "example.org/demo::Test Logic",
])
def test_named_test_id_format(test_id: str) -> None:
    with pytest.raises(ValueError):
        GoAcceptanceCriterion("AC-001", "description", (test_id,))


@pytest.mark.parametrize("change", [
    {"id": "AC-000"}, {"id": "AC-1"}, {"description": ""},
    {"tests": ()}, {"tests": ("example.org/demo::TestA",) * 2},
])
def test_criterion_requires_nonempty_unique_tests(change: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        replace(manifest().criteria[0], **change)


@pytest.mark.parametrize("path", [
    "main_test.go", "ACCEPTANCE.json", "acceptance.JSON", "go.mod", "go.sum",
    "src/go.env", "AGENTS.md", "src/setup.py", "tests", "../main.go", "C:/main.go",
])
def test_edit_scope_cannot_cover_tests_acceptance_or_controls(path: str) -> None:
    with pytest.raises(ValueError):
        replace(manifest(), editable_paths=(path,), test_files=("tests/logic_test.go",))


@pytest.mark.parametrize("path", ["main.go", "test_main.py", "main_TEST.go", "../main_test.go"])
def test_only_go_test_files(path: str) -> None:
    with pytest.raises(ValueError):
        replace(manifest(), test_files=(path,))


def test_path_collisions_and_duplicate_criteria_or_tests() -> None:
    value = manifest()
    for changes in (
        {"editable_paths": ("src", "src/main.go")},
        {"editable_paths": ("Src", "src2"), "test_files": ("src/logic_test.go",)},
        {"test_files": ("main_test.go",) * 2},
        {"criteria": value.criteria * 2},
        {"criteria": (*value.criteria, replace(value.criteria[0], id="AC-002"))},
    ):
        with pytest.raises(ValueError):
            replace(value, **changes)


def test_business_criterion_ids_must_cover_exactly_all_criteria() -> None:
    value = manifest()
    with pytest.raises(ValueError):
        replace(value, criteria=(*value.criteria, GoAcceptanceCriterion(
            "AC-002", "uncovered", ("example.org/demo::TestOther",))))
    with pytest.raises(ValueError):
        replace(value, business=GoBusinessSuite(tuple(
            replace(case, criterion_id="AC-002") for case in value.business.cases)))


def test_editable_boundary_protects_future_tests_and_controls() -> None:
    value = replace(manifest(), editable_paths=("src",))
    assert value.is_editable("src/new.go")
    for path in ("src/new_test.go", "src/ACCEPTANCE.json", "src/go.mod", "src/AGENTS.md",
                 "src/setup.py", "src2/main.go", "src/../main.go", None):
        assert not value.is_editable(path)


def test_caller_budget_allows_more_than_1000_tests_and_128_cases() -> None:
    value = manifest()
    value = replace(value, criteria=(replace(value.criteria[0], tests=tuple(
        f"example.org/demo::TestCase{index}" for index in range(1201))),),
        business=GoBusinessSuite(tuple(replace(value.business.cases[index % 2], id=f"case-{index}")
                                      for index in range(160))))
    assert GoAcceptanceManifest.from_json(json.dumps(value.to_payload()).encode(), limits=limits()) == value
    with pytest.raises(ValueError):
        GoAcceptanceManifest.from_payload(value.to_payload(), limits=limits(max_cases=159))
    with pytest.raises(ValueError):
        GoAcceptanceManifest.from_json(json.dumps(value.to_payload()).encode(),
                                       limits=limits(max_json_bytes=100))
    with pytest.raises(ValueError):
        GoAcceptanceManifest.from_payload(value.to_payload(), limits=limits(max_stream_bytes=1))


def test_go_criteria_can_exceed_historical_three_digit_ids() -> None:
    criterion = GoAcceptanceCriterion("AC-1000", "Additional criterion", ("example.org/demo::TestA",))
    assert GoAcceptanceCriterion.from_payload(criterion.to_payload()) == criterion
    for invalid in ("AC-000", "AC-0000", "AC-0999"):
        with pytest.raises(ValueError):
            replace(criterion, id=invalid)


def test_manifest_accepts_more_than_999_covered_criteria() -> None:
    value = manifest()
    criteria = tuple(GoAcceptanceCriterion(f"AC-{index:03d}", "criterion",
                     (f"example.org/demo::TestCase{index}",)) for index in range(1, 1202))
    cases = tuple(replace(value.business.cases[0], id=f"case-{row.id}", criterion_id=row.id)
                  for row in criteria)
    value = replace(value, criteria=criteria,
                    business=GoBusinessSuite((*cases, value.business.cases[1])))
    assert GoAcceptanceManifest.from_json(json.dumps(value.to_payload()).encode(), limits=limits()) == value


def test_json_allocation_covers_manifest_not_only_business_suite() -> None:
    value = manifest()
    budget = len(json.dumps(value.business.to_payload(), separators=(",", ":")).encode())
    value.business.require_limits(limits(max_json_bytes=budget))
    with pytest.raises(ValueError):
        GoAcceptanceManifest.from_payload(value.to_payload(), limits=limits(max_json_bytes=budget))


def test_nested_fields_are_exact_and_every_field_is_required() -> None:
    payload = manifest().to_payload()
    for key in payload:
        with pytest.raises(ValueError):
            GoAcceptanceManifest.from_payload({k: v for k, v in payload.items() if k != key})
    criterion = manifest().criteria[0].to_payload()
    with pytest.raises(ValueError):
        GoAcceptanceCriterion.from_payload(criterion | {"extra": True})
    for key in criterion:
        with pytest.raises(ValueError):
            GoAcceptanceCriterion.from_payload({k: v for k, v in criterion.items() if k != key})
    for business in (
        manifest().business.to_payload() | {"extra": True},
        manifest().business.to_payload() | {"schema_version": True},
        manifest().business.to_payload() | {"cases": [
            case.to_payload() | {"extra": True} for case in manifest().business.cases]},
        manifest().business.to_payload() | {"cases": [
            manifest().business.cases[0].to_payload()] * 2},
    ):
        with pytest.raises(ValueError):
            GoAcceptanceManifest.from_payload(payload | {"business": business})


def test_case_order_is_preserved_but_criteria_and_test_ids_are_canonical() -> None:
    value = manifest()
    other = GoAcceptanceCriterion("AC-002", "another", ("example.org/demo::TestZ",
                                                         "example.org/demo::TestA"))
    suite = GoBusinessSuite((*value.business.cases,
                            replace(value.business.cases[0], id="another", criterion_id="AC-002")))
    value = replace(value, criteria=(other, *value.criteria), business=suite)
    assert value.criteria[0].id == "AC-001"
    assert value.criteria[1].tests == ("example.org/demo::TestA", "example.org/demo::TestZ")
    assert value.business.cases == suite.cases
    assert value.business.sha256 == suite.sha256


def test_test_ids_follow_go_case_sensitive_and_unicode_inventory_names() -> None:
    criterion = GoAcceptanceCriterion("AC-001", "Go names", (
        "example.org/demo::TestFoo", "example.org/demo::TestFOO", "example.org/demo::Test\u4e2d"))
    assert len(criterion.tests) == 3
