"""Pure Go CLI business expectations and caller-allocated byte budgets."""

from __future__ import annotations

import hashlib
import importlib
import importlib.util
import json
from dataclasses import FrozenInstanceError, replace
from typing import Any, cast

import pytest

MODULE = "agent_hub.capabilities.go_project_business"


def test_business_values_exist() -> None:
    assert importlib.util.find_spec(MODULE) is not None


@pytest.fixture
def api() -> Any:
    if importlib.util.find_spec(MODULE) is None:
        pytest.skip("Business values missing; existence test is RED")
    return importlib.import_module(MODULE)


def case_payload(**changes: object) -> dict[str, object]:
    return {
        "id": "success", "criterion_id": "AC-001", "argv": ["--name", "world"],
        "stdin": "", "expected_stdout": "hello world\n", "expected_stderr": "",
        "expected_exit": 0,
    } | changes


def suite_payload() -> dict[str, object]:
    return {"schema_version": 1, "kind": "go_cli_business_v1", "cases": [
        case_payload(),
        case_payload(id="invalid", criterion_id="AC-002", argv=["--bad"],
                     expected_stdout="", expected_stderr="bad argument\n", expected_exit=2),
    ]}


def payload_cases(payload: dict[str, object]) -> list[dict[str, object]]:
    return cast(list[dict[str, object]], payload["cases"])


def encoded(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, allow_nan=False).encode("ascii")


def limits(api: Any, **changes: object) -> Any:
    return api.GoBusinessLimits(**({"max_cases": 200, "max_json_bytes": 100_000,
                                   "max_stream_bytes": 10_000, "max_argv_bytes": 10_000}
                                  | changes))


def test_roundtrip_and_digest_bind_every_expectation(api: Any) -> None:
    payload = suite_payload()
    suite = api.GoBusinessSuite.from_payload(payload, limits=limits(api))
    assert suite.to_payload() == payload
    assert suite.schema_version == 1 and suite.kind == "go_cli_business_v1"
    assert suite.criterion_ids == ("AC-001", "AC-002")
    assert suite.sha256 == hashlib.sha256(encoded(payload)).hexdigest()
    assert api.GoBusinessSuite.from_json(encoded(payload), limits=limits(api)) == suite
    assert api.GoBusinessSuite.from_json(json.dumps(payload), limits=limits(api)) == suite
    assert api.GoBusinessSuite(suite.cases) == suite
    assert suite.require_limits(limits(api)) is None
    case = suite.cases[0]
    assert api.GoBusinessCase.from_payload(case.to_payload()) == case
    for field, changed in {
        "id": "other", "criterion_id": "AC-003", "argv": ("--name=other",),
        "stdin": "input\n", "expected_stdout": "other\n", "expected_stderr": "err\n",
    }.items():
        modified = replace(suite, cases=(replace(case, **{field: changed}), suite.cases[1]))
        assert modified.sha256 != suite.sha256
    assert replace(suite, cases=(case, replace(suite.cases[1], expected_exit=255))).sha256 != suite.sha256
    assert replace(suite, cases=tuple(reversed(suite.cases))).sha256 != suite.sha256
    assert replace(suite, cases=(case, replace(suite.cases[1], criterion_id="AC-001"))).criterion_ids == (
        "AC-001",
    )


@pytest.mark.parametrize("criterion", ["AC-1000", "AC-100000"])
def test_criterion_identifiers_do_not_cap_allocated_project_size(api: Any, criterion: str) -> None:
    payload = case_payload(criterion_id=criterion)
    assert api.GoBusinessCase.from_payload(payload).to_payload() == payload


def test_values_are_immutable_and_payloads_are_detached(api: Any) -> None:
    payload = suite_payload()
    suite = api.GoBusinessSuite.from_payload(payload)
    for value, field, changed in [(suite, "cases", ()), (suite.cases[0], "stdin", "x"),
                                   (limits(api), "max_cases", 1)]:
        assert not hasattr(value, "__dict__")
        with pytest.raises(FrozenInstanceError):
            setattr(value, field, changed)
    exported = suite.to_payload()
    exported["cases"][0]["argv"].append("mutated")
    cast(list[str], payload_cases(payload)[0]["argv"]).append("also mutated")
    assert suite.cases[0].argv == ("--name", "world")


