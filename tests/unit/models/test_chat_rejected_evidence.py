from __future__ import annotations

import asyncio
from decimal import Decimal
from types import SimpleNamespace

import httpx
import pytest
from openai import APIResponseValidationError

from agent_hub.models.failure_receipt import MAX_GATEWAY_FAILURE_TOKENS
from agent_hub.models.gateway import DeploymentPricing, GatewayRejectedOutput, ModelGateway
from agent_hub.models.litellm_client import ModelResponseCancelled, ModelResponseError
from agent_hub.models.registry import ModelRegistry
from agent_hub.models.types import RejectedOutputEvidence, TokenUsage
from tests.contracts.test_litellm_client import (
    API_KEY,
    PROMPT,
    RAW_ERROR,
    captured_traceback,
    deployment,
    mock_transport,
    request,
    sdk_response,
)
from tests.unit.models.test_gateway import CapacityStub, SecretStub, lease


def tool(arguments: str = "{broken", *, identifier: str = "call_1") -> SimpleNamespace:
    return SimpleNamespace(
        id=identifier, function=SimpleNamespace(name="lookup", arguments=arguments)
    )


async def rejected(response: object) -> RejectedOutputEvidence:
    client, _, create, close = mock_transport(result=response)
    with pytest.raises(ModelResponseError) as caught:
        await client.complete(deployment(), request(), API_KEY)
    assert create.await_count == 1
    assert close.await_count == 1
    evidence = caught.value.evidence
    assert evidence is not None
    assert not evidence.correction_eligible
    assert not hasattr(evidence, "tool_calls")
    return evidence


@pytest.mark.parametrize(
    "calls",
    [
        "not-a-list",
        [tool()],
        [tool("{}"), tool(identifier="call_2")],
        [tool("[]")],
        [tool('{"x":NaN}')],
        [tool("{}", identifier="")],
        [tool('{"x":' + "[" * 1100 + "0" + "]" * 1100 + "}")],
    ],
)
async def test_malformed_tools_preserve_actual_usage_without_accepting_any_call(
    calls: object,
) -> None:
    response = sdk_response()
    response.choices[0].message.tool_calls = calls  # type: ignore[attr-defined]
    evidence = await rejected(response)
    assert evidence.usage == TokenUsage(2, 3, 5)
    assert evidence.usage_status == "known"
    assert evidence.reason == "invalid_tool"
    assert evidence.final_text == "hello"


@pytest.mark.parametrize("choices", [None, [], "bad", [SimpleNamespace(message=None)]])
async def test_malformed_choices_preserve_usage_without_inventing_text(choices: object) -> None:
    response = sdk_response()
    response.choices = choices  # type: ignore[attr-defined]
    evidence = await rejected(response)
    assert evidence.usage == TokenUsage(2, 3, 5)
    assert evidence.reason == "invalid_output"
    assert evidence.final_text is None
    assert evidence.status == "unknown"


async def test_malformed_content_preserves_usage_without_coercion() -> None:
    response = sdk_response()
    response.choices[0].message.content = {"text": RAW_ERROR}  # type: ignore[attr-defined]
    evidence = await rejected(response)
    assert evidence.usage == TokenUsage(2, 3, 5)
    assert evidence.final_text is None
    assert evidence.reason == "invalid_output"


@pytest.mark.parametrize(
    ("usage", "status"),
    [
        (None, "missing"),
        (SimpleNamespace(), "invalid"),
        (SimpleNamespace(prompt_tokens=True, completion_tokens=3, total_tokens=4), "invalid"),
        (SimpleNamespace(prompt_tokens=-1, completion_tokens=3, total_tokens=2), "invalid"),
        (SimpleNamespace(prompt_tokens=2, completion_tokens=3, total_tokens=99), "invalid"),
        (
            SimpleNamespace(prompt_tokens=float("nan"), completion_tokens=3, total_tokens=5),
            "invalid",
        ),
        (SimpleNamespace(prompt_tokens="9" * 5000, completion_tokens=3, total_tokens=5), "invalid"),
    ],
)
async def test_missing_or_invalid_usage_never_becomes_zero_usage(
    usage: object,
    status: str,
) -> None:
    response = sdk_response(tool_calls=[tool()])
    response.usage = usage  # type: ignore[attr-defined]
    evidence = await rejected(response)
    assert evidence.usage is None
    assert evidence.usage_status == status


async def test_usage_outside_accounting_bounds_is_not_known_evidence() -> None:
    evidence = await rejected(
        sdk_response(
            tool_calls=[tool()],
            usage=SimpleNamespace(
                prompt_tokens=MAX_GATEWAY_FAILURE_TOKENS + 1,
                completion_tokens=0,
                total_tokens=MAX_GATEWAY_FAILURE_TOKENS + 1,
            ),
        )
    )
    assert evidence.usage is None
    assert evidence.usage_status == "invalid"


@pytest.mark.parametrize("counts", [(0, 0, 0), ("2", 3.0, 5)])
async def test_actual_valid_usage_retains_existing_coercion(counts: tuple[object, ...]) -> None:
    response = sdk_response(
        tool_calls=[tool()],
        usage=SimpleNamespace(
            prompt_tokens=counts[0], completion_tokens=counts[1], total_tokens=counts[2]
        ),
    )
    evidence = await rejected(response)
    assert evidence.usage == (TokenUsage(0, 0, 0) if counts[0] == 0 else TokenUsage(2, 3, 5))


