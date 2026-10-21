"""Synthetic protocol tests only: these fixtures do not execute or attest Go."""

from __future__ import annotations

import hashlib
import importlib
import importlib.util
import json
import os
import shutil
import subprocess
import tempfile
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from typing import Any

import pytest

MODULE = "agent_hub.skills.sandbox.go_project_evidence"
PKG = "example.test/ledger"
STAMP = "2026-10-21T10:00:00.123456789Z"


def test_parser_module_exists() -> None:
    assert importlib.util.find_spec(MODULE) is not None, "Go evidence parser is not implemented"


@pytest.fixture
def api() -> Any:
    assert importlib.util.find_spec(MODULE) is not None, "Go evidence parser is not implemented"
    return importlib.import_module(MODULE)


def event(action: str, test: str = "", package: str = PKG, **extra: object) -> dict[str, object]:
    item: dict[str, object] = {"Time": STAMP, "Action": action, "Package": package}
    if test:
        item["Test"] = test
    if action in {"pass", "fail"}:
        item["Elapsed"] = 0.001
    return {**item, **extra}


def stream(*items: dict[str, object]) -> bytes:
    return ("\n".join(json.dumps(item) for item in items) + "\n").encode()


def happy(package: str = PKG, name: str = "TestLedger") -> list[dict[str, object]]:
    return [
        event("start", package=package),
        event("run", name, package),
        event("pass", name, package),
        event("pass", package=package),
    ]


def expectation(api: Any) -> Any:
    return api.GoTestExpectation(
        packages=(api.GoPackageTests(PKG, ("TestLedger",)),),
        source_sha256="a" * 64,
        protected_sha256="b" * 64,
    )


def process(api: Any, **changes: object) -> Any:
    value = api.GoTestProcess(
        execution_id="project-execution",
        purpose="project_tests",
        argv=("/trusted/go/bin/go", "test", "-json", "-count=1", PKG),
        exit_code=0,
        timed_out=False,
        output_limited=False,
    )
    return replace(value, **changes)


def parse(api: Any, items: list[dict[str, object]], **changes: object) -> Any:
    return api.parse_go_test_evidence(
        stream(*items),
        expected=expectation(api),
        process=process(api, **changes),
    )


def test_completed_named_evidence_is_immutable_and_not_authority(api: Any) -> None:
    proof = parse(api, happy())
    assert proof.status == "passed"
    assert proof.authority == "native_process_output"
    assert proof.required_passed == ((PKG, "TestLedger"),)
    assert proof.execution_id == "project-execution"
    assert len(proof.report_sha256) == len(proof.expected_sha256) == 64
    with pytest.raises(FrozenInstanceError):
        proof.status = "trusted"


def test_logic_failure_is_typed_diagnostic(api: Any) -> None:
    items = [
        event("start"),
        event("run", "TestLedger"),
        event("output", "TestLedger", Output="bad balance\n", OutputType="error"),
        event("fail", "TestLedger"),
        event("fail"),
    ]
    proof = parse(api, items, exit_code=1)
    assert proof.status == "test_failed"
    assert proof.required_passed == ()
    assert proof.tests[0].status == "fail"
    assert proof.events[2].output_type == "error"


def test_build_failure_can_explain_unrun_required_tests(api: Any) -> None:
    proof = parse(
        api,
        [
            event("start"),
            event("output", Output="syntax error\n"),
            event("fail", FailedBuild="example.test/dependency"),
        ],
        exit_code=1,
    )
    assert proof.status == "build_failed"
    assert proof.tests == ()
    assert proof.packages[0].failed_build == "example.test/dependency"
    assert proof.required_passed == ()


def test_nonzero_process_with_passing_events_is_not_success(api: Any) -> None:
    assert parse(api, happy(), exit_code=2).status == "process_failed"


def test_package_exit_failure_without_named_failure_is_process_failure(api: Any) -> None:
    items = happy()
    items[-1] = event("fail")
    assert parse(api, items, exit_code=1).status == "process_failed"


def test_failed_build_preserves_augmented_go_list_package_id(api: Any) -> None:
    package_id = "example.test/ledger [example.test/ledger.test]"
    proof = parse(api, [event("start"), event("fail", FailedBuild=package_id)], exit_code=1)
    assert proof.status == "build_failed"
    assert proof.packages[0].failed_build == package_id


def test_official_build_events_can_precede_and_interleave_test_events(api: Any) -> None:
    package_id = "example.test/ledger [example.test/ledger.test]"
    items: list[dict[str, object]] = [
        {"Action": "build-output", "ImportPath": package_id, "Output": "syntax error\n"},
        {"Action": "build-fail", "ImportPath": package_id},
        event("start"),
        event("fail", FailedBuild=package_id),
    ]
    proof = parse(api, items, exit_code=1)
    assert proof.status == "build_failed"
    assert proof.events[0].import_path == package_id
    assert proof.events[0].time == ""
    assert proof.events[0].output == "syntax error\n"
    assert (
        parse(
            api, [{"Action": "build-output", "ImportPath": PKG, "Output": "warning\n"}, *happy()]
        ).status
        == "passed"
    )


@pytest.mark.parametrize(
    "build_events",
    [
        [{"Action": "build-fail", "ImportPath": PKG}],
        [{"Action": "build-fail", "ImportPath": "unrelated"}],
        [{"Action": "build-fail", "ImportPath": PKG}, {"Action": "build-fail", "ImportPath": PKG}],
        [{"Action": "build-output", "ImportPath": PKG, "Output": "x", "Time": STAMP}],
        [{"Action": "build-fail", "ImportPath": PKG, "Output": "x"}],
    ],
)
def test_unmatched_duplicate_or_malformed_build_events_rejected(
    api: Any,
    build_events: list[dict[str, object]],
) -> None:
    with pytest.raises(api.GoEvidenceError):
        parse(api, [*build_events, *happy()], exit_code=1)


