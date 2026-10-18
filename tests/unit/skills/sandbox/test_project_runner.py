"""Offline runner contracts; only the bwrap launch is replaced on Windows."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

SOURCE = Path(__file__).resolve().parents[4] / "src/agent_hub/skills/sandbox/project_runner.py"


@pytest.fixture
def runner() -> Any:
    assert SOURCE.is_file(), "trusted project runner has not been implemented"
    spec = importlib.util.spec_from_file_location("trusted_project_runner", SOURCE)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "input"
    root.mkdir()
    (root / "package.json").write_text(
        json.dumps(
            {
                "name": "offline-case",
                "scripts": {"build": "node build.js", "test": "node test.js"},
            }
        ),
        encoding="utf-8",
    )
    return root


def test_bounded_process_captures_only_child_streams(runner: Any, tmp_path: Path) -> None:
    result = runner._run_bounded(
        [sys.executable, "-I", "-c", "import sys; print('{fake report}'); sys.stderr.write('err')"],
        cwd=tmp_path,
        deadline=time.monotonic() + 5,
    )
    assert result.exit_code == 0 and not result.timed_out and not result.output_limited
    assert result.stdout == "{fake report}" + os.linesep and result.stderr == "err"


@pytest.mark.parametrize("stream", [1, 2])
def test_output_flood_is_killed_and_bounded(runner: Any, tmp_path: Path, stream: int) -> None:
    start = time.monotonic()
    result = runner._run_bounded(
        [sys.executable, "-I", "-c", f"import os;\nwhile True: os.write({stream}, b'x'*4096)"],
        cwd=tmp_path,
        deadline=start + 5,
    )
    assert result.output_limited and result.exit_code != 0
    assert max(len(result.stdout.encode()), len(result.stderr.encode())) <= 8192
    assert time.monotonic() - start < 3


def test_timeout_kills_and_reaps_real_process(runner: Any, tmp_path: Path) -> None:
    pidfile = tmp_path / "pid"
    script = "import os,pathlib,sys,time; pathlib.Path(sys.argv[1]).write_text(str(os.getpid())); time.sleep(20)"
    start = time.monotonic()
    result = runner._run_bounded(
        [sys.executable, "-I", "-c", script, str(pidfile)],
        cwd=tmp_path,
        deadline=start + 1,
    )
    assert result.timed_out and result.exit_code != 0
    assert time.monotonic() - start < 2
    if sys.platform == "linux":
        with pytest.raises(ProcessLookupError):
            os.kill(int(pidfile.read_text()), 0)


def test_expired_deadline_never_starts_child(runner: Any, tmp_path: Path) -> None:
    marker = tmp_path / "spawned"
    result = runner._run_bounded(
        [sys.executable, "-c", f"open({str(marker)!r}, 'w').close()"],
        cwd=tmp_path,
        deadline=time.monotonic() - 1,
    )
    assert result.timed_out and result.exit_code is None and not marker.exists()


def test_snapshot_digest_matches_broker_and_leaves_source_unchanged(
    runner: Any,
    project: Path,
    tmp_path: Path,
) -> None:
    (project / "nested").mkdir()
    (project / "nested/hi.txt").write_bytes(b"hello")
    (project / "DELIVERY_MANIFEST.json").write_text("metadata", encoding="utf-8")
    files = {
        "package.json": hashlib.sha256(b'f\0' + (project / "package.json").read_bytes()).hexdigest(),
        "nested/hi.txt": hashlib.sha256(b'f\0hello').hexdigest(),
        "DELIVERY_MANIFEST.json": hashlib.sha256(b'f\0metadata').hexdigest(),
    }
    expected = hashlib.sha256(
        json.dumps(
            files,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode()
    ).hexdigest()
    work = tmp_path / "work"
    digest = runner._copy_snapshot(project, work, time.monotonic() + 5)
    assert digest == expected
    assert (work / "nested/hi.txt").read_bytes() == b"hello"
    (work / "package.json").write_text("changed", encoding="utf-8")
    assert (
        hashlib.sha256(b'f\0' + (project / "package.json").read_bytes()).hexdigest() == files["package.json"]
    )


@pytest.mark.skipif(os.name != "posix", reason="POSIX executable permissions")
@pytest.mark.parametrize(
    "source_mode,work_mode", [(0o555, 0o700), (0o444, 0o600), (0o6755, 0o700)]
)
def test_snapshot_work_modes_preserve_execution_without_special_or_shared_permissions(
    runner: Any,
    project: Path,
    tmp_path: Path,
    source_mode: int,
    work_mode: int,
) -> None:
    script = project / "helper.sh"
    script.write_bytes(b"#!/bin/sh\nprintf 'helper-ok\\n'\n")
    script.chmod(source_mode)
    work = tmp_path / "work"

    runner._copy_snapshot(project, work, time.monotonic() + 5)

    copied = work / script.name
    assert stat.S_IMODE(copied.stat().st_mode) == work_mode
    assert stat.S_IMODE(script.stat().st_mode) == source_mode
    assert copied.read_bytes() == script.read_bytes()
    if work_mode == 0o700:
        completed = subprocess.run(
            [str(copied)], cwd=work, capture_output=True, timeout=5, check=False
        )
        assert completed.returncode == 0
        assert completed.stdout == b"helper-ok\n"


def test_snapshot_rejects_hardlinks(runner: Any, project: Path, tmp_path: Path) -> None:
    os.link(project / "package.json", project / "alias")
    with pytest.raises(runner.Blocked, match="unsafe_input_entry"):
        runner._copy_snapshot(project, tmp_path / "work", time.monotonic() + 5)


def test_snapshot_rejects_symlinks(runner: Any, project: Path, tmp_path: Path) -> None:
    try:
        (project / "alias").symlink_to(project / "package.json")
    except OSError:
        pytest.skip("symlink creation unavailable")
    with pytest.raises(runner.Blocked, match="unsafe_input_entry"):
        runner._copy_snapshot(project, tmp_path / "work", time.monotonic() + 5)


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="POSIX FIFO")
def test_snapshot_rejects_special_files_without_opening_them(
    runner: Any,
    project: Path,
    tmp_path: Path,
) -> None:
    mkfifo = getattr(os, "mkfifo", None)
    assert mkfifo is not None
    mkfifo(project / "fifo")
    with pytest.raises(runner.Blocked, match="unsafe_input_entry"):
        runner._copy_snapshot(project, tmp_path / "work", time.monotonic() + 5)


def test_existing_work_is_not_overwritten(runner: Any, project: Path, tmp_path: Path) -> None:
    work = tmp_path / "work"
    work.mkdir()
    (work / "keep").write_text("existing", encoding="utf-8")
    with pytest.raises(runner.Blocked, match="work_not_empty"):
        runner._copy_snapshot(project, work, time.monotonic() + 5)
    assert (work / "keep").read_text() == "existing"


@pytest.fixture
def backend(runner: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, Any]:
    harness = cast(dict[str, Any], runner._load_sandbox())
    namespace = harness["generated_command"].__globals__
    monkeypatch.setitem(namespace, "sandbox_available", lambda: True)
    node = tmp_path / "trusted-node"
    node.mkdir()
    monkeypatch.setitem(namespace, "_NODE_HOME", node)
    monkeypatch.setattr(runner, "_load_sandbox", lambda: harness)
    monkeypatch.setattr(runner, "_backend_reason", lambda sandbox: None)
    return harness


def test_command_assembly_uses_existing_guard_and_fixed_mount(
    runner: Any,
    backend: dict[str, Any],
    project: Path,
) -> None:
    install = runner._phase_command(backend, "install", project)
    assert install[0] == "/usr/bin/bwrap" and "--unshare-pid" in install
    assert "--unshare-net" not in install and "--clearenv" in install
    assert install[install.index("--") - 3 : install.index("--")] == [
        "--setenv",
        "NPM_CONFIG_ENGINE_STRICT",
        "true",
    ]
    inner = install[install.index("--") + 1 :]
    assert inner[:2] == ["/opt/validator/node/bin/node", "-e"]
    assert inner[2] == backend["_INSTALL_GUARD"]
    assert inner[3:] == [
        "/opt/validator/node/bin/npm",
        "install",
        "--ignore-scripts",
        "--no-audit",
        "--no-fund",
    ]
    for phase in ("build", "test"):
        command = runner._phase_command(backend, phase, project)
        assert "--unshare-net" in command
        assert command[-4:] == ["--", "/opt/validator/node/bin/npm", "run", phase]
        assert "--if-present" not in command


def _fake_processes(
    runner: Any, monkeypatch: pytest.MonkeyPatch, results: dict[str, Any]
) -> list[Any]:
    calls: list[Any] = []

    def run(command: list[str], *, cwd: Path, deadline: float) -> Any:
        assert command[0] == "/usr/bin/bwrap"
        calls.append((command, cwd, deadline))
        inner = command[command.index("--") + 1 :]
        if inner[-1] == "--version":
            version = "v22.18.0\n" if inner[0].endswith("/node") else "10.9.3\n"
            assert "--unshare-net" in command
            return runner.ProcessResult(0, False, False, version, "")
        phase = "install" if "--ignore-scripts" in inner else inner[-1]
        result = results.get(phase)
        if callable(result):
            return result(cwd)
        return result or runner.ProcessResult(0, False, False, phase + " ok", "")

    monkeypatch.setattr(runner, "_run_bounded", run)
    return calls


def test_success_reports_actual_versions_and_cleans_work(
    runner: Any,
    backend: Any,
    monkeypatch: pytest.MonkeyPatch,
    project: Path,
    tmp_path: Path,
) -> None:
    calls = _fake_processes(runner, monkeypatch, {})
    report = runner.run_project(project, tmp_path / "work", 10, install_allowed=True)
    assert report["status"] == "passed" and report["schema_version"] == 1
    assert report["runtime"] == {"node": "v22.18.0", "npm": "10.9.3"}
    assert [c["phase"] for c in report["checks"]] == ["install", "build", "test"]
    assert all(c["status"] == "passed" for c in report["checks"])
    assert len(calls) == 5 and len({item[2] for item in calls}) == 1
    assert len(report["source_sha256"]) == 64
    assert not (tmp_path / "work").exists()


@pytest.mark.parametrize("phase", ["build", "test"])
def test_missing_script_returns_real_npm_failure_and_repair_diagnostic(
    runner: Any,
    backend: Any,
    monkeypatch: pytest.MonkeyPatch,
    project: Path,
    tmp_path: Path,
    phase: str,
) -> None:
    from agent_hub.capabilities.project_verification import (
        ProjectVerificationError,
        _validate_report,
    )

    node = shutil.which("node")
    if node is None:
        pytest.skip("local Node runtime is unavailable")
    candidates = [Path(node).resolve().parent / "node_modules/npm/bin/npm-cli.js"]
    npm = shutil.which("npm")
    if npm is not None:
        candidates.append(Path(npm).resolve())
    npm_cli = next((path for path in candidates if path.suffix == ".js" and path.is_file()), None)
    if npm_cli is None:
        pytest.skip("local npm CLI is unavailable")
    (project / "package.json").write_text(
        json.dumps(
            {
                "scripts": {"test" if phase == "build" else "build": "node check.js"},
            }
        ),
        encoding="utf-8",
    )
    run_bounded = runner._run_bounded

    def missing_script(cwd: Path) -> Any:
        return run_bounded(
            [node, str(npm_cli), "--offline", "--update-notifier=false",
             "--cache", str(tmp_path / "npm-cache"), "run", phase],
            cwd=cwd,
            deadline=time.monotonic() + 5,
        )

    calls = _fake_processes(runner, monkeypatch, {phase: missing_script})
    report = runner.run_project(project, tmp_path / "work", 10, install_allowed=True)
    index = ("install", "build", "test").index(phase)
    failed = report["checks"][index]
    assert report["status"] == "failed"
    assert failed["status"] == "failed" and failed["exit_code"] != 0
    assert failed["timed_out"] is False
    assert "Missing script" in failed["stderr"] and phase in failed["stderr"]
    assert all(check["status"] == "passed" for check in report["checks"][:index])
    assert all(check["status"] == "not_run" for check in report["checks"][index + 1 :])
    assert len(calls) == index + 3
    with pytest.raises(ProjectVerificationError) as caught:
        _validate_report(json.dumps(report), expected_source=report["source_sha256"])
    diagnostic = caught.value.diagnostic
    assert diagnostic is not None
    assert diagnostic.exit_code == failed["exit_code"]
    assert "Missing script" in cast(str, diagnostic.to_payload()["stderr"])


def test_install_requires_authorization(
    runner: Any,
    backend: Any,
    monkeypatch: pytest.MonkeyPatch,
    project: Path,
    tmp_path: Path,
) -> None:
    calls = _fake_processes(runner, monkeypatch, {})
    report = runner.run_project(project, tmp_path / "work", 10, install_allowed=False)
    assert report["reason"] == "install_authorization_required" and report["status"] == "blocked"
    assert len(calls) == 2 and all(c["status"] == "not_run" for c in report["checks"])


@pytest.mark.parametrize("phase", ["install", "build", "test"])
def test_failure_retains_evidence_and_stops_following_phases(
    runner: Any,
    backend: Any,
    monkeypatch: pytest.MonkeyPatch,
    project: Path,
    tmp_path: Path,
    phase: str,
) -> None:
    _fake_processes(
        runner,
        monkeypatch,
        {
            phase: runner.ProcessResult(7, False, False, '{"status":"passed"}', "failure"),
        },
    )
    report = runner.run_project(project, tmp_path / "work", 10, install_allowed=True)
    index = ["install", "build", "test"].index(phase)
    assert report["status"] == "failed"
    assert report["checks"][index]["exit_code"] == 7
    assert report["checks"][index]["stdout"] == '{"status":"passed"}'
    assert all(c["status"] == "not_run" for c in report["checks"][index + 1 :])


def test_lock_changes_are_blocked_and_input_lock_untouched(
    runner: Any,
    backend: Any,
    monkeypatch: pytest.MonkeyPatch,
    project: Path,
    tmp_path: Path,
) -> None:
    (project / "package-lock.json").write_text('{"lockfileVersion":3}', encoding="utf-8")

    def install(cwd: Path) -> Any:
        (cwd / "package-lock.json").write_text("changed", encoding="utf-8")
        return runner.ProcessResult(0, False, False, "ok", "")

    _fake_processes(runner, monkeypatch, {"install": install})
    report = runner.run_project(project, tmp_path / "work", 10, install_allowed=True)
    assert report["status"] == "blocked" and report["reason"] == "input_lock_changed"
    assert report["checks"][1]["status"] == "not_run"
    assert (project / "package-lock.json").read_text() == '{"lockfileVersion":3}'


@pytest.mark.parametrize(
    "filename,content,reason",
    [
        ("pnpm-lock.yaml", "lockfileVersion: 9", "unsupported_pnpm"),
        ("pyproject.toml", "[project]", "unsupported_python"),
    ],
)
def test_unsupported_ecosystems_never_execute(
    runner: Any,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    filename: str,
    content: str,
    reason: str,
) -> None:
    root = tmp_path / "input"
    root.mkdir()
    (root / filename).write_text(content, encoding="utf-8")
    report = runner.run_project(root, tmp_path / "work", 5, install_allowed=True)
    assert report["status"] == "blocked" and report["reason"] == reason
    assert all(c["status"] == "not_run" for c in report["checks"])


def test_missing_runtime_never_falls_back_to_path(
    runner: Any,
    monkeypatch: pytest.MonkeyPatch,
    project: Path,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(runner, "_backend_reason", lambda sandbox: "node_runtime_missing")
    report = runner.run_project(project, tmp_path / "work", 5, install_allowed=True)
    assert report["status"] == "blocked" and report["reason"] == "node_runtime_missing"
    assert report["runtime"] == {"node": "", "npm": ""}


def test_report_serialization_is_bounded_even_for_json_escaped_output(runner: Any) -> None:
    report = runner._empty_report()
    for check in report["checks"]:
        check["stdout"] = "\x00" * 8192
        check["stderr"] = "\x01" * 8192
    encoded = runner._encode_report(report)
    assert len(encoded) < 65536
    payload = json.loads(encoded)
    assert len(payload["checks"]) == 3
    assert all(len(c["stdout"].encode()) <= 8192 for c in payload["checks"])


def test_isolated_cli_reports_blocked_without_loading_project_python(
    runner: Any,
    project: Path,
    tmp_path: Path,
) -> None:
    (project / "json.py").write_text("raise RuntimeError('untrusted shadow')", encoding="utf-8")
    env = dict(os.environ, AGENT_HUB_GENERATED_PROJECT_VALIDATION_SANDBOX="denied")
    completed = subprocess.run(
        [
            sys.executable,
            "-I",
            "-B",
            str(SOURCE),
            "--input",
            str(project),
            "--work",
            str(tmp_path / "work"),
            "--timeout",
            "5",
        ],
        cwd=project,
        env=env,
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert completed.returncode == 0 and completed.stderr == b""
    report = json.loads(completed.stdout)
    assert report["status"] == "blocked" and len(report["source_sha256"]) == 64
    assert [c["phase"] for c in report["checks"]] == ["install", "build", "test"]


def test_cli_infrastructure_error_does_not_echo_arguments(runner: Any, tmp_path: Path) -> None:
    secret = "private-sentinel-do-not-echo"
    completed = subprocess.run(
        [
            sys.executable,
            "-I",
            "-B",
            str(SOURCE),
            "--input",
            str(tmp_path),
            "--work",
            str(tmp_path / "work"),
            "--timeout",
            secret,
        ],
        capture_output=True,
        timeout=5,
        check=False,
    )
    assert completed.returncode != 0 and completed.stdout == b""
    assert secret.encode() not in completed.stderr


@pytest.mark.parametrize(
    "timed_out,limited,reason",
    [
        (True, False, "phase_deadline_exceeded"),
        (False, True, "phase_output_limit_exceeded"),
    ],
)
def test_zero_exit_cannot_hide_timeout_or_excess_output(
    runner: Any,
    backend: Any,
    monkeypatch: pytest.MonkeyPatch,
    project: Path,
    tmp_path: Path,
    timed_out: bool,
    limited: bool,
    reason: str,
) -> None:
    _fake_processes(
        runner,
        monkeypatch,
        {
            "build": runner.ProcessResult(0, timed_out, limited, "truncated", ""),
        },
    )
    report = runner.run_project(project, tmp_path / "work", 10, install_allowed=True)
    assert report["status"] == "failed" and report["reason"] == reason
    assert report["checks"][1]["status"] == "failed"
    assert report["checks"][2]["status"] == "not_run"


def test_failing_version_probe_blocks_install(
    runner: Any,
    backend: Any,
    monkeypatch: pytest.MonkeyPatch,
    project: Path,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        runner,
        "_run_bounded",
        lambda *args, **kwargs: runner.ProcessResult(
            127, False, False, "", "private probe details"
        ),
    )
    report = runner.run_project(project, tmp_path / "work", 5, install_allowed=True)
    assert report["reason"] == "node_probe_failed"
    assert report["status"] == "blocked" and report["runtime"]["node"] == ""
    assert "private probe details" not in json.dumps(report)
    assert all(c["status"] == "not_run" for c in report["checks"])


@pytest.mark.parametrize('stderr,reason', [
    ('bwrap: Creating new namespace failed: Operation not permitted', 'user_namespace_unavailable'),
    ('bwrap: Can\'t bind mount /private: Permission denied', 'sandbox_mount_failed'),
    ('bwrap: execvp node: Permission denied', 'runtime_access_denied'),
])
def test_runtime_probe_emits_fixed_infrastructure_reason_not_private_stderr(
    runner: Any, backend: Any, monkeypatch: pytest.MonkeyPatch,
    project: Path, tmp_path: Path, stderr: str, reason: str,
) -> None:
    monkeypatch.setattr(runner, '_run_bounded', lambda *args, **kwargs:
        runner.ProcessResult(1, False, False, '', stderr))
    report = runner.run_project(project, tmp_path / 'work', 5, install_allowed=True)
    assert report['status'] == 'blocked' and report['reason'] == reason
    assert stderr not in json.dumps(report)


def test_cleanup_does_not_follow_project_created_links(
    runner: Any,
    project: Path,
    tmp_path: Path,
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep").write_text("keep", encoding="utf-8")
    try:
        (project / "escape").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation unavailable")
    runner._cleanup_work(project, time.monotonic() + 5)
    assert (outside / "keep").read_text() == "keep"
    assert not project.exists()


def test_copy_deadline_is_checked_between_chunks(
    runner: Any,
    monkeypatch: pytest.MonkeyPatch,
    project: Path,
    tmp_path: Path,
) -> None:
    (project / "large").write_bytes(b"x" * 200000)
    clock = [10.0]
    real_read = os.read

    def read(descriptor: int, amount: int) -> bytes:
        chunk = real_read(descriptor, amount)
        clock[0] += 1
        return chunk

    monkeypatch.setattr(runner.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(runner.os, "read", read)
    with pytest.raises(runner.Blocked, match="deadline_exceeded"):
        runner._copy_snapshot(project, tmp_path / "work", 11.5)


def test_cleanup_budget_prevents_false_pass(
    runner: Any,
    backend: Any,
    monkeypatch: pytest.MonkeyPatch,
    project: Path,
    tmp_path: Path,
) -> None:
    _fake_processes(runner, monkeypatch, {})

    def cleanup(*args: Any) -> None:
        raise runner.Blocked("deadline_exceeded")

    monkeypatch.setattr(runner, "_cleanup_work", cleanup)
    report = runner.run_project(project, tmp_path / "work", 5, install_allowed=True)
    assert report["status"] == "blocked" and report["reason"] == "cleanup_deadline_exceeded"


def test_actual_failed_child_cannot_forge_outer_cli_json(
    runner: Any,
    project: Path,
    tmp_path: Path,
) -> None:
    # Load the unchanged trusted source in an isolated interpreter and replace
    # only platform-specific launch/probe boundaries with an actual Python child.
    script = r"""
