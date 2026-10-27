from __future__ import annotations

import os
import zipfile
from pathlib import Path

import pytest

from agent_hub.capabilities.python_product_bundle import append_python_product
from tests.unit.skills.sandbox.test_python_product_transport import descriptor


def test_actual_product_added_without_replacing_source(tmp_path: Path) -> None:
    wheel = tmp_path / "product"
    wheel.write_bytes(b"wheel")
    bundle = tmp_path / "project.zip"
    with zipfile.ZipFile(bundle, "w") as archive:
        archive.writestr("README.md", b"read me")
    with wheel.open("rb") as stream:
        name = append_python_product(bundle, stream.fileno(), descriptor(),
            max_file_bytes=5, max_bundle_bytes=12, max_bundle_files=2)
    with zipfile.ZipFile(bundle) as archive:
        assert archive.namelist() == ["README.md", name]
        assert archive.read(name) == b"wheel"
        assert archive.read("README.md") == b"read me"
        assert not archive.getinfo(name).external_attr >> 16 & 0o111


@pytest.mark.parametrize("limits", [(4, 100, 2), (10, 11, 2), (10, 100, 1)])
def test_product_shares_existing_bundle_limits(tmp_path: Path, limits: tuple[int, int, int]) -> None:
    bundle = tmp_path / "project.zip"
    with zipfile.ZipFile(bundle, "w") as archive:
        archive.writestr("README.md", b"read me")
    wheel = tmp_path / "wheel"
    wheel.write_bytes(b"wheel")
    with wheel.open("rb") as stream, pytest.raises(ValueError):
        append_python_product(bundle, stream.fileno(), descriptor(),
            max_file_bytes=limits[0], max_bundle_bytes=limits[1], max_bundle_files=limits[2])


def test_product_collision_and_changed_bytes_rejected(tmp_path: Path) -> None:
    bundle = tmp_path / "project.zip"
    wheel = tmp_path / "wheel"
    wheel.write_bytes(b"WRONG")
    with zipfile.ZipFile(bundle, "w") as archive:
        archive.writestr("README.md", b"read me")
    fd = os.open(wheel, os.O_RDONLY)
    try:
        with pytest.raises(ValueError):
            append_python_product(bundle, fd, descriptor(), max_file_bytes=10,
                                  max_bundle_bytes=100, max_bundle_files=3)
    finally:
        os.close(fd)


@pytest.mark.parametrize("kind", ["descendant", "case_parent", "case_same", "case_descendant"])
def test_product_file_cannot_replace_source_paths_on_windows(tmp_path: Path, kind: str) -> None:
    bundle = tmp_path / "project.zip"
    nested = {"descendant": "dist/" + descriptor().filename + "/existing.txt",
        "case_parent": "Dist", "case_same": "DIST/" + descriptor().filename.upper(),
        "case_descendant": "DiSt/" + descriptor().filename.upper() + "/existing.txt"}[kind]
    with zipfile.ZipFile(bundle, "w") as archive:
        archive.writestr(nested, b"source file")
    wheel = tmp_path / "wheel"
    wheel.write_bytes(b"wheel")
    with wheel.open("rb") as stream, pytest.raises(ValueError, match="bundle contract"):
        append_python_product(bundle, stream.fileno(), descriptor(), max_file_bytes=100,
                              max_bundle_bytes=1000, max_bundle_files=10)
    with zipfile.ZipFile(bundle) as archive:
        assert archive.namelist() == [nested]
