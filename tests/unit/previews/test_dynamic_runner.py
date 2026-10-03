"""Local trusted relay fixtures only, not generated-app or 20-project acceptance."""

from __future__ import annotations

import base64
import http.server
import importlib
import sys
import threading
from pathlib import Path
from typing import Any

import pytest


def runner() -> Any:
    assert importlib.util.find_spec("agent_hub.previews.dynamic_runner"), "Task1 runner missing"
    return importlib.import_module("agent_hub.previews.dynamic_runner")


def test_generated_process_has_clean_environment_and_no_control_descriptors() -> None:
    mod = runner()
    env = mod.application_environment()
    assert env["PORT"] == str(mod.APP_PORT)
    assert env["DATA_DIR"] == "/preview/work/data"
    assert env["NPM_CONFIG_USERCONFIG"] == "/tmp/npm-user.npmrc"
    assert env["NPM_CONFIG_GLOBALCONFIG"] == "/tmp/npm-global.npmrc"
    assert not {"AUTHORIZATION", "HTTP_PROXY", "PYTHONPATH", "LISTEN_FDS"}.intersection(env)
    assert mod.stage_argv("start") == ("/preview/node/bin/npm", "start", "--ignore-scripts")
    assert mod.stage_argv("build") == (
        "/preview/node/bin/npm", "run", "build", "--if-present", "--ignore-scripts",
    )


def test_install_uses_existing_trusted_validation_guard() -> None:
    mod = runner()
    assert mod.stage_argv("install") == (
        "/preview/node/bin/node", "-e", mod.install_guard(), "/preview/node/bin/npm",
        "install", "--ignore-scripts", "--no-audit", "--no-fund",
    )
    assert "subprocess execution is forbidden" in mod.install_guard()
    assert "unsupported dependency source" in mod.install_guard()


def test_unknown_stage_cannot_select_executable() -> None:
    with pytest.raises(ValueError):
        runner().stage_argv("host-shell")


def test_actual_local_npm_parses_distinct_private_config_paths(tmp_path: Path) -> None:
    """Trusted version-only command, no generated code/network or Linux isolation proof."""
    import os
    import shutil
    import subprocess
    node = shutil.which("node")
    if node is None:
        pytest.skip("local trusted Node/npm unavailable")
    npm = Path(node).parent / "node_modules/npm/bin/npm-cli.js"
    if not npm.is_file():
        npm_command = shutil.which("npm")
        if npm_command is None or os.name == "nt":
            pytest.skip("local trusted npm CLI unavailable")
        npm = Path(npm_command).resolve()
    env = runner().application_environment()
    env.update(PATH=str(Path(node).parent) + os.pathsep + os.defpath, HOME=str(tmp_path),
               TMPDIR=str(tmp_path), TMP=str(tmp_path), NPM_CONFIG_CACHE=str(tmp_path / "cache"))
    if "SystemRoot" in os.environ:
        env["SystemRoot"] = os.environ["SystemRoot"]
    env["NPM_CONFIG_USERCONFIG"] = str(tmp_path / Path(env["NPM_CONFIG_USERCONFIG"]).name)
    env["NPM_CONFIG_GLOBALCONFIG"] = env["NPM_CONFIG_USERCONFIG"]
    failed = subprocess.run((node, str(npm), "--version"), env=env, cwd=tmp_path,
                            stdin=subprocess.DEVNULL, capture_output=True, timeout=10, check=False)
    assert failed.returncode != 0
    assert b"double-loading config" in failed.stderr
    env["NPM_CONFIG_GLOBALCONFIG"] = str(tmp_path / "npm-global.npmrc")
    passed = subprocess.run((node, str(npm), "--version"), env=env, cwd=tmp_path,
                            stdin=subprocess.DEVNULL, capture_output=True, timeout=10, check=False)
    assert passed.returncode == 0, "trusted npm version command failed"
    assert passed.stdout.strip()


