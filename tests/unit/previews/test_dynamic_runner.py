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


def run_stage_with_logs(
    monkeypatch: pytest.MonkeyPatch, stderr: tuple[bytes, ...], *,
    stdout: tuple[bytes, ...] = (), stage: str = "install", exit_code: int = 1,
    log_limit: int | None = None,
) -> dict[str, object]:
    import io
    from types import SimpleNamespace
    mod = runner()
    output = io.BytesIO()
    cleaned: list[object] = []
    killed: list[int] = []

    class ChunkedPipe:
        def __init__(self, chunks: tuple[bytes, ...]) -> None:
            self.chunks = iter(chunks)
            self.closed = False

        def read(self, size: int) -> bytes:
            chunk = next(self.chunks, b"")
            assert len(chunk) <= size
            return chunk

        def close(self) -> None:
            self.closed = True

    class ImmediateThread:
        def __init__(self, *, target: Any, args: tuple[object, ...], daemon: bool) -> None:
            self.target, self.args = target, args

        def start(self) -> None:
            self.target(*self.args)

        def join(self, timeout: float) -> None:
            pass

    child = SimpleNamespace(
        pid=123, wait=lambda timeout: exit_code, poll=lambda: exit_code,
        stdout=ChunkedPipe(stdout), stderr=ChunkedPipe(stderr),
    )
    monkeypatch.setattr(mod, "sys", SimpleNamespace(
        platform="linux", argv=["runner", stage], stdout=SimpleNamespace(buffer=output),
    ))
    monkeypatch.setattr(mod, "_ensure_work_directory", lambda path: None)
    monkeypatch.setattr(mod, "_copy_source", lambda source, work: None)
    monkeypatch.setattr(mod, "_spawn_application", lambda selected: child)
    monkeypatch.setattr(mod, "_kill_child", cleaned.append)
    monkeypatch.setattr(mod, "_kill_group", killed.append)
    monkeypatch.setattr(mod.threading, "Thread", ImmediateThread)
    if log_limit is not None:
        monkeypatch.setattr(mod, "MAX_LOG", log_limit)
    overflow = sum(len(chunk) for chunk in stdout + stderr) > mod.MAX_LOG
    if exit_code == 0 and not overflow:
        mod.main()
    else:
        with pytest.raises(SystemExit) as failure:
            mod.main()
        assert failure.value.code == 1
    assert cleaned == [child]
    assert child.stdout.closed and child.stderr.closed
    assert bool(killed) is overflow
    assert b"sentinel" not in output.getvalue()
    output.seek(0)
    result: dict[str, object] = mod.read_frame(output)
    assert not output.read(), "only one sanitized frame may leave the runner"
    return result


@pytest.mark.parametrize("prefix", [b"npm ERR! code ", b"npm error code "])
@pytest.mark.parametrize("code,reason", [
    (b"EACCES", "permission_denied"), (b"EPERM", "permission_denied"),
    (b"ENOENT", "not_found"),
    (b"ENOSPC", "storage_full"), (b"EROFS", "read_only"),
    (b"EFBIG", "file_size_limit"),
    (b"ENOMEM", "resource_limit"), (b"EMFILE", "resource_limit"),
    (b"ENFILE", "resource_limit"), (b"ENOTFOUND", "registry_unavailable"),
    (b"EAI_AGAIN", "registry_unavailable"), (b"ECONNREFUSED", "registry_unavailable"),
    (b"ECONNRESET", "registry_unavailable"), (b"ETIMEDOUT", "registry_unavailable"),
    (b"ERR_SOCKET_TIMEOUT", "registry_unavailable"),
    (b"E404", "dependency_unavailable"), (b"ETARGET", "dependency_unavailable"),
    (b"ERESOLVE", "dependency_conflict"), (b"EJSONPARSE", "package_invalid"),
    (b"ENOLOCK", "package_invalid"), (b"EPACKAGEJSON", "package_invalid"),
    (b"CERT_HAS_EXPIRED", "certificate_error"),
    (b"UNABLE_TO_VERIFY_LEAF_SIGNATURE", "certificate_error"),
    (b"SELF_SIGNED_CERT_IN_CHAIN", "certificate_error"),
    (b"DEPTH_ZERO_SELF_SIGNED_CERT", "certificate_error"),
    (b"EEXIST", "storage_conflict"), (b"ENOTEMPTY", "storage_conflict"),
    (b"EBADENGINE", "runtime_incompatible"), (b"EBADDEVENGINES", "runtime_incompatible"),
    (b"EBADPLATFORM", "runtime_incompatible"), (b"EINTEGRITY", "integrity_error"),
    (b"E401", "registry_denied"), (b"E403", "registry_denied"),
    (b"ENEEDAUTH", "registry_denied"),
    (b"EINVALIDPACKAGENAME", "package_invalid"), (b"EINVALIDTAGNAME", "package_invalid"),
    (b"EINVALIDPACKAGETYPE", "package_invalid"), (b"EUSAGE", "package_invalid"),
    (b"E500", "registry_unavailable"), (b"E502", "registry_unavailable"),
    (b"E503", "registry_unavailable"), (b"E504", "registry_unavailable"),
    (b"E429", "registry_unavailable"), (b"ENOTCACHED", "registry_unavailable"),
    (b"ESOCKETTIMEDOUT", "registry_unavailable"),
    (b"ERR_WORKER_INIT_FAILED", "resource_limit"),
])
def test_install_stderr_reports_only_fixed_npm_category(
    monkeypatch: pytest.MonkeyPatch, prefix: bytes, code: bytes, reason: str,
) -> None:
    result = run_stage_with_logs(monkeypatch, (
        b"/private/source token=sentinel\n", prefix + code + b"\n",
        b"npm error path /private/cache token=sentinel\n",
    ))
    assert result == {
        "ok": False, "error": "preview startup failed", "phase": "install", "reason": reason,
    }


