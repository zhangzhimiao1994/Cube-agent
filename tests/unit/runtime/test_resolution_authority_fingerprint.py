"""Installed review evidence must expire when an authorization gate changes."""
from pathlib import Path

import pytest

from agent_hub.runtime.review_assembly import runtime_source_fingerprint


@pytest.mark.parametrize("relative", [
    "api/routers/runs.py",
    "capabilities/approvals.py",
    "capabilities/python_resolution_authorization.py",
    "execution_backends.py",
])
def test_changed_resolution_or_backend_authority_invalidates_evidence(
    monkeypatch: pytest.MonkeyPatch, relative: str,
) -> None:
    original = runtime_source_fingerprint()
    read = Path.read_bytes
    changed = False

    def changed_read(path: Path) -> bytes:
        nonlocal changed
        raw = read(path)
        if path.as_posix().endswith("/agent_hub/" + relative):
            changed = True
            return raw + b"\n# changed authority implementation\n"
        return raw

    monkeypatch.setattr(Path, "read_bytes", changed_read)
    assert runtime_source_fingerprint() != original
    assert changed