@pytest.mark.skipif(sys.platform == "linux", reason="Windows fail-closed contract fixture")
def test_nonlinux_runner_rejects_before_any_work_or_spawn() -> None:
    with pytest.raises(RuntimeError, match="Linux"):
        runner()._run_stage("start")


def test_copy_of_frozen_source_is_writable_only_in_private_prepare_work(tmp_path: Path) -> None:
    mod = runner()
    source = tmp_path / "frozen"
    source.mkdir()
    (source / "package.json").write_text("fixture")
    (source / "package.json").chmod(0o444)
    work = tmp_path / "private"
    work.mkdir()
    try:
        mod._copy_source(source, work)
        (work / "app/package.json").write_text("prepared fixture")
        assert (source / "package.json").read_text() == "fixture"
    finally:
        (source / "package.json").chmod(0o644)


def test_child_creation_has_no_protocol_fds_or_host_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace
    mod = runner()
    observed: list[dict[str, object]] = []
    protection: list[str] = []

    def popen(argv: tuple[str, ...], **kwargs: object) -> object:
        assert protection == ["protected"]
        assert argv == mod.stage_argv("start")
        observed.append(kwargs)
        return object()

    monkeypatch.setenv("AUTHORIZATION", "sentinel")
    monkeypatch.setenv("HTTP_PROXY", "sentinel")
    monkeypatch.setattr(mod, "sys", SimpleNamespace(platform="linux"))
    monkeypatch.setattr(mod, "_protect_control_fds", lambda: protection.append("protected"), raising=False)
    monkeypatch.setattr(mod.subprocess, "Popen", popen)
    mod._spawn_application("start")
    assert observed[0]["close_fds"] is True
    assert observed[0]["stdin"] == mod.subprocess.DEVNULL
    assert observed[0]["stdout"] == observed[0]["stderr"] == mod.subprocess.PIPE
    assert "pass_fds" not in observed[0]
    assert observed[0]["env"] == mod.application_environment()


def test_existing_work_directory_does_not_require_previous_stage_uid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    mod = runner()
    directory = tmp_path / "previous-stage-owned"
    directory.mkdir()

    def chmod(*args: object, **kwargs: object) -> None:
        raise PermissionError("different DynamicUser fixture")

    monkeypatch.setattr(Path, "chmod", chmod)
    mod._ensure_work_directory(directory)


def test_app_exit_is_detected_while_no_browser_request_arrives() -> None:
    from types import SimpleNamespace
    mod = runner()
    child = SimpleNamespace(poll=lambda: 1)
    with pytest.raises(RuntimeError, match="exited"):
        mod._wait_request(child, threading.Event())