@pytest.mark.parametrize("prefix", [b"npm error ", b"npm ERR! "])
@pytest.mark.parametrize("ending", [b"\n", b"\r\n", b""])
@pytest.mark.parametrize("chunk_size", [1, 4096])
@pytest.mark.parametrize("message,reason", [
    (b"Exit handler never called!", "npm_exit_incomplete"),
    (b"Cannot read properties of undefined (reading 'sentinel')", "npm_internal_error"),
    (b"Cannot read properties of null (reading 'sentinel')", "npm_internal_error"),
    (b"Invalid Version: /private/token=sentinel", "package_invalid"),
    (b"Invalid comparator: /private/token=sentinel", "package_invalid"),
])
def test_install_known_npm_messages_report_only_fixed_category(
    monkeypatch: pytest.MonkeyPatch, prefix: bytes, ending: bytes, chunk_size: int,
    message: bytes, reason: str,
) -> None:
    line = prefix + message + ending
    chunks = tuple(line[i:i + chunk_size] for i in range(0, len(line), chunk_size))
    assert run_stage_with_logs(monkeypatch, chunks) == {
        "ok": False, "error": "preview startup failed", "phase": "install", "reason": reason,
    }


@pytest.mark.parametrize("line,reason", [
    (b"npm ERR! code ENOSPC\n", "storage_full"),
    (b"npm error code ERESOLVE\r\n", "dependency_conflict"),
    (b"npm error code ENOLOCK", "package_invalid"),
    (b"npm error code EEXIST\n", "storage_conflict"),
    (b"npm ERR! code EBADDEVENGINES\r\n", "runtime_incompatible"),
    (b"npm error code EINTEGRITY", "integrity_error"),
    (b"npm error code ENEEDAUTH\n", "registry_denied"),
    (b"npm error code EINVALIDPACKAGETYPE\n", "package_invalid"),
    (b"npm error code ENOTCACHED\n", "registry_unavailable"),
    (b"npm error code ERR_WORKER_INIT_FAILED\n", "resource_limit"),
    (b"generated dependency source rejected: /private/token=sentinel\n", "dependency_rejected"),
    (b"generated dependency source rejected: /private/token=sentinel", "dependency_rejected"),
])
def test_install_diagnostic_handles_single_byte_chunks(
    monkeypatch: pytest.MonkeyPatch, line: bytes, reason: str,
) -> None:
    result = run_stage_with_logs(monkeypatch, tuple(line[i:i + 1] for i in range(len(line))))
    assert result == {
        "ok": False, "error": "preview startup failed", "phase": "install", "reason": reason,
    }


