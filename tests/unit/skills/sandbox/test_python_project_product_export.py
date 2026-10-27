"""Product byte/FD contracts; native Linux cases never run project hooks."""

from __future__ import annotations

import hashlib
import importlib
import importlib.util
import json
import os
import sys
import tempfile
import time
from contextlib import ExitStack
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from agent_hub.skills.sandbox.base import SkillResult
from agent_hub.skills.sandbox.python_project_protocol import PythonProjectBrokerResponse
from tests.unit.skills.sandbox import test_python_project_runner as runner_helpers

MODULE = "agent_hub.skills.sandbox.python_project_product_export"
ROOT = pytest.mark.skipif(
    sys.platform != "linux" or not hasattr(os, "getuid") or os.getuid() != 0,
    reason="native Linux Root UID/dir-FD proof required",
)
NAME = "billing-1.0.0-py3-none-any.whl"
RAW = b"PK\x03\x04opaque wheel bytes" * 10000


@pytest.fixture
def api() -> ModuleType:
    assert importlib.util.find_spec(MODULE) is not None, "wheel product export missing"
    return importlib.import_module(MODULE)


def native_report() -> tuple[Any, dict[str, Any]]:
    runner = importlib.import_module("agent_hub.skills.sandbox.python_project_runner")
    plan = runner.PythonProjectRunnerPlan.model_validate(runner_helpers.plan_payload(), strict=True)
    return plan.invocation, {
        "schema_version": 2,
        "kind": "python_project",
        "evidence": "native_process_only",
        "status": "passed",
        "execution_id": plan.invocation.execution_id,
        "source_sha256": plan.source_sha256,
        "recipe_sha256": plan.invocation.recipe_sha256,
        "checks": [
            {"phase": phase, "status": "passed", "exit_code": 0, "timed_out": False}
            for phase in ("install", "build", "test")
        ],
        "wheel": {"filename": NAME, "sha256": hashlib.sha256(RAW).hexdigest(), "size": len(RAW)},
    }


def response(invocation: Any, report: dict[str, Any]) -> PythonProjectBrokerResponse:
    return PythonProjectBrokerResponse(
        action="run_python_project",
        execution_id=invocation.execution_id,
        ok=True,
        result=SkillResult(exit_code=0, timed_out=False, stdout=json.dumps(report), stderr=""),
    )