def test_static_probe_checks_dedicated_node_and_npm_without_generated_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import subprocess
    from types import SimpleNamespace
    mod = runner()
    commands: list[tuple[str, ...]] = []

    def run(argv: tuple[str, ...], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        commands.append(argv)
        assert kwargs["env"] == mod.application_environment()
        assert kwargs["stdin"] == subprocess.DEVNULL
        assert kwargs["stdout"] == kwargs["stderr"] == subprocess.DEVNULL
        assert kwargs["close_fds"] is True
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(mod, "sys", SimpleNamespace(platform="linux"))
    monkeypatch.setattr(mod.subprocess, "run", run)
    mod._probe_toolchain()
    assert commands == [
        ("/preview/node/bin/node", "--version"),
        ("/preview/node/bin/node", "/preview/node/bin/npm", "--version"),
    ]


def test_probe_control_failure_reports_fixed_phase_without_exception_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import io
    from types import SimpleNamespace
    mod = runner()
    output = io.BytesIO()

    def fail() -> None:
        raise PermissionError("secret fixture must not leave the runner")

    monkeypatch.setattr(mod, "sys", SimpleNamespace(
        platform="linux", argv=["runner", "probe"], stdout=SimpleNamespace(buffer=output),
    ))
    monkeypatch.setattr(mod, "_protect_control_fds", fail)
    with pytest.raises(SystemExit):
        mod.main()
    output.seek(0)
    assert mod.read_frame(output) == {
        "ok": False, "error": "preview probe failed", "phase": "control_fds", "reason": "permission_denied",
    }


@pytest.mark.parametrize("failed_index,phase", [(0, "node"), (1, "npm")])
def test_probe_toolchain_failure_identifies_only_fixed_stage(
    monkeypatch: pytest.MonkeyPatch, failed_index: int, phase: str,
) -> None:
    import subprocess
    from types import SimpleNamespace
    mod = runner()
    count = 0

    def run(argv: tuple[str, ...], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        nonlocal count
        result: subprocess.CompletedProcess[bytes] = subprocess.CompletedProcess(
            argv, 2 if count == failed_index else 0,
        )
        count += 1
        return result

    monkeypatch.setattr(mod, "sys", SimpleNamespace(platform="linux"))
    monkeypatch.setattr(mod.subprocess, "run", run)
    with pytest.raises(mod.ProbeFailure) as failure:
        mod._probe_toolchain()
    assert failure.value.phase == phase
    assert failure.value.reason == "nonzero_exit"


def test_actual_trusted_http_relay_preserves_patch_query_status_and_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mod = runner()
    observed: list[tuple[str, bytes, str | None]] = []

    class FixtureHandler(http.server.BaseHTTPRequestHandler):
        def do_PATCH(self) -> None:
            body = self.rfile.read(int(self.headers["Content-Length"]))
            observed.append((self.path, body, self.headers.get("Authorization")))
            self.send_response(422)
            self.send_header("Content-Type", "application/json")
            self.send_header("Set-Cookie", "sentinel")
            self.end_headers()
            self.wfile.write(b'{"fixture":"actual HTTP error"}')

        def log_message(self, format: str, *args: object) -> None:
            pass

    with http.server.HTTPServer(("127.0.0.1", 0), FixtureHandler) as server:
        monkeypatch.setattr(mod, "APP_PORT", server.server_port)
        worker = threading.Thread(target=server.serve_forever)
        worker.start()
        try:
            result = mod.relay_http(mod.validate_http_request(
                "PATCH", "/tasks?q=a%20b", (("Authorization", "sentinel"),), b"payload",
            ))
        finally:
            server.shutdown()
            worker.join(timeout=2)
    assert observed == [("/tasks?q=a%20b", b"payload", None)]
    assert result["status_code"] == 422
    assert result["headers"] == [["content-type", "application/json"]]
    assert base64.b64decode(result["body"]) == b'{"fixture":"actual HTTP error"}'


@pytest.mark.parametrize("stage,mode,reason", [
    ("install", "exit", "nonzero_exit"),
    ("build", "exit", "nonzero_exit"),
    ("build", "timeout", "timeout"),
    ("start", "exit", "nonzero_exit"),
    ("start", "clean_exit", "invalid_result"),
    ("start", "timeout", "timeout"),
    ("install", "logs", "log_limit"),
    ("start", "logs", "log_limit"),
])
def test_startup_runner_emits_only_fixed_failure_fields(
    monkeypatch: pytest.MonkeyPatch, stage: str, mode: str, reason: str,
) -> None:
    import io
    import subprocess
    from types import SimpleNamespace
    mod = runner()
    output = io.BytesIO()
    cleaned: list[object] = []

    def wait(timeout: float) -> int:
        if mode == "timeout":
            raise subprocess.TimeoutExpired("/private/source token=sentinel", timeout)
        return 9

    class ImmediateThread:
        def __init__(self, *, target: Any, args: tuple[object, ...], daemon: bool) -> None:
            self.target, self.args = target, args

        def start(self) -> None:
            self.target(*self.args)

        def join(self, timeout: float) -> None:
            pass

    child = SimpleNamespace(
        pid=123, wait=wait,
        poll=lambda: None if mode == "timeout" else 0 if mode == "clean_exit" else 9,
        stdout=io.BytesIO(b"/private/source token=sentinel"), stderr=io.BytesIO(b"stderr-sentinel"),
    )
    monkeypatch.setattr(mod, "sys", SimpleNamespace(
        platform="linux", argv=["runner", stage], stdout=SimpleNamespace(buffer=output),
    ))
    monkeypatch.setattr(mod, "_ensure_work_directory", lambda path: None)
    monkeypatch.setattr(mod, "_copy_source", lambda source, work: None)
    monkeypatch.setattr(mod, "_spawn_application", lambda selected: child)
    monkeypatch.setattr(mod, "_kill_child", cleaned.append)
    monkeypatch.setattr(mod, "_kill_group", lambda pid: None)
    monkeypatch.setattr(mod.threading, "Thread", ImmediateThread)
    if mode == "logs":
        monkeypatch.setattr(mod, "MAX_LOG", 1)
    if stage == "start" and mode == "timeout":
        times = iter([0.0, mod.READY_TIMEOUT + 1])
        monkeypatch.setattr(mod.time, "monotonic", lambda: next(times))
    with pytest.raises(SystemExit) as failure:
        mod.main()
    assert failure.value.code == 1
    output.seek(0)
    assert mod.read_frame(output) == {
        "ok": False, "error": "preview startup failed", "phase": stage, "reason": reason,
    }
    assert cleaned == [child]
    assert b"sentinel" not in output.getvalue()


@pytest.mark.parametrize("error,reason", [
    (PermissionError("/private sentinel"), "permission_denied"),
    (FileNotFoundError("/private sentinel"), "not_found"),
    (OSError(30, "/private sentinel"), "read_only"),
    (TimeoutError("/private sentinel"), "timeout"),
    (ValueError("/private sentinel"), "unsafe_tree"),
    (RuntimeError("/private sentinel"), "failed"),
])
def test_startup_phase_sanitizes_errors_and_preserves_inner_phase(
    error: Exception, reason: str,
) -> None:
    mod = runner()
    with (
        pytest.raises(mod.PreviewStartupFailure) as failure,
        mod.preview_startup_phase("install"),
        mod.preview_startup_phase("install_validate"),
    ):
        raise error
    assert failure.value.response() == {
        "ok": False, "error": "preview startup failed",
        "phase": "install_validate", "reason": reason,
    }
    assert "sentinel" not in str(failure.value)
    assert failure.value.__suppress_context__


@pytest.mark.parametrize("phase,reason", [
    ("/private/source", "failed"), ("install", "token=sentinel"),
    ([], "failed"), ("install", {}), (True, "failed"), ("probe", "failed"),
])
def test_startup_diagnostic_rejects_non_whitelisted_values(phase: Any, reason: Any) -> None:
    mod = runner()
    with pytest.raises(ValueError, match="invalid startup diagnostic"):
        mod.PreviewStartupFailure(phase, reason)


def test_running_application_failure_does_not_become_startup_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import io
    from types import SimpleNamespace
    mod = runner()
    output = io.BytesIO()
    child = SimpleNamespace(pid=123, poll=lambda: None,
                            stdout=io.BytesIO(), stderr=io.BytesIO())

    def fail(*args: object) -> None:
        raise RuntimeError("business failure sentinel")

    monkeypatch.setattr(mod, "sys", SimpleNamespace(
        platform="linux", argv=["runner", "start"], stdout=SimpleNamespace(buffer=output),
    ))
    monkeypatch.setattr(mod, "_ensure_work_directory", lambda path: None)
    monkeypatch.setattr(mod, "_spawn_application", lambda stage: child)
    monkeypatch.setattr(mod, "_kill_child", lambda process: None)
    monkeypatch.setattr(mod, "relay_http", lambda request: {})
    monkeypatch.setattr(mod, "_wait_request", fail)
    with pytest.raises(SystemExit):
        mod.main()
    output.seek(0)
    assert mod.read_frame(output) == {"ok": True, "state": "ready"}
    assert mod.read_frame(output) == {"ok": False, "error": "preview stage failed"}
