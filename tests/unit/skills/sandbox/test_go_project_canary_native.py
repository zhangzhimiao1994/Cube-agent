"""Opt-in Windows SDK calibration using ONLY this file's trusted synthetic CLI.

Host paths replace the canary's virtual paths for this test only. This does not
validate the Linux command, evidence parser, isolation or production authority.
All subprocesses finish before owned scratch cleanup; metadata survives cleanup.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from collections import Counter
from pathlib import Path
from typing import Any

import pytest

from agent_hub.skills.sandbox.go_project_canary import create_go_overlay_canary

_MODULE = "example.invalid/agenthubcanary"
_TEST_NAME = "TestFrozenSum"
_SOURCE = {
    "go.mod": b"module example.invalid/agenthubcanary\n\ngo 1.27\n",
    "main.go": b'''package main
import (
    "fmt"
    "os"
    "strconv"
)
func sum(a, b int) int { return a + b }
func main() {
    if len(os.Args) != 3 { fmt.Fprintln(os.Stderr, "invalid input"); os.Exit(2) }
    a, first := strconv.Atoi(os.Args[1])
    b, second := strconv.Atoi(os.Args[2])
    if first != nil || second != nil { fmt.Fprintln(os.Stderr, "invalid input"); os.Exit(2) }
    fmt.Println(sum(a, b))
}
''',
    "main_test.go": b'''package main
import "testing"
func TestFrozenSum(t *testing.T) {
    if sum(2, 3) != 5 { t.Fatal("positive sum") }
    if sum(-2, -3) != -5 { t.Fatal("negative sum") }
}
''',
}


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _manifest(root: Path) -> dict[str, dict[str, object]]:
    result: dict[str, dict[str, object]] = {}
    for path in sorted(root.rglob("*")):
        assert not path.is_symlink(), "synthetic fixture must not contain links"
        if path.is_file():
            raw = path.read_bytes()
            result[path.relative_to(root).as_posix()] = {
                "raw_sha256": _sha(raw), "size_bytes": len(raw), "mode": path.stat().st_mode,
            }
    return result


def _digest(value: object) -> str:
    return _sha(json.dumps(value, sort_keys=True, separators=(",", ":")).encode("ascii"))


def _environment(go: Path, scratch: Path) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items()
           if key.upper() in {"SYSTEMROOT", "WINDIR"}}
    env.update({"GOROOT": str(go.parent.parent), "GOTOOLCHAIN": "local", "GOENV": "off",
        "GOWORK": "off", "GOPROXY": "off", "GOSUMDB": "off", "GOTELEMETRY": "off",
        "GO111MODULE": "on", "GOFLAGS": "", "GOEXPERIMENT": "", "GOAMD64": "v1",
        "GOOS": "windows", "GOARCH": "amd64", "CGO_ENABLED": "0",
        "PATH": str(go.parent) + os.pathsep + str(Path(os.environ["SYSTEMROOT"]) / "System32")})
    for name in ("GOCACHE", "GOPATH", "GOMODCACHE", "GOBIN", "TMPDIR", "TMP", "TEMP",
                 "HOME", "USERPROFILE", "LOCALAPPDATA", "APPDATA"):
        directory = scratch / name.lower()
        directory.mkdir()
        env[name] = str(directory)
    return env


def _run(argv: list[str], *, cwd: Path, env: dict[str, str], phase: str,
         record: dict[str, Any]) -> subprocess.CompletedProcess[bytes]:
    start = time.monotonic()
    timed_out = False
    with subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=False,
                          close_fds=True) as process:
        try:
            stdout, stderr = process.communicate(timeout=180)
        except subprocess.TimeoutExpired:
            timed_out = True
            # Kill only this still-live owned process tree; never kill by image name.
            if process.poll() is None:
                subprocess.run([str(Path(env["SYSTEMROOT"]) / "System32/taskkill.exe"),
                    "/PID", str(process.pid), "/T", "/F"], stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30, check=False)
            if process.poll() is None:
                process.kill()
            stdout, stderr = process.communicate(timeout=30)
        exit_code = process.wait(timeout=30)
    record["processes"].append({"phase": phase, "actual_argv": argv, "cwd": str(cwd),
        "exit_code": exit_code, "timed_out": timed_out,
        "elapsed_ms": round((time.monotonic() - start) * 1000),
        "stdout_bytes": len(stdout), "stdout_sha256": _sha(stdout),
        "stderr_bytes": len(stderr), "stderr_sha256": _sha(stderr)})
    assert not timed_out, "owned synthetic SDK calibration timed out"
    return subprocess.CompletedProcess(argv, exit_code, stdout, stderr)


def _events(raw: bytes) -> list[dict[str, Any]]:
    # Deliberately independent of the production evidence parser: data checks only.
    rows = [json.loads(line) for line in raw.decode("utf-8", errors="strict").splitlines() if line]
    assert all(type(row) is dict for row in rows)
    return rows


def _test_actions(events: list[dict[str, Any]]) -> Counter[tuple[str, str, str]]:
    return Counter((str(row.get("Package", "")), str(row.get("Test", "")),
                    str(row.get("Action", ""))) for row in events if row.get("Test"))


def test_official_windows_sdk_overlay_canary_and_frozen_cli() -> None:
    configured = os.environ.get("AGENT_HUB_GO_PROOF_EXECUTABLE")
    if not configured:
        pytest.skip("official SDK not explicitly configured; native canary NOT RUN")
    if os.name != "nt":
        pytest.skip("this calibration is restricted to the official Windows/amd64 SDK")
    go = Path(configured)
    assert go.is_absolute() and go.is_file() and go.resolve() == go.absolute()
    sdk = go.parent.parent
    assert go.name.lower() == "go.exe" and go.parent.name == "bin" and sdk.name == "go"
    proofroot = sdk.parent.resolve(strict=True)
    owned = Path(tempfile.mkdtemp(prefix="go-overlay-canary-native-", dir=proofroot))
    scratch = owned / "scratch"
    scratch.mkdir()
    record_path = owned / "metadata.json"
    record: dict[str, Any] = {"schema_version": 1, "kind": "go_overlay_windows_synthetic_calibration_v1",
        "scope": "trusted_fixed_fixture_only", "linux_command_validated": False,
        "production_parser_validated": False, "native_execution_authority": False,
        "status": "running", "sdk_executable": str(go), "sdk_binary_sha256": _sha(go.read_bytes()),
        "processes": [], "scratch_cleaned": False}
    try:
        project, trusted, output = (scratch / name for name in ("project", "trusted", "output"))
        for directory in (project, trusted, output):
            directory.mkdir()
        for name, source_bytes in _SOURCE.items():
            (project / name).write_bytes(source_bytes)
        before = _manifest(project)
        record["source_before"] = before
        record["source_manifest_sha256"] = _digest(before)
        env = _environment(go, scratch)
        record["controlled_environment"] = env
        version = _run([str(go), "version"], cwd=scratch, env=env, phase="sdk_version", record=record)
        sdk_version = version.stdout.decode("ascii", errors="strict").strip()
        assert version.returncode == 0 and re.fullmatch(r"go version go1\.[0-9]+\.[0-9]+ windows/amd64", sdk_version)
        record["sdk_version"] = sdk_version
        canary = create_go_overlay_canary(project_execution_id="fixed-fixture-project",
            execution_id="fixed-fixture-canary", module_path=_MODULE,
            source_sha256=_digest(before), protected_sha256=_digest({"main_test.go": before["main_test.go"]}),
            source_paths=tuple(before))
        record["nonce_test"] = canary.test_name
        source = trusted / "go-canary.go"
        source.write_bytes(canary.source_bytes)
        virtual = json.loads(canary.overlay_bytes)
        assert virtual == {"Replace": {"/workspace/" + canary.source_path: "/trusted/go-canary.go"}}
        overlay = trusted / "overlay.json"
        mapped = {"Replace": {str(project / canary.source_path): str(source)}}
        overlay.write_text(json.dumps(mapped, sort_keys=True, separators=(",", ":")), encoding="ascii")
        record["canary_source_raw_sha256"] = _sha(canary.source_bytes)
        record["virtual_overlay"] = virtual
        record["host_overlay"] = mapped
        record["overlay_raw_sha256"] = _sha(overlay.read_bytes())
        failed = _run([str(go), "test", "-json", "-count=1", "-vet=off", "-mod=readonly",
            "-overlay=" + str(overlay), "-run=^" + canary.test_name + "$", _MODULE],
            cwd=project, env=env, phase="overlay_negative_canary", record=record)
        events = _events(failed.stdout)
        actions = _test_actions(events)
        no_build_failure = not any(row.get("FailedBuild") or row.get("Action") == "build-fail" for row in events)
        record["overlay_test_actions"] = [{"package": p, "name": n, "action": a, "count": count}
                                           for (p, n, a), count in sorted(actions.items())]
        record["overlay_no_build_failure"] = no_build_failure
        assert failed.returncode == 1 and no_build_failure, "Go overlay runtime failure is not confirmed"
        assert actions[(_MODULE, canary.test_name, "run")] == 1
        assert actions[(_MODULE, canary.test_name, "fail")] == 1
        assert {(p, name) for p, name, _ in actions} == {(_MODULE, canary.test_name)}
        assert not any(a in {"pass", "skip"} for _, _, a in actions)
        assert any(row.get("Package") == _MODULE and not row.get("Test")
                   and row.get("Action") == "fail" for row in events)
        assert not (project / canary.source_path).exists()
        assert _manifest(project) == before
        binary = output / "probe.exe"
        built = _run([str(go), "build", "-trimpath", "-mod=readonly", "-o", str(binary), "."],
            cwd=project, env=env, phase="build_fixed_cli", record=record)
        assert built.returncode == 0 and binary.is_file()
        binary_before = {"raw_sha256": _sha(binary.read_bytes()), "size_bytes": binary.stat().st_size}
        record["built_artifact"] = binary_before
        positive = _run([str(binary), "2", "3"], cwd=output, env=env, phase="business_valid", record=record)
        assert positive.returncode == 0 and positive.stdout == b"5\n" and positive.stderr == b""
        negative = _run([str(binary), "2", "invalid"], cwd=output, env=env, phase="business_invalid", record=record)
        assert negative.returncode != 0 and negative.stdout == b"" and negative.stderr == b"invalid input\n"
        binary_after = {"raw_sha256": _sha(binary.read_bytes()), "size_bytes": binary.stat().st_size}
        record["built_artifact_after_execution"] = binary_after
        assert binary_before == binary_after
        passed = _run([str(go), "test", "-json", "-count=1", "-vet=off", "-mod=readonly", _MODULE],
            cwd=project, env=env, phase="same_frozen_tests_without_overlay", record=record)
        frozen_events = _events(passed.stdout)
        frozen_actions = _test_actions(frozen_events)
        assert passed.returncode == 0
        assert frozen_actions[(_MODULE, _TEST_NAME, "run")] == 1
        assert frozen_actions[(_MODULE, _TEST_NAME, "pass")] == 1
        assert {(p, name) for p, name, _ in frozen_actions} == {(_MODULE, _TEST_NAME)}
        assert not any(a in {"fail", "skip"} for _, _, a in frozen_actions)
        record["source_after"] = _manifest(project)
        record["source_unchanged"] = record["source_after"] == before
        assert record["source_unchanged"] and not (project / canary.source_path).exists()
        assert source.read_bytes() == canary.source_bytes
        record["business_valid_passed"] = record["business_invalid_passed"] = True
        record["same_frozen_tests_passed"] = True
        record["status"] = "passed"
    except BaseException as error:
        record["status"] = "failed"
        record["failure_type"] = type(error).__name__
        raise
    finally:
        try:
            assert owned.resolve().parent == proofroot and scratch.resolve().parent == owned.resolve()
            assert scratch.name == "scratch" and not scratch.is_symlink()
            shutil.rmtree(scratch)
            record["scratch_cleaned"] = not scratch.exists()
        finally:
            record_path.write_text(json.dumps(record, sort_keys=True, indent=2), encoding="ascii")
            print("Go synthetic calibration metadata: " + str(record_path))
