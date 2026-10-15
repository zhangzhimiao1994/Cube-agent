"""Relocate generated pip entry points without importing installed packages."""

from __future__ import annotations

import base64
import csv
import hashlib
import io
import os
import stat
from pathlib import Path
from zipfile import BadZipFile, ZipFile


def _script_bytes(data: bytes, before: bytes, after: bytes) -> bytes:
    if data.startswith(b"MZ"):
        try:
            with ZipFile(io.BytesIO(data)) as archive:
                members = archive.infolist()
                if len(members) != 1 or members[0].filename != "__main__.py":
                    return data
                offset = members[0].header_offset
        except BadZipFile:
            return data
        prefix = data[:offset]
        start = prefix.rfind(b"#!")
        if start < 0:
            return data
        header = prefix[start:]
        if not header.endswith(b"\n") or header.count(b"\n") != 1:
            raise ValueError("invalid Python executable entry point")
        return prefix[:start] + header.replace(before, after, 1) + data[offset:]
    if not data.startswith(b"#!"):
        return data
    lines = data.splitlines(keepends=True)
    count = 3 if data.startswith(b"#!/bin/sh\n'''exec' ") else 1
    header = b"".join(lines[:count])
    return header.replace(before, after, 1) + b"".join(lines[count:])


def relocate_python_environment(staging: Path, final: Path) -> None:
    scripts = staging / ("Scripts" if os.name == "nt" else "bin")
    if not scripts.is_dir():
        raise ValueError("Python environment scripts directory is missing")
    before, after = str(staging).encode("utf-8"), str(final).encode("utf-8")
    changed: dict[Path, tuple[str, str]] = {}
    for script in scripts.iterdir():
        info = script.lstat()
        if stat.S_ISLNK(info.st_mode):
            continue
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError("unsafe Python environment script")
        original = script.read_bytes()
        relocated = _script_bytes(original, before, after)
        if relocated != original:
            script.write_bytes(relocated)
            digest = (
                base64.urlsafe_b64encode(hashlib.sha256(relocated).digest())
                .rstrip(b"=")
                .decode("ascii")
            )
            changed[script.resolve()] = "sha256=" + digest, str(len(relocated))
    if not changed:
        return
    # Installed RECORD paths are CSV-relative to site-packages, not to dist-info.
    for record in staging.rglob("*.dist-info/RECORD"):
        info = record.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError("unsafe Python environment record")
        with record.open(encoding="utf-8", newline="") as stream:
            rows = list(csv.reader(stream))
        updated = False
        for row in rows:
            if len(row) != 3:
                raise ValueError("invalid Python environment record")
            replacement = changed.get((record.parent.parent / row[0]).resolve())
            if replacement is not None:
                row[1:] = replacement
                updated = True
        if updated:
            with record.open("w", encoding="utf-8", newline="") as stream:
                csv.writer(stream, lineterminator="\n").writerows(rows)