@pytest.mark.parametrize("line", [
    b"", b"unknown /private/path token=sentinel\n", b"npm error code EUNKNOWN\n",
    b"npm error code /private/EACCES\n", b"npm error code EACCES token=sentinel\n",
    b"npm error code EACCES/sentinel\n", b"prefix npm error code EACCES\n",
    b"\x1b[31mnpm error code EACCES\x1b[0m\n", b"npm error code EACCES\x1b[0m\n",
    b"npm error code \x1b[31mEACCES\n", b"npm error code EACCES\x00\n",
    b"npm error code EACCES\rhidden\n", b"npm error code EACCES\xff\n",
    b"generated dependency source rejected:/private/token=sentinel\n",
    b"prefix generated dependency source rejected: sentinel\n",
    b"npm error code E501\n", b"npm error code E499\n",
    b"npm error code EEXIST token=sentinel\n", b"npm error code EEXISTING\n",
    b"npm error code ERR_WORKER_INIT_FAILED_EXTRA\n",
    b"npm error code eintegrity\n", b"npm error code E401\x00\n",
    b"npm error /private/token=sentinel Exit handler never called!\n",
    b"npm error Exit handler never called! token=sentinel\n",
    b"npm error Exit handler never called\n",
    b"npm error Exit handler never called!\rhidden\n",
    b"npm error Exit handler never called!\x1b[0m\n",
    b"npm error Cannot read properties of undefined\n",
    b"npm error Cannot read properties of null sentinel\n",
    b"npm error Cannot read properties of undefined (reading sentinel)\n",
    b"npm error Cannot read properties of null (reading \"sentinel\")\n",
    b"npm error Cannot read properties of undefined (reading 'sentinel'\n",
    b"npm error Cannot read properties of undefined (reading 'sentinel') suffix\n",
    b"npm error Cannot read properties of null (writing 'sentinel')\n",
    b"npm error Cannot read properties of false (reading 'sentinel')\n",
    b"npm error Cannot read properties of null (reading 'sentinel\x00')\n",
    b"npm error Cannot read properties of null (reading 'sentinel\rhidden')\n",
    b"npm error Cannot read properties of null (reading 'sentinel\xff')\n",
    b"npm error Invalid Version:/private/token=sentinel\n",
    b"npm error Invalid comparator:/private/token=sentinel\n",
    b"npm error Invalid Version \n", b"npm error Invalid comparator \n",
    b"npm error Invalid version: token=sentinel\n",
    b"npm error Invalid Comparator: token=sentinel\n",
])
def test_unknown_install_stderr_does_not_classify_or_leak(
    monkeypatch: pytest.MonkeyPatch, line: bytes,
) -> None:
    assert run_stage_with_logs(monkeypatch, (line,) if line else ()) == {
        "ok": False, "error": "preview startup failed", "phase": "install",
        "reason": "nonzero_exit",
    }


@pytest.mark.parametrize("prefix", [
    b"", b"prefix npm error ", b"npm warn ", b"npm notice ",
    b"npm ERR ", b"npm error code ", b"\x1b[31mnpm error ",
])
@pytest.mark.parametrize("message", [
    b"Exit handler never called!",
    b"Cannot read properties of undefined (reading 'sentinel')",
    b"Cannot read properties of null (reading 'sentinel')",
    b"Invalid Version: /private/token=sentinel",
    b"Invalid comparator: /private/token=sentinel",
])
def test_npm_message_requires_exact_error_prefix(
    monkeypatch: pytest.MonkeyPatch, prefix: bytes, message: bytes,
) -> None:
    assert run_stage_with_logs(monkeypatch, (prefix + message + b"\n",)) == {
        "ok": False, "error": "preview startup failed", "phase": "install",
        "reason": "nonzero_exit",
    }


@pytest.mark.parametrize("tail,reason", [
    (b"npm error code EACCES\n", "nonzero_exit"),
    (b"npm error code EACCES", "nonzero_exit"),
    (b"npm error code EACCES\nnpm error code ENOSPC\n", "storage_full"),
    (b"npm error Exit handler never called!\n", "nonzero_exit"),
    (b"npm error Invalid Version: sentinel", "nonzero_exit"),
    (b"npm error Invalid comparator: sentinel\nnpm error code EEXIST\n", "storage_conflict"),
])
def test_install_discards_entire_overlong_line_until_newline(
    monkeypatch: pytest.MonkeyPatch, tail: bytes, reason: str,
) -> None:
    result = run_stage_with_logs(monkeypatch, (
        b"generated dependency source rejected: " + b"x" * 1000,
        b"x" * 4096, tail,
    ))
    assert result["reason"] == reason


