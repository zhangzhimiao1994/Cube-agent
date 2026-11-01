"""Bounded product wire transfer; no root execution or model calls."""
from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from typing import BinaryIO

import pytest

from agent_hub.skills.sandbox.python_product_transport import receive_product
from agent_hub.skills.sandbox.python_project_protocol import (
    PythonProductDescriptor,
    PythonProjectInvocation,
)
from agent_hub.subprocess_cleanup import ProcessCleanupIncomplete
from tests.unit.runs.project_auto_repair_typed_fixture import present
from tests.unit.skills.sandbox.test_python_project_client import invocation


def descriptor(data: bytes = b"wheel") -> PythonProductDescriptor:
    inv = invocation()
    return PythonProductDescriptor(schema_version=1, execution_id=inv.execution_id,
        tenant_id=str(inv.tenant_id), run_id=str(inv.run_id), project_id=inv.project_id,
        session_id=inv.session_id, source_sha256="e" * 64, recipe_sha256=inv.recipe_sha256,
        filename="demo-1.0-py3-none-any.whl", sha256=hashlib.sha256(data).hexdigest(), size=len(data))


@pytest.mark.asyncio
async def test_product_stream_is_seekable_and_exact() -> None:
    reader = asyncio.StreamReader()
    reader.feed_data(b"wheelFINAL")
    reader.feed_eof()
    with await receive_product(reader, descriptor(), invocation()) as stream:
        assert stream.read() == b"wheel"
        assert await reader.read() == b"FINAL"
    assert stream.closed


@pytest.mark.asyncio
@pytest.mark.parametrize("data", [b"whee", b"WRONG"])
async def test_truncated_or_changed_product_is_rejected(data: bytes) -> None:
    reader = asyncio.StreamReader()
    reader.feed_data(data)
    reader.feed_eof()
    with pytest.raises((ValueError, asyncio.IncompleteReadError)):
        await receive_product(reader, descriptor(), invocation())


@pytest.mark.asyncio
@pytest.mark.parametrize("update", [{"run_id": "00000000-0000-0000-0000-000000000003"},
    {"execution_id": "other"}, {"recipe_sha256": "f" * 64}, {"project_id": "other"},
    {"session_id": "other"}, {"tenant_id": "00000000-0000-0000-0000-000000000003"},
    {"size": 2**31}])
async def test_scope_and_size_checked_before_read(update: dict[str, object]) -> None:
    product = PythonProductDescriptor.model_validate_json(json.dumps(
        descriptor().model_dump() | update), strict=True)
    with pytest.raises(ValueError):
        await receive_product(asyncio.StreamReader(), product, invocation())


@pytest.mark.parametrize("name", ["../x.whl", "x/y.whl", "x\\y.whl", "a.zip", ".whl"])
def test_product_name_is_not_a_host_path(name: str) -> None:
    with pytest.raises(ValueError):
        PythonProductDescriptor.model_validate(descriptor().model_dump() | {"filename": name})


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["success", "cleanup_failed", "changed_descriptor", "missing_stream", "close_failed", "metadata_only", "cleanup_failed_close", "cleanup_failed_wait"])
async def test_client_requires_final_cleanup_reply(monkeypatch: pytest.MonkeyPatch, outcome: str) -> None:
    from agent_hub.skills.sandbox import python_project_client as client
    from agent_hub.skills.sandbox.python_project_protocol import PythonProjectBrokerRequest
    from tests.unit.skills.sandbox.test_python_project_client import FrameWriter, response

    product = descriptor()
    final = response("run")
    final["product"] = product.model_dump()
    final["result"]["stdout"] = json.dumps({"status": "passed",
        "execution_id": product.execution_id, "source_sha256": product.source_sha256,
        "recipe_sha256": product.recipe_sha256, "wheel": {"filename": product.filename,
            "sha256": product.sha256, "size": product.size}})
    if outcome.startswith("cleanup_failed"):
        final = {"version": 4, "action": "run_python_project", "execution_id": product.execution_id,
            "ok": False, "error": "python_project_cleanup_incomplete"}
    elif outcome == "changed_descriptor":
        final["product"]["sha256"] = "f" * 64
    elif outcome == "metadata_only":
        final.pop("product")
    reader = asyncio.StreamReader()
    def frame(value: object) -> None:
        data = json.dumps(value).encode()
        reader.feed_data(len(data).to_bytes(4) + data)
    if outcome not in {"missing_stream", "metadata_only"}:
        frame({"python_product_stream": 1, "product": product.model_dump()})
        reader.feed_data(b"wheel")
    frame(final)
    reader.feed_eof()
    writer = FrameWriter()
    spools = []
    if outcome in {"close_failed", "cleanup_failed_close", "cleanup_failed_wait"}:
        from agent_hub.skills.sandbox import python_product_transport as transport
        original = transport.receive_product
        async def receive(reader: asyncio.StreamReader, product: PythonProductDescriptor,
                          invocation: PythonProjectInvocation) -> BinaryIO:
            spool = await original(reader, product, invocation)
            spools.append(spool)
            return spool
        async def close_failed() -> None:
            writer.waited = True
            raise OSError("close failed")
        monkeypatch.setattr(transport, "receive_product", receive)
        if outcome == "cleanup_failed_close":
            close_original = writer.close
            def bad_close() -> None:
                close_original()
                raise OSError("close failed")
            monkeypatch.setattr(writer, "close", bad_close)
        else:
            monkeypatch.setattr(writer, "wait_closed", close_failed)
    async def connect(_path: str) -> tuple[asyncio.StreamReader, FrameWriter]:
        return reader, writer
    monkeypatch.setattr(asyncio, "open_unix_connection", connect, raising=False)
    request = PythonProjectBrokerRequest(invocation=invocation(), product_transfer_version=1)
    if outcome == "success":
        reply = await client.request_python_project_broker(request, socket_path=Path("/fake"))
        with present(reply._product_stream) as stream:
            assert stream.read() == b"wheel"
    else:
        error_type = (ProcessCleanupIncomplete if outcome.startswith("cleanup_failed")
                      else OSError if outcome == "close_failed" else ValueError)
        with pytest.raises(error_type):
            await client.request_python_project_broker(request, socket_path=Path("/fake"))
        assert all(spool.closed for spool in spools)
    assert writer.closed and (writer.waited or outcome == "cleanup_failed_close")
