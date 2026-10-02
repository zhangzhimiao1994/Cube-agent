from __future__ import annotations

import httpx
import pytest
from openai import AuthenticationError

from agent_hub.models.gateway import ModelGateway
from agent_hub.models.litellm_client import ModelTransportError
from agent_hub.models.registry import ModelRegistry
from tests.contracts.test_litellm_client import PROMPT, captured_traceback, deployment, request
from tests.unit.models.test_gateway import (
    CapacityStub,
    PerCallStreamingTransportStub,
    SecretStub,
    TransportStub,
    lease,
)


async def poisoned_request_id_failure(*, streaming: bool) -> ModelTransportError:
    capacity = CapacityStub([lease("primary-1", "default-account")])
    failure = AuthenticationError(
        "authentication failed",
        response=httpx.Response(
            401, headers={"x-request-id": PROMPT},
            request=httpx.Request("POST", "https://provider.example/v1"),
        ),
        body=None,
    )
    transport = (
        PerCallStreamingTransportStub(capacity.events, [[failure]])
        if streaming else TransportStub(capacity.events, failure=failure)
    )
    del failure
    gateway = ModelGateway(
        ModelRegistry([deployment()]), capacity, SecretStub(capacity.events), transport,
    )
    try:
        if streaming:
            _ = [event async for event in gateway.stream_openai_compatible_events(request())]
        else:
            await gateway.complete_with_context(request())
    except ModelTransportError as error:
        return error
    raise AssertionError("expected authentication failure")


@pytest.mark.parametrize("streaming", [False, True])
async def test_gateway_provider_request_id_cannot_leak_prompt_in_locals(
    streaming: bool,
) -> None:
    error = await poisoned_request_id_failure(streaming=streaming)
    assert error.status_code == 401
    assert error.__cause__ is None and error.__context__ is None
    assert PROMPT not in captured_traceback(error)