@pytest.mark.parametrize(
    "text",
    [API_KEY, PROMPT, "x" * 65537, "\ud800"],
    ids=["key", "prompt", "oversized", "invalid-utf8"],
)
async def test_unsafe_text_is_omitted_but_actual_usage_is_retained(text: str) -> None:
    evidence = await rejected(sdk_response(content=text, tool_calls=[tool()]))
    assert evidence.final_text is None
    assert evidence.text_sha256 is None
    assert evidence.usage == TokenUsage(2, 3, 5)


@pytest.mark.parametrize("finish_reason", [None, "length", "unknown", "content_filter"])
async def test_unconfirmed_completed_text_is_not_repair_evidence(finish_reason: object) -> None:
    response = sdk_response(tool_calls=[tool()])
    response.choices[0].finish_reason = finish_reason  # type: ignore[attr-defined]
    evidence = await rejected(response)
    assert evidence.final_text is None
    assert evidence.usage == TokenUsage(2, 3, 5)


async def test_gateway_accounts_rejected_chat_once_without_backup_or_raw_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    client, _, create, _ = mock_transport(
        result=sdk_response(content="hello", tool_calls=[tool(RAW_ERROR + API_KEY + PROMPT)])
    )
    selected = deployment()
    capacity = CapacityStub(
        [lease(selected.id, selected.quota_scope_id), lease("backup", "default-account")]
    )
    gateway = ModelGateway(
        ModelRegistry([selected, deployment(id="backup", logical_model="backup")]),
        capacity,
        SecretStub(capacity.events),
        client,
        fallbacks={"primary": "backup"},
        pricing={selected.id: DeploymentPricing(Decimal(1), Decimal(2))},
    )
    with pytest.raises(GatewayRejectedOutput) as caught:
        await gateway.complete_with_context(request())
    assert caught.value.evidence is not None
    assert caught.value.evidence.usage == TokenUsage(2, 3, 5)
    assert caught.value.cost_usd == Decimal("0.000008")
    assert not caught.value.fallback_used
    assert create.await_count == 1
    assert len(capacity.releases) == 1
    assert len(capacity.records) == 1
    rendered = captured_traceback(caught.value) + caplog.text + repr(caught.value.evidence)
    for private in (API_KEY, PROMPT, RAW_ERROR):
        assert private not in rendered


async def test_close_cancellation_preserves_only_actual_rejected_receipt() -> None:
    client, _, create, close = mock_transport(result=sdk_response(tool_calls=[tool()]))
    entered, release = asyncio.Event(), asyncio.Event()

    async def block() -> None:
        entered.set()
        await release.wait()

    close.side_effect = block
    task = asyncio.create_task(client.complete(deployment(), request(), API_KEY))
    await entered.wait()
    task.cancel()
    release.set()
    with pytest.raises(ModelResponseCancelled) as caught:
        await task
    assert isinstance(caught.value.receipt, RejectedOutputEvidence)
    assert caught.value.receipt.usage == TokenUsage(2, 3, 5)
    assert not caught.value.receipt.correction_eligible
    assert create.await_count == 1


async def test_cancellation_before_response_does_not_invent_a_receipt() -> None:
    client, _, create, close = mock_transport()
    entered = asyncio.Event()

    async def block(**_: object) -> object:
        entered.set()
        await asyncio.Future[None]()
        return sdk_response()

    create.side_effect = block
    task = asyncio.create_task(client.complete(deployment(), request(), API_KEY))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError) as caught:
        await task
    assert not isinstance(caught.value, ModelResponseCancelled)
    assert not hasattr(caught.value, "receipt")
    assert create.await_count == 1
    assert close.await_count == 1


async def test_valid_chat_response_still_accepts_tools_and_usage() -> None:
    client, _, create, close = mock_transport(result=sdk_response(tool_calls=[tool("{}")]))
    response = await client.complete(deployment(), request(), API_KEY)
    assert response.text == "hello"
    assert response.usage == TokenUsage(2, 3, 5)
    assert len(response.tool_calls) == 1
    assert response.tool_calls[0].arguments == {}
    assert create.await_count == close.await_count == 1


@pytest.mark.parametrize(
    "usage", [None, SimpleNamespace(prompt_tokens=2, completion_tokens=3, total_tokens=99)]
)
async def test_gateway_rejection_without_valid_usage_remains_unaccounted(usage: object) -> None:
    response = sdk_response(tool_calls=[tool()])
    response.usage = usage  # type: ignore[attr-defined]
    client, _, create, _ = mock_transport(result=response)
    selected = deployment()
    capacity = CapacityStub([lease(selected.id, selected.quota_scope_id)])
    gateway = ModelGateway(
        ModelRegistry([selected]),
        capacity,
        SecretStub(capacity.events),
        client,
        pricing={selected.id: DeploymentPricing(Decimal(1), Decimal(2))},
    )
    with pytest.raises(GatewayRejectedOutput) as caught:
        await gateway.complete_with_context(request())
    assert caught.value.evidence is not None
    assert caught.value.evidence.usage is None
    assert caught.value.cost_usd is None
    assert not caught.value.evidence.correction_eligible
    assert create.await_count == len(capacity.releases) == 1


async def test_sdk_validation_body_is_not_a_received_accounting_receipt() -> None:
    failure = APIResponseValidationError(
        response=httpx.Response(200, request=httpx.Request("POST", "https://provider.example/v1")),
        body=sdk_response(tool_calls=[tool()]),
    )
    client, _, create, close = mock_transport(error=failure)
    with pytest.raises(ModelResponseError) as caught:
        await client.complete(deployment(), request(), API_KEY)
    assert caught.value.evidence is None
    assert create.await_count == 1
    assert close.await_count == 1
