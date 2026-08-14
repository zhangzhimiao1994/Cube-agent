import sys

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