def test_exports_reject_non_linux_without_opening_or_creating_files(
    api: Any,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(api, "sys", type("Platform", (), {"platform": "win32"}))
    invocation, report = native_report()
    with pytest.raises(ValueError, match="python_project_product_rejected"):
        api.export_python_product(
            tmp_path / "output",
            tmp_path / "export",
            expected=report["wheel"],
            maximum=invocation.workspace_limit_bytes,
            deadline=time.monotonic() + 10,
        )
    with pytest.raises(ValueError, match="python_project_product_rejected"):
        api.retain_python_product(
            tmp_path, invocation, report, service_uid=10001, deadline=time.monotonic() + 10
        )
    with (
        pytest.raises(ValueError, match="python_project_product_rejected"),
        api.open_python_product(tmp_path, response(invocation, report), invocation),
    ):
        pytest.fail("off-Linux output became available")
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("change", ["failed", "execution", "recipe", "source", "count", "bool"])
def test_descriptor_requires_current_passed_native_execution(api: Any, change: str) -> None:
    invocation, report = native_report()
    if change == "failed":
        report["status"] = "failed"
    elif change in {"execution", "recipe", "source"}:
        report[
            {"execution": "execution_id", "recipe": "recipe_sha256", "source": "source_sha256"}[
                change
            ]
        ] = "a" * 64
    elif change == "count":
        report["checks"].pop()
    else:
        report["wheel"]["size"] = True
    with pytest.raises(ValueError):
        api.product_descriptor(invocation, report)


def test_descriptor_is_small_pathless_and_bound_to_workspace_limit(api: Any) -> None:
    invocation, report = native_report()
    descriptor = api.product_descriptor(invocation, report)
    assert descriptor.model_dump() == {
        "schema_version": 1,
        "execution_id": invocation.execution_id,
        "tenant_id": str(invocation.tenant_id),
        "run_id": str(invocation.run_id),
        "project_id": invocation.project_id,
        "session_id": invocation.session_id,
        "source_sha256": report["source_sha256"],
        "recipe_sha256": invocation.recipe_sha256,
        "filename": NAME,
        "sha256": hashlib.sha256(RAW).hexdigest(),
        "size": len(RAW),
    }
    with pytest.raises(ValueError):
        api.product_descriptor(
            invocation.model_copy(update={"workspace_limit_bytes": len(RAW) - 1}), report
        )


@pytest.mark.parametrize("reuse", [False, True])
@pytest.mark.parametrize("failed", [None, "build", "test"])
def test_runner_exports_only_after_all_checks_and_final_source_environment_validation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reuse: bool,
    failed: str | None,
) -> None:
    runner = importlib.import_module("agent_hub.skills.sandbox.python_project_runner")
    case = runner_helpers.orchestrator(
        runner, tmp_path, monkeypatch, reuse=reuse, failed_phase=failed
    )
    exports: list[str] = []

    def wheel(output: Path, expected: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        if kwargs.get("export_root") is not None:
            assert case.events[-1] in {"observe", "export"}
            assert "closed:test" in case.events and case.events.count("protect") >= 1
            assert kwargs["export_root"] == case.paths["EXPORT"]
            assert kwargs["maximum"] == case.plan.invocation.workspace_limit_bytes
            exports.append("product")
        return dict(expected)

    monkeypatch.setattr(runner, "_wheel_actual", wheel)
    result = runner.run_python_project(case.plan)
    assert result["status"] == ("failed" if failed else "passed")
    assert exports == ([] if failed else ["product"])


@ROOT
@pytest.mark.parametrize("reuse", [False, True])
def test_root_retention_and_open_fd_preserve_exact_bytes_and_close_on_exit(
    api: Any,
    tmp_path: Path,
    reuse: bool,
) -> None:
    if sys.platform != "linux":
        pytest.skip("native Linux UID/dir-FD required")
    invocation, report = native_report()
    stage = tmp_path / "stage"
    stage.mkdir(mode=0o700)
    export = stage / "export"
    export.mkdir(mode=0o1777)
    export.chmod(0o1777)
    product = export / "product"
    product.mkdir(mode=0o700)
    wheel = product / NAME
    wheel.write_bytes(RAW)
    os.chown(wheel, 61111, 61111)
    wheel.chmod(0o444)
    os.chown(product, 61111, 61111)
    product.chmod(0o555)
    if not reuse:
        (export / "ready").mkdir()
    api.retain_python_product(
        stage, invocation, report, service_uid=10001, deadline=time.monotonic() + 30
    )
    assert not product.exists()
    assert (stage / "product" / NAME).stat().st_uid == 0
    assert (stage / "product.json").stat().st_uid == 0
    assert (export / "ready").exists() is (not reuse)
    with api.open_python_product(stage, response(invocation, report), invocation) as (
        fd,
        descriptor,
    ):
        assert descriptor.filename == NAME
        assert os.fstat(fd).st_nlink == 1
        with os.fdopen(os.dup(fd), "rb") as stream:
            assert stream.read() == RAW
    with pytest.raises(OSError):
        os.fstat(fd)


@ROOT
@pytest.mark.parametrize("bad", ["hash", "size", "extra", "hardlink", "symlink", "owner", "fifo"])
def test_root_rejects_unsafe_product_before_creating_metadata(
    api: Any, tmp_path: Path, bad: str
) -> None:
    if sys.platform != "linux":
        pytest.skip("native Linux UID/dir-FD required")
    invocation, report = native_report()
    tmp_path.chmod(0o700)
    export = tmp_path / "export"
    export.mkdir(mode=0o1777)
    export.chmod(0o1777)
    product = export / "product"
    product.mkdir(mode=0o700)
    wheel = product / NAME
    wheel.write_bytes(RAW if bad != "hash" else b"wrong")
    os.chown(wheel, 61111, 61111)
    wheel.chmod(0o444)
    if bad == "size":
        report["wheel"]["size"] += 1
    elif bad == "extra":
        (product / "extra").write_bytes(b"extra")
    elif bad == "hardlink":
        os.link(wheel, tmp_path / "alias")
    elif bad in {"symlink", "fifo"}:
        wheel.unlink()
        if bad == "symlink":
            wheel.symlink_to(tmp_path / "outside")
        else:
            os.mkfifo(wheel)
    elif bad == "owner":
        os.chown(wheel, 10001, 10001)
    os.chown(product, 61111, 61111)
    product.chmod(0o555)
    with pytest.raises(ValueError):
        api.retain_python_product(
            tmp_path, invocation, report, service_uid=10001, deadline=time.monotonic() + 30
        )
    assert not (tmp_path / "product.json").exists()


@pytest.mark.parametrize("change", [None, "hash", "append", "size", "owner", "nlink", "mode"])
def test_chunked_measurement_checks_real_bytes_and_file_shape(
    api: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    change: str | None,
) -> None:
    # Real Windows-compatible FD bytes plus a Linux stat policy model: not native UID proof.
    path, target = tmp_path / "input", tmp_path / "copy"
    path.write_bytes(RAW)
    source = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0))
    copied = os.open(
        target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600
    )
    calls: list[int] = []

    def read(fd: int, amount: int) -> bytes:
        calls.append(amount)
        return os.read(fd, amount)

    shape = SimpleNamespace(st_uid=61111, st_nlink=1, st_mode=0o100444, st_size=len(RAW))
    expected = {"filename": NAME, "sha256": hashlib.sha256(RAW).hexdigest(), "size": len(RAW)}
    if change == "hash":
        expected["sha256"] = "0" * 64
    elif change == "append":
        shape.st_size -= 1
        expected["size"] = len(RAW) - 1
    elif change == "size":
        expected["size"] = len(RAW) + 1
    elif change == "owner":
        shape.st_uid = 10001
    elif change == "nlink":
        shape.st_nlink = 2
    elif change == "mode":
        shape.st_mode = 0o100644
    monkeypatch.setattr(
        api,
        "os",
        SimpleNamespace(
            fstat=lambda fd: shape,
            read=read,
            lseek=os.lseek,
            SEEK_SET=os.SEEK_SET,
            write=lambda fd, raw: os.write(fd, raw[:317]),
        ),
    )
    try:
        if change:
            with pytest.raises(ValueError):
                api._measure(
                    source,
                    SimpleNamespace(check=lambda: None),
                    expected=expected,
                    owner=61111,
                    maximum=len(RAW) + 1,
                    deadline=time.monotonic() + 10,
                    target=copied,
                )
        else:
            api._measure(
                source,
                SimpleNamespace(check=lambda: None),
                expected=expected,
                owner=61111,
                maximum=len(RAW),
                deadline=time.monotonic() + 10,
                target=copied,
            )
    finally:
        os.close(source)
        os.close(copied)
    if change is None:
        assert target.read_bytes() == RAW and len(calls) >= 4 and max(calls) <= 65536


