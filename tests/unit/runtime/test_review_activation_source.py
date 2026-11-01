from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta, tzinfo
from pathlib import Path

import pytest

from agent_hub.runtime import review_assembly as assembly
from tests.unit.runtime.test_review_assembly import evidence_document, fixture


async def source(tmp_path: Path) -> tuple[assembly.ServerReviewEvidenceSource, bytes, datetime]:
    f = await fixture(tmp_path)
    value = evidence_document(f)
    value['valid_until'] = (datetime.now(UTC) + timedelta(seconds=30)).isoformat()
    body = json.dumps(value).encode()
    path = tmp_path / 'review-evidence.json'
    path.write_bytes(body)
    return assembly.ServerReviewEvidenceSource(path, hashlib.sha256(body).hexdigest()), body, datetime.now(UTC)


async def test_verified_activation_keeps_actual_bytes_and_contract_binding(tmp_path: Path) -> None:
    reader, body, _ = await source(tmp_path)
    activation = reader.current()
    assert activation.evidence_sha256 == hashlib.sha256(body).hexdigest()
    assert activation.record_reference == str(reader.path)
    assert activation.source_sha256 == assembly.runtime_source_fingerprint()
    assert len(activation.contracts_sha256) == 64 and activation.contracts
    assert activation.verification.source_sha256 == activation.source_sha256
    assert reader.path.read_bytes() == body


async def test_only_time_expiry_is_allowed_at_sealed_original_admission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    reader, body, admitted_at = await source(tmp_path)
    original = reader.current()
    later = original.valid_until + timedelta(seconds=1)

    class Later(datetime):
        @classmethod
        def now(cls, tz: tzinfo | None = None) -> Later:
            return cls.fromtimestamp(later.timestamp(), tz=tz)

    monkeypatch.setattr(assembly, 'datetime', Later)
    with pytest.raises(ValueError, match='invalid server review evidence'):
        reader.current()
    with pytest.raises(ValueError, match='invalid server review evidence'):
        assembly.load_server_review_evidence(reader.path, expected_sha256=reader.expected_sha256)
    restored = reader.admitted(original.binding(), admitted_at=admitted_at)
    assert restored.binding() == original.binding()
    assert restored.contracts == original.contracts
    assert reader.path.read_bytes() == body
    with pytest.raises(ValueError, match='invalid server review evidence'):
        reader.admitted(original.binding(), admitted_at=later)


@pytest.mark.parametrize('change', ['remove', 'replace', 'contract', 'namespace'])
async def test_historical_read_preserves_revocation_and_exact_binding(
    tmp_path: Path, change: str,
) -> None:
    reader, body, admitted_at = await source(tmp_path)
    binding = reader.current().binding()
    if change == 'remove':
        reader.path.unlink()
    elif change == 'replace':
        reader.path.write_bytes(body + b' ')
    elif change == 'contract':
        binding['contracts_sha256'] = '0' * 64
    else:
        binding['record_reference'] = str(tmp_path.parent / 'different' / 'review-evidence.json')
    with pytest.raises(ValueError):
        reader.admitted(binding, admitted_at=admitted_at)
