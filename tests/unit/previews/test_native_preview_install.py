"""Portable installer contracts; these do not prove Linux sandbox isolation."""

import configparser
import io
import json
import re
import socket
import struct
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parents[3]


def read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def unit(suffix: str) -> configparser.ConfigParser:
    parser = configparser.ConfigParser(interpolation=None)
    parser.read_string(read(f"deploy/native/systemd/agent-hub-preview-broker.{suffix}"))
    return parser


def function(name: str) -> str:
    script = read("scripts/lib/install_native.sh")
    match = re.search(rf"^{name}\(\) \{{\n(.*?)^\}}$", script, re.MULTILINE | re.DOTALL)
    assert match is not None, f"missing installer function: {name}"
    return match[1]


def python_body(name: str) -> str:
    match = re.search(r"<<'PY'\n(.*?)\nPY\n", function(name), re.DOTALL)
    assert match is not None, f"missing Python payload: {name}"
    return match[1]


def test_node_archive_does_not_restore_publisher_ownership() -> None:
    body = function("download_native_node")
    extraction = next(line for line in body.splitlines() if "tar -xJf" in line)
    assert "--no-same-owner" in extraction
    assert 'node_home="$(native_node_home)"' in body
    assert "normalize_native_node_ownership" in body


def test_dedicated_node_is_required_and_repaired_before_execution() -> None:
    body = function("ensure_native_nodejs")
    assert "command -v node" not in body
    assert "bash -lc" not in body
    assert body.index("normalize_native_node_ownership") < body.index("native_node_runtime_ok")
    ownership = function("normalize_native_node_ownership")
    assert "chown -hR -P root:root" in ownership
    assert 'node_home="$(native_node_home)"' in ownership
    assert "command -v node" not in ownership


def test_preview_policy_uses_the_same_resolved_custom_node_home() -> None:
    body = function("write_native_preview_broker_config")
    assert 'node_home="$(native_node_home)"' in body
    assert '"$INSTALL_ROOT/current/src/agent_hub" "$node_home"' in body


def test_preview_socket_is_independent_and_only_service_group_can_connect() -> None:
    config = unit("socket")
    assert dict(config["Socket"]) == {
        "listenstream": "/run/agent-hub/preview-broker.sock",
        "socketuser": "root",
        "socketgroup": "agent-hub",
        "socketmode": "0660",
        "removeonstop": "yes",
    }
    assert "agent-hub-api.service" in config["Unit"]["Before"].split()
    assert config["Install"]["WantedBy"] == "sockets.target"


def test_preview_broker_has_no_production_secret_group_or_workspace_write_access() -> None:
    service = unit("service")["Service"]
    assert service["User"] == service["Group"] == "root"
    assert not service.get("SupplementaryGroups", "")
    assert service["CapabilityBoundingSet"].split() == ["CAP_DAC_OVERRIDE", "CAP_CHOWN"]
    assert not service.get("AmbientCapabilities", "")
    assert "EnvironmentFile" not in service
    assert service["ProtectSystem"] == "strict"
    assert service["ReadWritePaths"].split() == ["/run/agent-hub-preview"]
    assert service["RestrictAddressFamilies"] == "AF_UNIX"
    assert service["NoNewPrivileges"] == "yes"
    assert service["ProtectHome"] == "yes"
    assert service["UMask"] == "0077"
    assert "-/etc/agent-hub/secrets.env" in service["InaccessiblePaths"].split()
    assert service["ExecStart"] == (
        "/opt/agent-hub/current/.venv/bin/python -m agent_hub.previews.dynamic_broker "
        "--config /etc/agent-hub/preview-broker.json"
    )
    assert "PYTHONPATH=/opt/agent-hub/current/src" in service["Environment"]


def test_preview_runtime_directory_is_root_only_and_separate_from_console_runtime() -> None:
    rows = [line.split() for line in read("deploy/native/agent-hub.tmpfiles").splitlines()]
    assert ["d", "/run/agent-hub-preview", "0700", "root", "root", "-"] in rows
    assert ["d", "/run/agent-hub", "0750", "agent-hub", "agent-hub", "-"] in rows


def test_preview_config_serializes_only_policy_with_actual_uid() -> None:
    result = subprocess.run(
        [sys.executable, "-", "23456", "/var/lib/agent-hub/workspaces",
         "/opt/agent-hub/current/src/agent_hub", "/opt/agent-hub/node"],
        input=python_body("write_native_preview_broker_config"),
        text=True, capture_output=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "allowed_uid": 23456,
        "workspace_root": "/var/lib/agent-hub/workspaces",
        "runtime_root": "/run/agent-hub-preview",
        "trusted_source_root": "/opt/agent-hub/current/src/agent_hub",
        "node_root": "/opt/agent-hub/node",
    }
    body = function("write_native_preview_broker_config")
    assert "id -u agent-hub" in body
    assert "chown root:root" in body and "chmod 0600" in body
    assert "mktemp" in body and "mv -fT" in body
    assert "source " not in body
    assert "native_secret_value AGENT_HUB_PROJECT_WORKSPACE_DIR" in body


