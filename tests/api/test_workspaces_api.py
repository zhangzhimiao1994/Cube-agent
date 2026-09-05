from __future__ import annotations

import zipfile
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient

from agent_hub.app import create_app
from agent_hub.auth.models import AuthenticatedPrincipal, InvalidCredentials, Role
from agent_hub.capabilities.runtime import RuntimeCapabilityGateway
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


def client_with_workspace(tmp_path: Path) -> tuple[TestClient, ProjectWorkspaceStore, AuthenticatedPrincipal]:
    principal = AuthenticatedPrincipal(uuid4(), uuid4(), Role.OPERATOR)
    app = create_app(
        auth_service=StubAuthService(principal),
        rate_limiter=object(),
        config_service=object(),
        admin_resource_service=object(),
        run_service=object(),
    )
    store = ProjectWorkspaceStore(tmp_path)
    app.state.project_workspace_store = store
    return TestClient(app), store, principal


def test_lists_project_session_workspace_files(tmp_path: Path) -> None:
    client, store, principal = client_with_workspace(tmp_path)
    store.write_bytes(principal.tenant_id, "project", "session", "src/app.py", b"print('ok')\n", "text/x-python")

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


def test_downloads_project_session_workspace_file(tmp_path: Path) -> None:
    client, store, principal = client_with_workspace(tmp_path)
    store.write_bytes(principal.tenant_id, "project", "session", "docs/plan.md", b"# Plan\n", "text/markdown")

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
    store.write_bytes(principal.tenant_id, "project", "session", "src/app.py", b"print('ok')\n", "text/x-python")
    store.write_bytes(principal.tenant_id, "project", "session", "README.md", b"# Demo\n", "text/markdown")

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