@pytest.mark.parametrize("stamp", ["2026-10-21T10:00:00+00:99", "2026-10-21T10:00:00+24:00"])
def test_invalid_rfc3339_offsets_are_not_normalized(api: Any, stamp: str) -> None:
    items = happy()
    items[0]["Time"] = stamp
    with pytest.raises(api.GoEvidenceError):
        parse(api, items)


def test_legal_subtests_examples_parallel_attr_and_artifacts_are_preserved(api: Any) -> None:
    items = [
        event("start"),
        event("run", "TestLedger"),
        event("run", "TestLedger/edge_case#01"),
        event("pause", "TestLedger/edge_case#01"),
        event("cont", "TestLedger/edge_case#01"),
        event("attr", "TestLedger/edge_case#01", Key="owner", Value="qa team"),
        event("artifacts", "TestLedger/edge_case#01", Path="/tmp/artifacts"),
        event("pass", "TestLedger/edge_case#01"),
        event("pass", "TestLedger"),
        event("run", "ExampleLedger_balance"),
        event("pass", "ExampleLedger_balance"),
        event("pass"),
    ]
    proof = parse(api, items)
    assert proof.status == "passed"
    assert proof.required_passed == ((PKG, "TestLedger"),)
    assert {item.test for item in proof.tests} == {
        "TestLedger",
        "TestLedger/edge_case#01",
        "ExampleLedger_balance",
    }
    assert proof.events[5].value == "qa team"
    assert proof.events[6].path == "/tmp/artifacts"


def test_slashes_in_subtest_names_do_not_imply_intermediate_test_events(api: Any) -> None:
    items = [
        event("start"),
        event("run", "TestLedger"),
        event("run", "TestLedger/a/b"),
        event("pause", "TestLedger/a/b"),
        event("run", "TestLedger/a"),
        event("pass", "TestLedger/a"),
        event("cont", "TestLedger/a/b"),
        event("pass", "TestLedger/a/b"),
        event("run", "TestLedger//edge"),
        event("pass", "TestLedger//edge"),
        event("pass", "TestLedger"),
        event("pass"),
    ]
    proof = parse(api, items)
    assert proof.status == "passed"
    assert "TestLedger//edge" in {test.test for test in proof.tests}


@pytest.mark.parametrize(
    "name", ["Test", "Test9", "Test_", "Test\u03a9", "Test\u1c89", "Test\u4e2d", "Test\u02b0"]
)
def test_go_unicode_inventory_expectation_evidence_roundtrip(api: Any, name: str) -> None:
    inventory_api = importlib.import_module("agent_hub.skills.sandbox.go_project_inventory")
    sources = {
        "main.go": b"package main\nfunc main() {}\n",
        "main_test.go": (
            'package main\nimport "testing"\nfunc ' + name + "(t *testing.T) {}\n"
        ).encode(),
    }
    payload = {
        "schema_version": 1,
        "kind": "go_source_inventory_v1",
        "module_path": PKG,
        "packages": [PKG],
        "expected_tests": [{"package": PKG, "name": name}],
        "project_main_package": PKG,
        "source_go_files": [
            {"path": path, "raw_sha256": hashlib.sha256(data).hexdigest(), "size_bytes": len(data)}
            for path, data in sources.items()
        ],
    }
    inventory = inventory_api.parse_go_source_inventory(
        json.dumps(payload).encode(),
        expected_module=PKG,
        expected_source={
            path: (hashlib.sha256(data).hexdigest(), len(data)) for path, data in sources.items()
        },
    )
    expected = inventory.to_test_expectation(source_sha256="a" * 64, protected_sha256="b" * 64)
    proof = api.parse_go_test_evidence(
        stream(*happy(name=name)), expected=expected, process=process(api)
    )
    assert proof.status == "passed"
    assert proof.required_passed == ((PKG, name),)
    assert proof.tests[0].test == name


@pytest.mark.parametrize(
    "name",
    [
        "Test\u1c89/Injected",
        "Test\u1c89\\Injected",
        "Test\u1c89 injected",
        "Test\u1c89\u00a0injected",
        "Test\u1c89\nInjected",
        "Test\u1c89\x00Injected",
        "Test\u1c89\x7fInjected",
        "TestA\u0301",
        "Test\u216b",
        "Test\u1c89\U0001f600",
        "Testlower",
    ],
)
def test_go_unicode_rule_does_not_admit_malformed_required_ids(api: Any, name: str) -> None:
    expected = replace(expectation(api), packages=(api.GoPackageTests(PKG, (name,)),))
    with pytest.raises(api.GoEvidenceError):
        api.parse_go_test_evidence(
            stream(*happy(name=name)), expected=expected, process=process(api)
        )
    with pytest.raises(api.GoEvidenceError):
        api.parse_go_test_evidence(
            stream(*happy(name=name)), expected=expectation(api), process=process(api)
        )


def test_go_unicode_root_preserves_legitimate_subtests_and_examples(api: Any) -> None:
    name = "Test\u1c89"
    expected = replace(expectation(api), packages=(api.GoPackageTests(PKG, (name,)),))
    child = name + "/edge/case"
    example = "Example\u1c89"
    items = [
        event("start"),
        event("run", name),
        event("run", child),
        event("pass", child),
        event("pass", name),
        event("run", example),
        event("pass", example),
        event("pass"),
    ]
    proof = api.parse_go_test_evidence(stream(*items), expected=expected, process=process(api))
    assert proof.required_passed == ((PKG, name),)
    assert {row.test for row in proof.tests} == {name, child, example}


