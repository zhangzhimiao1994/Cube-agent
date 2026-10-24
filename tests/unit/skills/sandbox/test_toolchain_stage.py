"""Tiny archive fixtures prove staging guards, not a runnable SDK or approval."""

from __future__ import annotations

import hashlib
import importlib
import importlib.util
import io
import os
import sys
import tarfile
import time
from pathlib import Path
from typing import Any

import pytest


def test_native_sdk_stager_exists() -> None:
    assert importlib.util.find_spec("agent_hub.skills.sandbox.toolchain_stage") is not None


@pytest.fixture
def api() -> Any:
    name = "agent_hub.skills.sandbox.toolchain_stage"
    if importlib.util.find_spec(name) is None:
        pytest.skip("native SDK staging module missing")
    return importlib.import_module(name)


def fixture() -> tuple[bytes, Any]:
    from agent_hub.skills.sandbox.toolchain_catalog import ToolchainArtifact

    result = io.BytesIO()
    with tarfile.open(fileobj=result, mode="w:gz", format=tarfile.USTAR_FORMAT) as archive:
        for name, data in (("go/bin/go", b"fixture-not-an-sdk"),
                           ("go/bin/gofmt", b"fixture-not-gofmt"),
                           ("go/VERSION", b"go1.27.3\ntime 2026-10-01T00:00:00Z\n"),
                           ("go/src/fmt/print.go", b"package fmt\n")):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = 0o755 if name.startswith("go/bin/") else 0o644
            archive.addfile(info, io.BytesIO(data))
    raw = result.getvalue()
    artifact = ToolchainArtifact.from_payload({"stack": "go", "version": "1.27.3",
        "platform": "linux_amd64", "url": "https://dl.google.com/go/go1.27.3.linux-amd64.tar.gz",
        "sha256": hashlib.sha256(raw).hexdigest(), "archive_bytes": len(raw),
        "unpacked_bytes_limit": 1024, "tar_bytes_limit": 10240, "archive_entries_limit": 16,
        "directory_entries_limit": 16})
    return raw, artifact


def native() -> None:
    if sys.platform != "linux" or os.geteuid() != 0:
        pytest.skip("requires Linux Root nofollow staging, never mocked as native evidence")


def test_unsupported_host_fails_without_writing(api: Any, tmp_path: Path) -> None:
    if sys.platform == "linux" and os.geteuid() == 0:
        pytest.skip("native path exercised below")
    raw, artifact = fixture()
    with pytest.raises(api.ToolchainStageError, match="toolchain_stage_unsupported"):
        api.stage_go_toolchain(raw, artifact=artifact, stage=tmp_path,
                               deadline=time.monotonic() + 10)
    assert not list(tmp_path.iterdir())


def test_real_nofollow_stage_and_fresh_profile_match_archive(api: Any, tmp_path: Path) -> None:
    native()
    tmp_path.chmod(0o700)
    raw, artifact = fixture()
    measured = api.stage_go_toolchain(raw, artifact=artifact, stage=tmp_path,
                                      deadline=time.monotonic() + 10)
    assert measured.version == "1.27.3"
    assert measured.status == "measured_not_executed"
    assert measured.archive_sha256 == artifact.sha256
    assert measured.file_count == 4
    assert (tmp_path / "go/bin/go").stat().st_mode & 0o777 == 0o555
    assert (tmp_path / "go/VERSION").stat().st_mode & 0o777 == 0o444
    assert (tmp_path / "go/bin").stat().st_mode & 0o777 == 0o555
    fresh = api.measure_staged_go_toolchain(tmp_path, artifact=artifact,
        manifest=measured.manifest, deadline=time.monotonic() + 10)
    assert fresh == measured