def test_broker_execution_marker_need_not_use_product_canonical_json(
    api: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "execution.json"
    path.write_bytes(b'{"state": "RUNNING"}')
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0))
    monkeypatch.setattr(api, "_file", lambda *args: (fd, SimpleNamespace(check=lambda: None)))
    monkeypatch.setattr(
        api,
        "os",
        SimpleNamespace(
            fstat=lambda _: SimpleNamespace(st_uid=0, st_mode=0o100600, st_size=20), read=os.read
        ),
    )
    try:
        with ExitStack() as stack:
            assert api._read_small(0, "execution.json", stack, [], 0o600, canonical=False) == {
                "state": "RUNNING"
            }
    finally:
        os.close(fd)


@ROOT
@pytest.mark.parametrize(
    "change", ["replace", "mutate", "extra", "metadata", "unknown", "header", "missing"]
)
def test_open_refuses_drift_unknown_and_missing_product(
    api: Any, tmp_path: Path, change: str
) -> None:
    if sys.platform != "linux":
        pytest.skip("native Linux UID/dir-FD required")
    invocation, report = native_report()
    tmp_path.chmod(0o700)
    exported = tmp_path / "export"
    exported.mkdir(mode=0o1777)
    exported.chmod(0o1777)
    product = exported / "product"
    product.mkdir()
    wheel = product / NAME
    wheel.write_bytes(RAW)
    os.chown(wheel, 61111, 61111)
    wheel.chmod(0o444)
    os.chown(product, 61111, 61111)
    product.chmod(0o555)
    api.retain_python_product(
        tmp_path, invocation, report, service_uid=10001, deadline=time.monotonic() + 30
    )
    result = response(invocation, report)
    held: list[int] = []
    if change == "unknown":
        (tmp_path / "execution.json").write_text(json.dumps({"state": "UNKNOWN"}))
        (tmp_path / "execution.json").chmod(0o600)
    elif change == "header":
        result = result.model_copy(update={"execution_id": "other"})
    elif change == "missing":
        (tmp_path / "product.json").unlink()
    with pytest.raises(ValueError), api.open_python_product(tmp_path, result, invocation) as opened:
        assert opened is not None
        fd, _ = opened
        held.append(fd)
        wheel = tmp_path / "product" / NAME
        if change == "replace":
            wheel.unlink()
            wheel.write_bytes(RAW)
            wheel.chmod(0o444)
        elif change == "mutate":
            wheel.write_bytes(b"x" * len(RAW))
        elif change == "extra":
            (wheel.parent / "extra").write_bytes(b"unexpected")
        elif change == "metadata":
            (tmp_path / "product.json").write_bytes(b"{}")
    for fd in held:
        with pytest.raises(OSError):
            os.fstat(fd)