@pytest.mark.parametrize("output_type", ["", "frame", "error", "error-continue"])
def test_official_output_types_and_printed_claims_are_only_output(
    api: Any, output_type: str
) -> None:
    items = happy()
    items.insert(
        2,
        event(
            "output",
            "TestLedger",
            OutputType=output_type,
            Output="cached no tests to run CANARY PASS \u0016=== RUN fake\n",
        ),
    )
    assert parse(api, items).status == "passed"


def test_interleaved_packages_and_exact_names(api: Any) -> None:
    other = "example.test/other"
    expected = replace(
        expectation(api),
        packages=(
            api.GoPackageTests(other, ("TestLedger",)),
            api.GoPackageTests(PKG, ("TestLedger",)),
        ),
    )
    items = [value for pair in zip(happy(), happy(other), strict=True) for value in pair]
    proof = api.parse_go_test_evidence(
        stream(*items),
        expected=expected,
        process=process(api, argv=("go", "test", "-json", "-count=1", other, PKG)),
    )
    assert proof.status == "passed" and len(proof.required_passed) == 2
    changed = replace(expected, source_sha256="c" * 64)
    assert (
        api.parse_go_test_evidence(
            stream(*items),
            expected=changed,
            process=process(api, argv=("go", "test", "-json", "-count=1", other, PKG)),
        ).expected_sha256
        != proof.expected_sha256
    )


@pytest.mark.parametrize(
    "items",
    [
        [],
        [event("start"), event("pass")],
        happy()[1:],
        happy()[:-1],
        [event("start"), event("pass", "TestLedger"), event("pass")],
        [event("start"), event("run", "TestLedger"), event("run", "TestLedger"), *happy()[2:]],
        [*happy()[:3], event("pass", "TestLedger"), event("pass")],
        [event("start"), *happy()],
        [*happy(), event("pass")],
        [event("start"), event("run", "TestWrong"), event("pass", "TestWrong"), event("pass")],
        [event("start"), event("run", "TestLedger"), event("skip", "TestLedger"), event("pass")],
        [event("start"), event("skip")],
        [event("start"), event("run", "TestLedger"), event("fail", "TestLedger"), event("pass")],
        [event("start"), event("fail", FailedBuild=PKG), event("run", "TestLedger")],
        [
            event("start"),
            event("run", "TestLedger/child"),
            event("pass", "TestLedger/child"),
            *happy()[1:],
        ],
        [
            event("start"),
            event("run", "TestLedger"),
            event("pause", "TestLedger"),
            event("pass", "TestLedger"),
            event("pass"),
        ],
        [event("start"), event("cont", "TestLedger"), *happy()[1:]],
        [
            event("start"),
            event("run", "TestLedger"),
            event("attr", "TestLedger", Key="x"),
            *happy()[2:],
        ],
        [event("start"), event("run", "TestLedger"), event("bench", "TestLedger"), *happy()[2:]],
    ],
)
def test_missing_duplicate_skip_out_of_order_or_unsupported_events_rejected(
    api: Any,
    items: list[dict[str, object]],
) -> None:
    with pytest.raises(api.GoEvidenceError):
        parse(api, items)


@pytest.mark.parametrize(
    "field,value",
    [
        ("Time", None),
        ("Time", ""),
        ("Time", "not-a-date"),
        ("Time", "2026-02-31T00:00:00Z"),
        ("Elapsed", float("nan")),
        ("Elapsed", float("inf")),
        ("Elapsed", -1),
        ("Elapsed", True),
        ("Action", "unknown"),
        ("Package", "other"),
        ("Test", "testledger"),
        ("OutputType", "made-up"),
        ("Extra", 1),
        ("FailedBuild", "x"),
        ("Key", "x"),
    ],
)
def test_strict_fields(api: Any, field: str, value: object) -> None:
    items = happy()
    items[2][field] = value
    with pytest.raises(api.GoEvidenceError):
        parse(api, items)


@pytest.mark.parametrize(
    "raw",
    [
        b"{}\n",
        b"[]\n",
        b"null\n",
        b"true\n",
        b"garbage\n",
        b"\xff\n",
        b"\n",
        b'{"Action":"start","Action":"pass"}\n',
        b'{"Action":"start","Package":"x","nested":{"a":1,"a":2}}\n',
        b'{"Action":"start","Elapsed":1e999}\n',
        b'{"Action":"start","Package":"\\ud800"}\n',
    ],
)
def test_invalid_ndjson_rejected(api: Any, raw: bytes) -> None:
    with pytest.raises(api.GoEvidenceError):
        api.parse_go_test_evidence(raw, expected=expectation(api), process=process(api))


def test_missing_time_trailing_fragment_and_blank_lines_rejected(api: Any) -> None:
    items = happy()
    del items[1]["Time"]
    for raw in (
        stream(*items),
        stream(*happy())[:-1],
        stream(*happy()) + b'{"Action":',
        stream(*happy()) + b"\n",
    ):
        with pytest.raises(api.GoEvidenceError):
            api.parse_go_test_evidence(raw, expected=expectation(api), process=process(api))


@pytest.mark.parametrize(
    "text",
    [
        "ok  \texample.test/ledger\t(cached)\n",
        "?   \texample.test/ledger\t[no test files]\n",
        "ok  \texample.test/ledger\t0.001s [no tests to run]\n",
    ],
)
def test_official_package_summary_rejected_even_if_split(api: Any, text: str) -> None:
    items = happy()
    items[3:3] = [event("output", Output=text[:8]), event("output", Output=text[8:])]
    with pytest.raises(api.GoEvidenceError):
        parse(api, items)


def test_limits_bound_bytes_and_events(api: Any) -> None:
    for raw in (
        b" " * (api.MAX_GO_TEST_OUTPUT_BYTES + 1),
        stream(*([event("start")] * (api.MAX_GO_TEST_EVENTS + 1))),
    ):
        with pytest.raises(api.GoEvidenceError):
            api.parse_go_test_evidence(raw, expected=expectation(api), process=process(api))


