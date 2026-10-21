"""Inventory protocol tests and opt-in official-SDK helper black-box tests.

The first recipe conservatively validates all captured Go syntax/TestMain,
including inactive files. Native-source suffixes are forbidden everywhere.
"""

from __future__ import annotations

import hashlib
import importlib
import importlib.util
import json
import os
import subprocess
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import Any

import pytest

MODULE = "agent_hub.skills.sandbox.go_project_inventory"
PKG = "example.test/ledger"
HELPER = Path(__file__).resolve().parents[4] / "src/agent_hub/skills/sandbox/go_project_inspect.go"
MAIN = b"package main\nfunc main() {}\n"
TEST = b'import "testing"\nfunc TestLedger(t *testing.T) {}\n'
TEST_FILE = b"package main\n" + TEST


@pytest.fixture
def api() -> Any:
    assert importlib.util.find_spec(MODULE) is not None, "Go inventory decoder is missing"
    return importlib.import_module(MODULE)


def payload() -> dict[str, Any]:
    return {"schema_version": 1, "kind": "go_source_inventory_v1", "module_path": PKG,
            "packages": [PKG], "expected_tests": [{"package": PKG, "name": "TestLedger"}],
            "project_main_package": PKG,
            "source_go_files": [{"path": "main.go", "raw_sha256": hashlib.sha256(MAIN).hexdigest(),
                                 "size_bytes": len(MAIN)},
                                {"path": "main_test.go", "raw_sha256": hashlib.sha256(TEST_FILE).hexdigest(),
                                 "size_bytes": len(TEST_FILE)}]}


def test_decode_roundtrip_and_grouping_are_immutable(api: Any) -> None:
    item = api.parse_go_source_inventory(json.dumps(payload()).encode(), expected_module=PKG,
        expected_source={"main.go": (hashlib.sha256(MAIN).hexdigest(), len(MAIN)),
                         "main_test.go": (hashlib.sha256(TEST_FILE).hexdigest(), len(TEST_FILE)),
                         "ignored_windows.go": ("a" * 64, 3)})
    assert item.packages == (PKG,)
    assert item.expected_tests == ((PKG, "TestLedger"),)
    assert item.source_go_files[0].size_bytes == len(MAIN)
    assert item.to_payload() == payload()
    assert api.GoSourceInventory.from_payload(payload(), expected_module=PKG) == item
    expected = item.to_test_expectation(source_sha256="a" * 64, protected_sha256="b" * 64)
    assert expected.packages[0].package == PKG
    assert expected.packages[0].tests == ("TestLedger",)
    with pytest.raises(FrozenInstanceError):
        item.module_path = "other"


@pytest.mark.parametrize("change", [
    {"schema_version": True}, {"kind": "other"}, {"module_path": "other.test/m"},
    {"project_main_package": PKG + "/sub"}, {"packages": []},
    {"packages": [PKG, PKG]}, {"packages": [PKG + "/z", PKG]},
    {"packages": [PKG, "elsewhere.test/lib"]}, {"expected_tests": []},
    {"expected_tests": [{"package": PKG, "name": "TestMain"}]},
    {"expected_tests": [{"package": PKG, "name": "Testlower"}]},
    {"expected_tests": [{"package": PKG, "name": "TestLedger"}] * 2},
    {"expected_tests": [{"package": PKG + "/missing", "name": "TestA"}]},
    {"extra": "source body must not be accepted"},
    {"source_go_files": [{"path": "../main.go", "raw_sha256": "a" * 64, "size_bytes": 1}]},
    {"source_go_files": [{"path": "main.go", "raw_sha256": "a" * 64, "size_bytes": True}]},
    {"source_go_files": [{"path": "main.go", "raw_sha256": "a" * 63, "size_bytes": 1}]},
    {"source_go_files": [{"path": "main.go", "raw_sha256": "a" * 64, "size_bytes": 1}]},
])
def test_decoder_rejects_unbound_or_malformed_inventory(api: Any, change: dict[str, Any]) -> None:
    with pytest.raises(api.GoInventoryError):
        api.parse_go_source_inventory(json.dumps(payload() | change).encode(), expected_module=PKG)