@pytest.mark.parametrize("prefix,suffix,reason", [
    (b"generated dependency source rejected: ", b"", "dependency_rejected"),
    (b"npm error Invalid Version: ", b"", "package_invalid"),
    (b"npm ERR! Invalid comparator: ", b"", "package_invalid"),
    (b"npm error Cannot read properties of null (reading '", b"')", "npm_internal_error"),
])
@pytest.mark.parametrize("length", [512, 513])
@pytest.mark.parametrize("ending", [b"\n", b""])
def test_install_diagnostic_line_buffer_has_fixed_boundary(
    monkeypatch: pytest.MonkeyPatch, prefix: bytes, suffix: bytes, reason: str,
    length: int, ending: bytes,
) -> None:
    line = prefix.ljust(length - len(suffix), b"x") + suffix
    result = run_stage_with_logs(monkeypatch, (line[:500], line[500:] + ending))
    assert result["reason"] == (reason if length == 512 else "nonzero_exit")


@pytest.mark.parametrize("stage,stream", [
    ("install", "stdout"), ("build", "stderr"), ("start", "stderr"),
    ("build", "stdout"), ("start", "stdout"),
])
@pytest.mark.parametrize("line", [
    b"npm error code EACCES\n", b"generated dependency source rejected: sentinel\n",
    b"npm error code EEXIST\n", b"npm ERR! Exit handler never called!\n",
    b"npm error Cannot read properties of undefined (reading 'sentinel')\n",
    b"npm error Cannot read properties of null (reading 'sentinel')\n",
    b"npm error Invalid Version: token=sentinel\n",
    b"npm ERR! Invalid comparator: token=sentinel\n",
])
def test_install_classification_is_restricted_to_install_stderr(
    monkeypatch: pytest.MonkeyPatch, stage: str, stream: str, line: bytes,
) -> None:
    result = run_stage_with_logs(
        monkeypatch, (line,) if stream == "stderr" else (),
        stdout=(line,) if stream == "stdout" else (), stage=stage,
    )
    assert result["reason"] == "nonzero_exit"


@pytest.mark.parametrize("line", [
    b"npm error code EACCES\n", b"generated dependency source rejected: sentinel\n",
    b"npm error code EINTEGRITY\n", b"npm error Exit handler never called!\n",
    b"npm error Cannot read properties of undefined (reading 'sentinel')\n",
    b"npm error Cannot read properties of null (reading 'sentinel')\n",
    b"npm ERR! Invalid Version: token=sentinel\n",
    b"npm error Invalid comparator: token=sentinel\n",
])
def test_successful_install_ignores_error_looking_stderr(
    monkeypatch: pytest.MonkeyPatch, line: bytes,
) -> None:
    assert run_stage_with_logs(monkeypatch, (line,), exit_code=0) == {
        "ok": True, "state": "prepared",
    }


@pytest.mark.parametrize("stage", ["install", "build", "start"])
@pytest.mark.parametrize("stderr", [(), (b"unknown /private/token=sentinel\n",)])
@pytest.mark.parametrize("exit_code,reason", [
    (-6, "signal_abort"), (-9, "signal_kill"), (-11, "signal_segv"),
    (-15, "signal_term"), (-25, "file_size_limit"),
    (-1, "signal_exit"), (-127, "signal_exit"),
])
def test_signal_exit_reports_fixed_category_only_for_preparation(
    monkeypatch: pytest.MonkeyPatch, stage: str, stderr: tuple[bytes, ...],
    exit_code: int, reason: str,
) -> None:
    assert run_stage_with_logs(monkeypatch, stderr, stage=stage, exit_code=exit_code) == {
        "ok": False, "error": "preview startup failed", "phase": stage,
        "reason": "nonzero_exit" if stage == "start" else reason,
    }


@pytest.mark.parametrize("stage", ["install", "build"])
@pytest.mark.parametrize("exit_code", [0, 1, 6, 9, 11, 15, 25, 134, 137, 139, 143, 153])
def test_nonnegative_preparation_exit_does_not_become_signal_category(
    monkeypatch: pytest.MonkeyPatch, stage: str, exit_code: int,
) -> None:
    result = run_stage_with_logs(monkeypatch, (), stage=stage, exit_code=exit_code)
    if exit_code == 0:
        assert result == {"ok": True, "state": "prepared"}
    else:
        assert result == {
            "ok": False, "error": "preview startup failed", "phase": stage,
            "reason": "nonzero_exit",
        }


