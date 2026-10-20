"""Selected wheel copies only; dummy bytes are never imported or extracted."""

from __future__ import annotations

import hashlib
import importlib
import os
import stat
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from agent_hub.capabilities.python_project_wheels import PythonWheelBinding
from agent_hub.capability_installer.python_attestation import ScanLimits


def api() -> Any:
    return importlib.import_module("agent_hub.skills.sandbox.python_project_wheel_stage")


def wheel() -> PythonWheelBinding:
    return PythonWheelBinding(
        "selected-1.0-py3-none-any.whl",
        "selected",
        "1.0",
        hashlib.sha256(b"dummy wheel").hexdigest(),
    )


@pytest.mark.parametrize(
    "defect", [None, "list", "duplicate", "bytes", "entries", "path", "deadline"]
)
def test_selected_copy_validates_bounded_trusted_recipe_before_native_io(
    defect: str | None,
) -> None:
    selected: Any = (wheel(),)
    limits = ScanLimits(1024)
    deadline = time.monotonic() + 2
    if defect == "list":
        selected = list(selected)
    elif defect == "duplicate":
        selected = (wheel(), wheel())
    elif defect == "bytes":
        limits = ScanLimits(0)
    elif defect == "entries":
        limits = ScanLimits(1024, max_entries=0)
    elif defect == "path":
        limits = ScanLimits(1024, max_path_bytes=1)
    elif defect == "deadline":
        deadline = float("nan")
    if defect is None:
        api()._validate_selection(selected, limits, deadline)
    else:
        with pytest.raises(api().PythonWheelStageError):
            api()._validate_selection(selected, limits, deadline)


def test_unsupported_host_has_no_copy_fallback(tmp_path: Path) -> None:
    if sys.platform == "linux":
        pytest.skip("unsupported host only")
    with pytest.raises(api().PythonWheelStageError, match="wheel_stage_unsupported"):
        api().stage_python_project_wheels(
            tmp_path / "catalog",
            tmp_path / "stage",
            (wheel(),),
            limits=ScanLimits(1024),
            deadline=time.monotonic() + 2,
        )
    assert not (tmp_path / "stage").exists()


native = pytest.mark.skipif(
    sys.platform != "linux", reason="requires actual Linux root FD ownership/no-follow checks"
)


@native
@pytest.mark.parametrize("defect", [None, "hash", "symlink", "hardlink", "mode", "quota", "owner"])
def test_actual_copy_exposes_only_selected_verified_regular_payload(
    tmp_path: Path,
    defect: str | None,
) -> None:
    if sys.platform != "linux" or os.geteuid() != 0:
        pytest.skip("requires actual Linux root")
    catalog, stage = tmp_path / "catalog", tmp_path / "stage"
    catalog.mkdir(mode=0o755)
    stage.mkdir(mode=0o700)
    selected = wheel()
    source = catalog / selected.filename
    source.write_bytes(b"dummy wheel" if defect != "hash" else b"bad bytes")
    source.chmod(0o644)
    hidden = catalog / "unselected-9.0-py3-none-any.whl"
    hidden.write_bytes(b"must never be exposed")
    if defect == "symlink":
        source.unlink()
        source.symlink_to(hidden)
    elif defect == "hardlink":
        os.link(source, catalog / "alias")
    elif defect == "mode":
        source.chmod(0o666)
    elif defect == "owner":
        os.chown(source, 10001, 10001)
    limits = ScanLimits(1 if defect == "quota" else 1024)
    if defect:
        with pytest.raises(api().PythonWheelStageError):
            api().stage_python_project_wheels(
                catalog, stage, (selected,), limits=limits, deadline=time.monotonic() + 2
            )
    else:
        copied = api().stage_python_project_wheels(
            catalog, stage, (selected,), limits=limits, deadline=time.monotonic() + 2
        )
        assert copied == stage / "selected-wheels"
        assert {path.name for path in copied.iterdir()} == {selected.filename}
        target = copied / selected.filename
        assert (
            target.read_bytes() == b"dummy wheel" and target.stat().st_ino != source.stat().st_ino
        )
        assert target.stat().st_uid == 0 and target.stat().st_nlink == 1
        assert stat.S_IMODE(target.stat().st_mode) == 0o444
        assert stat.S_IMODE(copied.stat().st_mode) == 0o555
        api().verify_staged_python_wheels(
            copied, (selected,), limits=limits, deadline=time.monotonic() + 2
        )
        target.chmod(0o644)
        target.write_bytes(b"tamper")
        target.chmod(0o444)
        with pytest.raises(api().PythonWheelStageError):
            api().verify_staged_python_wheels(
                copied, (selected,), limits=limits, deadline=time.monotonic() + 2
            )
    assert hidden.read_bytes() == b"must never be exposed"