def test_decoder_preserves_go_unicode_and_official_test_names(api: Any) -> None:
    names = ("Test", "Test9", "Test_", "Test\u03a9", "Test\u1c89", "Test\u4e2d")
    data = payload() | {"expected_tests": [{"package": PKG, "name": name} for name in names]}
    item = api.parse_go_source_inventory(json.dumps(data).encode(), expected_module=PKG)
    assert item.expected_tests == tuple((PKG, name) for name in names)


@pytest.mark.parametrize("raw", [b'{"schema_version":1,"schema_version":1}', b'{}{}', b'NaN', b'\xff'])
def test_decoder_rejects_duplicate_keys_non_json_and_invalid_utf8(api: Any, raw: bytes) -> None:
    with pytest.raises(api.GoInventoryError):
        api.parse_go_source_inventory(raw, expected_module=PKG)


def test_decoder_checks_source_bytes_and_output_budget(api: Any) -> None:
    raw = json.dumps(payload()).encode()
    for source in ({}, {"main.go": ("a" * 64, len(MAIN))},
                   {"main.go": (hashlib.sha256(MAIN).hexdigest(), len(MAIN) + 1)}):
        with pytest.raises(api.GoInventoryError):
            api.parse_go_source_inventory(raw, expected_module=PKG, expected_source=source)
    with pytest.raises(api.GoInventoryError):
        api.parse_go_source_inventory(raw, expected_module=PKG, max_output_bytes=len(raw) - 1)


def test_decoder_threads_caller_package_and_test_budgets(api: Any) -> None:
    packages = [PKG, *(PKG + f"/p{i:03}" for i in range(256))]
    tests = [(p, "TestOne") for p in packages] + [(PKG, f"Test{i:04}") for i in range(744)]
    sources = [{"path": (p[len(PKG) + 1:] + "/" if p != PKG else "") + name,
                "raw_sha256": "a" * 64, "size_bytes": 1}
               for p in packages for name in ("main.go", "main_test.go")]
    data = payload() | {"packages": packages,
        "expected_tests": [{"package": p, "name": name} for p, name in sorted(tests)],
        "source_go_files": sorted(sources, key=lambda row: str(row["path"]))}
    with pytest.raises(api.GoInventoryError):
        api.parse_go_source_inventory(json.dumps(data).encode(), expected_module=PKG)
    budgets = {"max_packages": 300, "max_tests": 2000}
    item = api.parse_go_source_inventory(json.dumps(data).encode(), expected_module=PKG, **budgets)
    assert len(item.packages) == 257 and len(item.expected_tests) == 1001
    assert api.GoSourceInventory.from_payload(data, expected_module=PKG, **budgets) == item
    expected = item.to_test_expectation(source_sha256="a" * 64, protected_sha256="b" * 64, **budgets)
    assert len(expected.packages) == 257 and sum(len(p.tests) for p in expected.packages) == 1001
    for key in budgets:
        for bad in (0, -1, True, 1.5):
            with pytest.raises(api.GoInventoryError):
                api.parse_go_source_inventory(json.dumps(data).encode(), expected_module=PKG,
                                              **(budgets | {key: bad}))