def test_flags_shell_metacharacters_and_complete_streams_are_literal_data(api: Any) -> None:
    argv = ["-n", "-1", "--out=file", "--", "", "a b", "$(touch nope)", ";", "|", "../x"]
    streams = " \r\n\t\x00\u4e2d\U0001f600\n "
    case = api.GoBusinessCase.from_payload(case_payload(
        argv=argv, stdin=streams, expected_stdout=streams, expected_stderr=streams,
    ))
    assert case.argv == tuple(argv)
    assert case.to_payload() == case_payload(argv=argv, stdin=streams,
                                           expected_stdout=streams, expected_stderr=streams)
    suite = api.GoBusinessSuite((case, api.GoBusinessCase.from_payload(
        case_payload(id="failure", argv=[], expected_exit=1),
    )))
    assert api.GoBusinessSuite.from_json(encoded(suite.to_payload()), limits=limits(api)) == suite


@pytest.mark.parametrize("field,bad", [
    ("id", ""), ("id", "has space"), ("id", "../bad"), ("id", "\u4e2d"),
    ("id", "bad\n"), ("id", True), ("criterion_id", "AC-000"),
    ("criterion_id", "AC-01"), ("criterion_id", "AC-0000"), ("criterion_id", "ac-001"),
    ("criterion_id", "AC-001\n"), ("criterion_id", True),
    ("argv", "--flag"), ("argv", ("--flag",)), ("argv", [True]),
    ("argv", [1]), ("argv", [None]), ("argv", ["x\x00y"]),
    ("stdin", True), ("stdin", None), ("expected_stdout", b"bytes"),
    ("expected_stderr", []), ("expected_exit", True), ("expected_exit", False),
    ("expected_exit", -1), ("expected_exit", 256), ("expected_exit", 0.0),
    ("expected_exit", float("nan")), ("expected_exit", float("inf")),
    ("expected_exit", float("-inf")), ("expected_exit", "0"),
])
def test_case_payload_rejects_malformed_values(api: Any, field: str, bad: object) -> None:
    with pytest.raises(ValueError):
        api.GoBusinessCase.from_payload(case_payload(**{field: bad}))


@pytest.mark.parametrize("field", ["id", "criterion_id", "stdin", "expected_stdout",
                                   "expected_stderr", "argv"])
@pytest.mark.parametrize("codepoints", [(0xD800,), (0xDFFF,), (0xD83D, 0xDE00)])
def test_unencodable_surrogates_are_rejected_at_all_entries(
    api: Any, field: str, codepoints: tuple[int, ...],
) -> None:
    surrogate = "".join(chr(codepoint) for codepoint in codepoints)
    data = case_payload(**{field: [surrogate] if field == "argv" else surrogate})
    with pytest.raises(ValueError):
        api.GoBusinessCase.from_payload(data)
    direct = dict(data)
    direct["argv"] = tuple(cast(list[str], direct["argv"]))
    with pytest.raises(ValueError):
        api.GoBusinessCase(**direct)
    suite = suite_payload()
    payload_cases(suite)[0] = data
    # JSON combines a valid surrogate pair into an encodable Unicode scalar.
    if surrogate != "\ud83d\ude00":
        with pytest.raises(ValueError):
            api.GoBusinessSuite.from_json(json.dumps(suite), limits=limits(api))


def test_direct_constructors_validate_types_and_do_not_coerce(api: Any) -> None:
    case = api.GoBusinessCase.from_payload(case_payload())
    for field, bad in [("argv", []), ("argv", (False,)), ("argv", ("\0",)),
                       ("expected_exit", True), ("stdin", 1), ("id", "bad id")]:
        with pytest.raises(ValueError):
            replace(case, **{field: bad})
    suite = api.GoBusinessSuite.from_payload(suite_payload())
    for field, bad in [("cases", list(suite.cases)), ("cases", (case, object())),
                       ("schema_version", True), ("schema_version", 1.0),
                       ("kind", True), ("kind", "go_cli_business_v2")]:
        with pytest.raises(ValueError):
            replace(suite, **{field: bad})


@pytest.mark.parametrize("value", [None, [], True, 1, "{}"])
def test_payload_requires_objects(api: Any, value: object) -> None:
    with pytest.raises(ValueError):
        api.GoBusinessCase.from_payload(value)
    with pytest.raises(ValueError):
        api.GoBusinessSuite.from_payload(value)


