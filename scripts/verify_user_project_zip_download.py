from __future__ import annotations

import json
import subprocess
import sys
import zipfile
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

import anyio
from fastapi.testclient import TestClient

from agent_hub.app import create_app
from agent_hub.auth.models import AuthenticatedPrincipal, InvalidCredentials, Role
from agent_hub.capabilities.runtime import RuntimeCapabilityGateway
from agent_hub.files.workspace import ProjectWorkspaceStore


class _StubAuthService:
    def __init__(self, principal: AuthenticatedPrincipal) -> None:
        self.principal = principal

    def authenticate_token(self, token: str) -> AuthenticatedPrincipal:
        if token != "valid-token":
            raise InvalidCredentials("bad token")
        return self.principal


def run_verification(root: Path) -> dict[str, str | list[str]]:
    root.mkdir(parents=True, exist_ok=True)
    workspace_dir = root / "workspaces"
    generated_dir = root / "generated"
    principal = AuthenticatedPrincipal(uuid4(), uuid4(), Role.OPERATOR)
    store = ProjectWorkspaceStore(workspace_dir)
    app = create_app(
        auth_service=_StubAuthService(principal),
        rate_limiter=object(),
        config_service=object(),
        admin_resource_service=object(),
        run_service=object(),
    )
    app.state.project_workspace_store = store
    client = TestClient(app)
    gateway = RuntimeCapabilityGateway(
        skill_store_dir=root / "skills",
        generated_artifact_dir=generated_dir,
        project_workspace_dir=workspace_dir,
    )

    async def execute_project_generation() -> None:
        await gateway.execute(
            tenant_id=principal.tenant_id,
            run_id=uuid4(),
            actor="engineer",
            name="project.generate_zip",
            arguments={
                "title": "Hello Python",
                "project_id": "Mofang Agent",
                "workspace_session_id": "Conv Verify",
                "files": {
                    "main.py": (
                        "from pathlib import Path\n"
                        "title = Path('README.md').read_text(encoding='utf-8').splitlines()[0].removeprefix('# ')\n"
                        "print('hello from ' + title)\n"
                    ),
                    "README.md": "# Hello Python\n\nRun `python main.py`.\n",
                },
            },
            idempotency_key="verify_project_zip_user_flow",
        )

    anyio.run(execute_project_generation)

    response = client.get(
        "/api/v1/workspaces/projects/mofang-agent/sessions/conv-verify/bundle/download",
        headers={"Authorization": "Bearer valid-token"},
    )
    if response.status_code != 200:
        raise RuntimeError(f"workspace zip download failed: HTTP {response.status_code}")

    downloaded_zip = root / "downloaded-workspace.zip"
    downloaded_zip.write_bytes(response.content)
    extract_dir = root / "extracted"
    extract_dir.mkdir(exist_ok=True)
    with zipfile.ZipFile(downloaded_zip) as archive:
        members = extract_workspace_zip(archive, extract_dir)

    completed = subprocess.run(
        [sys.executable, str(extract_dir / "main.py")],
        cwd=extract_dir,
        check=True,
        capture_output=True,
        text=True,
    )
    return {
        "downloaded_zip": str(downloaded_zip),
        "extract_dir": str(extract_dir),
        "archive_members": members,
        "main_py_output": completed.stdout.strip(),
    }


def extract_workspace_zip(archive: zipfile.ZipFile, extract_dir: Path) -> list[str]:
    extract_root = extract_dir.resolve()
    members = archive.namelist()
    for info in archive.infolist():
        normalized = info.filename.replace("\\", "/")
        target = Path(normalized)
        if target.is_absolute() or any(part in {"", ".", ".."} for part in target.parts):
            raise RuntimeError(f"unsafe zip member: {info.filename}")
        destination = (extract_dir / target).resolve()
        if not destination.is_relative_to(extract_root):
            raise RuntimeError(f"unsafe zip member: {info.filename}")
        if info.is_dir():
            destination.mkdir(parents=True, exist_ok=True)
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        with archive.open(info) as source, destination.open("wb") as target_file:
            target_file.write(source.read())
    return members


def main() -> int:
    with TemporaryDirectory(prefix="agent-hub-user-flow-") as temporary_dir:
        result = run_verification(Path(temporary_dir))
        print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