@pytest.fixture(scope="module")
def native_helper(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, dict[str, str]]:
    configured = os.environ.get("AGENT_HUB_GO_PROOF_EXECUTABLE")
    if not configured:
        pytest.skip("official Go SDK not explicitly configured; native helper NOT RUN")
    go = Path(configured)
    assert go.is_absolute() and go.is_file(), "configured official Go executable is unavailable"
    assert HELPER.is_file(), "trusted Go inventory helper is missing"
    work = tmp_path_factory.mktemp("go-inventory-compiler")
    env = {key: value for key, value in os.environ.items()
           if key.upper() in {"SYSTEMROOT", "WINDIR", "PATH"}}
    env.update({"GOROOT": str(go.parent.parent), "GOTOOLCHAIN": "local", "GOENV": "off",
                "GOWORK": "off", "GOPROXY": "off", "GOSUMDB": "off", "GOTELEMETRY": "off",
                "CGO_ENABLED": "0", "GOOS": "windows" if os.name == "nt" else "linux",
                "GOARCH": "amd64"})
    for key in ("GOCACHE", "GOPATH", "GOMODCACHE", "TMPDIR", "TMP", "TEMP"):
        directory = work / key.lower()
        directory.mkdir()
        env[key] = str(directory)
    binary = work / ("inventory.exe" if os.name == "nt" else "inventory")
    compiled = subprocess.run([str(go), "build", "-trimpath", "-o", str(binary), str(HELPER)],
        cwd=work, env=env, capture_output=True, timeout=180, check=False)
    assert compiled.returncode == 0, compiled.stderr.decode(errors="replace")
    return binary, env


def fixture(root: Path, extras: dict[str, bytes] | None = None) -> None:
    files = {"go.mod": b"module example.test/ledger\ngo 1.27\n", "main.go": MAIN,
             "main_test.go": b"package main\n" + TEST}
    files.update(extras or {})
    for name, data in files.items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)


def inspect(native: tuple[Path, dict[str, str]], root: Path,
            extra: list[str] | None = None, *, files: int = 100, size: int = 100000,
            milliseconds: int = 10000, packages: int = 256, tests: int = 1000,
) -> subprocess.CompletedProcess[bytes]:
    binary, env = native
    return subprocess.run([str(binary), "--root", str(root), "--module", PKG,
        "--max-files", str(files), "--max-bytes", str(size), "--timeout-ms", str(milliseconds),
        "--max-packages", str(packages), "--max-tests", str(tests), *(extra or [])],
        env=env | {"GOOS": "windows", "GOARCH": "arm64", "CGO_ENABLED": "1"},
        capture_output=True, timeout=15, check=False)


def test_native_linux_selection_external_tests_unicode_and_no_execution(
    api: Any, native_helper: tuple[Path, dict[str, str]], tmp_path: Path,
) -> None:
    fixture(tmp_path, {
        "active_linux_amd64.go": b"package main\nfunc init() { panic(\"MUST NOT RUN\") }\n",
        "inactive_windows.go": b"package main\n",
        "inactive_test.go": b"//go:build windows\n\npackage main\n" + TEST.replace(b"TestLedger", b"TestInactive"),
        "external_test.go": b"package main_test\n" + TEST.replace(b"TestLedger", "Test\u03a9".encode()),
        "names_test.go": b"package main\nimport \"testing\"\n"
                         b"func Test(t *testing.T) {}\nfunc Test9(t *testing.T) {}\n"
                         b"func Testlower(t *testing.T) {}\n" +
                         "func Test\u1c89(t *testing.T) {}\n".encode(),
        "sub/lib.go": b"package sub\n",
        "sub/lib_test.go": b"package sub\n" + TEST.replace(b"TestLedger", b"TestSub"),
    })
    result = inspect(native_helper, tmp_path)
    assert result.returncode == 0, result.stderr
    assert result.stderr == b"" and b"MUST NOT RUN" not in result.stdout
    item = api.parse_go_source_inventory(result.stdout, expected_module=PKG,
        expected_source={p.relative_to(tmp_path).as_posix(): (hashlib.sha256(p.read_bytes()).hexdigest(),
                         p.stat().st_size) for p in tmp_path.rglob("*.go")})
    assert item.packages == (PKG, PKG + "/sub")
    assert item.expected_tests == ((PKG, "Test"), (PKG, "Test9"), (PKG, "TestLedger"),
                                   (PKG, "Test\u03a9"), (PKG, "Test\u1c89"), (PKG + "/sub", "TestSub"))
    assert {row.path for row in item.source_go_files} == {
        "main.go", "main_test.go", "active_linux_amd64.go", "external_test.go",
        "names_test.go", "sub/lib.go", "sub/lib_test.go"}