def test_fields_are_exact_and_cannot_carry_authority(api: Any) -> None:
    for payload, parse in [(case_payload(), api.GoBusinessCase.from_payload),
                            (suite_payload(), api.GoBusinessSuite.from_payload)]:
        for field in payload:
            with pytest.raises(ValueError):
                parse({key: value for key, value in payload.items() if key != field})
        for extra in ["approved", "pass", "command", "cwd", "max_cases", "sha256", "\ud800"]:
            with pytest.raises(ValueError):
                parse(payload | {extra: True})


@pytest.mark.parametrize("change", [
    {"schema_version": True}, {"schema_version": 1.0}, {"schema_version": 2},
    {"kind": False}, {"kind": "other"}, {"cases": ()}, {"cases": {}},
    {"cases": None}, {"cases": [True]},
])
def test_suite_payload_rejects_wrong_types(api: Any, change: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        api.GoBusinessSuite.from_payload(suite_payload() | change)


@pytest.mark.parametrize("exits", [(), (0,), (1,), (0, 0), (1, 255)])
def test_suite_requires_success_and_expected_failure(api: Any, exits: tuple[int, ...]) -> None:
    cases = tuple(api.GoBusinessCase.from_payload(case_payload(id=f"case-{i}", expected_exit=code))
                  for i, code in enumerate(exits))
    with pytest.raises(ValueError):
        api.GoBusinessSuite(cases)
    with pytest.raises(ValueError):
        api.GoBusinessSuite.from_payload(suite_payload() | {"cases": [c.to_payload() for c in cases]})


def test_duplicate_case_ids_are_invalid_even_across_criteria(api: Any) -> None:
    payload = suite_payload()
    payload_cases(payload)[1]["id"] = "success"
    with pytest.raises(ValueError):
        api.GoBusinessSuite.from_payload(payload)


@pytest.mark.parametrize("field", ["max_cases", "max_json_bytes", "max_stream_bytes", "max_argv_bytes"])
@pytest.mark.parametrize("bad", [True, False, 0, -1, 1.0, float("nan"), float("inf"), "100", None])
def test_budgets_are_positive_integers(api: Any, field: str, bad: object) -> None:
    with pytest.raises(ValueError):
        limits(api, **{field: bad})


def test_caller_budget_supports_more_than_128_cases(api: Any) -> None:
    payload = suite_payload() | {"cases": [
        case_payload(id=f"case-{i}", expected_exit=i % 2) for i in range(130)
    ]}
    budget = limits(api, max_cases=130)
    suite = api.GoBusinessSuite.from_payload(payload, limits=budget)
    assert len(suite.cases) == 130
    assert api.GoBusinessSuite.from_json(encoded(payload), limits=budget) == suite
    with pytest.raises(ValueError):
        suite.require_limits(replace(budget, max_cases=129))
    with pytest.raises(ValueError):
        api.GoBusinessSuite.from_payload(payload, limits=replace(budget, max_cases=129))


def test_exact_utf8_cumulative_budgets_and_json_boundary(api: Any) -> None:
    payload = suite_payload() | {"cases": [
        case_payload(argv=["\u4e2d", ""], stdin="\u4e2d", expected_stdout="\U0001f600",
                     expected_stderr="x"),
        case_payload(id="invalid", argv=["\U0001f600", "x"], stdin="ab", expected_stdout="",
                     expected_stderr="\u4e2d", expected_exit=255),
    ]}
    raw = encoded(payload)
    # Stream bytes: (3 + 4 + 1) + (2 + 0 + 3) = 13; argv: 3 + 0 + 4 + 1 = 8.
    budget = limits(api, max_cases=2, max_json_bytes=len(raw), max_stream_bytes=13, max_argv_bytes=8)
    suite = api.GoBusinessSuite.from_payload(payload, limits=budget)
    assert api.GoBusinessSuite.from_json(raw, limits=budget) == suite
    assert suite.to_payload() == payload
    for field in ["max_cases", "max_json_bytes", "max_stream_bytes", "max_argv_bytes"]:
        smaller = replace(budget, **{field: getattr(budget, field) - 1})
        with pytest.raises(ValueError):
            suite.require_limits(smaller)
        with pytest.raises(ValueError):
            api.GoBusinessSuite.from_payload(payload, limits=smaller)
        with pytest.raises(ValueError):
            api.GoBusinessSuite.from_json(raw, limits=smaller)
    with pytest.raises(ValueError):
        api.GoBusinessSuite.from_json(raw + b" ", limits=budget)


@pytest.mark.parametrize("field", ["stdin", "expected_stdout", "expected_stderr"])
def test_each_complete_stream_contributes_to_total(api: Any, field: str) -> None:
    payload = suite_payload() | {"cases": [
        case_payload(stdin="", expected_stdout="", expected_stderr=""),
        case_payload(id="invalid", stdin="", expected_stdout="", expected_stderr="", expected_exit=1),
    ]}
    payload_cases(payload)[0][field] = "a" * 5
    payload_cases(payload)[1][field] = "b" * 6
    suite = api.GoBusinessSuite.from_payload(payload)
    suite.require_limits(limits(api, max_stream_bytes=11))
    with pytest.raises(ValueError):
        suite.require_limits(limits(api, max_stream_bytes=10))


@pytest.mark.parametrize("raw", [
    b"", b"\xff", b"\xef\xbb\xbf{}", b"{} trailing", b"[]", b"null", b"true",
    b'{"schema_version":1,"schema_version":1}',
    b'{"schema_version":NaN}', b'{"schema_version":Infinity}',
    b'{"schema_version":-Infinity}', b'{"schema_version":1e999}',
    b"[" * 2000 + b"0" + b"]" * 2000,
    "\ud800", True, None, {},
])
def test_json_errors_are_bounded_value_errors(api: Any, raw: object) -> None:
    with pytest.raises(ValueError) as error:
        api.GoBusinessSuite.from_json(raw, limits=limits(api))
    assert len(str(error.value)) < 160


def test_json_rejects_nested_duplicate_keys_and_escaped_aliases(api: Any) -> None:
    raw = encoded(suite_payload())
    for malformed in [
        raw.replace(b'"schema_version":1', b'"schema_version":1,"schema_version":1'),
        raw.replace(b'"expected_exit":0', b'"expected_exit":0,"expected_exit":0'),
        raw.replace(b'"expected_exit":0', b'"expected_exit":0,"expected_\\u0065xit":0'),
        raw.replace(b'"expected_exit":0', b'"expected_exit":NaN'),
        raw.replace(b'"expected_exit":0', b'"expected_exit":1e999'),
        raw.replace(b'"expected_exit":0', b'"expected_exit":true'),
    ]:
        with pytest.raises(ValueError):
            api.GoBusinessSuite.from_json(malformed, limits=limits(api))


def test_json_counts_raw_utf8_not_character_count(api: Any) -> None:
    payload = suite_payload()
    payload_cases(payload)[0]["stdin"] = "\u4e2d"
    raw = json.dumps(payload, ensure_ascii=False, indent=4)
    assert len(raw.encode("utf-8")) > len(raw)
    with pytest.raises(ValueError):
        api.GoBusinessSuite.from_json(raw, limits=limits(api, max_json_bytes=len(raw)))
    assert api.GoBusinessSuite.from_json(raw, limits=limits(api, max_json_bytes=len(raw.encode())))


def test_canonical_json_must_fit_even_when_raw_unicode_is_smaller(api: Any) -> None:
    payload = suite_payload()
    payload_cases(payload)[0]["expected_stderr"] = "\u4e2d" * 10
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    canonical_size = len(encoded(payload))
    assert len(raw) < canonical_size
    with pytest.raises(ValueError):
        api.GoBusinessSuite.from_json(raw, limits=limits(api, max_json_bytes=canonical_size - 1))
    suite = api.GoBusinessSuite.from_json(raw, limits=limits(api, max_json_bytes=canonical_size))
    assert suite.to_payload() == payload


@pytest.mark.parametrize("bad", [None, True, {}, 100])
def test_budget_entry_points_require_limits_values(api: Any, bad: object) -> None:
    suite = api.GoBusinessSuite.from_payload(suite_payload())
    with pytest.raises(ValueError):
        suite.require_limits(bad)
    with pytest.raises(ValueError):
        api.GoBusinessSuite.from_json(encoded(suite.to_payload()), limits=bad)
    if bad is not None:
        with pytest.raises(ValueError):
            api.GoBusinessSuite.from_payload(suite.to_payload(), limits=bad)
