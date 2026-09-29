from __future__ import annotations

import base64
import hashlib
import io
import json
import zipfile
from pathlib import Path

import httpx
import pytest

from agent_hub.skills.sandbox.base import SkillResult
from agent_hub.skills.sandbox.remote import (
    HttpRemoteSandboxSettings,
    HttpRemoteSkillSandbox,
    SshSandboxSettings,
    SshSkillSandbox,
    build_ssh_command,
)
from tests.contracts.test_skill_sandbox import invocation


async def test_http_remote_sandbox_invokes_versioned_protocol_and_restores_workspace(
    tmp_path: Path,
) -> None:
    package = tmp_path / "skill.zip"
    package.write_bytes(b"package")
    workdir = tmp_path / "work"
    workdir.mkdir()
    seen: dict[str, object] = {}
    archive_buffer = io.BytesIO()
    with zipfile.ZipFile(archive_buffer, "w") as archive:
        archive.writestr("result.txt", "restored")

    def handler(request: httpx.Request) -> httpx.Response:
        seen["authorization"] = request.headers.get("authorization")
        payload = json.loads(request.content)
        seen["payload"] = payload
        return httpx.Response(
            200,
            json={
                "protocol": "agent-hub.skill-sandbox",
                "version": 1,
                "result": {
                    "exit_code": 0,
                    "stdout": '{"ok":true}',
                    "stderr": "",
                    "timed_out": False,
                },
                "workspace_archive_base64": base64.b64encode(archive_buffer.getvalue()).decode(),
            },
        )

    transport = httpx.MockTransport(handler)
    sandbox = HttpRemoteSkillSandbox(
        HttpRemoteSandboxSettings(
            provider="modal",
            endpoint="https://sandbox.example",
            bearer_token="secret-token",
        ),
        transport=transport,
    )

    result = await sandbox.run(
        invocation(
            package_path=package,
            package_sha256=hashlib.sha256(b"package").hexdigest(),
            writable_tmp_path=workdir,
            read_only_inputs=(),
        )
    )

    assert result == SkillResult(exit_code=0, stdout='{"ok":true}', stderr="", timed_out=False)
    assert (workdir / "result.txt").read_text(encoding="utf-8") == "restored"
    assert seen["authorization"] == "Bearer secret-token"
    payload = seen["payload"]
    assert isinstance(payload, dict)
    assert payload["protocol"] == "agent-hub.skill-sandbox"
    assert payload["version"] == 1
    assert payload["action"] == "run"
    assert payload["invocation"]["package_sha256"] == hashlib.sha256(b"package").hexdigest()


def test_remote_http_settings_require_https_and_no_embedded_credentials() -> None:
    with pytest.raises(ValueError, match="HTTPS"):
        HttpRemoteSandboxSettings(provider="modal", endpoint="http://sandbox.example")
    with pytest.raises(ValueError, match="credentials"):
        HttpRemoteSandboxSettings(
            provider="modal",
            endpoint="https://user:pass@sandbox.example",
        )


def test_ssh_command_requires_host_key_verification_and_uses_fixed_runner(tmp_path: Path) -> None:
    known_hosts = tmp_path / "known_hosts"
    known_hosts.write_text("sandbox.example ssh-ed25519 AAAA", encoding="utf-8")
    settings = SshSandboxSettings(
        host="sandbox.example",
        user="runner",
        known_hosts_file=known_hosts,
        runner="agent-hub-skill-remote-runner",
    )

    command = build_ssh_command(settings, action="run")

    assert "StrictHostKeyChecking=yes" in command
    assert f"UserKnownHostsFile={known_hosts}" in command
    assert command[-2:] == ("agent-hub-skill-remote-runner", "run")
    assert "runner@sandbox.example" in command


async def test_ssh_sandbox_parses_strict_remote_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    package = tmp_path / "skill.zip"
    package.write_bytes(b"package")
    known_hosts = tmp_path / "known_hosts"
    known_hosts.write_text("host key", encoding="utf-8")
    workdir = tmp_path / "work"
    workdir.mkdir()
    captured: dict[str, object] = {}

    async def fake_run(argv: tuple[str, ...], current: object, **kwargs: object) -> SkillResult:
        captured["argv"] = argv
        captured["stdin_payload"] = kwargs["stdin_payload"]
        return SkillResult(
            exit_code=0,
            stdout=json.dumps(
                {
                    "protocol": "agent-hub.skill-sandbox",
                    "version": 1,
                    "result": {
                        "exit_code": 0,
                        "stdout": "ssh-ok",
                        "stderr": "",
                        "timed_out": False,
                    },
                }
            ),
            stderr="",
            timed_out=False,
        )

    monkeypatch.setattr("agent_hub.skills.sandbox.remote.run_subprocess_with_limits", fake_run)
    sandbox = SshSkillSandbox(
        SshSandboxSettings(host="sandbox.example", user="runner", known_hosts_file=known_hosts)
    )

    result = await sandbox.run(
        invocation(
            package_path=package,
            package_sha256=hashlib.sha256(b"package").hexdigest(),
            writable_tmp_path=workdir,
            read_only_inputs=(),
        )
    )

    assert result.stdout == "ssh-ok"
    stdin_payload = captured["stdin_payload"]
    assert isinstance(stdin_payload, bytes)
    assert json.loads(stdin_payload)["action"] == "run"
