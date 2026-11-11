"""Issued provider rejections survive actual AutoGen wrapping without another send."""
from __future__ import annotations

import json
from uuid import uuid4

import httpx
import pytest

from agent_hub.domain.runs import TaskMode
from agent_hub.models.gateway import ModelGateway
from agent_hub.models.provider_diagnostics import RequestShape
from agent_hub.models.registry import ModelRegistry
from agent_hub.runs.repository import _public_event_payload
from agent_hub.runtime.autogen.adapter import (
    AutoGenDiscussionRuntime,
    DiscussionParticipant,
    DiscussionPlan,
)
from agent_hub.runtime.contracts import EventKind, RunEvent, TaskContext
from tests.unit.models.provider_capacity_typed_fixture import mock_client
from tests.unit.models.test_gateway import CapacityStub, SecretStub, deployment, lease


@pytest.mark.parametrize("known", [True, False])
async def test_actual_runtime_provider400_is_finite_durable_and_does_not_retry(known: bool) -> None:
    sends: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        sends.append(request.method)
        return httpx.Response(400, json={"error": {
            "code": "invalid_json_schema" if known else "PRIVATE_CODE",
            "param": "response_format" if known else "PRIVATE_PARAM",
            "message": "PRIVATE_PROMPT authorization=Bearer sk-PRIVATE_REASONING",
        }})

    capacity = CapacityStub([lease("primary")])
    secrets = SecretStub(capacity.events)
    gateway = ModelGateway(ModelRegistry([deployment(
        "primary", provider_model="openai/test-model", request_model="test-model", api_base="https://mock.example/v1",
    )]), capacity, secrets, mock_client(handle))
    context = TaskContext(run_id=uuid4(), tenant_id=uuid4(), mode=TaskMode.DISCUSS,
                          request="Compare evidence", token_budget=1000)
    runtime = AutoGenDiscussionRuntime(gateway, DiscussionPlan(
        participants=(
            DiscussionParticipant(id="engineer", role="Engineer", goal="Implement", logical_model="primary"),
            DiscussionParticipant(id="reviewer", role="Reviewer", goal="Verify", logical_model="primary"),
        ), selector_model="primary", wall_time_seconds=5, token_budget=1000,
    ))
    events = [event async for event in runtime.run(context)]
    failure = events[-1]
    assert failure.kind is EventKind.RUNTIME_FAILED
    assert failure.payload["error_code"] == "model.provider_bad_request"
    assert failure.payload["status_code"] == 400 and failure.payload["retryable"] is False
    assert failure.payload["provider_rejection_class"] == ("structured_output" if known else "unknown")
    assert failure.payload["provider_parameter_family"] == ("response_format" if known else "unknown")
    assert failure.payload["provider_protocol"] == "chat_completions"
    assert failure.payload["provider_classification_source"] == ("structured_code" if known else "status_only")
    assert failure.payload["provider_body_state"] == "available"
    serialized = failure.to_payload()["payload"]
    assert isinstance(serialized, dict)
    shape = RequestShape.from_payload(serialized["provider_request_shape"])
    assert shape.protocol == "chat_completions"
    assert shape.send_ordinal == 1 and shape.stream is False
    assert shape.output_token_parameter == "max_completion_tokens"
    assert RunEvent.model_validate_json(failure.model_dump_json()).payload == failure.payload
    public_payload = _public_event_payload(failure.to_payload())["payload"]
    assert isinstance(public_payload, dict)
    assert public_payload == serialized
    assert RequestShape.from_payload(public_payload["provider_request_shape"]) == shape
    assert "PRIVATE" not in json.dumps([event.to_payload() for event in events])
    assert sends == ["POST"] and len(secrets.references) == len(capacity.releases) == 1