@pytest.mark.parametrize(("extras", "code"), [
    ({"main.go": b"package library\n"}, "go_inventory_main_required"),
    ({"main_test.go": b"package main\n"}, "go_inventory_tests_missing"),
    ({"main_test.go": b"package main\nimport \"testing\"\nfunc TestMain(m *testing.M) {}\n"},
     "go_inventory_testmain_forbidden"),
    ({"more_test.go": b"package main\n" + TEST}, "go_inventory_duplicate"),
    ({"bad.go": b"package main\nimport _ \"evil.test/payload\"\n"}, "go_inventory_import_rejected"),
    ({"bad.go": b"package main\nimport _ \"../escape\"\n"}, "go_inventory_import_rejected"),
    ({"bad.go": b'package main\nimport _ "example.test/ledger/missing"\n'}, "go_inventory_import_rejected"),
    ({"bad.go": b"package main\nimport \"C\"\n"}, "go_inventory_native_import_unsupported"),
    ({"bad.go": b'package main\nimport _ "runtime/cgo"\n'}, "go_inventory_native_import_unsupported"),
    ({"bad.go": b"package main\nfunc ("}, "go_inventory_syntax_invalid"),
    ({"bad_test.go": b"package main\nfunc TestWrong() {}\n"}, "go_inventory_test_invalid"),
    ({"sub/go.mod": b"module nested\n"}, "go_inventory_module_rejected"),
    ({"sub/lib.go": b"package sub\n"}, "go_inventory_tests_missing"),
    pytest.param({"Main.go": b"package main\n"}, "go_inventory_duplicate",
                 marks=pytest.mark.skipif(os.name == "nt", reason="host cannot create case-distinct files")),
])
def test_native_rejects_unsafe_or_incomplete_inventory(
    native_helper: tuple[Path, dict[str, str]], tmp_path: Path, extras: dict[str, bytes], code: str,
) -> None:
    fixture(tmp_path, extras)
    result = inspect(native_helper, tmp_path)
    assert (result.returncode, result.stdout, result.stderr) == (2, b"", (code + "\n").encode())


@pytest.mark.parametrize("suffix", (".s", ".syso"))
def test_native_rejects_assembly_and_object_without_compiling_project(
    native_helper: tuple[Path, dict[str, str]], tmp_path: Path, suffix: str,
) -> None:
    fixture(tmp_path, {"native" + suffix: b"synthetic native payload, never compiled\n"})
    result = inspect(native_helper, tmp_path)
    assert (result.returncode, result.stdout, result.stderr) == (
        2, b"", b"go_inventory_native_source_unsupported\n")


@pytest.mark.parametrize("suffix", (
    ".c", ".cc", ".cpp", ".cxx", ".h", ".hh", ".hpp", ".s", ".syso", ".m",
    ".f", ".for", ".f90", ".swig", ".swigcxx",
))
@pytest.mark.parametrize("uppercase", (False, True))
def test_native_rejects_native_suffixes_even_in_ignored_snapshot_paths(
    native_helper: tuple[Path, dict[str, str]], tmp_path: Path, suffix: str, uppercase: bool,
) -> None:
    extension = suffix.upper() if uppercase else suffix
    fixture(tmp_path, {"testdata/nested/native" + extension: b"synthetic native payload\n"})
    result = inspect(native_helper, tmp_path)
    assert (result.returncode, result.stdout, result.stderr) == (
        2, b"", b"go_inventory_native_source_unsupported\n")


@pytest.mark.parametrize(("path", "data", "code"), [
    ("inactive_windows.go", b"package main\nfunc (", "go_inventory_syntax_invalid"),
    ("testdata/bad.go", b"package main\nfunc (", "go_inventory_syntax_invalid"),
    ("_ignored/bad.go", b"package main\nfunc (", "go_inventory_syntax_invalid"),
    ("inactive_windows_test.go", b'package main\nimport "testing"\nfunc TestMain(m *testing.M) {}\n',
     "go_inventory_testmain_forbidden"),
])
def test_native_preserves_explicit_v1_conservative_inactive_source_boundary(
    native_helper: tuple[Path, dict[str, str]], tmp_path: Path, path: str, data: bytes, code: str,
) -> None:
    fixture(tmp_path, {path: data})
    result = inspect(native_helper, tmp_path)
    assert (result.returncode, result.stdout, result.stderr) == (2, b"", (code + "\n").encode())


