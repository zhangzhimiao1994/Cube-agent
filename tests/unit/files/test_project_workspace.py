from __future__ import annotations

import zipfile
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

import pytest

from agent_hub.files.workspace import ProjectWorkspaceStore


def test_project_workspace_store_writes_and_lists_session_files(tmp_path: Path) -> None:
    tenant_id = uuid4()
    store = ProjectWorkspaceStore(tmp_path)

    metadata = store.write_bytes(
        tenant_id=tenant_id,
        project_id="Mofang Agent",
        session_id="Conv Main 01",
        relative_path="src/app.py",
        data=b"print('hello')\n",
        mime_type="text/x-python",
    )

    assert metadata.path == "src/app.py"
    assert metadata.size_bytes == 15
    assert metadata.sha256 == sha256(b"print('hello')\n").hexdigest()
    assert metadata.download_url == (
        "/api/v1/workspaces/projects/mofang-agent/sessions/conv-main-01/files/download"
        "?path=src%2Fapp.py"
    )
    assert store.list_files(tenant_id, "Mofang Agent", "Conv Main 01") == (metadata,)


def test_project_workspace_store_uses_run_workspace_segment_rules(tmp_path: Path) -> None:
    tenant_id = uuid4()
    store = ProjectWorkspaceStore(tmp_path)

    metadata = store.write_bytes(
        tenant_id=tenant_id,
        project_id="Foo_Bar",
        session_id="Session_01",
        relative_path="main.py",
        data=b"print('ok')\n",
        mime_type="text/x-python",
    )

    assert metadata.download_url == (
        "/api/v1/workspaces/projects/foo_bar/sessions/session_01/files/download?path=main.py"
    )
    assert store.bundle_download_url("Foo_Bar", "Session_01") == (
        "/api/v1/workspaces/projects/foo_bar/sessions/session_01/bundle/download"
    )


@pytest.mark.parametrize(
    "relative_path",
    [
        "../secret.txt",
        "/absolute.txt",
        "src/../../secret.txt",
        "src/.hidden",
        ".env",
        "bad\x1fname.txt",
    ],
)
def test_project_workspace_store_rejects_unsafe_relative_paths(
    tmp_path: Path, relative_path: str
) -> None:
    store = ProjectWorkspaceStore(tmp_path)

    with pytest.raises(ValueError, match="workspace path"):
        store.write_bytes(
            tenant_id=uuid4(),
            project_id="project",
            session_id="session",
            relative_path=relative_path,
            data=b"unsafe",
            mime_type="text/plain",
        )


def test_project_workspace_store_rejects_symlink_escape_on_download(tmp_path: Path) -> None:
    tenant_id = uuid4()
    store = ProjectWorkspaceStore(tmp_path)
    session_root = store.session_root(tenant_id, "project", "session")
    session_root.mkdir(parents=True)
    secret = tmp_path / "secret.txt"
    secret.write_text("secret", encoding="utf-8")
    link = session_root / "leak.txt"
    try:
        link.symlink_to(secret)
    except OSError:
        pytest.skip("symlink creation is not available on this platform")

    with pytest.raises(ValueError, match="workspace path"):
        store.resolve_file(tenant_id, "project", "session", "leak.txt")


def test_project_workspace_store_creates_bounded_zip_without_hidden_files(
    tmp_path: Path,
) -> None:
    tenant_id = uuid4()
    store = ProjectWorkspaceStore(tmp_path)
    store.write_bytes(tenant_id, "project", "session", "src/app.py", b"print('ok')\n", "text/x-python")
    store.write_bytes(tenant_id, "project", "session", "README.md", b"# Demo\n", "text/markdown")

    bundle = store.create_session_zip(tenant_id, "project", "session")

    assert bundle.filename == "project-session-workspace.zip"
    assert bundle.mime_type == "application/zip"
    with zipfile.ZipFile(bundle.path) as archive:
        assert archive.namelist() == ["README.md", "src/app.py"]
        assert archive.read("src/app.py") == b"print('ok')\n"


def test_project_workspace_store_rejects_empty_session_zip(tmp_path: Path) -> None:
    store = ProjectWorkspaceStore(tmp_path)

    with pytest.raises(FileNotFoundError, match="workspace has no files"):
        store.create_session_zip(uuid4(), "project", "session")


def test_project_workspace_store_zip_rejects_too_many_files(tmp_path: Path) -> None:
    tenant_id = uuid4()
    store = ProjectWorkspaceStore(tmp_path, max_bundle_files=1)
    store.write_bytes(tenant_id, "project", "session", "a.txt", b"a", "text/plain")
    store.write_bytes(tenant_id, "project", "session", "b.txt", b"b", "text/plain")

    with pytest.raises(ValueError, match="too many workspace files"):
        store.create_session_zip(tenant_id, "project", "session")


def test_project_workspace_store_list_rejects_oversized_existing_file(tmp_path: Path) -> None:
    tenant_id = uuid4()
    store = ProjectWorkspaceStore(tmp_path, max_file_bytes=3)
    session_root = store.session_root(tenant_id, "project", "session")
    session_root.mkdir(parents=True)
    (session_root / "large.txt").write_bytes(b"large")

    with pytest.raises(ValueError, match="workspace file is too large"):
        store.list_files(tenant_id, "project", "session")