def test_preview_config_accepts_quoted_selected_workspace_without_shell_evaluation() -> None:
    result = subprocess.run(
        [sys.executable, "-", "23456", '"/srv/preview-projects"',
         "/opt/agent-hub/current/src/agent_hub", "/opt/agent-hub/node"],
        input=python_body("write_native_preview_broker_config"),
        text=True, capture_output=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["workspace_root"] == "/srv/preview-projects"


@pytest.mark.parametrize("workspace", ["", "relative", "/srv/../etc", "/srv/a b"])
def test_preview_config_rejects_unsafe_workspace(workspace: str) -> None:
    result = subprocess.run(
        [sys.executable, "-", "23456", workspace,
         "/opt/agent-hub/current/src/agent_hub", "/opt/agent-hub/node"],
        input=python_body("write_native_preview_broker_config"),
        text=True, capture_output=True, check=False,
    )
    assert result.returncode != 0
    assert not result.stdout


def test_api_wants_preview_socket_without_requiring_preview_availability() -> None:
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    parser.read_string(read("deploy/native/systemd/agent-hub-api.service"))
    api = parser["Unit"]
    assert "agent-hub-preview-broker.socket" in api["Wants"].split()
    assert "agent-hub-preview-broker.socket" in api["After"].split()
    assert "agent-hub-preview-broker.socket" not in api.get("Requires", "").split()


@pytest.mark.parametrize("uid", ["0", "-1", "invalid"])
def test_preview_config_rejects_invalid_console_uid(uid: str) -> None:
    result = subprocess.run(
        [sys.executable, "-", uid, "/var/lib/agent-hub/workspaces",
         "/opt/agent-hub/current/src/agent_hub", "/opt/agent-hub/node"],
        input=python_body("write_native_preview_broker_config"),
        text=True, capture_output=True, check=False,
    )
    assert result.returncode != 0
    assert not result.stdout


def test_native_upgrade_restarts_preview_before_api_and_probes_before_success() -> None:
    body = function("install_native_mode")
    ordered = [
        "deploy_native_release", "write_native_preview_broker_config",
        "install_native_systemd_units", "run_native_migrations", "systemctl daemon-reload",
        "systemctl enable --now agent-hub-preview-broker.socket",
        "systemctl restart agent-hub-preview-broker.service",
        "systemctl restart agent-hub-api.service",
        "require_native_preview_broker", 'mark_stage "native-up"',
    ]
    positions = [body.index(item) for item in ordered]
    assert positions == sorted(positions)
    assert body.count("deploy_native_release") == 1
    assert body.count("systemctl restart agent-hub-preview-broker.service") == 1
    assert "require_native_service_active agent-hub-preview-broker.socket" in body
    assert "require_native_service_active agent-hub-preview-broker.service" in body


def test_preview_probe_uses_console_uid_clean_environment_and_fails_closed() -> None:
    body = function("require_native_preview_broker")
    assert "runuser -u agent-hub -- env -i" in body
    assert "/run/agent-hub/preview-broker.sock" in body
    assert "secrets.env" not in body and "native_secret_value" not in body
    assert 'die "native preview broker isolation probe failed"' in body
    assert "settimeout(" in python_body("require_native_preview_broker")
    compile(python_body("require_native_preview_broker"), "<installer-probe>", "exec")


@pytest.mark.parametrize(
    ("payload", "accepted"),
    [
        (b'{"ok":true,"state":"probe"}', True),
        (b'{"ok":false,"error":"isolation unavailable"}', False),
        (b'{"ok":1,"state":"probe"}', False),
        (b'{"ok":true,"state":"ready"}', False),
        (b'{"ok":true,"state":"probe","extra":true}', False),
        (b'[]', False),
        (b'{', False),
        (b'', False),
        (b' ' * 4097, False),
    ],
    ids=["probe", "failed", "numeric-ok", "wrong-state", "extra-field", "list",
         "malformed", "empty", "oversized"],
)
def test_installer_probe_requires_exact_bounded_success_response(
    monkeypatch: pytest.MonkeyPatch, payload: bytes, accepted: bool,
) -> None:
    incoming = io.BytesIO(struct.pack("!I", len(payload)) + payload)
    connection = MagicMock()
    connection.__enter__.return_value = connection
    connection.recv.side_effect = lambda size: incoming.read(min(size, 3))
    factory = MagicMock(return_value=connection)
    monkeypatch.setattr(socket, "AF_UNIX", 1, raising=False)
    monkeypatch.setattr(socket, "socket", factory)
    monkeypatch.setattr(sys, "argv", ["-", "/run/agent-hub/preview-broker.sock"])
    code = compile(python_body("require_native_preview_broker"), "<installer-probe>", "exec")
    if accepted:
        exec(code, {})  # noqa: S102 - exercise the trusted installer payload with fake transport
    else:
        with pytest.raises((SystemExit, ValueError)):
            exec(code, {})  # noqa: S102
    factory.assert_called_once_with(1, socket.SOCK_STREAM)
    connection.connect.assert_called_once_with("/run/agent-hub/preview-broker.sock")
    request = b'{"version":1,"action":"probe"}'
    connection.sendall.assert_called_once_with(struct.pack("!I", len(request)) + request)


def test_installer_probe_rejects_truncated_frame(monkeypatch: pytest.MonkeyPatch) -> None:
    incoming = io.BytesIO(struct.pack("!I", 20) + b'{"ok":')
    connection = MagicMock()
    connection.__enter__.return_value = connection
    connection.recv.side_effect = incoming.read
    monkeypatch.setattr(socket, "AF_UNIX", 1, raising=False)
    monkeypatch.setattr(socket, "socket", MagicMock(return_value=connection))
    monkeypatch.setattr(sys, "argv", ["-", "/run/agent-hub/preview-broker.sock"])
    with pytest.raises(SystemExit, match="connection closed"):
        exec(python_body("require_native_preview_broker"), {})  # noqa: S102