def test_event_limit_is_checked_independently_of_byte_limit(api: Any) -> None:
    output = event("output", Output="", Time="2026-10-21T10:00:00Z")
    items = [*happy()[:2], *([output] * (api.MAX_GO_TEST_EVENTS - 4)), *happy()[2:]]
    raw = ("\n".join(json.dumps(item, separators=(",", ":")) for item in items) + "\n").encode()
    assert len(raw) < api.MAX_GO_TEST_OUTPUT_BYTES
    assert (
        api.parse_go_test_evidence(raw, expected=expectation(api), process=process(api)).status
        == "passed"
    )
    extra = json.dumps(output, separators=(",", ":")).encode() + b"\n"
    assert len(extra + raw) < api.MAX_GO_TEST_OUTPUT_BYTES
    with pytest.raises(api.GoEvidenceError):
        api.parse_go_test_evidence(
            raw.replace(b"\n", b"\n" + extra, 1), expected=expectation(api), process=process(api)
        )


def test_byte_limit_counts_utf8_bytes_and_accepts_exact_limit(api: Any) -> None:
    items = happy()
    items.insert(2, event("output", "TestLedger", Output=""))
    room = api.MAX_GO_TEST_OUTPUT_BYTES - len(stream(*items))
    items[2]["Output"] = "a" * room
    raw = stream(*items)
    assert len(raw) == api.MAX_GO_TEST_OUTPUT_BYTES
    assert (
        api.parse_go_test_evidence(raw, expected=expectation(api), process=process(api)).status
        == "passed"
    )
    raw = raw.replace(b'"Output": "a', '"Output": "\u754c'.encode(), 1)
    assert len(raw.decode()) <= api.MAX_GO_TEST_OUTPUT_BYTES < len(raw)
    with pytest.raises(api.GoEvidenceError):
        api.parse_go_test_evidence(raw, expected=expectation(api), process=process(api))


@pytest.mark.parametrize(
    "changes",
    [
        {"timed_out": True},
        {"output_limited": True},
        {"exit_code": True},
        {"execution_id": ""},
        {"purpose": "negative_canary"},
        {"argv": ("go", "test", "-json", PKG)},
        {"argv": ("go", "test", "-json", "-count=1", "./...")},
        {"argv": ("go", "test", "-json", "-count=1", PKG, "-run=TestLedger")},
    ],
)
def test_process_and_fixed_command_are_required(api: Any, changes: dict[str, object]) -> None:
    with pytest.raises(api.GoEvidenceError):
        parse(api, happy(), **changes)


@pytest.mark.parametrize(
    "packages",
    [
        (),
        ((PKG, ()),),
        ((PKG, ("TestLedger", "TestLedger")),),
        ((PKG, ("TestLedger/child",)),),
        ((PKG, ("ExampleLedger",)),),
        ((PKG, ("TestLedger",)), (PKG, ("TestOther",))),
    ],
)
def test_expected_set_must_be_nonzero_unique_top_level_tests(
    api: Any, packages: tuple[Any, ...]
) -> None:
    expected = replace(expectation(api), packages=tuple(api.GoPackageTests(*p) for p in packages))
    with pytest.raises(api.GoEvidenceError):
        api.parse_go_test_evidence(stream(*happy()), expected=expected, process=process(api))


def test_zero_exit_does_not_turn_logic_or_build_failure_into_valid_evidence(api: Any) -> None:
    for items in (
        [event("start"), event("fail", FailedBuild=PKG)],
        [event("start"), event("run", "TestLedger"), event("fail", "TestLedger"), event("fail")],
    ):
        with pytest.raises(api.GoEvidenceError):
            parse(api, items)


def test_canary_has_separate_identity_and_requires_expected_named_failure(api: Any) -> None:
    expected = api.GoTestExpectation(
        (api.GoPackageTests("canary.test/probe", ("TestMustFail",)),), "c" * 64, "d" * 64
    )
    identity = api.GoCanaryIdentity("project-execution", "canary-execution", expected)
    observation = process(
        api,
        execution_id="canary-execution",
        purpose="negative_canary",
        argv=("go", "test", "-json", "-count=1", "canary.test/probe"),
        exit_code=1,
    )
    items = [
        event("start", package="canary.test/probe"),
        event("run", "TestMustFail", "canary.test/probe"),
        event("fail", "TestMustFail", "canary.test/probe"),
        event("fail", package="canary.test/probe"),
    ]
    result = api.parse_go_canary_evidence(stream(*items), identity=identity, process=observation)
    assert result.observed_expected_failure is True
    assert result.authority == "native_process_output"
    assert result.project_execution_id == "project-execution"
    assert result.evidence.status == "test_failed"
    for changed in (
        replace(identity, execution_id="project-execution"),
        replace(identity, execution_id="different"),
    ):
        with pytest.raises(api.GoEvidenceError):
            api.parse_go_canary_evidence(stream(*items), identity=changed, process=observation)
    for raw in (
        stream(*happy("canary.test/probe", "TestMustFail")),
        stream(
            event("start", package="canary.test/probe"),
            event("fail", package="canary.test/probe", FailedBuild="canary.test/probe"),
        ),
    ):
        outcome = api.parse_go_canary_evidence(
            raw,
            identity=identity,
            process=replace(observation, exit_code=0)
            if b'"Action": "pass"' in raw
            else observation,
        )
        assert outcome.observed_expected_failure is False


def test_forged_well_formed_stream_cannot_be_detected_by_parser_alone(api: Any) -> None:
    # A controlled source can forge test2json framing too. This is deliberately
    # accepted as structured output, never promoted to independent provenance.
    proof = parse(api, happy())
    assert proof.authority == "native_process_output"


