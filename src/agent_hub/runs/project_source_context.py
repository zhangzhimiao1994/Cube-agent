"""Bounded original source data for runtimes without native model read tools."""

from __future__ import annotations

import hashlib
import json
from pathlib import PurePosixPath
from typing import TypedDict

from agent_hub.capabilities.project_baselines import StoredProjectBaseline
from agent_hub.capabilities.scoped_read import _read_no_links


class SourceContextFile(TypedDict):
    path: str
    content: str
    sha256: str


class OriginalSourceContext(TypedDict):
    files: tuple[SourceContextFile, ...]
    file_count: int
    included_count: int
    truncated: bool


def original_source_context(stored: StoredProjectBaseline, *, byte_budget: int) -> OriginalSourceContext:
    if type(byte_budget) is not int or not 1 <= byte_budget <= 96_000:
        raise ValueError('original project source context budget is invalid')
    files: list[SourceContextFile] = []
    remaining = byte_budget
    for path in sorted(stored.source_files, key=lambda name: (not stored.contract.is_editable(name), name)):
        if len(files) >= 128 or remaining < 64:
            break
        try:
            raw = _read_no_links(stored.snapshot, PurePosixPath(path).parts, max_bytes=remaining)
            if len(raw) > remaining:
                continue
            expected = stored.source_files[path]
            if expected not in {hashlib.sha256(tag + raw).hexdigest() for tag in (b'f\0', b'x\0')}:
                raise ValueError
            try:
                text = raw.decode('utf-8', errors='strict')
            except UnicodeError:
                continue
            if '\x00' in text:
                continue
            item: SourceContextFile = {'path': path, 'content': text, 'sha256': expected}
            size = len(json.dumps(item, ensure_ascii=True, separators=(',', ':')).encode('ascii'))
            if size <= remaining:
                remaining -= size
                files.append(item)
        except (OSError, ValueError, RuntimeError):
            raise RuntimeError('original project source context is unavailable or changed') from None
    return {'files': tuple(files), 'file_count': stored.reference.file_count,
            'included_count': len(files), 'truncated': len(files) != stored.reference.file_count}
