"""Bounded private wheel export and Root-held product FDs, never delivery authority.

Call retain only after native quiescence, open only after both units stop and
before stage cleanup. The caller owns the stage, original deadline and UNKNOWN
fence. Neither an arbitrary report nor this descriptor authorizes execution.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import stat
import sys
import time
from collections.abc import Iterator, Mapping
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Any

from agent_hub.skills.sandbox.project_contract_broker import (
    _encoded,
    _object,
    _Observed,
    _open_directory,
    _root,
    _stamp,
)
from agent_hub.skills.sandbox.python_product_transport import require_product_report
from agent_hub.skills.sandbox.python_project_protocol import (
    PythonProductDescriptor,
    PythonProjectBrokerResponse,
    PythonProjectInvocation,
)

_ERROR = "python_project_product_rejected"
_CONTROL_MAX = 16 * 1024
_CHUNK = 65536


def _need(value: object) -> None:
    if not value:
        raise ValueError(_ERROR)


@contextmanager
def _guard() -> Iterator[None]:
    try:
        yield
    except Exception:  # noqa: BLE001 - finite private boundary; never leak host paths.
        raise ValueError(_ERROR) from None


def _native(*, root: bool) -> int:
    if sys.platform != "linux":
        raise ValueError(_ERROR)
    _need(
        getattr(os, "O_NOFOLLOW", 0)
        and getattr(os, "O_DIRECTORY", 0)
        and os.open in os.supports_dir_fd
        and os.stat in os.supports_dir_fd
        and os.scandir in os.supports_fd
    )
    uid = os.getuid()
    _need(os.geteuid() == uid)
    if root:
        _need(uid == 0)
    else:
        _need(
            0 < uid < 2**32 - 1
            and os.getegid() == os.getgid()
            and os.environ.get("AGENT_HUB_GENERATED_PROJECT_VALIDATION_SANDBOX") == "systemd"
        )
    return uid


def _remaining(deadline: float) -> None:
    _need(
        type(deadline) in {int, float} and math.isfinite(deadline) and time.monotonic() < deadline
    )


def _budget(maximum: int, deadline: float) -> None:
    _need(type(maximum) is int and 0 < maximum <= 2**63 - 1)
    _remaining(deadline)


def product_descriptor(
    invocation: PythonProjectInvocation, report: Mapping[str, Any]
) -> PythonProductDescriptor:
    """Bind already validated native success; not a substitute for Root measurement."""
    with _guard():
        _need(type(invocation) is PythonProjectInvocation)
        invocation = PythonProjectInvocation.model_validate(invocation.model_dump(), strict=True)
        _need(
            report.get("schema_version") == 2
            and type(report.get("schema_version")) is int
            and report.get("kind") == "python_project"
            and report.get("status") == "passed"
            and report.get("evidence") == "native_process_only"
        )
        checks = report["checks"]
        _need(isinstance(checks, (list, tuple)) and len(checks) == 3)
        for phase, row in zip(("install", "build", "test"), checks, strict=True):
            _need(
                isinstance(row, Mapping)
                and row.get("phase") == phase
                and row.get("status") == "passed"
                and type(row.get("exit_code")) is int
                and row["exit_code"] == 0
                and row.get("timed_out") is False
            )
        wheel = report["wheel"]
        _need(isinstance(wheel, Mapping) and set(wheel) == {"filename", "sha256", "size"})
        descriptor = PythonProductDescriptor.model_validate(
            {
                "schema_version": 1,
                "execution_id": report["execution_id"],
                "tenant_id": str(invocation.tenant_id),
                "run_id": str(invocation.run_id),
                "project_id": invocation.project_id,
                "session_id": invocation.session_id,
                "source_sha256": report["source_sha256"],
                "recipe_sha256": report["recipe_sha256"],
                **wheel,
            },
            strict=True,
        )
        descriptor.require_for(invocation)
        if invocation.source_kind == "original":
            _need(descriptor.source_sha256 == invocation.original_reference.input_source_sha256)
        return descriptor


def _only(parent: int, names: set[str], deadline: float) -> None:
    _remaining(deadline)
    os.lseek(parent, 0, os.SEEK_SET)
    found: set[str] = set()
    with os.scandir(parent) as members:
        for entry in members:
            _remaining(deadline)
            _need(entry.name in names and entry.name not in found)
            found.add(entry.name)
    _need(found == names)


def _file(parent: int, name: str, stack: ExitStack) -> tuple[int, _Observed]:
    if sys.platform != "linux":
        raise ValueError(_ERROR)
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
    descriptor = os.open(name, flags, dir_fd=parent)
    stack.callback(os.close, descriptor)
    info = os.fstat(descriptor)
    _need(stat.S_ISREG(info.st_mode) and info.st_nlink == 1)
    pin = _Observed(parent, name, _stamp(info, full=True), True, descriptor)
    pin.check()
    return descriptor, pin


def _write(descriptor: int, raw: bytes, deadline: float) -> None:
    rest = memoryview(raw)
    while rest:
        _remaining(deadline)
        count = os.write(descriptor, rest)
        _need(0 < count <= len(rest))
        rest = rest[count:]
    _remaining(deadline)


def _measure(
    descriptor: int,
    pin: _Observed,
    *,
    expected: Mapping[str, Any],
    owner: int,
    maximum: int,
    deadline: float,
    target: int | None = None,
    sealed: bool = True,
) -> None:
    _budget(maximum, deadline)
    _need(type(expected["size"]) is int and 0 < expected["size"] <= maximum)
    pin.check()
    before = os.fstat(descriptor)
    _need(
        before.st_uid == owner
        and before.st_nlink == 1
        and stat.S_ISREG(before.st_mode)
        and before.st_size == expected["size"]
        and not before.st_mode & 0o7133
    )
    _need(not sealed or stat.S_IMODE(before.st_mode) == 0o444)
    os.lseek(descriptor, 0, os.SEEK_SET)
    digest, count = hashlib.sha256(), 0
    while True:
        _remaining(deadline)
        chunk = os.read(descriptor, min(_CHUNK, before.st_size - count + 1))
        _remaining(deadline)
        if not chunk:
            break
        count += len(chunk)
        _need(count <= before.st_size)
        digest.update(chunk)
        if target is not None:
            _write(target, chunk, deadline)
    _need(count == expected["size"] and digest.hexdigest() == expected["sha256"])
    pin.check()
    _remaining(deadline)


def export_python_product(
    output_root: Path,
    export_root: Path,
    *,
    expected: Mapping[str, Any],
    maximum: int,
    deadline: float,
) -> None:
    """Copy exactly one wheel into export/product, exclusively after all checks."""
    with _guard(), ExitStack() as stack:
        if sys.platform != "linux":
            raise ValueError(_ERROR)
        uid = _native(root=False)
        _budget(maximum, deadline)
        _need(set(expected) == {"filename", "sha256", "size"})
        # The shared DTO enforces the portable, single-component wheel name.
        _need(
            type(expected["filename"]) is str
            and len(expected["filename"]) <= 255
            and Path(expected["filename"]).name == expected["filename"]
            and "\\" not in expected["filename"]
            and "/" not in expected["filename"]
            and expected["filename"].endswith(".whl")
        )
        name = expected["filename"]
        pins: list[_Observed] = []
        source = _root(output_root, uid, stack, pins)
        wheels = _open_directory(source, "wheels", uid, stack, pins, full=True)
        parent = _root(export_root.parent, 0, stack, pins)
        exported = _open_directory(parent, export_root.name, 0, stack, pins, mode=0o1777)
        _only(wheels, {name}, deadline)
        fd, pin = _file(wheels, name, stack)
        _measure(
            fd, pin, expected=expected, owner=uid, maximum=maximum, deadline=deadline, sealed=False
        )
        os.mkdir("product", 0o700, dir_fd=exported)
        product = _open_directory(exported, "product", uid, stack, [], mode=0o700)
        target = os.open(
            name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
            dir_fd=product,
        )
        stack.callback(os.close, target)
        _need(os.fstat(target).st_uid == uid and os.fstat(target).st_nlink == 1)
        _measure(
            fd,
            pin,
            expected=expected,
            owner=uid,
            maximum=maximum,
            deadline=deadline,
            target=target,
            sealed=False,
        )
        _remaining(deadline)
        os.fchmod(target, 0o444)
        _remaining(deadline)
        os.fsync(target)
        copied, copied_pin = _file(product, name, stack)
        _need(_stamp(os.fstat(copied), full=True) == _stamp(os.fstat(target), full=True))
        _measure(
            copied, copied_pin, expected=expected, owner=uid, maximum=maximum, deadline=deadline
        )
        _only(product, {name}, deadline)
        _only(wheels, {name}, deadline)
        pin.check()
        for item in pins:
            item.check()
        os.fchmod(product, 0o555)
        os.fsync(product)
        os.fsync(exported)
        _Observed(exported, "product", _stamp(os.fstat(product), full=True), True, product).check()
        copied_pin.check()
        _remaining(deadline)


def _invocation_sha(invocation: PythonProjectInvocation) -> str:
    return hashlib.sha256(invocation.model_dump_json().encode("utf-8")).hexdigest()


def retain_python_product(
    stage: Path,
    invocation: PythonProjectInvocation,
    report: Mapping[str, Any],
    *,
    service_uid: int,
    deadline: float,
) -> None:
    """Root adopts one independently measured inode after quiescence; no cache writes."""
    with _guard(), ExitStack() as stack:
        if sys.platform != "linux":
            raise ValueError(_ERROR)
        _native(root=True)
        _budget(invocation.workspace_limit_bytes, deadline)
        _need(type(service_uid) is int and 0 < service_uid < 2**32 - 1)
        descriptor = product_descriptor(invocation, report)
        expected = {
            "filename": descriptor.filename,
            "sha256": descriptor.sha256,
            "size": descriptor.size,
        }
        pins: list[_Observed] = []
        parent = _root(stage, 0, stack, pins, stage=True)
        exported = _open_directory(parent, "export", 0, stack, [], mode=0o1777)
        dynamic_info = os.stat("product", dir_fd=exported, follow_symlinks=False)
        owner = dynamic_info.st_uid
        _need(0 < owner < 2**32 - 1 and owner != service_uid)
        product = _open_directory(exported, "product", owner, stack, [], mode=0o555)
        _only(product, {descriptor.filename}, deadline)
        source, source_pin = _file(product, descriptor.filename, stack)
        _measure(
            source,
            source_pin,
            expected=expected,
            owner=owner,
            maximum=invocation.workspace_limit_bytes,
            deadline=deadline,
        )
        for pin in pins:
            pin.check()
        os.mkdir("product", 0o700, dir_fd=parent)
        retained = _open_directory(parent, "product", 0, stack, [], mode=0o700)
        # Same stage filesystem: adoption avoids a second unbudgeted wheel copy.
        os.rename(descriptor.filename, descriptor.filename, src_dir_fd=product, dst_dir_fd=retained)
        os.fchown(source, 0, 0)
        os.fchmod(source, 0o444)
        os.fsync(source)
        actual, actual_pin = _file(retained, descriptor.filename, stack)
        _need(
            os.fstat(actual).st_ino == os.fstat(source).st_ino
            and os.fstat(actual).st_dev == os.fstat(source).st_dev
        )
        _measure(
            actual,
            actual_pin,
            expected=expected,
            owner=0,
            maximum=invocation.workspace_limit_bytes,
            deadline=deadline,
        )
        _only(retained, {descriptor.filename}, deadline)
        _only(product, set(), deadline)
        os.rmdir("product", dir_fd=exported)
        os.fchmod(retained, 0o555)
        os.fsync(retained)
        os.fsync(exported)
        record = {
            "descriptor": descriptor.model_dump(),
            "deadline": deadline,
            "invocation_sha256": _invocation_sha(invocation),
            "file_identity": list(actual_pin.identity),
        }
        raw = _encoded(record)
        _need(0 < len(raw) <= _CONTROL_MAX)
        control = os.open(
            "product.json",
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
            dir_fd=parent,
        )
        stack.callback(os.close, control)
        _write(control, raw, deadline)
        os.fchmod(control, 0o400)
        os.fsync(control)
        os.fsync(parent)
        actual_pin.check()
        for pin in pins:
            pin.check()
        _remaining(deadline)


def _read_small(
    parent: int,
    name: str,
    stack: ExitStack,
    pins: list[_Observed],
    mode: int,
    *,
    canonical: bool = True,
) -> dict[str, Any]:
    descriptor, pin = _file(parent, name, stack)
    info = os.fstat(descriptor)
    _need(
        info.st_uid == 0 and stat.S_IMODE(info.st_mode) == mode and 0 < info.st_size <= _CONTROL_MAX
    )
    raw = os.read(descriptor, _CONTROL_MAX + 1)
    _need(len(raw) == info.st_size and not os.read(descriptor, 1))
    pin.check()
    value: object = json.loads(raw, object_pairs_hook=_object)
    _need(type(value) is dict and (not canonical or _encoded(value) == raw))
    assert isinstance(value, dict)
    pins.append(pin)
    return value


@contextmanager
def open_python_product(
    stage: Path, response: PythonProjectBrokerResponse, invocation: PythonProjectInvocation
) -> Iterator[tuple[int, PythonProductDescriptor] | None]:
    """After stop_units, before cleanup: hold/recheck inode and return no host path.

    The private record carries the ORIGINAL monotonic deadline; opening a product
    does not replenish execution/transfer budgets. The caller must not close fd.
    """
    with _guard(), ExitStack() as stack:
        if sys.platform != "linux":
            raise ValueError(_ERROR)
        _native(root=True)
        _need(
            type(response) is PythonProjectBrokerResponse
            and response.execution_id == invocation.execution_id
        )
        if not response.ok:
            _need(response.product is None)
            yield None
            return
        _need(response.action == "run_python_project" and response.result is not None)
        assert response.result is not None
        report: object = json.loads(response.result.stdout, object_pairs_hook=_object)
        _need(isinstance(report, dict))
        assert isinstance(report, dict)
        if report.get("status") == "failed":
            _need(response.product is None)
            yield None
            return
        _need(
            type(response.result.exit_code) is int
            and response.result.exit_code == 0
            and response.result.timed_out is False
        )
        pins: list[_Observed] = []
        parent = _root(stage, 0, stack, pins, stage=True)
        record = _read_small(parent, "product.json", stack, pins, 0o400)
        _need(
            set(record) == {"descriptor", "deadline", "invocation_sha256", "file_identity"}
            and record["invocation_sha256"] == _invocation_sha(invocation)
        )
        deadline = record["deadline"]
        _budget(invocation.workspace_limit_bytes, deadline)
        _need(
            type(record["descriptor"]) is dict
            and type(record["descriptor"].get("schema_version")) is int
        )
        descriptor = PythonProductDescriptor.model_validate(record["descriptor"], strict=True)
        descriptor.require_for(invocation)
        _need(
            product_descriptor(invocation, report) == descriptor
            and (response.product is None or response.product == descriptor)
        )
        require_product_report(response.model_copy(update={"product": descriptor}), descriptor)
        try:
            os.stat("execution.json", dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            pass  # Standalone trusted backend callers have no broker marker.
        else:
            marker = _read_small(parent, "execution.json", stack, pins, 0o600, canonical=False)
            _need(
                marker.get("state") == "RUNNING"
                and all(
                    marker.get(key) == value
                    for key, value in {
                        "execution_id": invocation.execution_id,
                        "tenant_id": str(invocation.tenant_id),
                        "run_id": str(invocation.run_id),
                        "project_id": invocation.project_id,
                        "session_id": invocation.session_id,
                    }.items()
                )
            )
        product = _open_directory(parent, "product", 0, stack, pins, mode=0o555, full=True)
        _only(product, {descriptor.filename}, deadline)
        fd, pin = _file(product, descriptor.filename, stack)
        identity = record["file_identity"]
        _need(
            type(identity) is list
            and all(type(item) is int for item in identity)
            and list(pin.identity) == identity
        )
        expected = {
            "filename": descriptor.filename,
            "sha256": descriptor.sha256,
            "size": descriptor.size,
        }
        _measure(
            fd,
            pin,
            expected=expected,
            owner=0,
            maximum=invocation.workspace_limit_bytes,
            deadline=deadline,
        )
        for item in pins:
            item.check()
        os.lseek(fd, 0, os.SEEK_SET)
        try:
            yield fd, descriptor
        finally:
            _measure(
                fd,
                pin,
                expected=expected,
                owner=0,
                maximum=invocation.workspace_limit_bytes,
                deadline=deadline,
            )
            _only(product, {descriptor.filename}, deadline)
            for item in pins:
                item.check()