@pytest.mark.parametrize("extra", [["--root", "relative"], ["--unknown", "x"],
                                   ["--max-files", "1"], ["--timeout-ms", "0"]])
def test_native_rejects_duplicate_unknown_and_invalid_flags(
    native_helper: tuple[Path, dict[str, str]], tmp_path: Path, extra: list[str],
) -> None:
    fixture(tmp_path)
    result = inspect(native_helper, tmp_path, extra)
    assert result.returncode == 2 and result.stdout == b""
    assert result.stderr == b"go_inventory_arguments_invalid\n"


def test_native_file_and_byte_budgets_cover_non_go_entries(
    native_helper: tuple[Path, dict[str, str]], tmp_path: Path,
) -> None:
    fixture(tmp_path, {"large.txt": b"x" * 100001})
    result = inspect(native_helper, tmp_path)
    assert (result.returncode, result.stdout, result.stderr) == (2, b"", b"go_inventory_limit\n")


def test_native_entry_budget_counts_directories(
    native_helper: tuple[Path, dict[str, str]], tmp_path: Path,
) -> None:
    fixture(tmp_path)
    (tmp_path / "empty").mkdir()
    result = inspect(native_helper, tmp_path, files=3)
    assert result.returncode == 2 and result.stderr == b"go_inventory_limit\n"


def test_native_deadline_covers_a_single_large_input(
    native_helper: tuple[Path, dict[str, str]], tmp_path: Path,
) -> None:
    fixture(tmp_path, {"large.go": b"package main\n" + b"// comment\n" * 800000})
    result = inspect(native_helper, tmp_path, size=10000000, milliseconds=1)
    assert result.returncode == 2 and result.stdout == b""
    assert result.stderr == b"go_inventory_timeout\n"


def test_native_caller_can_exceed_default_package_and_test_budgets(
    api: Any, native_helper: tuple[Path, dict[str, str]], tmp_path: Path,
) -> None:
    extras = {f"p{i:03}/lib.go": b"package lib\n" for i in range(256)}
    extras.update({f"p{i:03}/lib_test.go": b"package lib\n" + TEST for i in range(256)})
    extras["main_test.go"] = b'package main\nimport "testing"\n' + b"".join(
        f"func Test{i:04}(t *testing.T) {{}}\n".encode() for i in range(1001))
    fixture(tmp_path, extras)
    for packages, tests in ((256, 2000), (300, 1000)):
        result = inspect(native_helper, tmp_path, files=2000, size=1000000,
                         packages=packages, tests=tests)
        assert result.returncode == 2 and result.stderr == b"go_inventory_limit\n"
    result = inspect(native_helper, tmp_path, files=2000, size=1000000, packages=300, tests=2000)
    assert result.returncode == 0, result.stderr
    item = api.parse_go_source_inventory(result.stdout, expected_module=PKG, max_packages=300, max_tests=2000)
    assert len(item.packages) == 257 and len(item.expected_tests) == 1257
    assert len(item.to_test_expectation(source_sha256="a" * 64, protected_sha256="b" * 64,
                                       max_packages=300, max_tests=2000).packages) == 257


def test_native_rejects_symlinks_without_reading_target(
    native_helper: tuple[Path, dict[str, str]], tmp_path: Path,
) -> None:
    fixture(tmp_path)
    try:
        (tmp_path / "linked.go").symlink_to(tmp_path / "main.go")
    except OSError:
        pytest.skip("host does not permit fixture symlinks")
    result = inspect(native_helper, tmp_path)
    assert result.returncode == 2 and result.stderr == b"go_inventory_source_unsafe\n"
