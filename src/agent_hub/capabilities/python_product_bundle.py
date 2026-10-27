"""Append measured product bytes only after the source archive was verified."""
from __future__ import annotations

import hashlib
import os
import zipfile
from pathlib import Path

from agent_hub.skills.sandbox.python_project_protocol import PythonProductDescriptor


def append_python_product(path: Path, fd: int, product: PythonProductDescriptor, *,
                          max_file_bytes: int, max_bundle_bytes: int,
                          max_bundle_files: int) -> str:
    name = "dist/" + product.filename
    with zipfile.ZipFile(path, "a", compression=zipfile.ZIP_DEFLATED) as archive:
        members = archive.infolist()
        names = {entry.filename for entry in members}
        folded = name.casefold()
        collision = any(
            (entry.filename.rstrip("/").casefold() == folded
             or entry.filename.rstrip("/").casefold().startswith(folded + "/")
             or not entry.is_dir() and folded.startswith(entry.filename.casefold() + "/"))
            for entry in members)
        if (len(names) != len(members) or name in names or "dist" in names
                or collision
                or len(members) + 1 > max_bundle_files
                or product.size > max_file_bytes
                or sum(item.file_size for item in members) + product.size > max_bundle_bytes):
            raise ValueError("Python product exceeds bundle contract")
        before = os.fstat(fd)
        if before.st_size != product.size:
            raise ValueError("Python product changed")
        os.lseek(fd, 0, os.SEEK_SET)
        digest = hashlib.sha256()
        remaining = product.size
        info = zipfile.ZipInfo(name)
        info.external_attr = 0o100600 << 16
        info.compress_type = zipfile.ZIP_DEFLATED
        with archive.open(info, "w", force_zip64=True) as target:
            while remaining:
                chunk = os.read(fd, min(65536, remaining))
                if not chunk:
                    raise ValueError("Python product changed")
                target.write(chunk)
                digest.update(chunk)
                remaining -= len(chunk)
        after = os.fstat(fd)
        def identity(info: os.stat_result) -> tuple[int, ...]:
            return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns,
                    info.st_ctime_ns, info.st_nlink, info.st_mode)
        if (os.read(fd, 1) or digest.hexdigest() != product.sha256
                or identity(before) != identity(after)):
            raise ValueError("Python product changed")
        # Re-read compressed output, independent of the writer's byte counter.
        actual = hashlib.sha256()
        with archive.open(name) as stored:
            while chunk := stored.read(65536):
                actual.update(chunk)
        if actual.hexdigest() != product.sha256:
            raise ValueError("Python product archive changed")
    return name