import runpy, sys
m = runpy.run_path(sys.argv[1])
g = m['main'].__globals__
g['_backend_reason'] = lambda sandbox: None
def probe(sandbox, work, deadline, runtime):
    runtime.update(node='v22.18.0', npm='10.9.3')
g['_probe_runtime'] = probe
g['_phase_command'] = lambda *args: [sys.executable, '-I', '-c',
    "import sys; print('{\"status\":\"passed\"}'); sys.exit(9)"]
raise SystemExit(m['main'](sys.argv[2:]))
"""
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-B",
            "-c",
            script,
            str(SOURCE),
            "--input",
            str(project),
            "--work",
            str(tmp_path / "work"),
            "--timeout",
            "5",
            "--install-allowed",
        ],
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0 and result.stderr == b""
    report = json.loads(result.stdout)
    assert report["status"] == "failed" and report["checks"][0]["exit_code"] == 9
    assert json.loads(report["checks"][0]["stdout"])["status"] == "passed"


def test_parent_traversal_paths_are_rejected_before_copy(
    runner: Any,
    project: Path,
    tmp_path: Path,
) -> None:
    (tmp_path / "bridge").mkdir()
    with pytest.raises(runner.Blocked, match="unsafe_path"):
        runner._copy_snapshot(project, tmp_path / "bridge/../input", time.monotonic() + 5)


def test_cli_missing_input_is_blocked_not_an_infrastructure_failure(
    runner: Any, tmp_path: Path
) -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-B",
            str(SOURCE),
            "--input",
            str(tmp_path / "missing"),
            "--work",
            str(tmp_path / "work"),
            "--timeout",
            "5",
        ],
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0 and result.stderr == b""
    assert json.loads(result.stdout)["reason"] == "input_unavailable"


@pytest.mark.parametrize(
    "metadata,reason",
    [
        ({"workspaces": ["packages/*"]}, "unsupported_workspaces"),
        ({"workspaces": {}}, "unsupported_workspaces"),
        ({"overrides": {"example": "file:../other"}}, "unsupported_overrides"),
        (
            {"resolutions": {"example": "git+https://example.invalid/repo"}},
            "unsupported_resolutions",
        ),
        ({"pnpm": {"patchedDependencies": {}}}, "unsupported_pnpm"),
        ({"packageManager": "yarn@4.0.0"}, "unsupported_package_manager"),
        ({"devEngines": {"packageManager": {"name": "pnpm"}}}, "unsupported_package_manager"),
    ],
)
def test_verify_independently_rejects_unsupported_install_configuration(
    runner: Any,
    monkeypatch: pytest.MonkeyPatch,
    project: Path,
    tmp_path: Path,
    metadata: dict[str, Any],
    reason: str,
) -> None:
    manifest = json.loads((project / "package.json").read_text())
    manifest.update(metadata)
    (project / "package.json").write_text(json.dumps(manifest), encoding="utf-8")

    def unexpected(*args: Any, **kwargs: Any) -> None:
        pytest.fail("unsupported metadata must be rejected before any process launch")

    monkeypatch.setattr(runner, "_run_bounded", unexpected)
    report = runner.run_project(project, tmp_path / "work", 5, install_allowed=True)
    assert report["status"] == "blocked" and report["reason"] == reason
    assert all(c["status"] == "not_run" for c in report["checks"])


def test_cleanup_failure_cannot_emit_passed_json(
    runner: Any,
    project: Path,
    tmp_path: Path,
) -> None:
    script = r"""
