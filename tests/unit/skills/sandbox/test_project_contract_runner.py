"""Opt-in native runner contracts; no project or model code runs on the host."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import runpy
import shutil
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

SOURCE = Path(__file__).resolve().parents[4] / "src/agent_hub/skills/sandbox/project_runner.py"
MOUNTS = SOURCE.with_name("project_contract_mounts.py")


@pytest.fixture
def runner() -> Any:
    spec = importlib.util.spec_from_file_location("contract_runner", SOURCE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def digest_files(root: Path) -> str:
    files = {
        file.relative_to(root).as_posix(): hashlib.sha256(
            (b"x\0" if file.stat().st_mode & 0o111 else b"f\0") + file.read_bytes()
        ).hexdigest()
        for file in root.rglob("*") if file.is_file()
    }
    return hashlib.sha256(json.dumps(files, sort_keys=True, separators=(",", ":"),
                                    ensure_ascii=True).encode("ascii")).hexdigest()


@pytest.fixture
def case(tmp_path: Path) -> Iterator[dict[str, Any]]:
    original = tmp_path / "original"
    original.mkdir()
    for name, data in {
        "package.json": b'{"scripts":{"build":"node build.js","test":"node --test"}}',
        ".npmrc": b"ignore-scripts=false\n",
        "tests/original.test.js": b"original assertion; not executed on host",
        "src/app.js": b"original implementation",
    }.items():
        target = original / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    candidate = tmp_path / "input"
    shutil.copytree(original, candidate)
    (candidate / "src/app.js").write_bytes(b"repaired by workspace API before verification")
    protected = {name: hashlib.sha256(b"f\0" + (original / name).read_bytes()).hexdigest()
                 for name in ("package.json", ".npmrc", "tests/original.test.js")}
    payload = {
        "schema_version": 1, "tenant_id": "10000000-0000-4000-8000-000000000001",
        "run_id": "10000000-0000-4000-8000-000000000002", "project_id": "project-a",
        "session_id": "session-a", "input_source_sha256": digest_files(original),
        "editable_paths": ["src"], "protected_files": protected,
    }
    protected_sha = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"),
                                             ensure_ascii=True).encode("ascii")).hexdigest()
    payload["protected_sha256"] = protected_sha
    contract = tmp_path / "contract.json"
    raw = json.dumps(payload, indent=2).encode("utf-8")
    contract.write_bytes(raw)
    contract.chmod(0o444)
    for entry in original.rglob("*"):
        entry.chmod(0o555 if entry.is_dir() else 0o444)
    original.chmod(0o555)
    try:
        yield {"input": candidate, "original": original, "work": tmp_path / "work",
               "contract": contract, "sha": protected_sha,
               "protected_sha": protected_sha, "payload": payload}
    finally:
        contract.chmod(0o600)
        original.chmod(0o700)
        for entry in original.rglob("*"):
            entry.chmod(0o700 if entry.is_dir() else 0o600)


def invoke(runner: Any, case: dict[str, Any], **changes: Any) -> dict[str, Any]:
    options = {"install_allowed": True, "check_contract": case["contract"],
               "original_input": case["original"], "expected_contract_sha256": case["sha"]}
    options.update(changes)
    return dict(runner.run_project(case["input"], case["work"], 15, **options))


@pytest.mark.parametrize("missing", ["check_contract", "original_input", "expected_contract_sha256"])
def test_partial_opt_in_is_rejected_before_copy(runner: Any, case: dict[str, Any], missing: str) -> None:
    report = invoke(runner, case, **{missing: None})
    assert report["status"] == "blocked"
    assert report["reason"] == "invalid_check_contract"
    assert not case["work"].exists()
    assert "original_check_contract" not in report


def test_immutable_json_read_has_stable_descriptor_identity(
    runner: Any, case: dict[str, Any],
) -> None:
    for _ in range(50):
        payload = runner._read_check_contract(case["contract"], case["sha"], time.monotonic() + 5)
        assert payload == case["payload"]


@pytest.mark.parametrize("fault", ["duplicate", "nested_duplicate", "writable", "digest", "uppercase",
                                      "relative", "parent", "authority", "protected_sha", "nan",
                                      "bool_schema"])
def test_untrusted_contract_never_launches_or_claims_binding(
    runner: Any, case: dict[str, Any], monkeypatch: pytest.MonkeyPatch, fault: str,
) -> None:
    path = case["contract"]
    raw = path.read_bytes()
    changes: dict[str, Any] = {}
    if fault in {"duplicate", "nested_duplicate", "authority", "protected_sha", "nan", "bool_schema"}:
        if fault == "duplicate":
            raw = raw.replace(b'"schema_version": 1', b'"schema_version": 1,"schema_version":1')
        elif fault == "nested_duplicate":
            raw = raw.replace(b'"protected_files": {', b'"protected_files": {"package.json":"' +
                              case["payload"]["protected_files"]["package.json"].encode() + b'",')
        elif fault == "authority":
            raw = raw.replace(b'"schema_version": 1', b'"install_allowed":true,"schema_version":1')
        elif fault == "protected_sha":
            raw = raw.replace(case["protected_sha"].encode(), b"0" * 64)
        elif fault == "nan":
            raw = raw.replace(b'"schema_version": 1', b'"schema_version": NaN')
        else:
            raw = raw.replace(b'"schema_version": 1', b'"schema_version": true')
        path.chmod(0o600)
        path.write_bytes(raw)
        path.chmod(0o444)
    elif fault == "writable":
        path.chmod(0o600)
    elif fault in {"digest", "uppercase"}:
        changes["expected_contract_sha256"] = "0" * 64 if fault == "digest" else case["sha"].upper()
    elif fault == "relative":
        changes["check_contract"] = Path("contract.json")
    else:
        changes["check_contract"] = path.parent / "bridge/../contract.json"

    def unexpected(*args: Any, **kwargs: Any) -> None:
        pytest.fail("invalid binding must be rejected before launching a process")

    monkeypatch.setattr(runner, "_run_bounded", unexpected)
    report = invoke(runner, case, **changes)
    assert report["status"] == "blocked" and report["reason"] == "invalid_check_contract"
    assert "original_check_contract" not in report


def native_launch_boundary(
    runner: Any, monkeypatch: pytest.MonkeyPatch, *, failure: str | None = None,
) -> list[tuple[str, ...]]:
    sandbox = runner._load_sandbox()
    monkeypatch.setitem(sandbox["generated_command"].__globals__, "sandbox_available", lambda: True)
    monkeypatch.setattr(runner, "_load_sandbox", lambda: sandbox)
    monkeypatch.setattr(runner, "_backend_reason", lambda sandbox: None)
    commands: list[tuple[str, ...]] = []
    active: list[bool] = []
    real_support = runner._load_contract_mounts()

    @contextmanager
    def structural_mounts(command: list[str], **kwargs: Any) -> Iterator[Any]:
        # Windows does not execute bwrap. Keep actual contract/tree validation
        # and command parsing; replace only Linux sealing/mount availability.
        view = real_support["validate_contract_work"](**kwargs)
        prefix, suffix = real_support["_command_prefix"](command, kwargs["work_root"])
        active.append(True)
        try:
            yield SimpleNamespace(argv=(*prefix, "--contract-test-boundary", *suffix), pass_fds=(71, 72),
                                  source_sha256=view.source_sha256, contract_sha256=view.contract_sha256)
        finally:
            active.pop()

    monkeypatch.setattr(runner, "_load_contract_mounts", lambda: {
        **real_support, "build_contract_mounts": structural_mounts,
    })

    def launch(command: list[str], *, cwd: Path, deadline: float,
               pass_fds: tuple[int, ...] = ()) -> Any:
        assert active == [True], "every process must stay inside the mount context until reaped"
        assert pass_fds == (71, 72)
        assert deadline > time.monotonic()
        assert "--contract-test-boundary" in command
        commands.append(tuple(command))
        tail = command[command.index("--") + 1:]
        if tail[-1] == "--version":
            output = "v22.12.0\n" if Path(tail[0]).name == "node" else "10.9.0\n"
        else:
            if tail[-1] == failure or (failure == "install" and any(arg in {'install', 'ci'} for arg in tail)):
                return runner.ProcessResult(9, False, False, '{"status":"passed"}', "original failure")
            output = '{"original_check_contract":{"schema_version":99,"contract_sha256":"fake"}}'
        return runner.ProcessResult(0, False, False, output, "")

    monkeypatch.setattr(runner, "_run_bounded", launch)
    return commands


def test_opt_in_binds_all_five_launches_and_independent_native_report(
    runner: Any, case: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = native_launch_boundary(runner, monkeypatch)
    report = invoke(runner, case)
    assert report["status"] == "passed"
    assert report["original_check_contract"] == {
        "schema_version": 1, "contract_sha256": case["sha"], "protected_sha256": case["protected_sha"],
    }
    assert type(report["original_check_contract"]["schema_version"]) is int
    assert case["sha"] != hashlib.sha256(case["contract"].read_bytes()).hexdigest()
    assert report["source_sha256"] == digest_files(case["input"])
    assert len(calls) == 5 and not case["work"].exists()
    assert [check["status"] for check in report["checks"]] == ["passed"] * 3
    assert all("--unshare-net" in command[:command.index("--")] for command in calls[:2] + calls[3:])
    install = calls[2]
    assert "--unshare-net" not in install[:install.index("--")]
    assert "NPM_CONFIG_ENGINE_STRICT" in install
    assert install[-5:] == ("install", "--ignore-scripts", "--no-audit", "--no-fund", '--package-lock=false')
    assert "pre-existing node_modules" in install[install.index("--") + 3]
    assert ("--ro-bind", "/dev/null", "/workspace/.npmrc") in tuple(
        install[index:index + 3] for index in range(len(install) - 2)
    )


def test_contract_output_budget_derives_from_native_workspace_allocation(
    runner: Any, case: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
) -> None:
    native_launch_boundary(runner, monkeypatch)
    support = runner._load_contract_mounts()
    original = support['validate_contract_work']
    allocations: list[tuple[int, int]] = []

    def validate(**kwargs: Any) -> Any:
        allocations.append((kwargs['ephemeral_budget_bytes'], kwargs['ephemeral_file_limit']))
        return original(**kwargs)

    monkeypatch.setitem(support, 'validate_contract_work', validate)
    monkeypatch.setattr(runner, '_load_contract_mounts', lambda: support)
    report = invoke(runner, case, workspace_limit_bytes=8 * 1024**2, file_limit=1234)
    assert report['status'] == 'passed'
    assert allocations and all(0 < size < 8 * 1024**2 and count == 12340 for size, count in allocations)


@pytest.mark.parametrize("phase", ["install", "build", "test"])
def test_opt_in_failed_phase_retains_binding_and_cannot_be_forged_passed(
    runner: Any, case: dict[str, Any], monkeypatch: pytest.MonkeyPatch, phase: str,
) -> None:
    calls = native_launch_boundary(runner, monkeypatch, failure=phase)
    report = invoke(runner, case)
    index = ("install", "build", "test").index(phase)
    assert report["status"] == "failed" and len(calls) == index + 3
    assert report["checks"][index]["exit_code"] == 9
    assert report["checks"][index]["stderr"] == "original failure"
    assert report["original_check_contract"]["contract_sha256"] == case["sha"]
    assert all(check["status"] == "not_run" for check in report["checks"][index + 1:])
    assert not case["work"].exists()


def test_contract_does_not_grant_install_authority(
    runner: Any, case: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = native_launch_boundary(runner, monkeypatch)
    report = invoke(runner, case, install_allowed=False)
    assert report["status"] == "blocked" and report["reason"] == "install_authorization_required"
    assert len(calls) == 2 and all(check["status"] == "not_run" for check in report["checks"])


def test_legacy_report_has_no_contract_marker(
    runner: Any, case: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runner, "_backend_reason", lambda sandbox: "node_runtime_missing")
    report = runner.run_project(case["input"], case["work"], 5, install_allowed=True)
    assert report["reason"] == "node_runtime_missing"
    assert "original_check_contract" not in report


def test_complete_cli_flags_use_trusted_json_not_project_output(
    case: dict[str, Any],
) -> None:
    result = subprocess.run(
        [sys.executable, "-I", "-B", str(SOURCE), "--input", str(case["input"]),
         "--work", str(case["work"]), "--timeout", "10", "--check-contract", str(case["contract"]),
         "--original-input", str(case["original"]), "--expected-contract-sha256", case["sha"]],
        capture_output=True, timeout=15, check=False,
        env={**os.environ, "AGENT_HUB_GENERATED_PROJECT_VALIDATION_SANDBOX": "denied"},
    )
    assert result.returncode == 0 and result.stderr == b""
    report = json.loads(result.stdout)
    assert report["status"] == "blocked" and all(c["status"] == "not_run" for c in report["checks"])
    assert report["original_check_contract"] == {
        "schema_version": 1, "contract_sha256": case["sha"], "protected_sha256": case["protected_sha"],
    }
    assert type(report["original_check_contract"]["schema_version"]) is int


@pytest.mark.parametrize("fault", ["modified_test", "new_test", "candidate_output", "original_output"])
def test_invalid_source_or_output_conflict_never_launches(
    runner: Any, case: dict[str, Any], monkeypatch: pytest.MonkeyPatch, fault: str,
) -> None:
    if fault == "modified_test":
        (case["input"] / "tests/original.test.js").write_bytes(b"weakened")
    elif fault == "new_test":
        (case["input"] / "tests/new.test.js").write_bytes(b"injected")
    else:
        root = case["input"] if fault == "candidate_output" else case["original"]
        root.chmod(0o700)
        (root / "dist").mkdir()
        if fault == "original_output":
            (root / "dist").chmod(0o555)
            root.chmod(0o555)
    calls = native_launch_boundary(runner, monkeypatch)
    report = invoke(runner, case)
    assert report["status"] == "blocked" and report["reason"] == "project_contract_mounts_rejected"
    assert calls == [] and all(check["status"] == "not_run" for check in report["checks"])


@pytest.mark.parametrize("kind", ["file", "ancestor", "hardlink"])
def test_contract_links_are_not_trusted(
    runner: Any, case: dict[str, Any], tmp_path: Path, kind: str,
) -> None:
    alias = tmp_path / "alias"
    try:
        if kind == "file":
            alias.symlink_to(case["contract"])
            path = alias
        elif kind == "ancestor":
            alias.symlink_to(tmp_path, target_is_directory=True)
            path = alias / "contract.json"
        else:
            os.link(case["contract"], alias)
            path = case["contract"]
    except OSError:
        pytest.skip("local filesystem policy does not permit creating this link")
    report = invoke(runner, case, check_contract=path)
    assert report["status"] == "blocked" and report["reason"] == "invalid_check_contract"


@pytest.mark.parametrize("partial", [
    ["--check-contract", "secret-path"],
    ["--original-input", "secret-path"],
    ["--expected-contract-sha256", "0" * 64],
])
def test_cli_partial_flags_are_safe_infrastructure_errors(tmp_path: Path, partial: list[str]) -> None:
    result = subprocess.run([sys.executable, "-I", "-B", str(SOURCE), "--input", str(tmp_path),
                             "--work", str(tmp_path / "work"), "--timeout", "5", *partial],
                            capture_output=True, timeout=10, check=False)
    assert result.returncode == 1 and result.stdout == b""
    assert result.stderr == ("project_runner_infrastructure_error" + os.linesep).encode()


def test_exact_install_npmrc_mask_survives_contract_mount_rewrite(
    case: dict[str, Any],
) -> None:
    helper = runpy.run_path(str(MOUNTS))
    command = ["/usr/bin/bwrap", "--bind", str(case["work"]), "/workspace",
               "--ro-bind", "/dev/null", "/workspace/.npmrc", "--", "node", "guard"]
    prefix, suffix = helper["_command_prefix"](command, case["work"])
    assert suffix == ("--", "node", "guard")
    assert prefix[-3:] == ["--ro-bind", "/dev/null", "/workspace/.npmrc"]
    rewritten = helper["_contract_argv"](command, case["work"],
                                          ("--tmpfs", "/workspace", "--remount-ro", "/workspace"))
    assert rewritten == ("/usr/bin/bwrap", "--tmpfs", "/workspace", "--remount-ro", "/workspace",
                         "--ro-bind", "/dev/null", "/workspace/.npmrc", "--", "node", "guard")


@pytest.mark.parametrize("source,destination", [
    ("/host/.npmrc", "/workspace/.npmrc"), ("/dev/null", "/workspace/package.json"),
    ("/dev/null", "/workspace/tests/.npmrc"),
])
def test_mask_compatibility_does_not_allow_other_workspace_overlays(
    case: dict[str, Any], source: str, destination: str,
) -> None:
    helper = runpy.run_path(str(MOUNTS))
    command = ["/usr/bin/bwrap", "--bind", str(case["work"]), "/workspace",
               "--ro-bind", source, destination, "--", "node", "guard"]
    with pytest.raises(helper["ContractMountError"]):
        helper["_command_prefix"](command, case["work"])


def test_duplicate_npmrc_masks_are_rejected(case: dict[str, Any]) -> None:
    helper = runpy.run_path(str(MOUNTS))
    command = ["/usr/bin/bwrap", "--bind", str(case["work"]), "/workspace",
               *(["--ro-bind", "/dev/null", "/workspace/.npmrc"] * 2), "--", "node", "guard"]
    with pytest.raises(helper["ContractMountError"]):
        helper["_command_prefix"](command, case["work"])


@pytest.mark.skipif(sys.platform != "linux", reason="actual Linux memfd sealing; no Windows execution claim")
@pytest.mark.parametrize("raise_during_child", [False, True])
def test_runner_real_mount_context_keeps_sealed_fds_until_reap_and_then_closes(
    runner: Any, case: dict[str, Any], monkeypatch: pytest.MonkeyPatch, raise_during_child: bool,
) -> None:
    if sys.platform != "linux":
        pytest.skip("native Linux FD sealing")
    import fcntl

    sandbox = runner._load_sandbox()
    monkeypatch.setitem(sandbox["generated_command"].__globals__, "sandbox_available", lambda: True)
    monkeypatch.setattr(runner, "_load_sandbox", lambda: sandbox)
    monkeypatch.setattr(runner, "_backend_reason", lambda sandbox: None)
    descriptors: list[int] = []
    calls: list[tuple[str, ...]] = []

    def child(command: tuple[str, ...], *, cwd: Path, deadline: float,
              pass_fds: tuple[int, ...] = ()) -> Any:
        assert deadline > time.monotonic() and pass_fds
        descriptors.extend(pass_fds)
        for fd in pass_fds:
            assert fcntl.fcntl(fd, 1034) & 15 == 15
        separator = command.index("--")
        prefix = command[:separator]
        assert prefix.count("--ro-bind-data") == 4
        assert "--remount-ro" in prefix
        assert prefix.count("--bind") == 3
        assert str(cwd) not in prefix
        assert all("/workspace/" + name in prefix for name in ("node_modules", "dist", "build"))
        if "--unshare-net" not in prefix:
            mask = ("--ro-bind", "/dev/null", "/workspace/.npmrc")
            assert prefix[-3:] == mask
            assert prefix.index("--remount-ro") < len(prefix) - 3
        calls.append(command)
        if raise_during_child:
            raise runner.InfrastructureError("process_reap_failed")
        tail = command[separator + 1:]
        output = ("v22.12.0" if Path(tail[0]).name == "node" else "10.9.0") if tail[-1] == "--version" else "ok"
        return runner.ProcessResult(0, False, False, output, "")

    monkeypatch.setattr(runner, "_run_bounded", child)
    if raise_during_child:
        with pytest.raises(runner.InfrastructureError, match="process_reap_failed"):
            invoke(runner, case)
    else:
        assert invoke(runner, case)["status"] == "passed" and len(calls) == 5
    assert descriptors and not case["work"].exists()
    for fd in descriptors:
        with pytest.raises(OSError):
            os.fstat(fd)


@pytest.mark.skipif(sys.platform != "linux", reason="real Linux FD inheritance; not a Windows PASS")
def test_bounded_process_inherits_only_explicit_contract_descriptors(runner: Any, tmp_path: Path) -> None:
    descriptor = os.open(SOURCE, os.O_RDONLY)
    try:
        result = runner._run_bounded(
            [sys.executable, "-I", "-c", "import os,sys; print(bool(os.read(int(sys.argv[1]), 1)))",
             str(descriptor)], cwd=tmp_path, deadline=time.monotonic() + 5, pass_fds=(descriptor,),
        )
        assert result.exit_code == 0 and result.stdout == "True\n"
    finally:
        os.close(descriptor)
