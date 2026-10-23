"""Budgeted data parser for two fixed uncached Go test argv variants.

This is NOT independently trusted execution authority. The caller must derive
the complete package/top-level Test IDs from trusted full source or protected
acceptance, never this report. It must bind actual argv, capture, exit, source,
toolchain, sandbox and execution identity independently. No process runs here.

Go test2json framing (including markFraming 0x16) can be forged by controlled
source/TestMain. Even a perfectly formed accepted stream does not prove bodies
ran. A full native negative-canary/provenance runner remains mandatory. Canary
parsing below separates identities and observations; it cannot attest isolation.

Supported subset: nonempty required top-level Test functions per package,
interleaved packages, parallel pause/cont, subtests, Examples, official OutputType,
attr, artifacts and interleaved BuildEvents. Benchmarks, skips, unknown fields/actions, cached/no-test
summaries and incomplete lifecycles fail closed. Metadata paths are inert data.
Schema references: https://pkg.go.dev/cmd/test2json and
https://go.dev/src/cmd/internal/test2json/test2json.go (observed 2026-10-21).
Build schema: https://go.dev/src/cmd/go/internal/help/helpdoc.go (HelpBuildJSON).
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import PurePosixPath, PureWindowsPath
from typing import TYPE_CHECKING, Literal, cast

if TYPE_CHECKING:
    from agent_hub.skills.sandbox.go_project_canary import GoOverlayCanary

# Compatibility names denote defaults, never non-increasable product thresholds.
MAX_GO_TEST_OUTPUT_BYTES = 1024 * 1024
MAX_GO_TEST_EVENTS = 10000
_FIELDS = frozenset(
    {
        "Time",
        "Action",
        "Package",
        "Test",
        "Elapsed",
        "Output",
        "OutputType",
        "FailedBuild",
        "Key",
        "Value",
        "Path",
    }
)
_ACTIONS = frozenset(
    {"start", "run", "pause", "cont", "pass", "fail", "output", "attr", "artifacts"}
)
_OUTPUT_TYPES = frozenset({"", "frame", "error", "error-continue"})
_TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?(?:Z|[+-]\d{2}:\d{2})")


class GoEvidenceError(ValueError):
    """Invalid input, inconsistent process observation or unusable NDJSON."""


@dataclass(frozen=True)
class GoEvidenceLimits:
    """Trusted finite resource allocation, separate from report/model input.

    Positive integer units are inherently finite (bool/float are rejected).
    Defaults preserve the initial parser budgets, not product size limits. The
    trusted caller may size allocations from its complete source inventory,
    protected tests, source bytes, invocation capture limit and workspace/tenant
    resources. It must enforce those budgets during collection too; raising this
    parser allocation never authorizes more output, memory or execution itself.
    No allocation is inferred from stdout. max_tests bounds both required Tests
    and the total observed named tests across packages, including subtests/Examples.
    """

    max_output_bytes: int = MAX_GO_TEST_OUTPUT_BYTES
    max_events: int = MAX_GO_TEST_EVENTS
    max_packages: int = 256
    max_tests: int = 1000

    def __post_init__(self) -> None:
        for value in (self.max_output_bytes, self.max_events, self.max_packages, self.max_tests):
            if type(value) is not int or value <= 0:
                raise GoEvidenceError("go_test_limits_invalid")


_DEFAULT_GO_EVIDENCE_LIMITS = GoEvidenceLimits()


@dataclass(frozen=True)
class GoPackageTests:
    package: str
    tests: tuple[str, ...]


@dataclass(frozen=True)
class GoTestExpectation:
    """Caller-trusted complete set; hashes bind source/protected acceptance.

    Construction does not establish trust. All packages require named Tests;
    Example and subtest events are preserved but cannot replace required Tests.
    """

    packages: tuple[GoPackageTests, ...]
    source_sha256: str
    protected_sha256: str


@dataclass(frozen=True)
class GoTestProcess:
    """Actual collector metadata, supplied outside project stdout.

    This data class is not a receipt validator. The runner must authenticate
    the binary and collection channel and enforce these exact argv/identities.
    argv is the actual capture, never normalized to remove flags or expand
    targets. Accepted suffixes are ``test -json -count=1 <exact packages>`` and
    ``test -json -count=1 -vet=off -mod=readonly ./...``. The latter's complete
    package inventory still comes from trusted source, never wildcard inference.
    """

    execution_id: str
    purpose: Literal["project_tests", "negative_canary"]
    argv: tuple[str, ...]
    exit_code: int
    timed_out: bool
    output_limited: bool


@dataclass(frozen=True)
class GoTestEvent:
    time: str
    action: str
    package: str
    test: str = ""
    elapsed: float | None = None
    output: str = ""
    output_type: str = ""
    failed_build: str = ""
    key: str = ""
    value: str = ""
    path: str = ""
    import_path: str = ""


@dataclass(frozen=True)
class GoNamedTestResult:
    package: str
    test: str
    status: Literal["pass", "fail"]


@dataclass(frozen=True)
class GoPackageResult:
    package: str
    status: Literal["pass", "fail"]
    failed_build: str


@dataclass(frozen=True)
class GoTestEvidence:
    status: Literal["passed", "test_failed", "build_failed", "process_failed"]
    execution_id: str
    report_sha256: str
    expected_sha256: str
    required_passed: tuple[tuple[str, str], ...]
    tests: tuple[GoNamedTestResult, ...]
    packages: tuple[GoPackageResult, ...]
    events: tuple[GoTestEvent, ...]
    authority: Literal["native_process_output"] = field(default="native_process_output", init=False)


@dataclass(frozen=True)
class GoCanaryIdentity:
    """Separate disposable execution; no identity is inferred from stdout."""

    project_execution_id: str
    execution_id: str
    expected: GoTestExpectation


@dataclass(frozen=True)
class GoCanaryEvidence:
    project_execution_id: str
    evidence: GoTestEvidence
    observed_expected_failure: bool
    authority: Literal["native_process_output"] = field(default="native_process_output", init=False)


def _need(condition: object, code: str = "go_test_output_invalid") -> None:
    if not condition:
        raise GoEvidenceError(code)


def _text(value: object, limit: int, *, empty: bool = False) -> str:
    _need(type(value) is str)
    text = cast(str, value)
    _need((empty or bool(text)) and len(text.encode("utf-8")) <= limit)
    _need(not any(ord(char) < 32 or ord(char) == 127 for char in text))
    return text


def _package(value: object) -> str:
    text = _text(value, 1024)
    # Import paths are identities, not filesystem targets or command options.
    _need(
        all(
            re.fullmatch(r"[A-Za-z0-9_.~+\-]+", part)
            and part not in {".", ".."}
            and not part.startswith("-")
            for part in text.split("/")
        )
    )
    return text


def _test_root(name: str, prefix: str) -> bool:
    # Match inventory's Go letter rules. New SDK letters may be Cn in Python;
    # the authenticated Go AST helper, not this decoder, owns their syntax.
    return (
        name.startswith(prefix)
        and all(
            char == "_"
            or unicodedata.category(char).startswith("L")
            or unicodedata.category(char) in {"Nd", "Cn"}
            for char in name
        )
        and (len(name) == len(prefix) or unicodedata.category(name[len(prefix)]) != "Ll")
    )


def _test(value: object, *, required: bool = False) -> str:
    name = _text(value, 512)
    parts = name.split("/")
    _need(len(parts) <= 32)
    _need(_test_root(parts[0], "Test") or (not required and _test_root(parts[0], "Example")))
    _need(not required or len(parts) == 1)
    return name


def _expected(
    expected: GoTestExpectation,
    limits: GoEvidenceLimits,
) -> tuple[dict[str, tuple[str, ...]], str]:
    _need(type(expected) is GoTestExpectation, "go_test_input_invalid")
    _need(
        type(expected.packages) is tuple and 0 < len(expected.packages) <= limits.max_packages,
        "go_test_input_invalid",
    )
    for digest in (expected.source_sha256, expected.protected_sha256):
        _need(
            type(digest) is str and re.fullmatch(r"[a-f0-9]{64}", digest), "go_test_input_invalid"
        )
    packages: dict[str, tuple[str, ...]] = {}
    test_count = 0
    for entry in expected.packages:
        _need(type(entry) is GoPackageTests, "go_test_input_invalid")
        package = _package(entry.package)
        _need(
            package not in packages
            and type(entry.tests) is tuple
            and 0 < len(entry.tests) <= limits.max_tests,
            "go_test_input_invalid",
        )
        test_count += len(entry.tests)
        _need(test_count <= limits.max_tests, "go_test_input_invalid")
        names = tuple(_test(name, required=True) for name in entry.tests)
        _need(len(set(names)) == len(names), "go_test_input_invalid")
        packages[package] = tuple(sorted(names))
    core = {
        "schema_version": 1,
        "packages": sorted(packages.items()),
        "source_sha256": expected.source_sha256,
        "protected_sha256": expected.protected_sha256,
    }
    encoded = json.dumps(core, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode()
    return packages, hashlib.sha256(encoded).hexdigest()


def _process(
    process: GoTestProcess,
    packages: dict[str, tuple[str, ...]],
    purpose: str,
    *,
    expected_command: tuple[str, ...] | None = None,
) -> None:
    _need(type(process) is GoTestProcess, "go_test_process_invalid")
    _text(process.execution_id, 256)
    _need(
        process.purpose == purpose
        and type(process.exit_code) is int
        and -(2**31) <= process.exit_code < 2**31
        and type(process.timed_out) is bool
        and not process.timed_out
        and type(process.output_limited) is bool
        and not process.output_limited,
        "go_test_process_invalid",
    )
    _need(
        type(process.argv) is tuple
        and len(process.argv) > 0
        and all(type(arg) is str for arg in process.argv),
        "go_test_process_invalid",
    )
    _text(process.argv[0], 4096)
    if expected_command is not None:
        # Only the closed typed-overlay entry point supplies this command. Keep
        # the captured argv intact: compare every suffix field, never normalize.
        binary = process.argv[0]
        posix, windows = PurePosixPath(binary), PureWindowsPath(binary)
        _need(
            purpose == "negative_canary"
            and process.argv[1:] == expected_command[1:]
            and (
                (posix.is_absolute() and ".." not in posix.parts)
                or (windows.is_absolute() and ".." not in windows.parts)
            ),
            "go_test_process_invalid",
        )
        return
    exact_packages = (
        len(process.argv) == 4 + len(packages)
        and process.argv[1:4] == ("test", "-json", "-count=1")
        and len(set(process.argv[4:])) == len(packages)
        and set(process.argv[4:]) == set(packages)
    )
    mount_target = process.argv[1:] == (
        "test",
        "-json",
        "-count=1",
        "-vet=off",
        "-mod=readonly",
        "./...",
    )
    _need(
        exact_packages or mount_target,
        "go_test_process_invalid",
    )


def _unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        _need(key not in result)
        result[key] = value
    return result


def _nonfinite(_: str) -> object:
    raise GoEvidenceError("go_test_output_invalid")


def _event(raw: str) -> GoTestEvent:
    value = cast(object, json.loads(raw, object_pairs_hook=_unique, parse_constant=_nonfinite))
    _need(type(value) is dict)
    item = cast(dict[str, object], value)
    if item.get("Action") in ("build-output", "build-fail"):
        action = cast(str, item["Action"])
        required = (
            {"Action", "ImportPath", "Output"}
            if action == "build-output"
            else {"Action", "ImportPath"}
        )
        _need(set(item) == required)
        import_path = _text(item["ImportPath"], 1024)
        output = item.get("Output", "")
        _need(type(output) is str)
        cast(str, output).encode("utf-8")
        return GoTestEvent("", action, "", output=cast(str, output), import_path=import_path)
    _need(set(item) <= _FIELDS and {"Time", "Action", "Package"} <= set(item))
    stamp = _text(item["Time"], 64)
    _need(_TIMESTAMP.fullmatch(stamp))
    if not stamp.endswith("Z"):
        _need(int(stamp[-5:-3]) < 24 and int(stamp[-2:]) < 60)
    datetime.fromisoformat(stamp)
    action = _text(item["Action"], 32)
    _need(action in _ACTIONS)
    package = _package(item["Package"])
    name = _test(item["Test"]) if "Test" in item else ""
    elapsed: float | None = None
    if "Elapsed" in item:
        _need(type(item["Elapsed"]) in (int, float) and action in {"pass", "fail"})
        elapsed = float(cast(int | float, item["Elapsed"]))
        _need(math.isfinite(elapsed) and elapsed >= 0)
    if action in {"pass", "fail"}:
        _need(elapsed is not None)
    for key in ("Output", "OutputType"):
        _need(key not in item or action == "output")
    output = item.get("Output", "")
    _need(type(output) is str)
    cast(str, output).encode("utf-8")
    output_type = _text(item.get("OutputType", ""), 32, empty=True)
    _need(output_type in _OUTPUT_TYPES)
    _need(action != "output" or "Output" in item)
    failed_build = _text(item["FailedBuild"], 1024) if "FailedBuild" in item else ""
    _need(not failed_build or (action == "fail" and not name))
    for key in ("Key", "Value"):
        _need(key not in item or action == "attr")
    _need("Path" not in item or action == "artifacts")
    key = _text(item.get("Key", ""), 512, empty=action != "attr")
    # Attribute values may be empty and contain whitespace, but never invalid Unicode.
    attr_value = item.get("Value", "")
    _need(type(attr_value) is str and (action != "attr" or "Value" in item))
    cast(str, attr_value).encode("utf-8")
    path = _text(item.get("Path", ""), 4096, empty=action != "artifacts")
    _need(action not in {"attr", "artifacts"} or bool(name))
    return GoTestEvent(
        stamp,
        action,
        package,
        name,
        elapsed,
        cast(str, output),
        output_type,
        failed_build,
        key,
        cast(str, attr_value),
        path,
    )


@dataclass
class _PackageState:
    started: bool = False
    terminal: GoPackageResult | None = None
    tests: dict[str, str] = field(default_factory=dict)
    # One joined package-output stream handles summaries split across events.
    output: list[str] = field(default_factory=list)


def _consume(event: GoTestEvent, state: _PackageState, required: tuple[str, ...]) -> None:
    action, name = event.action, event.test
    _need(state.terminal is None)
    if action == "start":
        _need(not state.started and not name)
        state.started = True
        return
    _need(state.started)
    if action == "output":
        # test2json can attribute trailing output to the last completed test.
        _need(not name or name in state.tests)
        if not name:
            state.output.append(event.output)
        return
    if not name:
        _need(action in {"pass", "fail"})
        if event.failed_build:
            _need(not state.tests)
        else:
            _need(
                set(required) <= state.tests.keys()
                and all(status in {"pass", "fail"} for status in state.tests.values())
            )
        if action == "pass":
            _need(all(status == "pass" for status in state.tests.values()))
        state.terminal = GoPackageResult(
            event.package, cast(Literal["pass", "fail"], action), event.failed_build
        )
        return
    root = name.split("/", 1)[0]
    _need(root in required or _test_root(root, "Example"))
    if action == "run":
        _need(name not in state.tests)
        if "/" in name:
            _need(state.tests.get(root) == "run")
        state.tests[name] = "run"
    elif action == "pause":
        _need(state.tests.get(name) == "run")
        state.tests[name] = "pause"
    elif action == "cont":
        _need(state.tests.get(name) == "pause")
        state.tests[name] = "run"
    elif action in {"pass", "fail"}:
        _need(state.tests.get(name) == "run")
        # t.Run names may themselves contain '/', so prefixes below the root
        # do not reliably identify actual parents. Only enforce the root group.
        if "/" not in name:
            children = [
                status for child, status in state.tests.items() if child.startswith(name + "/")
            ]
            _need(all(status in {"pass", "fail"} for status in children))
            _need(action != "pass" or all(status == "pass" for status in children))
        state.tests[name] = action
    else:
        _need(action in {"attr", "artifacts"} and state.tests.get(name) in {"run", "pause"})


def _summaries(package: str, output: str) -> None:
    # Match official package summaries only, not arbitrary occurrences in test logs.
    # This is conservative stream validation, not authentication of the summary.
    name = re.escape(package)
    pattern = rf"^(?:ok[ \t]+{name}[ \t]+(?:\(cached\)(?:[ \t].*)?|[^\n]*\[no tests to run\])|\?[ \t]+{name}[ \t]+\[no test files\])[ \t]*$"
    _need(re.search(pattern, output, re.MULTILINE) is None)


def _parse(
    raw: bytes,
    expected: GoTestExpectation,
    process: GoTestProcess,
    purpose: str,
    limits: GoEvidenceLimits,
    *,
    expected_command: tuple[str, ...] | None = None,
) -> GoTestEvidence:
    _need(type(limits) is GoEvidenceLimits, "go_test_limits_invalid")
    packages, expected_sha = _expected(expected, limits)
    _process(process, packages, purpose, expected_command=expected_command)
    _need(type(raw) is bytes and 0 < len(raw) <= limits.max_output_bytes)
    _need(raw.endswith(b"\n"))
    _need(raw.count(b"\n") <= limits.max_events)
    lines = raw.decode("utf-8").split("\n")[:-1]
    events = tuple(_event(line) for line in lines)
    states = {package: _PackageState() for package in packages}
    failed_builds: set[str] = set()
    observed_tests = 0
    for event in events:
        if event.import_path:
            _need(event.import_path not in failed_builds)
            if event.action == "build-fail":
                failed_builds.add(event.import_path)
            continue
        _need(event.package in states)
        if event.action == "run":
            observed_tests += 1
            _need(observed_tests <= limits.max_tests, "go_test_budget_exceeded")
        _consume(event, states[event.package], packages[event.package])
    results: list[GoPackageResult] = []
    tests: list[GoNamedTestResult] = []
    for package, state in sorted(states.items()):
        _need(state.started and state.terminal is not None)
        assert state.terminal is not None
        _summaries(package, "".join(state.output))
        results.append(state.terminal)
        tests.extend(
            GoNamedTestResult(package, name, cast(Literal["pass", "fail"], status))
            for name, status in sorted(state.tests.items())
        )
    _need(failed_builds <= {result.failed_build for result in results if result.failed_build})
    failed = any(result.status == "fail" for result in results)
    _need(not failed or process.exit_code != 0, "go_test_process_inconsistent")
    status: Literal["passed", "test_failed", "build_failed", "process_failed"]
    if any(result.failed_build for result in results):
        status = "build_failed"
    elif any(item.status == "fail" for item in tests):
        status = "test_failed"
    elif process.exit_code != 0:
        status = "process_failed"
    else:
        status = "passed"
    passed = tuple(
        (item.package, item.test)
        for item in tests
        if item.status == "pass" and item.test in packages[item.package]
    )
    return GoTestEvidence(
        status,
        process.execution_id,
        hashlib.sha256(raw).hexdigest(),
        expected_sha,
        passed,
        tuple(tests),
        tuple(results),
        events,
    )


def parse_go_test_evidence(
    raw: bytes,
    *,
    expected: GoTestExpectation,
    process: GoTestProcess,
    limits: GoEvidenceLimits = _DEFAULT_GO_EVIDENCE_LIMITS,
) -> GoTestEvidence:
    """Parse complete native output, not authorization or proof against forgery.

    Malformed/truncated/skipped/cached/missing evidence raises GoEvidenceError.
    Complete test/build/process failures remain typed diagnostic observations.
    Explicit FailedBuild permits unrun tests ONLY in that failed-build package;
    ordinary logic failures still require every required run->terminal exactly once.
    """
    try:
        return _parse(raw, expected, process, "project_tests", limits)
    except (ValueError, TypeError, UnicodeError, OverflowError, RecursionError) as exc:
        if isinstance(exc, GoEvidenceError):
            raise
        raise GoEvidenceError("go_test_output_invalid") from None


def parse_go_canary_evidence(
    raw: bytes,
    *,
    identity: GoCanaryIdentity,
    process: GoTestProcess,
    limits: GoEvidenceLimits = _DEFAULT_GO_EVIDENCE_LIMITS,
) -> GoCanaryEvidence:
    """Observe a separate negative canary; stdout never authenticates its body.

    All expected canary Tests must fail, with a nonzero exit and no build failure.
    A passing canary, build failure or unrelated process failure is not a negative
    gate pass. The native runner must protect the canary and prove its provenance.
    This legacy entry point rejects overlay/random-name argv. The separate typed
    overlay parser admits only its closed contract, never arbitrary canary args.
    """
    try:
        _need(type(identity) is GoCanaryIdentity, "go_canary_identity_invalid")
        _text(identity.project_execution_id, 256)
        _text(identity.execution_id, 256)
        _need(
            type(process) is GoTestProcess
            and identity.execution_id == process.execution_id
            and identity.execution_id != identity.project_execution_id,
            "go_canary_identity_invalid",
        )
        evidence = _parse(raw, identity.expected, process, "negative_canary", limits)
        required = {
            (entry.package, name) for entry in identity.expected.packages for name in entry.tests
        }
        failed = {(item.package, item.test) for item in evidence.tests if item.status == "fail"}
        observed = evidence.status == "test_failed" and required <= failed
        return GoCanaryEvidence(identity.project_execution_id, evidence, observed)
    except (ValueError, TypeError, UnicodeError, OverflowError, RecursionError) as exc:
        if isinstance(exc, GoEvidenceError):
            raise
        raise GoEvidenceError("go_test_output_invalid") from None


def parse_go_overlay_canary_evidence(
    raw: bytes,
    *,
    canary: GoOverlayCanary,
    process: GoTestProcess,
    limits: GoEvidenceLimits = _DEFAULT_GO_EVIDENCE_LIMITS,
) -> GoCanaryEvidence:
    """Observe only the fixed trusted overlay's single nonce Test failure.

    Source/protected hashes and expected IDs come solely from the caller's typed
    canary. The full argv must match canary.command except for an absolute actual
    SDK executable path, whose authenticity remains the native runner's duty.
    No argv is rewritten and no arbitrary overlay, target, regex or flag is admitted.

    Project init can read the nonce and forge test2json framing, even without
    TestMain. This is native_process_output, never independent business proof.
    The runner must bind this canary to the actual sealed base source + overlay,
    SDK, isolation, live scope/approval and distinct process capture. A driver
    exit other than 1, passing test or compile failure cannot pass this gate.
    """
    # The contract imports evidence identities; keep this import lazy to avoid
    # a runtime cycle and leave non-overlay callers independent of that module.
    from agent_hub.skills.sandbox.go_project_canary import GoOverlayCanary

    try:
        _need(type(canary) is GoOverlayCanary, "go_canary_identity_invalid")
        checked = GoOverlayCanary(
            canary.project_execution_id,
            canary.execution_id,
            canary.module_path,
            canary.nonce,
            canary.source_sha256,
            canary.protected_sha256,
        )
        _need(
            type(process) is GoTestProcess and process.execution_id == checked.execution_id,
            "go_canary_identity_invalid",
        )
        evidence = _parse(
            raw,
            checked.identity.expected,
            process,
            "negative_canary",
            limits,
            expected_command=checked.command,
        )
        _need(
            all(
                item.package == checked.module_path and item.test == checked.test_name
                for item in evidence.tests
            ),
            "go_canary_identity_invalid",
        )
        observed = (
            process.exit_code == 1
            and evidence.status == "test_failed"
            and len(evidence.tests) == 1
            and evidence.tests[0].status == "fail"
        )
        return GoCanaryEvidence(checked.project_execution_id, evidence, observed)
    except (
        ValueError,
        TypeError,
        UnicodeError,
        OverflowError,
        RecursionError,
        AttributeError,
    ) as exc:
        if isinstance(exc, GoEvidenceError):
            raise
        raise GoEvidenceError("go_canary_identity_invalid") from None