import runpy, sys
m = runpy.run_path(sys.argv[1])
g = m['main'].__globals__
g['_backend_reason'] = lambda sandbox: None
def probe(sandbox, work, deadline, runtime):
    runtime.update(node='v22.18.0', npm='10.9.3')
def cleanup(*args):
    raise OSError('private-sentinel-cleanup')
g['_probe_runtime'] = probe
g['_cleanup_work'] = cleanup
g['_phase_command'] = lambda *args: [sys.executable, '-I', '-c', 'pass']
raise SystemExit(m['main'](sys.argv[2:]))
"""
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-B",
            "-c",
            script,
            str(SOURCE),
            "--input",
            str(project),
            "--work",
            str(tmp_path / "work"),
            "--timeout",
            "5",
            "--install-allowed",
        ],
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert result.returncode != 0 and result.stdout == b""
    assert b"private-sentinel" not in result.stderr


@pytest.mark.parametrize(
    "missing,reason",
    [
        ("bwrap", "bwrap_unavailable"),
        ("node", "node_runtime_missing"),
        ("npm", "npm_runtime_missing"),
    ],
)
def test_backend_checks_fixed_tools_even_when_path_contains_other_tools(
    runner: Any,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    missing: str,
    reason: str,
) -> None:
    trusted = tmp_path / "trusted"
    (trusted / "bin").mkdir(parents=True)
    bwrap = tmp_path / "bwrap"
    for name, path in (
        ("bwrap", bwrap),
        ("node", trusted / "bin/node"),
        ("npm", trusted / "bin/npm"),
    ):
        if name != missing:
            path.write_bytes(b"not executed")
            path.chmod(0o700)
    monkeypatch.setattr(runner, "sys", SimpleNamespace(platform="linux"))
    monkeypatch.setattr(runner, "_NODE_HOME", trusted)
    monkeypatch.setattr(runner, "_BWRAP", bwrap)
    monkeypatch.setenv("AGENT_HUB_GENERATED_PROJECT_VALIDATION_SANDBOX", "systemd")
    monkeypatch.setenv("PATH", str(Path(sys.executable).parent))
    assert runner._backend_reason({"sandbox_available": lambda: True}) == reason


def test_manifest_hash_includes_hidden_unicode_and_nested_delivery_metadata(
    runner: Any,
    project: Path,
    tmp_path: Path,
) -> None:
    (project / "\u6d4b\u8bd5").mkdir()
    (project / "\u6d4b\u8bd5/.hidden").write_bytes(b"hidden")
    (project / "\u6d4b\u8bd5/DELIVERY_MANIFEST.json").write_bytes(b"source metadata")
    (project / "DELIVERY_MANIFEST.json").write_bytes(b"ignored")
    expected = {
        p.relative_to(project).as_posix(): hashlib.sha256(b'f\0' + p.read_bytes()).hexdigest()
        for p in project.rglob("*")
        if p.is_file()
    }
    digest = runner._copy_snapshot(project, tmp_path / "work", time.monotonic() + 5)
    assert (
        digest
        == hashlib.sha256(
            json.dumps(
                expected,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
            ).encode("ascii")
        ).hexdigest()
    )