@pytest.mark.parametrize("fault", ["source", "environment", "cleanup"])
@pytest.mark.parametrize("reuse", [False, True])
def test_runner_integrity_and_cleanup_failure_cannot_export_product(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fault: str,
    reuse: bool,
) -> None:
    runner = importlib.import_module("agent_hub.skills.sandbox.python_project_runner")
    case = runner_helpers.orchestrator(runner, tmp_path, monkeypatch, reuse=reuse, fault=fault)

    def wheel(output: Path, expected: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        assert kwargs.get("export_root") is None, "failed verification attempted product export"
        return expected

    monkeypatch.setattr(runner, "_wheel_actual", wheel)
    with pytest.raises((ValueError, RuntimeError)):
        runner.run_python_project(case.plan)


@pytest.mark.parametrize("failed_response", [False, True])
def test_failed_response_or_native_checks_yield_no_product_without_stage_access(
    api: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failed_response: bool,
) -> None:
    invocation, report = native_report()
    report["status"] = "failed"
    result = response(invocation, report)
    if failed_response:
        result = PythonProjectBrokerResponse(
            action="run_python_project",
            execution_id=invocation.execution_id,
            ok=False,
            error="python_project_execution_unavailable",
        )
    monkeypatch.setattr(api, "sys", SimpleNamespace(platform="linux"))
    monkeypatch.setattr(api, "_native", lambda **kwargs: 0)
    with api.open_python_product(tmp_path / "not-created", result, invocation) as opened:
        assert opened is None
    assert list(tmp_path.iterdir()) == []


@ROOT
@pytest.mark.parametrize("bad", [None, "extra", "symlink"])
def test_native_unprivileged_export_copies_only_the_checked_wheel(
    api: Any, bad: str | None
) -> None:
    if sys.platform != "linux":
        pytest.skip("native Linux fork/UID/dir-FD required")
    # A separate unprivileged process exercises only file copying, never wheel code.
    with tempfile.TemporaryDirectory(prefix="wheel-product-copy-") as folder:
        root = Path(folder)
        root.chmod(0o755)
        output = root / "output"
        output.mkdir(mode=0o700)
        wheels = output / "wheels"
        wheels.mkdir(mode=0o700)
        wheel = wheels / NAME
        wheel.write_bytes(RAW)
        wheel.chmod(0o644)
        for path in (output, wheels, wheel):
            os.chown(path, 61111, 61111)
        exported = root / "export"
        exported.mkdir()
        exported.chmod(0o1777)
        if bad == "extra":
            (wheels / "extra.whl").write_bytes(b"extra")
        elif bad == "symlink":
            wheel.unlink()
            wheel.symlink_to(root / "outside")
        pid = os.fork()
        if pid == 0:
            try:
                os.setgroups([])
                os.setgid(61111)
                os.setuid(61111)
                os.environ["AGENT_HUB_GENERATED_PROJECT_VALIDATION_SANDBOX"] = "systemd"
                api.export_python_product(
                    output,
                    exported,
                    expected={
                        "filename": NAME,
                        "sha256": hashlib.sha256(RAW).hexdigest(),
                        "size": len(RAW),
                    },
                    maximum=len(RAW),
                    deadline=time.monotonic() + 20,
                )
            except BaseException:  # noqa: BLE001 - forked test child must exit, never resume pytest.
                os._exit(1)
            os._exit(0)
        _, status = os.waitpid(pid, 0)
        assert os.WIFEXITED(status) and os.WEXITSTATUS(status) == (1 if bad else 0)
        if bad is None:
            retained = exported / "product" / NAME
            assert retained.read_bytes() == RAW and retained.stat().st_uid == 61111
            assert retained.stat().st_nlink == 1
            assert (exported / "product").stat().st_mode & 0o777 == 0o555
        else:
            assert not (exported / "product").exists()
