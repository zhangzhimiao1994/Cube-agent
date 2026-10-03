from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from datetime import UTC, datetime
from typing import cast
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agent_hub.db.models import RunArtifactRow, RunEventRow
from agent_hub.runs.repository import RunRepository, _public_artifact_payload, _public_event_payload
from agent_hub.runtime.contracts import Artifact, EventKind, GatewayProvenance, JsonValue, RunEvent


def model_response(*, redacted: bool = True) -> Artifact:
    content: dict[str, JsonValue] = {
        "text": "Authorization: Bearer sk-private-review-value" if redacted else "Review complete.",
        "attempted_logical_model": "review-model",
        "score": 1e20,
        "label": "\u5ba1\u67e5",
    }
    if redacted:
        content["files"] = ({"filename": "review.txt", "storage_key": "PRIVATE_STORAGE_BODY"},)
    return Artifact(
        id=uuid4(), type="model_response", producer="security-reviewer", version=2,
        content=content, source_ids=(str(uuid4()),),
        provenance=GatewayProvenance(
            logical_model="review-model", deployment_id="review-deployment",
            provider_id="review-provider", provider_model="review-provider/model-v2",
        ),
    )


def canonical_public_hash(payload: dict[str, object]) -> str:
    envelope = {key: payload[key] for key in (
        "type", "producer", "version", "content", "source_ids", "provenance",
    )}
    return hashlib.sha256(json.dumps(
        envelope, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()


@pytest.mark.parametrize("redacted", [True, False])
def test_public_hash_verifies_projected_envelope_without_changing_private_artifact(
    redacted: bool,
) -> None:
    original = model_response(redacted=redacted)
    payload = original.to_payload()
    before = deepcopy(payload)

    public = _public_artifact_payload(payload)

    assert public["content_sha256"] == original.content_sha256
    assert public["public_content_sha256"] == canonical_public_hash(public)
    assert public["content_redacted"] is redacted
    assert (public["public_content_sha256"] != original.content_sha256) is redacted
    assert public["provenance"] == before["provenance"]
    assert public["source_ids"] == before["source_ids"]
    assert public["producer"] == "security-reviewer"
    content = cast(dict[str, object], public["content"])
    assert content["attempted_logical_model"] == "review-model"
    if redacted:
        assert content["text"] == "[redacted]"
        assert content["files"] == [{"filename": "review.txt"}]
        for private in ("sk-private-review-value", "PRIVATE_STORAGE_BODY", "storage_key"):
            assert private not in json.dumps(public)
    projected = {key: value for key, value in public.items()
                 if key not in {"public_content_sha256", "content_redacted"}}
    projected["content_sha256"] = public["public_content_sha256"]
    assert Artifact.from_payload(projected).content_sha256 == public["public_content_sha256"]
    assert payload == before
    assert original.to_payload() == before
    assert Artifact.from_payload(payload).content_sha256 == original.content_sha256


def test_real_reviewer_text_only_projection_keeps_other_envelope_fields() -> None:
    original = model_response().to_payload()
    content = cast(dict[str, object], original["content"])
    del content["files"]
    original["content_sha256"] = ""
    original = Artifact.from_payload(original).to_payload()

    public = _public_event_payload({"kind": "artifact.created", "artifact": original})
    artifact = cast(dict[str, object], public["artifact"])
    expected = deepcopy(original)
    cast(dict[str, object], expected["content"])["text"] = "[redacted]"
    expected["public_content_sha256"] = canonical_public_hash(expected)
    expected["content_redacted"] = True
    assert artifact == expected


@pytest.mark.parametrize("corruption", [
    "hash", "missing-id", "version", "hidden-key", "forged-metadata", "depth", "width", "string",
])
def test_invalid_original_never_receives_fabricated_projection_metadata(corruption: str) -> None:
    payload = model_response().to_payload()
    if corruption == "hash":
        payload["content_sha256"] = "0" * 64
    elif corruption == "missing-id":
        del payload["id"]
    elif corruption == "version":
        payload["version"] = True
    elif corruption == "hidden-key":
        cast(dict[str, object], payload["content"])["hidden_reasoning"] = "PRIVATE_HIDDEN_BODY"
    elif corruption == "forged-metadata":
        payload["public_content_sha256"] = "f" * 64
        payload["content_redacted"] = False
    elif corruption == "depth":
        nested: object = "Authorization: Bearer sk-private-review-value"
        for _ in range(25):
            nested = {"nested": nested}
        payload["content"] = {"nested": nested}
    elif corruption == "width":
        payload["content"] = {"items": [None] * 4097}
    else:
        payload["content"] = {"text": "secret " + "x" * 512_001}
    before = deepcopy(payload)
    with pytest.raises(ValueError, match="invalid runtime contract"):
        Artifact.from_payload(payload)

    public = _public_artifact_payload(payload)

    assert "public_content_sha256" not in public
    assert "content_redacted" not in public
    for private in ("sk-private-review-value", "PRIVATE_HIDDEN_BODY", "hidden_reasoning", "storage_key"):
        assert private not in json.dumps(public)
    assert payload == before


def test_valid_original_with_invalid_projected_provenance_has_no_public_proof() -> None:
    artifact = model_response()
    payload = artifact.to_payload()
    provenance = cast(dict[str, object], payload["provenance"])
    provenance["provider_model"] = "review-provider/secret-model"
    payload["content_sha256"] = ""
    payload = Artifact.from_payload(payload).to_payload()

    public = _public_artifact_payload(payload)

    assert "public_content_sha256" not in public
    assert "content_redacted" not in public
    assert "secret-model" not in json.dumps(public)
    assert public["content_sha256"] == payload["content_sha256"]


@pytest.mark.parametrize("missing", [True, False])
def test_original_without_persisted_digest_cannot_claim_public_proof(missing: bool) -> None:
    payload = model_response(redacted=False).to_payload()
    if missing:
        del payload["content_sha256"]
    else:
        payload["content_sha256"] = ""
    assert Artifact.from_payload(payload).content_sha256

    public = _public_artifact_payload(payload)

    assert "public_content_sha256" not in public
    assert "content_redacted" not in public
    assert public.get("content_sha256") == payload.get("content_sha256")


def test_nested_event_artifacts_match_direct_projection_without_resanitizing_metadata() -> None:
    original = model_response().to_payload()
    payload: dict[str, object] = {
        "kind": "artifact.created", "artifact": original,
        "payload": {"items": [{"artifact": original, "note": "Bearer sk-private-outer-value"}]},
    }
    before = deepcopy(payload)

    public = _public_event_payload(payload)

    direct = _public_artifact_payload(original)
    assert public["artifact"] == direct
    nested = cast(dict[str, object], public["payload"])
    items = cast(list[dict[str, object]], nested["items"])
    assert items == [{"artifact": direct, "note": "[redacted]"}]
    assert cast(dict[str, object], items[0]["artifact"])["public_content_sha256"] == (
        canonical_public_hash(direct)
    )
    assert payload == before


async def test_repository_public_events_and_artifact_lists_share_projection_and_preserve_raw() -> None:
    tenant_id, run_id = uuid4(), uuid4()
    artifact = model_response()
    raw = artifact.to_payload()
    event = RunEvent(kind=EventKind.ARTIFACT_CREATED, sequence=1, run_id=run_id, artifact=artifact)
    stored_event = event.to_payload()
    before = deepcopy((raw, stored_event))
    artifact_row = RunArtifactRow(
        id=artifact.id, tenant_id=tenant_id, run_id=run_id,
        type=artifact.type, producer=artifact.producer, payload=raw,
    )
    event_row = RunEventRow(
        id=uuid4(), tenant_id=tenant_id, run_id=run_id, sequence=1,
        kind=EventKind.ARTIFACT_CREATED.value, payload=stored_event, created_at=datetime.now(UTC),
    )
    session = AsyncMock(spec=AsyncSession)
    session.__aenter__.return_value = session
    session.scalar.return_value = run_id
    artifact_rows = MagicMock()
    artifact_rows.all.return_value = [artifact_row]
    event_rows = MagicMock()
    event_rows.all.return_value = [event_row]
    session.scalars.side_effect = [artifact_rows, event_rows, artifact_rows, event_rows]
    repository = RunRepository(cast(
        async_sessionmaker[AsyncSession], MagicMock(return_value=session),
    ))

    public_artifacts = await repository.artifacts(tenant_id, run_id)
    public_events = await repository.events(tenant_id, run_id)
    private_artifacts = await repository.raw_artifacts(tenant_id, run_id)
    private_events = await repository.raw_events(tenant_id, run_id)

    assert public_events[0]["artifact"] == public_artifacts[0]
    assert public_artifacts[0]["public_content_sha256"] == canonical_public_hash(public_artifacts[0])
    assert public_artifacts[0]["content_redacted"] is True
    assert private_artifacts == (before[0],)
    assert private_events[0].to_payload() == before[1]
    assert (artifact_row.payload, event_row.payload) == before
