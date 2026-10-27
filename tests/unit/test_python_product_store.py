"""Actual disk bytes, hash failures and publication collision safety."""
from __future__ import annotations

import hashlib
import io
from pathlib import Path
from uuid import UUID

import pytest

from agent_hub.files.generated import WHEEL_MIME_TYPE, GeneratedFileStore


def store(store: GeneratedFileStore, data: bytes = b"wheel", **kw: object):
    return store.store_stream(UUID(int=1), UUID(int=2), UUID(int=3),
        "demo-1.0-py3-none-any.whl", WHEEL_MIME_TYPE, io.BytesIO(data),
        expected_sha256=kw.get("sha", hashlib.sha256(data).hexdigest()),
        expected_size=kw.get("size", len(data)))


def test_exact_product_bytes_and_original_wheel_name(tmp_path: Path) -> None:
    files = GeneratedFileStore(tmp_path)
    product = store(files)
    assert product.filename == "demo-1.0-py3-none-any.whl"
    assert files.resolve(product.storage_key).read_bytes() == b"wheel"
    with pytest.raises(FileExistsError):
        store(files, b"other")
    assert files.resolve(product.storage_key).read_bytes() == b"wheel"


@pytest.mark.parametrize("kw", [{"sha": "0" * 64}, {"size": 4}, {"size": 6},
    {"size": True}, {"sha": "oops"}])
def test_incomplete_or_corrupt_product_never_published(tmp_path: Path, kw: dict) -> None:
    with pytest.raises(ValueError):
        store(GeneratedFileStore(tmp_path), **kw)
    assert not [p for p in tmp_path.rglob("*") if p.is_file()]
