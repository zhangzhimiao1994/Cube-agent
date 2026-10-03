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
