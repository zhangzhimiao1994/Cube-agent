from __future__ import annotations

import platform
import subprocess
import zipfile
from pathlib import Path, PurePosixPath, PureWindowsPath
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from agent_hub.app import create_app
from agent_hub.auth.models import AuthenticatedPrincipal, InvalidCredentials, Role
from agent_hub.capabilities.runtime import RuntimeCapabilityGateway
from agent_hub.files import workspace as workspace_module
from agent_hub.files.workspace import ProjectWorkspaceStore


class StubAuthService:
    def __init__(self, principal: AuthenticatedPrincipal) -> None:
        self.principal = principal

    def authenticate_token(self, token: str) -> AuthenticatedPrincipal:
        if token != "valid-token":
            raise InvalidCredentials("bad token")
        return self.principal


def bearer() -> dict[str, str]:
    return {"Authorization": "Bearer valid-token"}


def client_with_workspace(
    tmp_path: Path,
    *,
    role: Role = Role.OPERATOR,
    client_host: str = "127.0.0.1",
) -> tuple[TestClient, ProjectWorkspaceStore, AuthenticatedPrincipal]:
    principal = AuthenticatedPrincipal(uuid4(), uuid4(), role)
    app = create_app(
        auth_service=StubAuthService(principal),
        rate_limiter=object(),
        config_service=object(),
        admin_resource_service=object(),
        run_service=object(),
    )
    store = ProjectWorkspaceStore(tmp_path)
    app.state.project_workspace_store = store
    return (
        TestClient(app, base_url="http://127.0.0.1", client=(client_host, 50000)),
        store,
        principal,
    )


def test_lists_project_session_workspace_files(tmp_path: Path) -> None:
    client, store, principal = client_with_workspace(tmp_path)
    store.write_bytes(
        principal.tenant_id, "project", "session", "src/app.py", b"print('ok')\n", "text/x-python"
    )

    response = client.get(
        "/api/v1/workspaces/projects/project/sessions/session/files",
        headers=bearer(),
    )

    assert response.status_code == 200
    assert response.json()["bundle_download_url"] == (
        "/api/v1/workspaces/projects/project/sessions/session/bundle/download"
    )
    assert response.json()["items"] == [
        {
            "path": "src/app.py",
            "filename": "app.py",
            "mime_type": "text/x-python",
            "size_bytes": 12,
            "sha256": "ad64355106bb158b020ecf9702be48f7730fc091dd4bb6a2f092b40393495b3d",
            "download_url": (
                "/api/v1/workspaces/projects/project/sessions/session/files/download"
                "?path=src%2Fapp.py"
            ),
        }
    ]


def test_lists_empty_project_session_directories_without_creating_the_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _, principal = client_with_workspace(tmp_path)
    monkeypatch.setattr(
        workspace_module,
        "native_picker_capability",
        lambda: workspace_module.NativePickerCapability(
            available=False,
            unavailable_reason="interactive desktop unavailable",
            executable=None,
            picker=None,
        ),
    )

    response = client.get(
        "/api/v1/workspaces/projects/project/directories",
        headers=bearer(),
    )

    assert response.status_code == 200
    payload = response.json()
    separator = payload["separator"]
    expected_platform = {"windows": "windows", "linux": "linux"}.get(
        platform.system().casefold(), "other"
    )
    assert payload == {
        "project_id": "project",
        "platform": expected_platform,
        "separator": separator,
        "configured_root": str(tmp_path.resolve()),
        "logical_root": separator.join(
            (str(principal.tenant_id), "projects", "project", "sessions")
        ),
        "directories": [],
        "native_picker_available": False,
        "unavailable_reason": "interactive desktop unavailable",
    }
    assert not (tmp_path / str(principal.tenant_id) / "projects" / "project" / "sessions").exists()


def test_lists_only_safe_session_directories_in_stable_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, store, principal = client_with_workspace(tmp_path)
    sessions_root = tmp_path / str(principal.tenant_id) / "projects" / "project" / "sessions"
    for name in ("session-z", "session-a", ".hidden", "unsafe name", "alias", "junction"):
        (sessions_root / name).mkdir(parents=True, exist_ok=True)
    (sessions_root / "plain-file").write_text("not a directory", encoding="utf-8")
    store.write_bytes(
        uuid4(),
        "project",
        "other-tenant",
        "README.md",
        b"other tenant",
        "text/markdown",
    )
    store.write_bytes(
        principal.tenant_id,
        "other-project",
        "other-project-session",
        "README.md",
        b"other project",
        "text/markdown",
    )
    original_is_symlink = Path.is_symlink
    original_is_junction = Path.is_junction
    monkeypatch.setattr(
        Path,
        "is_symlink",
        lambda path: path.name == "alias" or original_is_symlink(path),
    )
    monkeypatch.setattr(
        Path,
        "is_junction",
        lambda path: path.name == "junction" or original_is_junction(path),
    )

    response = client.get(
        "/api/v1/workspaces/projects/project/directories",
        headers=bearer(),
    )

    assert response.status_code == 200
    assert response.json()["directories"] == ["session-a", "session-z"]