@pytest.mark.parametrize(
    "package_count,test_count,output_count", [(257, 1, 0), (1, 1001, 0), (1, 1, 10001)]
)
def test_trusted_dynamic_limits_allow_projects_above_default_counts(
    api: Any,
    package_count: int,
    test_count: int,
    output_count: int,
) -> None:
    groups = tuple(
        api.GoPackageTests(f"evidence.test/p{i}", tuple(f"TestCase{j}" for j in range(test_count)))
        for i in range(package_count)
    )
    expected = api.GoTestExpectation(groups, "a" * 64, "b" * 64)
    items: list[dict[str, object]] = []
    for group in groups:
        items.append(event("start", package=group.package))
        for name in group.tests:
            items.extend([event("run", name, group.package), event("pass", name, group.package)])
        items.extend(event("output", package=group.package, Output="") for _ in range(output_count))
        items.append(event("pass", package=group.package))
    for item in items:
        item["Time"] = "2026-10-21T10:00:00Z"
    raw = ("\n".join(json.dumps(item, separators=(",", ":")) for item in items) + "\n").encode()
    observation = process(
        api, argv=("go", "test", "-json", "-count=1", *(g.package for g in groups))
    )
    with pytest.raises(api.GoEvidenceError):
        api.parse_go_test_evidence(raw, expected=expected, process=observation)
    # These resource allocations are trusted fixture policy, not report fields.
    limits = api.GoEvidenceLimits(
        max_output_bytes=2 * 1024 * 1024, max_events=20000, max_packages=500, max_tests=2000
    )
    proof = api.parse_go_test_evidence(raw, expected=expected, process=observation, limits=limits)
    assert proof.status == "passed"
    assert len(proof.required_passed) == package_count * test_count
    assert len(proof.events) == len(items)


def test_trusted_dynamic_byte_limit_can_exceed_default(api: Any) -> None:
    items = happy()
    items.insert(2, event("output", "TestLedger", Output="a" * (1024 * 1024)))
    raw = stream(*items)
    with pytest.raises(api.GoEvidenceError):
        api.parse_go_test_evidence(raw, expected=expectation(api), process=process(api))
    limits = api.GoEvidenceLimits(max_output_bytes=2 * 1024 * 1024)
    assert (
        api.parse_go_test_evidence(
            raw, expected=expectation(api), process=process(api), limits=limits
        ).status
        == "passed"
    )


def test_limits_defaults_are_frozen_budgets_not_product_caps(api: Any) -> None:
    limits = api.GoEvidenceLimits()
    assert (limits.max_output_bytes, limits.max_events, limits.max_packages, limits.max_tests) == (
        1024 * 1024,
        10000,
        256,
        1000,
    )
    with pytest.raises(FrozenInstanceError):
        limits.max_tests = 2000


@pytest.mark.parametrize("field", ["max_output_bytes", "max_events", "max_packages", "max_tests"])
@pytest.mark.parametrize("value", [0, -1, True, False, 1.5, float("inf"), float("nan"), "10", None])
def test_limits_require_finite_positive_integer_budgets(
    api: Any, field: str, value: object
) -> None:
    with pytest.raises(api.GoEvidenceError):
        api.GoEvidenceLimits(**{field: value})


@pytest.mark.parametrize(
    "field,budget",
    [("max_output_bytes", 20), ("max_events", 3), ("max_packages", 1), ("max_tests", 1)],
)
def test_tight_limits_reject_otherwise_valid_projects(api: Any, field: str, budget: int) -> None:
    other = "evidence.test/other"
    expected = replace(
        expectation(api),
        packages=(
            api.GoPackageTests(PKG, ("TestLedger",)),
            api.GoPackageTests(other, ("TestOther",)),
        ),
    )
    observation = process(api, argv=("go", "test", "-json", "-count=1", PKG, other))
    raw = stream(*happy(), *happy(other, "TestOther"))
    assert (
        api.parse_go_test_evidence(raw, expected=expected, process=observation).status == "passed"
    )
    with pytest.raises(api.GoEvidenceError):
        api.parse_go_test_evidence(
            raw,
            expected=expected,
            process=observation,
            limits=api.GoEvidenceLimits(**{field: budget}),
        )


def test_test_budget_counts_all_packages_subtests_and_examples(api: Any) -> None:
    items = [
        event("start"),
        event("run", "TestLedger"),
        event("run", "TestLedger/sub"),
        event("pass", "TestLedger/sub"),
        event("pass", "TestLedger"),
        event("run", "ExampleLedger"),
        event("pass", "ExampleLedger"),
        event("pass"),
    ]
    raw = stream(*items)
    assert (
        api.parse_go_test_evidence(
            raw,
            expected=expectation(api),
            process=process(api),
            limits=api.GoEvidenceLimits(max_tests=3),
        ).status
        == "passed"
    )
    with pytest.raises(api.GoEvidenceError):
        api.parse_go_test_evidence(
            raw,
            expected=expectation(api),
            process=process(api),
            limits=api.GoEvidenceLimits(max_tests=2),
        )


def test_report_cannot_supply_or_raise_limits(api: Any) -> None:
    items = happy()
    items[0]["Limits"] = {"max_events": 999999}
    with pytest.raises(api.GoEvidenceError):
        api.parse_go_test_evidence(
            stream(*items),
            expected=expectation(api),
            process=process(api),
            limits=api.GoEvidenceLimits(),
        )
    with pytest.raises(api.GoEvidenceError):
        api.parse_go_test_evidence(
            stream(*happy()),
            expected=expectation(api),
            process=process(api),
            limits={"max_events": 10000},
        )