@pytest.mark.parametrize("case", ["content", "added", "removed", "symlink", "mode", "hardlink"])
def test_fresh_profile_rejects_changed_or_unsafe_sdk_members(api: Any, tmp_path: Path, case: str) -> None:
    native()
    tmp_path.chmod(0o700)
    raw, artifact = fixture()
    measured = api.stage_go_toolchain(raw, artifact=artifact, stage=tmp_path,
                                      deadline=time.monotonic() + 10)
    p = tmp_path / "go/VERSION"
    (tmp_path / "go").chmod(0o755)
    if case == "content":
        p.chmod(0o644)
        p.write_bytes(b"go1.27.4\n")
        p.chmod(0o444)
    elif case == "added":
        (tmp_path / "go/extra").write_bytes(b"extra")
    elif case == "removed":
        p.unlink()
    elif case == "symlink":
        p.unlink()
        p.symlink_to("/etc/passwd")
    elif case == "mode":
        p.chmod(0o644)
    else:
        os.link(p, tmp_path / "linked-version")
    with pytest.raises(api.ToolchainStageError):
        api.measure_staged_go_toolchain(tmp_path, artifact=artifact, manifest=measured.manifest,
                                         deadline=time.monotonic() + 10)


@pytest.mark.parametrize("case", ["nonempty", "symlink-stage", "wrong-version", "expired", "nan"])
def test_stage_rejects_conflicts_bad_version_and_deadlines(api: Any, tmp_path: Path, case: str) -> None:
    native()
    tmp_path.chmod(0o700)
    stage = tmp_path
    raw, artifact = fixture()
    deadline = time.monotonic() + 10
    if case == "nonempty":
        (stage / "retained").write_bytes(b"do-not-overwrite")
    elif case == "symlink-stage":
        stage = tmp_path / "alias"
        stage.symlink_to(tmp_path, target_is_directory=True)
    elif case == "wrong-version":
        artifact = type(artifact).from_payload(artifact.to_payload() | {
            "version": "1.27.4", "url": "https://dl.google.com/go/go1.27.4.linux-amd64.tar.gz"})
    else:
        deadline = {"expired": time.monotonic() - 1, "nan": float("nan")}[case]
    with pytest.raises(api.ToolchainStageError):
        api.stage_go_toolchain(raw, artifact=artifact, stage=stage, deadline=deadline)
    assert not (tmp_path / "go").exists()


def test_many_directories_need_depth_bounded_not_tree_sized_descriptors(api: Any, tmp_path: Path) -> None:
    if sys.platform == "win32":
        pytest.skip("resource limits require a POSIX runtime")
    native()
    import resource

    from agent_hub.skills.sandbox.toolchain_catalog import ToolchainArtifact

    raw, original = fixture()
    buffer = io.BytesIO()
    with (
        tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as incoming,
        tarfile.open(fileobj=buffer, mode="w:gz", format=tarfile.USTAR_FORMAT) as outgoing,
    ):
        for member in incoming:
            outgoing.addfile(member, incoming.extractfile(member))
        for index in range(150):
            member = tarfile.TarInfo(f"go/src/dir{index}/source.go")
            member.size = 12
            member.mode = 0o644
            outgoing.addfile(member, io.BytesIO(b"package sdk\n"))
    raw = buffer.getvalue()
    artifact = ToolchainArtifact.from_payload(original.to_payload() | {
        "sha256": hashlib.sha256(raw).hexdigest(), "archive_bytes": len(raw),
        "unpacked_bytes_limit": 4096, "tar_bytes_limit": 256 * 1024,
        "archive_entries_limit": 256, "directory_entries_limit": 256})
    tmp_path.chmod(0o700)
    old = resource.getrlimit(resource.RLIMIT_NOFILE)
    try:
        resource.setrlimit(resource.RLIMIT_NOFILE, (min(64, old[0]), old[1]))
        measured = api.stage_go_toolchain(raw, artifact=artifact, stage=tmp_path,
                                          deadline=time.monotonic() + 30)
        assert measured.file_count == 154
        assert api.measure_staged_go_toolchain(tmp_path, artifact=artifact,
            manifest=measured.manifest, deadline=time.monotonic() + 30) == measured
    finally:
        resource.setrlimit(resource.RLIMIT_NOFILE, old)
