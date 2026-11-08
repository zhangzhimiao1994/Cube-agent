"""Streamed Python products: controlled files, never execution or source authority."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import stat
from collections.abc import Iterator, Mapping
from contextlib import ExitStack, contextmanager
from typing import BinaryIO, cast
from uuid import UUID, uuid4

from agent_hub.capabilities.project_verification import ProjectVerificationError
from agent_hub.capability_installer.python_attestation import _directory, _identity
from agent_hub.files.generated import WHEEL_MIME_TYPE, GeneratedFileMetadata, GeneratedFileStore
from agent_hub.runtime.contracts import JsonValue, _freeze_object, _mutable_json
from agent_hub.skills.sandbox.python_product_transport import require_product_report
from agent_hub.skills.sandbox.python_project_protocol import (
    PythonProductDescriptor,
    PythonProjectBrokerRequest,
    PythonProjectBrokerResponse,
    PythonProjectInvocation,
)

_ERROR = "python_project_product_unavailable"


def _need(value: object) -> None:
    if not value:
        raise ProjectVerificationError(_ERROR)


def _digest(value: Mapping[str, JsonValue]) -> str:
    return hashlib.sha256(json.dumps(_mutable_json(value), sort_keys=True,
        separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode("ascii")).hexdigest()


@contextmanager
def consume_response(raw: PythonProjectBrokerResponse, invocation: PythonProjectInvocation,
                     source: str) -> Iterator[tuple[PythonProjectBrokerResponse, BinaryIO | None]]:
    """Take private spool ownership BEFORE public DTO revalidation, always close."""
    stream = raw._product_stream if type(raw) is PythonProjectBrokerResponse else None
    try:
        _need(type(raw) is PythonProjectBrokerResponse)
        raw._product_stream = None
        try:
            response = PythonProjectBrokerResponse.model_validate(raw.model_dump(), strict=True)
            response.require_for(PythonProjectBrokerRequest(
                invocation=invocation, product_transfer_version=1,
                ephemeral_environment_version=1 if invocation.install_allowed else None,
            ))
            if response.ephemeral_environment is not None:
                _need(response.ephemeral_environment.source_sha256 == source)
            if response.product is not None:
                _need(stream is not None and response.product.source_sha256 == source)
                require_product_report(response, response.product)
            else:
                _need(stream is None)
        except ProjectVerificationError:
            raise
        except Exception:  # noqa: BLE001 - native DTO and stream errors stay finite and private.
            raise ProjectVerificationError(_ERROR) from None
        yield response, stream
    finally:
        if stream is not None:
            stream.close()


def _stored(ref: Mapping[str, JsonValue], tenant: UUID, run: UUID,
            source: str) -> tuple[PythonProductDescriptor, UUID, str]:
    try:
        ref = _freeze_object(ref, name="Python product reference")
        _need(set(ref) == {"schema_version", "artifact_id", "descriptor", "file", "report_sha256"}
              and type(ref["schema_version"]) is int and ref["schema_version"] == 1)
        descriptor = PythonProductDescriptor.model_validate_json(
            json.dumps(_mutable_json(ref["descriptor"])), strict=True)
        _need(type(descriptor.schema_version) is int and type(descriptor.size) is int
              and (descriptor.tenant_id, descriptor.run_id, descriptor.source_sha256)
              == (str(tenant), str(run), source))
        artifact = UUID(cast(str, ref["artifact_id"]))
        _need(str(artifact) == ref["artifact_id"])
        key = f"{tenant}/{run}/{artifact}/{descriptor.filename}"
        expected: dict[str, JsonValue] = {
            "filename": descriptor.filename, "mime_type": WHEEL_MIME_TYPE,
            "size_bytes": descriptor.size, "sha256": descriptor.sha256, "storage_key": key,
            "download_url": f"/api/v1/runs/{run}/artifacts/{artifact}/download",
        }
        stored = ref["file"]
        _need(isinstance(stored, Mapping) and set(stored) == set(expected))
        assert isinstance(stored, Mapping)
        _need(all(type(stored[key]) is type(value) and stored[key] == value
                  for key, value in expected.items()))
        digest = ref["report_sha256"]
        _need(type(digest) is str and len(digest) == 64
              and all(c in "0123456789abcdef" for c in digest))
        return descriptor, artifact, key
    except ProjectVerificationError:
        raise
    except Exception:  # noqa: BLE001 - malformed persisted refs expose no private details.
        raise ProjectVerificationError(_ERROR) from None


@contextmanager
def open_product(store: GeneratedFileStore, ref: Mapping[str, JsonValue], tenant_id: UUID,
                 run_id: UUID, source_sha256: str) -> Iterator[tuple[int, PythonProductDescriptor]]:
    """Rehash a no-follow single-link file; the caller retains current receipt checks."""
    try:
        descriptor, artifact, key = _stored(ref, tenant_id, run_id, source_sha256)
        path = store.resolve_for(tenant_id, run_id, artifact, key)
        # resolve_for checks scope, but resolves links. Retain the lexical store
        # namespace as well, and pin every directory while opening the leaf.
        _need(path == store._root.joinpath(*key.split("/")))
        with ExitStack() as stack:
            parent = store._root
            parent_fd = stack.enter_context(_directory(parent))
            for component in key.split("/")[:-1]:
                parent = parent / component
                parent_fd = stack.enter_context(_directory(parent, parent_fd))
            before = path.lstat()
            _need(stat.S_ISREG(before.st_mode) and before.st_nlink == 1
                  and not getattr(before, "st_file_attributes", 0) & 0x400)
            flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
            flags |= getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0)
            fd = os.open(path if parent_fd is None else descriptor.filename, flags, dir_fd=parent_fd)
            stack.callback(os.close, fd)
            if os.name == "nt":
                from agent_hub.plugins.handover_sources import _check_windows_handle
                _check_windows_handle(fd, path)
            opened = os.fstat(fd)
            _need(stat.S_ISREG(opened.st_mode) and opened.st_nlink == 1
                  and (opened.st_dev, opened.st_ino, opened.st_size)
                  == (before.st_dev, before.st_ino, descriptor.size))
            digest = hashlib.sha256()
            remaining = descriptor.size
            while remaining:
                chunk = os.read(fd, min(65536, remaining))
                _need(chunk)
                digest.update(chunk)
                remaining -= len(chunk)
            _need(not os.read(fd, 1) and digest.hexdigest() == descriptor.sha256)
            _need(_identity(os.fstat(fd)) == _identity(opened)
                  and _identity(path.lstat()) == _identity(before))
            os.lseek(fd, 0, os.SEEK_SET)
            yield fd, descriptor
            _need(_identity(os.fstat(fd)) == _identity(opened)
                  and _identity(path.lstat()) == _identity(before))
    except ProjectVerificationError:
        raise
    except Exception:  # noqa: BLE001 - no host paths or filesystem errors enter model evidence.
        raise ProjectVerificationError(_ERROR) from None


def discard_product(store: GeneratedFileStore | None, ref: Mapping[str, JsonValue] | None) -> None:
    """Only for a ref minted by THIS uncommitted verification attempt."""
    if store is None or ref is None:
        return
    descriptor = cast(Mapping[str, JsonValue], ref["descriptor"])
    tenant, run = UUID(cast(str, descriptor["tenant_id"])), UUID(cast(str, descriptor["run_id"]))
    _, artifact, key = _stored(ref, tenant, run, cast(str, descriptor["source_sha256"]))
    try:
        path = store.resolve_for(tenant, run, artifact, key)
        _need(path == store._root.joinpath(*key.split("/")) and path.lstat().st_nlink == 1)
        path.unlink()
    except FileNotFoundError:
        pass
    except ProjectVerificationError:
        raise
    except Exception:  # noqa: BLE001 - cleanup paths and OS error text remain private.
        raise ProjectVerificationError(_ERROR) from None


async def persist_product(store: GeneratedFileStore | None, response: PythonProjectBrokerResponse,
                          stream: BinaryIO | None, invocation: PythonProjectInvocation,
                          source: str, report: Mapping[str, JsonValue]) -> Mapping[str, JsonValue] | None:
    if store is None:
        return None
    descriptor = response.product
    _need(descriptor is not None and stream is not None)
    assert descriptor is not None and stream is not None
    descriptor.require_for(invocation)
    require_product_report(response, descriptor)
    _need(descriptor.source_sha256 == source == report["source_sha256"] and report["status"] == "passed"
          and report["execution_id"] == descriptor.execution_id
          and report["recipe_sha256"] == descriptor.recipe_sha256
          and report["wheel"] == {"filename": descriptor.filename,
                                  "sha256": descriptor.sha256, "size": descriptor.size})
    artifact = uuid4()
    metadata = GeneratedFileMetadata(filename=descriptor.filename, mime_type=WHEEL_MIME_TYPE,
        size_bytes=descriptor.size, sha256=descriptor.sha256,
        storage_key=f"{invocation.tenant_id}/{invocation.run_id}/{artifact}/{descriptor.filename}",
        download_url=f"/api/v1/runs/{invocation.run_id}/artifacts/{artifact}/download")
    ref = _freeze_object({"schema_version": 1, "artifact_id": str(artifact),
        "descriptor": descriptor.model_dump(mode="json"),
        "file": metadata.to_content_file(), "report_sha256": _digest(report)}, name="Python product")
    task = asyncio.create_task(asyncio.to_thread(store.store_stream, invocation.tenant_id,
        invocation.run_id, artifact, descriptor.filename, WHEEL_MIME_TYPE, stream,
        expected_sha256=descriptor.sha256, expected_size=descriptor.size))
    try:
        try:
            actual = await asyncio.shield(task)
        except asyncio.CancelledError:
            # Join the writer before consume_response closes its anonymous spool.
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    continue
                except Exception:  # noqa: BLE001 - preserve the original cancellation after joining.
                    break
            if not task.cancelled():
                task.exception()
            raise
        _need(actual == metadata)
        with open_product(store, ref, invocation.tenant_id, invocation.run_id, source):
            pass
        return ref
    except BaseException:
        discard_product(store, ref)
        raise


def restore_product(store: GeneratedFileStore | None, ref: object,
                    invocation: PythonProjectInvocation, source: str,
                    report: Mapping[str, JsonValue]) -> Mapping[str, JsonValue] | None:
    if ref is None:
        _need(store is None)
        return None
    _need(store is not None and isinstance(ref, Mapping))
    assert store is not None and isinstance(ref, Mapping)
    controlled = _freeze_object(ref, name="restored Python product")
    descriptor, _, _ = _stored(controlled, invocation.tenant_id, invocation.run_id, source)
    descriptor.require_for(invocation)
    _need(controlled["report_sha256"] == _digest(report)
          and report["wheel"] == {"filename": descriptor.filename,
                                  "sha256": descriptor.sha256, "size": descriptor.size})
    with open_product(store, controlled, invocation.tenant_id, invocation.run_id, source):
        pass
    return controlled
