from __future__ import annotations

import base64
import hashlib
from pathlib import Path

import pytest

from agent_hub.skills.sandbox.remote import REMOTE_SANDBOX_PROTOCOL
from agent_hub.skills.sandbox.remote_runner import (
    RemoteRunnerError,
    _materialize_invocation,
    health_response,
)


def test_remote_runner_health_declares_protocol_and_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AGENT_HUB_REMOTE_RUNNER_BACKEND", "docker")
    monkeypatch.setattr(
        "agent_hub.skills.sandbox.remote_runner.probe_skill_broker",
        lambda backend: None,
    )

    assert health_response() == {
        "protocol": REMOTE_SANDBOX_PROTOCOL,
        "version": 1,
        "ready": True,
        "backend": "docker",
        "reason": None,
    }


def test_remote_runner_materializes_verified_package_and_limits(tmp_path: Path) -> None:
    package = b"package"
    invocation = _materialize_invocation(
        {
            "execution_id": "remote_1",
            "package_sha256": hashlib.sha256(package).hexdigest(),
            "package_base64": base64.b64encode(package).decode(),
            "input": {"prompt": "safe"},
            "timeout_seconds": 30,
            "output_limit_bytes": 1024,
            "memory_limit_bytes": 128 * 1024 * 1024,
            "cpu_quota_percent": 50,
            "sandbox_profile": "restricted",
            "network_allowlist": [],
            "selected_secret_refs": [],
            "read_only_inputs": [],
        },
        tmp_path,
    )

    assert invocation.package_path.read_bytes() == package
    assert invocation.writable_tmp_path.is_dir()


def test_remote_runner_rejects_package_checksum_mismatch(tmp_path: Path) -> None:
    with pytest.raises(RemoteRunnerError, match="checksum"):
        _materialize_invocation(
            {
                "package_sha256": "a" * 64,
                "package_base64": base64.b64encode(b"package").decode(),
            },
            tmp_path,
        )
