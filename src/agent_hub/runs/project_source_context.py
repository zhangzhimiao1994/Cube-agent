"""Bounded original source data for runtimes without native model read tools."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from pathlib import PurePosixPath
from typing import TypedDict

from agent_hub.capabilities.project_baselines import StoredProjectBaseline
from agent_hub.capabilities.scoped_read import _read_no_links
from agent_hub.runtime.contracts import _freeze_object


class SourceContextFile(TypedDict):
    path: str
    content: str
    sha256: str


class OriginalSourceContext(TypedDict):
    files: tuple[SourceContextFile, ...]
    file_count: int
    included_count: int
    truncated: bool


def original_source_context(
    stored: StoredProjectBaseline, *, byte_budget: int,
    fits: Callable[[OriginalSourceContext], bool] | None = None,
) -> OriginalSourceContext:
    if type(byte_budget) is not int or not 1 <= byte_budget <= 96_000:
        raise ValueError('original project source context budget is invalid')
    files: list[SourceContextFile] = []

    def projection(count: int) -> OriginalSourceContext:
        return {'files': tuple(files[:count]), 'file_count': stored.reference.file_count,
                'included_count': count, 'truncated': count != stored.reference.file_count}

    def accepted(candidate: OriginalSourceContext) -> bool:
        try:
            _freeze_object(candidate, name='original project source context')
        except (TypeError, ValueError):
            return False
        return fits is None or fits(candidate)

    empty = projection(0)
    if not accepted(empty):
        raise ValueError('original project source context budget is insufficient')
    remaining = byte_budget - len(json.dumps(empty, ensure_ascii=True, separators=(',', ':')).encode('ascii'))
    for path in sorted(stored.source_files, key=lambda name: (not stored.contract.is_editable(name), name)):
        # Legacy callers assemble their own carrier without a remaining-budget
        # validator. Preserve that envelope; the real producer supplies fits.
        if (fits is None and len(files) >= 128) or remaining < 64:
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
            size += bool(files) + len(str(len(files) + 1)) - len(str(len(files)))
            if len(files) + 1 == stored.reference.file_count:
                size += 1  # JSON false is one byte longer than true.
            if size <= remaining:
                remaining -= size
                files.append(item)
        except (OSError, ValueError, RuntimeError):
            raise RuntimeError('original project source context is unavailable or changed') from None
    full = projection(len(files))
    if accepted(full):
        return full
    # Only preview rows may be omitted. The producer tests the actual immutable
    # index, other artifacts and route on every proposal; an empty one must fit.
    low, high = 0, len(files) - 1
    while low < high:
        middle = (low + high + 1) // 2
        if accepted(projection(middle)):
            low = middle
        else:
            high = middle - 1
    return projection(low)
