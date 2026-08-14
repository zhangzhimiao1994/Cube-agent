import sys
from pathlib import Path

from fastapi.testclient import TestClient

from agent_hub.openclaw.local_adapter import OpenClawLocalAdapterConfig, create_local_adapter_app


def _client() -> TestClient:
    app = create_local_adapter_app(
        OpenClawLocalAdapterConfig(
            token="adapter-token",
            platform="windows",
            allowed_commands=[[sys.executable, "-c", "print('local-adapter-ok')"]],
        )
    )
    return TestClient(app)


def _headers() -> dict[str, str]:
    return {"Authorization": "Bearer adapter-token"}


def test_local_adapter_requires_bearer_token() -> None:
    response = _client().post(
        "/v1/openclaw/execute",
        json={
            "operation_id": "openclaw_probe",
            "platform": "windows",
            "kind": "server_command",
            "target": "desktop",
            "argv": [sys.executable, "-c", "print('local-adapter-ok')"],
            "risk_level": "low",
            "reason": "probe",
            "session_id": "openclaw_session_probe",
        },
    )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "openclaw_adapter_unauthorized"


def test_local_adapter_rejects_platform_mismatch() -> None:
    response = _client().post(
        "/v1/openclaw/execute",
        headers=_headers(),
        json={
            "operation_id": "openclaw_probe",
            "platform": "linux",
            "kind": "server_command",
            "target": "desktop",
            "argv": [sys.executable, "-c", "print('local-adapter-ok')"],
            "risk_level": "low",
            "reason": "probe",
            "session_id": "openclaw_session_probe",
        },
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "openclaw_adapter_platform_mismatch"


def test_local_adapter_rejects_unlisted_command() -> None:
    response = _client().post(
        "/v1/openclaw/execute",
        headers=_headers(),
        json={
            "operation_id": "openclaw_probe",
            "platform": "windows",
            "kind": "server_command",
            "target": "desktop",
            "argv": [sys.executable, "-c", "print('not-allowed')"],
            "risk_level": "low",
            "reason": "probe",
            "session_id": "openclaw_session_probe",
        },
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "openclaw_adapter_command_denied"


def test_local_adapter_runs_allowlisted_command() -> None:
    response = _client().post(
        "/v1/openclaw/execute",
        headers=_headers(),
        json={
            "operation_id": "openclaw_probe",
            "platform": "windows",
            "kind": "server_command",
            "target": "desktop",
            "argv": [sys.executable, "-c", "print('local-adapter-ok')"],
            "risk_level": "low",
            "reason": "probe",
            "session_id": "openclaw_session_probe",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["exit_code"] == 0
    assert body["stdout"].strip() == "local-adapter-ok"
    assert body["stderr"] == ""
    assert body["truncated"] is False




def test_local_adapter_runs_allowlisted_remote_operation_kinds() -> None:
    client = _client()
    for kind in ("desktop_action", "screen_read", "file_read"):
        response = client.post(
            "/v1/openclaw/execute",
            headers=_headers(),
            json={
                "operation_id": f"openclaw_probe_{kind}",
                "platform": "windows",
                "kind": kind,
                "target": "desktop",
                "argv": [sys.executable, "-c", "print('local-adapter-ok')"],
                "risk_level": "low",
                "reason": "probe",
                "session_id": "openclaw_session_probe",
            },
        )

        assert response.status_code == 200
        assert response.json()["stdout"].strip() == "local-adapter-ok"


def test_local_adapter_rejects_unlisted_remote_operation_kind_command() -> None:
    response = _client().post(
        "/v1/openclaw/execute",
        headers=_headers(),
        json={
            "operation_id": "openclaw_probe_desktop",
            "platform": "windows",
            "kind": "desktop_action",
            "target": "desktop",
            "argv": [sys.executable, "-c", "print('not-allowed')"],
            "risk_level": "low",
            "reason": "probe",
            "session_id": "openclaw_session_probe",
        },
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "openclaw_adapter_command_denied"

def _file_read_client(root: Path, *, limit: int = 64_000) -> TestClient:
    app = create_local_adapter_app(
        OpenClawLocalAdapterConfig(
            token="adapter-token",
            platform="windows",
            allowed_commands=[],
            allowed_file_roots=[root.resolve()],
            file_read_limit_bytes=limit,
        )
    )
    return TestClient(app)


def test_local_adapter_file_read_reads_file_inside_allowed_root(tmp_path: Path) -> None:
    target = tmp_path / "report.txt"
    target.write_text("openclaw file read ok", encoding="utf-8")

    response = _file_read_client(tmp_path).post(
        "/v1/openclaw/execute",
        headers=_headers(),
        json={
            "operation_id": "openclaw_probe_file_read",
            "platform": "windows",
            "kind": "file_read",
            "target": str(target),
            "risk_level": "low",
            "reason": "read a bounded allowed file",
            "session_id": "openclaw_session_probe",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["exit_code"] == 0
    assert body["stdout"] == "openclaw file read ok"
    assert body["stderr"] == ""
    assert body["truncated"] is False


def test_local_adapter_file_read_truncates_large_allowed_file(tmp_path: Path) -> None:
    target = tmp_path / "large.txt"
    target.write_text("abcdef", encoding="utf-8")

    response = _file_read_client(tmp_path, limit=3).post(
        "/v1/openclaw/execute",
        headers=_headers(),
        json={
            "operation_id": "openclaw_probe_file_read",
            "platform": "windows",
            "kind": "file_read",
            "target": str(target),
            "risk_level": "low",
            "reason": "read a bounded allowed file",
            "session_id": "openclaw_session_probe",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["stdout"] == "abc"
    assert body["truncated"] is True


def test_local_adapter_file_read_rejects_path_outside_allowed_roots(tmp_path: Path) -> None:
    allowed = tmp_path / "allowed"
    outside = tmp_path / "outside"
    allowed.mkdir()
    outside.mkdir()
    target = outside / "secret.txt"
    target.write_text("not allowed", encoding="utf-8")

    response = _file_read_client(allowed).post(
        "/v1/openclaw/execute",
        headers=_headers(),
        json={
            "operation_id": "openclaw_probe_file_read",
            "platform": "windows",
            "kind": "file_read",
            "target": str(target),
            "risk_level": "low",
            "reason": "read a bounded allowed file",
            "session_id": "openclaw_session_probe",
        },
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "openclaw_adapter_file_denied"


def test_local_adapter_file_read_requires_explicit_allowed_roots(tmp_path: Path) -> None:
    target = tmp_path / "report.txt"
    target.write_text("openclaw file read ok", encoding="utf-8")
    client = TestClient(
        create_local_adapter_app(
            OpenClawLocalAdapterConfig(
                token="adapter-token",
                platform="windows",
                allowed_commands=[],
            )
        )
    )

    response = client.post(
        "/v1/openclaw/execute",
        headers=_headers(),
        json={
            "operation_id": "openclaw_probe_file_read",
            "platform": "windows",
            "kind": "file_read",
            "target": str(target),
            "risk_level": "low",
            "reason": "read a bounded allowed file",
            "session_id": "openclaw_session_probe",
        },
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "openclaw_adapter_file_read_unavailable"
