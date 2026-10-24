from __future__ import annotations

import importlib.util
import zipfile
from pathlib import Path
from typing import Any

import pytest

from agent_hub.auth.models import AuthenticatedPrincipal, InvalidCredentials


def load_verifier() -> Any:
    module_path = Path("scripts/verify_user_project_zip_download.py")
    spec = importlib.util.spec_from_file_location("verify_user_project_zip_download", module_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_verify_project_zip_download_script_runs_user_flow(tmp_path: Path) -> None:
    module = load_verifier()

    result = module.run_verification(tmp_path)

    assert set(result["archive_members"]) == {"README.md", "main.py"}
    assert result["main_py_output"] == "hello from Hello Python"
    assert Path(result["downloaded_zip"]).is_file()


@pytest.mark.parametrize("defect,expected_status", [
    ("missing", 503), ("unavailable", 503), ("revoked", 401), ("changed", 503),
])
def test_verify_project_zip_download_script_keeps_identity_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, defect: str, expected_status: int,
) -> None:
    module = load_verifier()

    async def validate_principal(
        self: Any, principal: AuthenticatedPrincipal,
    ) -> AuthenticatedPrincipal:
        if defect == "unavailable":
            raise RuntimeError("identity backend unavailable")
        if defect == "revoked":
            raise InvalidCredentials("identity revoked")
        return AuthenticatedPrincipal(principal.user_id, principal.tenant_id, principal.role)

    if defect == "changed":
        from uuid import uuid4

        async def changed_principal(
            self: Any, principal: AuthenticatedPrincipal,
        ) -> AuthenticatedPrincipal:
            return AuthenticatedPrincipal(uuid4(), principal.tenant_id, principal.role)

        monkeypatch.setattr(module._StubAuthService, "validate_principal", changed_principal,
                            raising=False)
    else:
        monkeypatch.setattr(module._StubAuthService, "validate_principal",
                            None if defect == "missing" else validate_principal, raising=False)
    with pytest.raises(RuntimeError, match=f"workspace zip download failed: HTTP {expected_status}"):
        module.run_verification(tmp_path)


def test_verify_project_zip_download_script_rejects_unsafe_zip_members(tmp_path: Path) -> None:
    module = load_verifier()
    archive_path = tmp_path / "unsafe.zip"
    extract_dir = tmp_path / "extracted"
    extract_dir.mkdir()
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("README.md", "# Safe\n")
        archive.writestr("../escape.txt", "unsafe\n")

    with zipfile.ZipFile(archive_path) as archive:
        try:
            module.extract_workspace_zip(archive, extract_dir)
        except RuntimeError as error:
            assert "unsafe zip member" in str(error)
        else:
            raise AssertionError("unsafe zip member was extracted")

    assert not (tmp_path / "escape.txt").exists()


def test_verify_project_zip_download_script_rejects_absolute_zip_members(tmp_path: Path) -> None:
    module = load_verifier()
    archive_path = tmp_path / "absolute.zip"
    extract_dir = tmp_path / "extracted"
    extract_dir.mkdir()
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("/escape.txt", "unsafe\n")

    with zipfile.ZipFile(archive_path) as archive:
        try:
            module.extract_workspace_zip(archive, extract_dir)
        except RuntimeError as error:
            assert "unsafe zip member" in str(error)
        else:
            raise AssertionError("absolute zip member was extracted")