def test_project_session_directories_reject_unsafe_project_segment(tmp_path: Path) -> None:
    client, _, _ = client_with_workspace(tmp_path)

    safe_response = client.get(
        "/api/v1/workspaces/projects/project/directories",
        headers=bearer(),
    )
    response = client.get(
        "/api/v1/workspaces/projects/..%5Csecret/directories",
        headers=bearer(),
    )

    assert safe_response.status_code == 200
    assert response.status_code == 404


def test_project_session_directories_require_authentication(tmp_path: Path) -> None:
    client, _, _ = client_with_workspace(tmp_path)

    response = client.get("/api/v1/workspaces/projects/project/directories")

    assert response.status_code == 401


def test_remote_directory_listing_hides_server_absolute_path(tmp_path: Path) -> None:
    client, _, _ = client_with_workspace(tmp_path, client_host="203.0.113.10")

    response = client.get(
        "/api/v1/workspaces/projects/project/directories",
        headers=bearer(),
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["configured_root"] == payload["logical_root"]
    assert payload["configured_root"] != str(tmp_path.resolve())
    assert payload["native_picker_available"] is False


def test_describes_windows_workspace_root_for_display() -> None:
    description = workspace_module.describe_workspace_root(
        PureWindowsPath("C:/Agent Hub/workspaces"),
        system_name="Windows",
    )

    assert description.platform == "windows"
    assert description.separator == "\\"
    assert description.configured_root == "C:\\Agent Hub\\workspaces"


def test_describes_linux_workspace_root_for_display() -> None:
    description = workspace_module.describe_workspace_root(
        PurePosixPath("/srv/agent-hub/workspaces"),
        system_name="Linux",
    )

    assert description.platform == "linux"
    assert description.separator == "/"
    assert description.configured_root == "/srv/agent-hub/workspaces"


def test_builds_windows_folder_browser_command() -> None:
    capability = workspace_module.native_picker_capability(
        system_name="Windows",
        environ={"SESSIONNAME": "Console"},
        executable_finder=lambda name: (
            r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
            if name == "powershell.exe"
            else None
        ),
    )

    command = workspace_module.build_native_picker_command(
        PureWindowsPath("C:/Agent Hub/workspaces/tenant/projects/demo/sessions"),
        capability,
    )

    assert capability.available is True
    assert command[0].endswith("powershell.exe")
    assert "FolderBrowserDialog" in command[-2]
    assert command[-1] == r"C:\Agent Hub\workspaces\tenant\projects\demo\sessions"


def test_builds_linux_zenity_directory_command() -> None:
    capability = workspace_module.native_picker_capability(
        system_name="Linux",
        environ={"DISPLAY": ":0"},
        executable_finder=lambda name: "/usr/bin/zenity" if name == "zenity" else None,
    )

    command = workspace_module.build_native_picker_command(
        PurePosixPath("/srv/agent-hub/workspaces/tenant/projects/demo/sessions"),
        capability,
    )

    assert capability.available is True
    assert command == (
        "/usr/bin/zenity",
        "--file-selection",
        "--directory",
        "--filename",
        "/srv/agent-hub/workspaces/tenant/projects/demo/sessions/",
    )


def test_native_directory_selection_rejects_remote_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _, _ = client_with_workspace(tmp_path, client_host="203.0.113.10")
    monkeypatch.setattr(
        workspace_module,
        "native_picker_capability",
        lambda: workspace_module.NativePickerCapability(True, None, "picker", "zenity"),
    )

    response = client.post(
        "/api/v1/workspaces/projects/project/directories/select-native",
        headers=bearer(),
    )

    assert response.status_code == 403


def test_native_directory_selection_rejects_loopback_proxy_for_external_host(
    tmp_path: Path,
) -> None:
    client, _, _ = client_with_workspace(tmp_path, client_host="127.0.0.1")
    client.base_url = "https://agent.example"

    response = client.post(
        "/api/v1/workspaces/projects/project/directories/select-native",
        headers=bearer(),
    )

    assert response.status_code == 403


def test_native_directory_selection_requires_run_create_permission(tmp_path: Path) -> None:
    client, _, _ = client_with_workspace(tmp_path, role=Role.VIEWER)

    response = client.post(
        "/api/v1/workspaces/projects/project/directories/select-native",
        headers=bearer(),
    )

    assert response.status_code == 403


def test_native_picker_timeout_is_reported_as_unavailable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    capability = workspace_module.NativePickerCapability(True, None, "picker", "zenity")

    def timeout(*args: object, **kwargs: object) -> None:
        raise subprocess.TimeoutExpired(cmd="picker", timeout=120)

    monkeypatch.setattr(workspace_module.subprocess, "run", timeout)

    with pytest.raises(workspace_module.NativePickerUnavailable, match="timed out"):
        workspace_module.pick_native_directory(tmp_path, capability)


def test_native_directory_selection_returns_safe_direct_child(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _, principal = client_with_workspace(tmp_path, client_host="127.0.0.1")
    sessions_root = tmp_path / str(principal.tenant_id) / "projects" / "project" / "sessions"
    selected = sessions_root / "session-a"
    selected.mkdir(parents=True)
    monkeypatch.setattr(
        workspace_module,
        "native_picker_capability",
        lambda: workspace_module.NativePickerCapability(True, None, "picker", "zenity"),
    )
    monkeypatch.setattr(
        workspace_module, "pick_native_directory", lambda root, capability: selected
    )

    response = client.post(
        "/api/v1/workspaces/projects/project/directories/select-native",
        headers=bearer(),
    )

    assert response.status_code == 200
    assert response.json() == {"session_id": "session-a"}


def test_native_directory_selection_creates_managed_root_for_a_new_project(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _, principal = client_with_workspace(tmp_path, client_host="127.0.0.1")
    sessions_root = tmp_path / str(principal.tenant_id) / "projects" / "new-project" / "sessions"
    monkeypatch.setattr(
        workspace_module,
        "native_picker_capability",
        lambda: workspace_module.NativePickerCapability(True, None, "picker", "zenity"),
    )

    def choose_new_session(root: Path, capability: object) -> Path:
        assert root == sessions_root
        assert root.is_dir()
        selected = root / "main"
        selected.mkdir()
        return selected

    monkeypatch.setattr(workspace_module, "pick_native_directory", choose_new_session)

    response = client.post(
        "/api/v1/workspaces/projects/new-project/directories/select-native",
        headers=bearer(),
    )

    assert response.status_code == 200
    assert response.json() == {"session_id": "main"}


def test_native_directory_selection_rejects_path_outside_sessions_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _, principal = client_with_workspace(tmp_path, client_host="::1")
    sessions_root = tmp_path / str(principal.tenant_id) / "projects" / "project" / "sessions"
    sessions_root.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    monkeypatch.setattr(
        workspace_module,
        "native_picker_capability",
        lambda: workspace_module.NativePickerCapability(True, None, "picker", "zenity"),
    )
    monkeypatch.setattr(workspace_module, "pick_native_directory", lambda root, capability: outside)

    response = client.post(
        "/api/v1/workspaces/projects/project/directories/select-native",
        headers=bearer(),
    )

    assert response.status_code == 422


def test_downloads_project_session_workspace_file(tmp_path: Path) -> None:
    client, store, principal = client_with_workspace(tmp_path)
    store.write_bytes(
        principal.tenant_id, "project", "session", "docs/plan.md", b"# Plan\n", "text/markdown"
    )

    response = client.get(
        "/api/v1/workspaces/projects/project/sessions/session/files/download?path=docs%2Fplan.md",
        headers=bearer(),
    )

    assert response.status_code == 200
    assert response.content == b"# Plan\n"
    assert response.headers["content-type"].startswith("text/markdown")
    assert "plan.md" in response.headers["content-disposition"]


def test_downloads_project_session_workspace_zip(tmp_path: Path) -> None:
    client, store, principal = client_with_workspace(tmp_path)
    store.write_bytes(
        principal.tenant_id, "project", "session", "src/app.py", b"print('ok')\n", "text/x-python"
    )
    store.write_bytes(
        principal.tenant_id, "project", "session", "README.md", b"# Demo\n", "text/markdown"
    )

    response = client.get(
        "/api/v1/workspaces/projects/project/sessions/session/bundle/download",
        headers=bearer(),
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/zip")
    archive_path = tmp_path / "downloaded.zip"
    archive_path.write_bytes(response.content)
    with zipfile.ZipFile(archive_path) as archive:
        assert archive.namelist() == ["README.md", "src/app.py"]


async def test_downloads_runtime_generated_project_as_workspace_zip(tmp_path: Path) -> None:
    client, _, principal = client_with_workspace(tmp_path / "workspaces")
    gateway = RuntimeCapabilityGateway(
        skill_store_dir=tmp_path / "skills",
        generated_artifact_dir=tmp_path / "generated",
        project_workspace_dir=tmp_path / "workspaces",
    )

    await gateway.execute(
        tenant_id=principal.tenant_id,
        run_id=uuid4(),
        actor="engineer",
        name="project.generate_zip",
        arguments={
            "title": "Hello Python",
            "project_id": "Mofang Agent",
            "workspace_session_id": "Conv 01",
            "files": {
                "main.py": "print('hello from mofang')\n",
                "README.md": "# Hello Python\n\nRun `python main.py`.\n",
            },
        },
        idempotency_key="project_zip_user_download",
    )

    response = client.get(
        "/api/v1/workspaces/projects/mofang-agent/sessions/conv-01/bundle/download",
        headers=bearer(),
    )

    assert response.status_code == 200
    archive_path = tmp_path / "downloaded-runtime-project.zip"
    archive_path.write_bytes(response.content)
    with zipfile.ZipFile(archive_path) as archive:
        assert archive.namelist() == ["README.md", "main.py"]
        assert archive.read("main.py") == b"print('hello from mofang')\n"


def test_workspace_download_rejects_path_traversal(tmp_path: Path) -> None:
    client, _, _ = client_with_workspace(tmp_path)

    response = client.get(
        "/api/v1/workspaces/projects/project/sessions/session/files/download?path=..%2Fsecret.txt",
        headers=bearer(),
    )

    assert response.status_code == 404