def test_canary_obeys_its_separately_supplied_budget(api: Any) -> None:
    expected = expectation(api)
    identity = api.GoCanaryIdentity("project-execution", "canary-execution", expected)
    observation = process(
        api, execution_id="canary-execution", purpose="negative_canary", exit_code=1
    )
    raw = stream(
        event("start"), event("run", "TestLedger"), event("fail", "TestLedger"), event("fail")
    )
    assert api.parse_go_canary_evidence(
        raw, identity=identity, process=observation, limits=api.GoEvidenceLimits(max_events=4)
    ).observed_expected_failure
    with pytest.raises(api.GoEvidenceError):
        api.parse_go_canary_evidence(
            raw, identity=identity, process=observation, limits=api.GoEvidenceLimits(max_events=3)
        )


def test_exact_mount_argv_uses_trusted_inventory_without_rewriting_process(api: Any) -> None:
    argv = ("/sdk/bin/go", "test", "-json", "-count=1", "-vet=off", "-mod=readonly", "./...")
    observation = process(api, argv=argv)
    proof = api.parse_go_test_evidence(
        stream(*happy()), expected=expectation(api), process=observation
    )
    assert proof.status == "passed"
    assert observation.argv == argv
    other = "evidence.test/other"
    expected = replace(
        expectation(api),
        packages=(
            api.GoPackageTests(PKG, ("TestLedger",)),
            api.GoPackageTests(other, ("TestOther",)),
        ),
    )
    assert (
        api.parse_go_test_evidence(
            stream(*happy(), *happy(other, "TestOther")), expected=expected, process=observation
        ).status
        == "passed"
    )
    with pytest.raises(api.GoEvidenceError):
        api.parse_go_test_evidence(stream(*happy()), expected=expected, process=observation)


@pytest.mark.parametrize(
    "args",
    [
        ("test", "-json", "-count=1", "-run=None", PKG),
        ("test", "-json", "-count=1", "-vet=off", "-mod=readonly", "./...", "-run=None"),
        ("test", "-json", "-count=0", "-vet=off", "-mod=readonly", "./..."),
        ("test", "-json", "-count=1", "-vet=off", "-mod=readonly", "./other/..."),
        ("test", "-json", "-count=1", "-vet=off", "-mod=readonly", PKG),
        ("test", "-json", "-count=1", "-vet=off", "./..."),
        ("test", "-json", "-count=1", "-mod=readonly", "-vet=off", "./..."),
        ("test", "-json", "-count=1", "-vet=off", "-mod=mod", "./..."),
        (
            "test",
            "-json",
            "-count=1",
            "-overlay",
            "/trusted/overlay.json",
            "-run=RandomCanary",
            PKG,
        ),
    ],
)
def test_only_explicit_argv_variants_are_admitted(api: Any, args: tuple[str, ...]) -> None:
    with pytest.raises(api.GoEvidenceError):
        parse(api, happy(), argv=("/sdk/bin/go", *args))


def overlay_canary() -> Any:
    contract = importlib.import_module("agent_hub.skills.sandbox.go_project_canary")
    return contract.GoOverlayCanary(
        "project-execution", "overlay-execution", PKG, "d" * 64, "a" * 64, "b" * 64
    )


def overlay_process(api: Any, canary: Any, **changes: object) -> Any:
    return process(
        api,
        execution_id=canary.execution_id,
        purpose="negative_canary",
        argv=canary.command,
        exit_code=1,
        **changes,
    )


def overlay_failure(canary: Any) -> bytes:
    return stream(
        event("start", package=canary.module_path),
        event("run", canary.test_name, canary.module_path),
        event("fail", canary.test_name, canary.module_path),
        event("fail", package=canary.module_path),
    )


@pytest.mark.parametrize(
    "binary", ["/sdk/bin/go", "/attested/go/bin/go", "E:/proof-sdk/go/bin/go.exe"]
)
def test_overlay_canary_exact_command_and_actual_sdk_path_are_accepted(
    api: Any, binary: str
) -> None:
    canary = overlay_canary()
    observation = replace(overlay_process(api, canary), argv=(binary, *canary.command[1:]))
    proof = api.parse_go_overlay_canary_evidence(
        overlay_failure(canary), canary=canary, process=observation
    )
    assert proof.observed_expected_failure is True
    assert proof.project_execution_id == canary.project_execution_id
    assert proof.evidence.execution_id == canary.execution_id
    assert proof.evidence.tests[0].test == canary.test_name
    assert proof.authority == proof.evidence.authority == "native_process_output"
    assert observation.argv == (binary, *canary.command[1:])


@pytest.mark.parametrize(
    "position,value",
    [
        (0, "go"),
        (0, ""),
        (0, "-bad"),
        (0, "/sdk/bin/go\n"),
        (1, "build"),
        (2, "-json=false"),
        (3, "-count=0"),
        (4, "-vet=on"),
        (5, "-mod=mod"),
        (6, "-overlay=/workspace/model.json"),
        (7, "-run=None"),
        (7, "-run=TestAgentHubCanary"),
        (7, "-run=^TestAgentHubCanary" + "e" * 64 + "$"),
        (8, "./..."),
        (8, "other.test/module"),
    ],
)
def test_overlay_canary_rejects_each_changed_argv_field(
    api: Any, position: int, value: str
) -> None:
    canary = overlay_canary()
    argv = list(canary.command)
    argv[position] = value
    observation = replace(overlay_process(api, canary), argv=tuple(argv))
    with pytest.raises(api.GoEvidenceError):
        api.parse_go_overlay_canary_evidence(
            overlay_failure(canary), canary=canary, process=observation
        )


@pytest.mark.parametrize("position", range(1, 9))
def test_overlay_canary_rejects_each_missing_argv_field(api: Any, position: int) -> None:
    canary = overlay_canary()
    argv = canary.command[:position] + canary.command[position + 1 :]
    with pytest.raises(api.GoEvidenceError):
        api.parse_go_overlay_canary_evidence(
            overlay_failure(canary),
            canary=canary,
            process=replace(overlay_process(api, canary), argv=argv),
        )


