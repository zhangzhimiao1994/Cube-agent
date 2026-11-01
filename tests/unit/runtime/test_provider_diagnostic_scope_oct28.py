"""Real failure scope/Artifact/private-codec round trips, with mocked HTTP only."""
import copy
import json
from uuid import uuid4

import httpx
import pytest

from agent_hub.db.models import RuntimeArtifactRow
from agent_hub.models.litellm_client import ModelTransportError
from agent_hub.runs.artifacts import PostgresArtifactRepository
from agent_hub.runtime.contracts import Artifact
from agent_hub.runtime.model_scope import ModelScopeTracker, validate_model_scope_artifact
from tests.unit.models.test_provider_diagnostics_oct28 import gateway_fixture
from tests.unit.runs.reviewed_report_revision_typed_fixture import (
    object_array,
    object_map,
    read_map,
)


@pytest.mark.parametrize("protocol", ["chat_completions", "responses", "messages", "stream"])
async def test_provider_class_persists_through_actual_scope_artifact_and_private_codec(protocol: str) -> None:
    sent = []

    def handle(request: httpx.Request) -> httpx.Response:
        sent.append(request.url.path)
        return httpx.Response(400, json={"error": {"code": "tool_call_response_missing", "param": "messages[1]",
                                                   "message": "PRIVATE_PROMPT PRIVATE_REASONING PRIVATE_API_KEY"}})

    gateway, req, _, _ = gateway_fixture(protocol, handle)
    run, tenant = uuid4(), uuid4()
    tracker = ModelScopeTracker(run_id=run, tenant_id=tenant, logical_model="primary")
    index = tracker.begin()
    with pytest.raises(ModelTransportError) as caught:
        if protocol == "stream":
            _ = [event async for event in gateway.stream_openai_compatible_events(req)]
        else:
            await gateway.complete_with_context(req)
    tracker.failed(index, caught.value)
    artifacts = tracker.artifacts()
    assert len(artifacts) == 1 and not tracker.incomplete and len(sent) == 1
    artifact = Artifact.from_payload(artifacts[0].to_payload())
    serialized = json.dumps(artifact.to_payload(), sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    row = RuntimeArtifactRow(tenant_id=tenant, run_id=run, id=artifact.id, payload=serialized,
                             byte_size=len(serialized.encode()), content_sha256=artifact.content_sha256)
    hydrated = PostgresArtifactRepository._hydrate(row)
    scope = validate_model_scope_artifact(hydrated, str(run))
    receipt = read_map(object_array(scope["calls"])[0])["receipt"]
    receipt = read_map(receipt)
    assert receipt["schema_version"] == 2
    diagnostic = read_map(read_map(object_array(receipt["attempts"])[0])["provider_diagnostic"])
    assert read_map(object_array(diagnostic["rejections"])[0])["rejection_class"] == "tool_history"
    assert read_map(object_array(diagnostic["rejections"])[0])["parameter_family"] == "messages"
    assert read_map(read_map(object_array(diagnostic["rejections"])[0])["request_shape"])["send_ordinal"] == 1
    assert "PRIVATE" not in serialized
    broken = copy.deepcopy(hydrated.to_payload())
    call = object_map(object_array(object_map(broken["content"])["calls"])[0])
    attempt = object_map(object_array(object_map(call["receipt"])["attempts"])[0])
    object_map(attempt["provider_diagnostic"])["raw_body"] = "PRIVATE"
    broken.pop("content_sha256")
    with pytest.raises(ValueError):
        validate_model_scope_artifact(Artifact.from_payload(broken), str(run))
