"""Every new retirement safety component must revoke stale review evidence."""
from pathlib import Path

import pytest

from agent_hub.runtime import review_assembly


@pytest.mark.parametrize("changed", [
    "skills/sandbox/python_premodel_retirement.py",
    "skills/sandbox/python_premodel_retirement_backend.py",
    "skills/sandbox/python_project_cache.py",
    "skills/sandbox/python_project_client.py",
    "skills/sandbox/python_project_protocol.py",
])
def test_safety_source_change_revokes_existing_runtime_qualification(
    monkeypatch: pytest.MonkeyPatch, changed: str,
) -> None:
    root = Path(review_assembly.__file__).resolve().parents[1]
    body = b"first"

    def read(path: Path) -> bytes:
        relative = path.relative_to(root).as_posix()
        return body if relative == changed else b"stable:" + relative.encode()

    monkeypatch.setattr(Path, "read_bytes", read)
    first = review_assembly.runtime_source_fingerprint()
    body = b"second"
    assert review_assembly.runtime_source_fingerprint() != first