def test_overlay_canary_rejects_extra_reordered_or_non_tuple_args(api: Any) -> None:
    canary = overlay_canary()
    for argv in (
        (*canary.command, "-run=None"),
        (*canary.command[:4], canary.command[5], canary.command[4], *canary.command[6:]),
        list(canary.command),
    ):
        with pytest.raises(api.GoEvidenceError):
            api.parse_go_overlay_canary_evidence(
                overlay_failure(canary),
                canary=canary,
                process=replace(overlay_process(api, canary), argv=argv),
            )


@pytest.mark.parametrize(
    "changes",
    [
        {"execution_id": "other"},
        {"execution_id": "project-execution"},
        {"purpose": "project_tests"},
        {"timed_out": True},
        {"output_limited": True},
        {"exit_code": True},
    ],
)
def test_overlay_canary_process_identity_and_capture_must_match(
    api: Any,
    changes: dict[str, object],
) -> None:
    canary = overlay_canary()
    with pytest.raises(api.GoEvidenceError):
        api.parse_go_overlay_canary_evidence(
            overlay_failure(canary),
            canary=canary,
            process=replace(overlay_process(api, canary), **changes),
        )


@pytest.mark.parametrize(
    "field,value",
    [
        ("execution_id", "project-execution"),
        ("project_execution_id", "../outside"),
        ("module_path", "-run=None"),
        ("nonce", "INVALID"),
        ("source_sha256", "A" * 64),
        ("protected_sha256", "bad"),
    ],
)
def test_overlay_canary_revalidates_constructed_typed_identity(
    api: Any,
    field: str,
    value: object,
) -> None:
    canary = overlay_canary()
    raw, observation = overlay_failure(canary), overlay_process(api, canary)
    object.__setattr__(canary, field, value)
    with pytest.raises(api.GoEvidenceError):
        api.parse_go_overlay_canary_evidence(raw, canary=canary, process=observation)


def test_overlay_canary_binds_source_protection_nonce_and_package_from_contract(api: Any) -> None:
    canary = overlay_canary()
    raw, observation = overlay_failure(canary), overlay_process(api, canary)
    proof = api.parse_go_overlay_canary_evidence(raw, canary=canary, process=observation)
    for field in ("source_sha256", "protected_sha256"):
        changed = replace(canary, **{field: "c" * 64})
        bound = api.parse_go_overlay_canary_evidence(raw, canary=changed, process=observation)
        assert bound.evidence.expected_sha256 != proof.evidence.expected_sha256
    for changed in (
        replace(canary, nonce="e" * 64),
        replace(canary, module_path="other.test/module"),
    ):
        with pytest.raises(api.GoEvidenceError):
            api.parse_go_overlay_canary_evidence(
                raw, canary=changed, process=overlay_process(api, changed)
            )
    with pytest.raises(api.GoEvidenceError):
        api.parse_go_overlay_canary_evidence(
            raw, canary={"nonce": canary.nonce}, process=observation
        )


@pytest.mark.parametrize("result,exit_code", [("pass", 0), ("pass", 2), ("fail", 2), ("fail", -9)])
def test_overlay_canary_requires_expected_test_failure_and_driver_exit_one(
    api: Any,
    result: str,
    exit_code: int,
) -> None:
    canary = overlay_canary()
    raw = stream(
        event("start"),
        event("run", canary.test_name),
        event(result, canary.test_name),
        event(result),
    )
    proof = api.parse_go_overlay_canary_evidence(
        raw, canary=canary, process=replace(overlay_process(api, canary), exit_code=exit_code)
    )
    assert proof.observed_expected_failure is False


def test_overlay_canary_compile_failure_is_diagnostic_not_negative_gate_pass(api: Any) -> None:
    canary = overlay_canary()
    raw = stream(
        {"Action": "build-output", "ImportPath": PKG, "Output": "syntax error\n"},
        {"Action": "build-fail", "ImportPath": PKG},
        event("start"),
        event("fail", FailedBuild=PKG),
    )
    proof = api.parse_go_overlay_canary_evidence(
        raw, canary=canary, process=overlay_process(api, canary)
    )
    assert proof.evidence.status == "build_failed" and not proof.observed_expected_failure


def test_overlay_canary_rejects_unexpected_named_tests_and_obeys_limits(api: Any) -> None:
    canary = overlay_canary()
    for name in ("ExampleOther", canary.test_name + "/forged"):
        raw = stream(
            event("start"),
            event("run", canary.test_name),
            event("run", name),
            event("pass", name),
            event("fail", canary.test_name),
            event("fail"),
        )
        with pytest.raises(api.GoEvidenceError):
            api.parse_go_overlay_canary_evidence(
                raw, canary=canary, process=overlay_process(api, canary)
            )
    with pytest.raises(api.GoEvidenceError):
        api.parse_go_overlay_canary_evidence(
            overlay_failure(canary),
            canary=canary,
            process=overlay_process(api, canary),
            limits=api.GoEvidenceLimits(max_events=3),
        )


def test_overlay_argv_is_not_admitted_by_legacy_canary_or_project_parser(api: Any) -> None:
    canary = overlay_canary()
    raw = overlay_failure(canary)
    with pytest.raises(api.GoEvidenceError):
        api.parse_go_canary_evidence(
            raw, identity=canary.identity, process=overlay_process(api, canary)
        )
    with pytest.raises(api.GoEvidenceError):
        api.parse_go_test_evidence(
            raw,
            expected=canary.identity.expected,
            process=replace(overlay_process(api, canary), purpose="project_tests"),
        )