@pytest.mark.parametrize("exit_code", [-6, -9, -11, -15, -25, -1])
@pytest.mark.parametrize("line,reason", [
    (b"npm error code EEXIST\n", "storage_conflict"),
    (b"npm error Exit handler never called!\n", "npm_exit_incomplete"),
    (b"generated dependency source rejected: token=sentinel\n", "dependency_rejected"),
])
def test_install_diagnostic_takes_priority_over_signal_category(
    monkeypatch: pytest.MonkeyPatch, exit_code: int, line: bytes, reason: str,
) -> None:
    assert run_stage_with_logs(monkeypatch, (line,), exit_code=exit_code) == {
        "ok": False, "error": "preview startup failed", "phase": "install", "reason": reason,
    }


@pytest.mark.parametrize("stage,stream", [
    ("install", "stdout"), ("build", "stdout"), ("build", "stderr"),
])
def test_npm_text_outside_install_stderr_does_not_override_signal_category(
    monkeypatch: pytest.MonkeyPatch, stage: str, stream: str,
) -> None:
    line = b"npm error code EEXIST\n"
    assert run_stage_with_logs(
        monkeypatch, (line,) if stream == "stderr" else (),
        stdout=(line,) if stream == "stdout" else (), stage=stage, exit_code=-9,
    ) == {
        "ok": False, "error": "preview startup failed", "phase": stage, "reason": "signal_kill",
    }


@pytest.mark.parametrize("stage", ["install", "build"])
@pytest.mark.parametrize("exit_code", [0, 1, -6, -9, -11, -15, -25, -1])
@pytest.mark.parametrize("chunks", [
    (b"npm error code EACCES\n", b"x" * 65),
    (b"x" * 65 + b"\nnpm error code EACCES\n",),
    (b"npm error Exit handler never called!\n", b"x" * 65),
    (b"npm error Invalid Version: sentinel\n", b"x" * 65),
])
def test_log_limit_takes_priority_over_install_classification(
    monkeypatch: pytest.MonkeyPatch, stage: str, exit_code: int, chunks: tuple[bytes, ...],
) -> None:
    assert run_stage_with_logs(
        monkeypatch, chunks, stage=stage, exit_code=exit_code, log_limit=64,
    ) == {
        "ok": False, "error": "preview startup failed", "phase": stage, "reason": "log_limit",
    }


@pytest.mark.parametrize("exit_code", [0, 1])
@pytest.mark.parametrize("extra_byte", [b"", b"x"])
@pytest.mark.parametrize("stream", ["stderr", "combined"])
def test_install_diagnostic_preserves_one_mib_log_boundary(
    monkeypatch: pytest.MonkeyPatch, exit_code: int, extra_byte: bytes, stream: str,
) -> None:
    line = b"npm error Exit handler never called!\n"
    padding = b"x" * (1024 * 1024 - len(line)) + extra_byte
    chunks = tuple(padding[i:i + 4096] for i in range(0, len(padding), 4096))
    result = run_stage_with_logs(
        monkeypatch, (line,) + chunks if stream == "stderr" else (line,),
        stdout=chunks if stream == "combined" else (), exit_code=exit_code,
    )
    if not extra_byte and exit_code == 0:
        assert result == {"ok": True, "state": "prepared"}
    else:
        assert result == {
            "ok": False, "error": "preview startup failed", "phase": "install",
            "reason": "log_limit" if extra_byte else "npm_exit_incomplete",
        }


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
    ("install", "storage_conflict token=sentinel"), ("install", "runtime_incompatible/sentinel"),
    ("install", "integrity_error\n"), ("install", "registry_denied\x00"),
    ("install", "npm_exit_incomplete sentinel"), ("install", "npm_internal_error/sentinel"),
    ("install", "EEXIST"), ("install", "npm_unknown_error"),
    ("install", "signal_abort sentinel"), ("install", "signal_kill/sentinel"),
    ("install", "signal_segv\n"), ("install", "signal_term\x00"),
    ("install", "file_size_limit sentinel"), ("install", "signal_exit/sentinel"),
    ("install", "SIGKILL"), ("install", "signal_bus"),
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
