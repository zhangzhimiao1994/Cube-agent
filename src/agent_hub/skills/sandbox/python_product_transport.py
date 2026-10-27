"""Opt-in bounded wheel bytes over the existing authenticated AF_UNIX channel.

The preamble is not success. Only the final broker reply, sent after cleanup,
can commit the received anonymous spool. No binary content enters JSON.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import tempfile
from typing import BinaryIO, cast

from agent_hub.skills.sandbox.python_project_protocol import (
    PythonProductDescriptor,
    PythonProjectBrokerResponse,
    PythonProjectInvocation,
)


async def receive_product(reader: asyncio.StreamReader, product: PythonProductDescriptor,
                          invocation: PythonProjectInvocation) -> BinaryIO:
    product.require_for(invocation)
    stream = cast(BinaryIO, tempfile.TemporaryFile(mode="w+b"))  # noqa: SIM115 - ownership transfers after commit.
    try:
        digest = hashlib.sha256()
        remaining = product.size
        while remaining:
            chunk = await reader.readexactly(min(65536, remaining))
            stream.write(chunk)
            digest.update(chunk)
            remaining -= len(chunk)
        if digest.hexdigest() != product.sha256:
            raise ValueError("python_project_product_integrity")
        stream.flush()
        os.fsync(stream.fileno())
        stream.seek(0)
        return stream
    except BaseException:
        stream.close()
        raise


async def send_product(writer: asyncio.StreamWriter, descriptor: PythonProductDescriptor,
                       fd: int) -> None:
    from agent_hub.skills.sandbox.broker import _write_frame

    preamble = {"python_product_stream": 1, "product": descriptor.model_dump()}
    await _write_frame(writer, json.dumps(preamble, separators=(",", ":")).encode())
    remaining = descriptor.size
    digest = hashlib.sha256()
    os.lseek(fd, 0, os.SEEK_SET)
    while remaining:
        chunk = os.read(fd, min(65536, remaining))
        if not chunk:
            raise ValueError("python_project_product_integrity")
        digest.update(chunk)
        writer.write(chunk)
        await writer.drain()
        remaining -= len(chunk)
    if os.read(fd, 1) or digest.hexdigest() != descriptor.sha256:
        raise ValueError("python_project_product_integrity")


def require_product_report(response: PythonProjectBrokerResponse,
                           descriptor: PythonProductDescriptor) -> None:
    """Cross-check native stdout metadata before any service may persist bytes."""
    if not response.ok or response.result is None or response.product != descriptor:
        raise ValueError("python_project_product_response_invalid")
    report = json.loads(response.result.stdout)
    if (not isinstance(report, dict) or report.get("status") != "passed"
            or report.get("execution_id") != descriptor.execution_id
            or report.get("source_sha256") != descriptor.source_sha256
            or report.get("recipe_sha256") != descriptor.recipe_sha256
            or report.get("wheel") != {"filename": descriptor.filename,
                "sha256": descriptor.sha256, "size": descriptor.size}):
        raise ValueError("python_project_product_response_invalid")