def test_native_trusted_fixture_pass_fail_subtests_and_mount_argv(api: Any) -> None:
    """Opt-in Windows component calibration, never isolation/user acceptance."""
    configured_root = os.environ.get("AGENT_HUB_GO_EVIDENCE_PROOF_ROOT")
    if configured_root is None:
        pytest.skip("native proof requires an explicitly supplied local proof SDK; no download")
    proof_root = Path(configured_root).resolve(strict=True)
    sdk = proof_root / "go" / "bin" / "go.exe"
    assert sdk.is_file()
    owned = Path(tempfile.mkdtemp(prefix="go-evidence-calibration-", dir=proof_root)).resolve()
    assert owned.parent == proof_root
    fixture = owned / "fixture"
    fixture.mkdir()
    sources = {
        "go.mod": "module evidence.test/nativeprobe\n\ngo 1.27.0\n",
        "passed/probe_test.go": """package passed
import (
    "fmt"
    "testing"
)
func TestPass(t *testing.T) {
    t.Attr("owner", "native-fixture")
    t.Log("cached no tests to run CANARY PASS are ordinary fixture text")
    t.Run("edge/case", func(t *testing.T) {
        if 2+2 != 4 { t.Fatal("arithmetic") }
    })
    t.Run("parallel", func(t *testing.T) {
        t.Parallel()
        if 3*3 != 9 { t.Error("arithmetic") }
    })
}
func Example_sum() {
    fmt.Println(2+2)
    // Output: 4
}
""",
        "failed/probe_test.go": """package failed
import "testing"
func TestFail(t *testing.T) {
    t.Run("negative", func(t *testing.T) { t.Error("intentional native fixture failure") })
}
""",
    }
    for name, content in sources.items():
        target = fixture / name
        target.parent.mkdir(exist_ok=True)
        target.write_text(content, encoding="utf-8", newline="\n")
    digest = hashlib.sha256(
        b"".join(name.encode() + b"\0" + (fixture / name).read_bytes() for name in sorted(sources))
    ).hexdigest()
    scratch = {name: owned / name for name in ("cache", "modcache", "gopath", "tmp", "home")}
    for path in scratch.values():
        path.mkdir()
    system_root = os.environ["SYSTEMROOT"]
    env = {
        "SYSTEMROOT": system_root,
        "WINDIR": system_root,
        "PATH": str(sdk.parent) + os.pathsep + str(Path(system_root) / "System32"),
        "GOROOT": str(proof_root / "go"),
        "GOCACHE": str(scratch["cache"]),
        "GOMODCACHE": str(scratch["modcache"]),
        "GOPATH": str(scratch["gopath"]),
        "GOTMPDIR": str(scratch["tmp"]),
        "TMP": str(scratch["tmp"]),
        "TEMP": str(scratch["tmp"]),
        "HOME": str(scratch["home"]),
        "USERPROFILE": str(scratch["home"]),
        "LOCALAPPDATA": str(scratch["home"]),
        "APPDATA": str(scratch["home"]),
        "GOENV": "off",
        "GOFLAGS": "",
        "GOWORK": "off",
        "GOTOOLCHAIN": "local",
        "GOPROXY": "off",
        "GOSUMDB": "off",
        "GOVCS": "*:off",
        "GOTELEMETRY": "off",
        "CGO_ENABLED": "0",
        "GOMAXPROCS": "2",
    }
    passed = "evidence.test/nativeprobe/passed"
    failed = "evidence.test/nativeprobe/failed"
    groups = (api.GoPackageTests(passed, ("TestPass",)), api.GoPackageTests(failed, ("TestFail",)))
    limits = api.GoEvidenceLimits(
        max_output_bytes=128 * 1024, max_events=512, max_packages=2, max_tests=16
    )
    observations: list[dict[str, object]] = []
    try:
        for label, argv, expected_groups, status, exit_code in (
            ("pass", (str(sdk), "test", "-json", "-count=1", passed), groups[:1], "passed", 0),
            (
                "fail",
                (str(sdk), "test", "-json", "-count=1", "-vet=off", "-mod=readonly", "./..."),
                groups,
                "test_failed",
                1,
            ),
        ):
            completed = subprocess.run(
                argv, cwd=fixture, env=env, capture_output=True, timeout=180, check=False
            )
            (owned / f"{label}.ndjson").write_bytes(completed.stdout)
            (owned / f"{label}.stderr.txt").write_bytes(completed.stderr)
            observation = api.GoTestProcess(
                f"{owned.name}-{label}", "project_tests", argv, completed.returncode, False, False
            )
            evidence = api.parse_go_test_evidence(
                completed.stdout,
                expected=api.GoTestExpectation(expected_groups, digest, digest),
                process=observation,
                limits=limits,
            )
            assert completed.returncode == exit_code
            assert evidence.status == status
            assert (passed, "TestPass") in evidence.required_passed
            assert {"TestPass/edge/case", "TestPass/parallel"} <= {
                item.test for item in evidence.tests
            }
            assert any(item.action == "attr" for item in evidence.events)
            if label == "fail":
                assert any(
                    item.test == "TestFail/negative" and item.status == "fail"
                    for item in evidence.tests
                )
                assert any(item.output_type == "error" for item in evidence.events)
            observations.append(
                {
                    "label": label,
                    "argv": argv,
                    "exit_code": completed.returncode,
                    "status": evidence.status,
                    "report_sha256": evidence.report_sha256,
                    "events": len(evidence.events),
                    "named_tests": len(evidence.tests),
                }
            )
        assert (
            digest
            == hashlib.sha256(
                b"".join(
                    name.encode() + b"\0" + (fixture / name).read_bytes()
                    for name in sorted(sources)
                )
            ).hexdigest()
        )
        (owned / "observations.json").write_text(
            json.dumps(observations, indent=2), encoding="utf-8"
        )
        print(json.dumps({"native_go_evidence_root": str(owned), "observations": observations}))
    finally:
        for path in scratch.values():
            assert path.resolve().is_relative_to(owned) and path.parent == owned
            shutil.rmtree(path)
