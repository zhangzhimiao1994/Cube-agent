from __future__ import annotations

import importlib.util
import zipfile
from pathlib import Path
from typing import Any


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
