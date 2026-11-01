"""Concrete shapes of synthetic correction receipts; no grant or fabricated row."""
from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import replace
from typing import Literal, TypedDict, TypeGuard

import httpx

from agent_hub.models.litellm_client import LiteLLMClient, _OpenAIClient
from agent_hub.models.native_messages import NativeAssistantMessage, NativeToolCall
from agent_hub.models.types import ModelResponse
from agent_hub.runtime.contracts import Artifact, JsonValue, RuntimeCheckpoint
from agent_hub.runtime.crew import adapter


class Reference(TypedDict):
    id: str
    sha256: str


class RecoveryLink(TypedDict, total=False):
    version: int
    source_key: str
    source_request_sha256: str
    source_receipt_sha256: str
    source_cap: int


class ModelRow(TypedDict, total=False):
    attempt: int
    call_index: int
    status: str
    request_sha256: str
    provider_continuation: Reference | None
    required_tool_initial_incomplete: int
    initial_incomplete_recovery: RecoveryLink


class RepairRow(TypedDict, total=False):
    version: int
    status: str
    correction_key: str
    tool_history_sha256: str
    source_receipt_sha256: str
    source_request_sha256: str
    correction_request_sha256: str
    mode: str
    actor: str


type RepairField = Literal['tool_history_sha256', 'source_receipt_sha256', 'source_request_sha256', 'correction_request_sha256', 'mode', 'actor']


class RejectionRow(TypedDict, total=False):
    usage_status: str
    usage: object
    output_status: str
    final_text: str | None


class ToolRow(TypedDict, total=False):
    round: int


class UsageRow(TypedDict):
    tokens: int
    cost_usd: str


class State(TypedDict, total=False):
    phase: str
    required_tool_correction_version: int
    models: dict[str, ModelRow]
    structured_repairs: dict[str, RepairRow]
    rejected_outputs: dict[str, RejectionRow]
    tools: dict[str, ToolRow]
    usage: UsageRow


def _object(value: object) -> TypeGuard[dict[str, object]]:
    return isinstance(value, dict) and all(isinstance(k, str) for k in value)


def mutable(value: object) -> dict[str, object]:
    assert _object(value)
    return value


def _table[T](value: object, check: Callable[[object], TypeGuard[T]]) -> TypeGuard[dict[str, T]]:
    return _object(value) and all(check(row) for row in value.values())


def _strings(value: object) -> TypeGuard[dict[str, str]]:
    return _object(value) and all(isinstance(item, str) for item in value.values())


def _reference(value: object) -> TypeGuard[Reference]:
    return _strings(value) and {'id', 'sha256'} <= value.keys()


def _recovery(value: object) -> TypeGuard[RecoveryLink]:
    return _object(value) and all(
        key not in value or (type(value[key]) is int if key in {'version', 'source_cap'} else isinstance(value[key], str))
        for key in ('version', 'source_key', 'source_request_sha256', 'source_receipt_sha256', 'source_cap')
    )


def _model(value: object) -> TypeGuard[ModelRow]:
    if not _object(value):
        return False
    for key in ('attempt', 'call_index', 'required_tool_initial_incomplete'):
        if key in value and type(value[key]) is not int:
            return False
    for key in ('status', 'request_sha256'):
        if key in value and not isinstance(value[key], str):
            return False
    if 'provider_continuation' in value and value['provider_continuation'] is not None and not _reference(value['provider_continuation']):
        return False
    return 'initial_incomplete_recovery' not in value or _recovery(value['initial_incomplete_recovery'])


def _repair(value: object) -> TypeGuard[RepairRow]:
    if not _object(value):
        return False
    return all(key not in value or (type(value[key]) is int if key == 'version' else isinstance(value[key], str))
               for key in ('version', 'status', 'correction_key', 'tool_history_sha256', 'source_receipt_sha256', 'source_request_sha256', 'correction_request_sha256', 'mode', 'actor'))


def _rejection(value: object) -> TypeGuard[RejectionRow]:
    if not _object(value):
        return False
    return (all(key not in value or isinstance(value[key], str) for key in ('usage_status', 'output_status'))
            and ('final_text' not in value or value['final_text'] is None or isinstance(value['final_text'], str)))


def _tool(value: object) -> TypeGuard[ToolRow]:
    return _object(value) and ('round' not in value or type(value['round']) is int)


def _usage(value: object) -> TypeGuard[UsageRow]:
    return _object(value) and type(value.get('tokens')) is int and isinstance(value.get('cost_usd'), str)


def _state(value: object) -> TypeGuard[State]:
    if not _object(value):
        return False
    checks: dict[str, Callable[[object], bool]] = {
        'phase': lambda item: isinstance(item, str),
        'required_tool_correction_version': lambda item: type(item) is int,
        'models': lambda item: _table(item, _model),
        'structured_repairs': lambda item: _table(item, _repair),
        'rejected_outputs': lambda item: _table(item, _rejection),
        'tools': lambda item: _table(item, _tool),
        'usage': _usage,
    }
    return all(key not in value or check(value[key]) for key, check in checks.items())


def checked_state(value: object) -> State:
    assert _state(value)
    return value


def state_of(checkpoint: RuntimeCheckpoint) -> State:
    # Public state is immutable; assertions read an equivalent serialized copy.
    return checked_state(checkpoint.to_payload()['state'])


def _object_list(value: object) -> TypeGuard[list[dict[str, object]]]:
    return isinstance(value, list) and all(_object(item) for item in value)


def object_list(value: object) -> list[dict[str, object]]:
    assert _object_list(value)
    return value


def native(response: ModelResponse) -> ModelResponse:
    return replace(response, native_assistant=NativeAssistantMessage(
        'primary', response.text,
        tuple(NativeToolCall(call.id, call.name.replace('.', '_'), json.dumps(dict(call.arguments)))
              for call in response.tool_calls),
        reasoning_content='PRIVATE_NATIVE_CONTINUATION',
    ))


class LocalSDKTransport(LiteLLMClient):
    """Real SDK construction/decoding and close; all HTTP remains MockTransport."""
    def __init__(self, handle: Callable[[httpx.Request], httpx.Response]) -> None:
        super().__init__(_network_delegate_factory=lambda: httpx.MockTransport(handle))
        self.clients: list[_OpenAIClient] = []

    def _new_openai_client(self, *, api_key: str, base_url: str, max_retries: int) -> _OpenAIClient:
        assert max_retries == 0
        client = super()._new_openai_client(api_key=api_key, base_url=base_url, max_retries=max_retries)
        self.clients.append(client)
        return client


class ExecuteOptions(TypedDict):
    purpose: Literal['step', 'review']
    attempt: int
    cursor: adapter._ModelCallCursor
    ledger: adapter._ModelLedger
    sources: tuple[Artifact, ...]
    emit: adapter.EventEmitter
    model_boundary: adapter.ModelStateBoundary
    usage_boundary: adapter.UsageBoundary
    run_state: adapter._RunState
    step_deadline: float


class OptionalExecuteOptions(ExecuteOptions, total=False):
    repair: Mapping[str, JsonValue] | None
    tool_ledger: adapter._ToolLedger | None
    allow_structured_repair: bool
    reserved_max_output_tokens: int | None
    initial_incomplete_chain: Mapping[str, JsonValue] | None
    post_tool_growth: Mapping[str, JsonValue] | None
    initial_recovery_link: Mapping[str, JsonValue] | None
