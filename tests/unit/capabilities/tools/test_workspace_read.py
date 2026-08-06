from __future__ import annotations

import os
from pathlib import Path

import pytest

from agent_hub.capabilities.tools.workspace_read import UnsafePath, WorkspaceReader


def test_workspace_reader_reads_file_beneath_workspace_root(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "docs").mkdir()
    (root / "docs" / "note.txt").write_text("hello", encoding="utf-8")
    reader = WorkspaceReader(root, max_bytes=20)

    result = reader.read("docs/note.txt")

    assert result.relative_path == "docs/note.txt"
    assert result.text == "hello"
    assert result.truncated is False


@pytest.mark.parametrize(
    "path",
    [
        "../../etc/passwd",
        "../workspace/../secret.txt",
        "/absolute/path.txt",
        "C:/absolute/path.txt",
        r"C:\absolute\path.txt",
    ],
)
def test_workspace_reader_rejects_parent_escape_and_absolute_paths(
    tmp_path: Path,
    path: str,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    reader = WorkspaceReader(root)

    with pytest.raises(UnsafePath):
        reader.read(path)


def test_workspace_reader_rejects_symlink_escape(tmp_path: Path) -> None:
    if not hasattr(os, "symlink"):
        pytest.skip("symlinks are not supported on this platform")
    root = tmp_path / "workspace"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (outside / "secret.txt").write_text("secret", encoding="utf-8")
    try:
        (root / "link.txt").symlink_to(outside / "secret.txt")
    except OSError:
        pytest.skip("symlink creation is not permitted on this platform")
    reader = WorkspaceReader(root)

    with pytest.raises(UnsafePath):
        reader.read("link.txt")


def test_workspace_reader_bounds_output_bytes(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "large.txt").write_text("abcdef", encoding="utf-8")
    reader = WorkspaceReader(root, max_bytes=3)

    result = reader.read("large.txt")

    assert result.text == "abc"
    assert result.truncated is True
