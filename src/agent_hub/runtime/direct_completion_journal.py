"""Private, ordered Direct observations; never a tool or verification grant."""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import replace
from decimal import Decimal
from typing import cast
from uuid import UUID

from agent_hub.models.capacity import (
    CapacityBackendError,
    CapacityConfigurationError,
    CapacityQueueFull,
    CapacityRequestTooLarge,
    CapacityUnavailable,
    CapacityWaitTimeout,
)
from agent_hub.models.gateway import GatewayCompletion, preserve_gateway_scope_diagnostic
from agent_hub.models.types import JsonScalar as ModelJsonScalar
from agent_hub.models.types import JsonValue as ModelJsonValue
from agent_hub.models.types import ModelRequest, ModelResponse, TokenUsage, ToolCall
from agent_hub.runtime.artifacts import ArtifactRepository
from agent_hub.runtime.contracts import JsonValue
from agent_hub.runtime.instruction_context import model_request_sha256

_UNAVAILABLE = 'direct continuation is unavailable'
_CAPACITY_FAILURES = (
    CapacityRequestTooLarge, CapacityQueueFull, CapacityWaitTimeout,
    CapacityUnavailable, CapacityBackendError, CapacityConfigurationError,
)


def _response_payload(completion: GatewayCompletion) -> dict[str, JsonValue]:
    if type(completion) is not GatewayCompletion or completion.scope_diagnostic is not None:
        raise DirectJournalError(_UNAVAILABLE)
    response = completion.response
    usage = response.usage
    # Direct uses the textual workspace protocol, never native assistant history.
    # Persist only the visible response/tool rejection, never native reasoning.
    return {
        'text': response.text,
        'tools': tuple({'id': call.id, 'name': call.name, 'arguments': call.arguments}
                       for call in response.tool_calls),
        'usage': None if usage is None else {'prompt_tokens': usage.prompt_tokens,
            'completion_tokens': usage.completion_tokens, 'total_tokens': usage.total_tokens},
        'metadata': dict(response.provider_metadata), 'deployment': completion.deployment_id,
        'model': completion.logical_model, 'provider': completion.provider_id,
        'upstream': completion.provider_model,
        'cost': None if completion.cost_usd is None else str(completion.cost_usd),
        'fallback_used': completion.fallback_used, 'fallback_from': completion.fallback_from_logical_model,
        'fallback_reason': completion.fallback_reason,
        'attempted': completion.attempted_logical_models,
    }


def _response(value: JsonValue) -> GatewayCompletion:
    keys = {'text', 'tools', 'usage', 'metadata', 'deployment', 'model', 'provider', 'upstream',
            'cost', 'fallback_used', 'fallback_from', 'fallback_reason', 'attempted'}
    if (not isinstance(value, Mapping) or set(value) != keys
            or not isinstance(value['tools'], (list, tuple))
            or not isinstance(value['metadata'], Mapping)
            or not isinstance(value['attempted'], (list, tuple))
            or value['cost'] is not None and type(value['cost']) is not str):
        raise DirectJournalError(_UNAVAILABLE)
    calls = []
    for call in value['tools']:
        if not isinstance(call, Mapping) or set(call) != {'id', 'name', 'arguments'}:
            raise DirectJournalError(_UNAVAILABLE)
        calls.append(ToolCall(id=cast(str, call['id']), name=cast(str, call['name']),
                              arguments=cast(Mapping[str, ModelJsonValue], call['arguments'])))
    usage = value['usage']
    if usage is not None and (not isinstance(usage, Mapping)
                             or set(usage) != {'prompt_tokens', 'completion_tokens', 'total_tokens'}):
        raise DirectJournalError(_UNAVAILABLE)
    return GatewayCompletion(
        response=ModelResponse(text=cast(str | None, value['text']), tool_calls=tuple(calls),
            usage=None if usage is None else TokenUsage(
                prompt_tokens=cast(int, usage['prompt_tokens']),
                completion_tokens=cast(int, usage['completion_tokens']),
                total_tokens=cast(int, usage['total_tokens'])),
            provider_metadata=cast(Mapping[str, ModelJsonScalar], value['metadata'])),
        deployment_id=cast(str, value['deployment']), logical_model=cast(str, value['model']),
        provider_id=cast(str, value['provider']), provider_model=cast(str, value['upstream']),
        cost_usd=None if value['cost'] is None else Decimal(value['cost']),
        fallback_used=cast(bool, value['fallback_used']),
        fallback_from_logical_model=cast(str | None, value['fallback_from']),
        fallback_reason=cast(str | None, value['fallback_reason']),
        attempted_logical_models=tuple(cast(tuple[str, ...], value['attempted'])),
    )


class DirectJournalError(RuntimeError):
    pass


class DirectCompletionJournal:
    def __init__(self, repository: ArtifactRepository, *, tenant_id: UUID, run_id: UUID,
                 references: tuple[JsonValue, ...] = ()) -> None:
        if (type(tenant_id) is not UUID or type(run_id) is not UUID
                or type(references) is not tuple or len(references) > 16_384
                or any(not isinstance(item, Mapping) for item in references)):
            raise DirectJournalError(_UNAVAILABLE)
        self._repository, self._tenant, self._run = repository, tenant_id, run_id
        self._references = list(references)
        self._cursor = 0
        self._incomplete = False

    @property
    def references(self) -> tuple[JsonValue, ...]:
        return tuple(self._references)

    @property
    def replay_complete(self) -> bool:
        return not self._incomplete and self._cursor == len(self._references)

    async def complete(self, request: ModelRequest,
                       provider: Callable[[ModelRequest], Awaitable[GatewayCompletion]]) -> GatewayCompletion:
        from agent_hub.runtime.continuation_artifacts import load_continuation, save_continuation

        if self._incomplete or request.native_tool_continuation:
            raise DirectJournalError(_UNAVAILABLE)
        request_sha = model_request_sha256(replace(request, timeout_seconds=1.0))
        capacity_failure: Exception | None = None
        try:
            if self._cursor < len(self._references):
                value = await load_continuation(self._repository, tenant_id=self._tenant, run_id=self._run,
                    reference=cast(Mapping[str, JsonValue], self._references[self._cursor]))
                if (not isinstance(value, Mapping) or set(value) != {'position', 'request_sha256', 'response'}
                        or type(value['position']) is not int or value['position'] != self._cursor
                        or value['request_sha256'] != request_sha):
                    raise DirectJournalError(_UNAVAILABLE)
                result = _response(value['response'])
            else:
                result = await provider(request)
                ref = await save_continuation(self._repository, tenant_id=self._tenant, run_id=self._run,
                    value={'position': self._cursor, 'request_sha256': request_sha,
                           'response': _response_payload(result)})
                self._references.append(cast(JsonValue, ref))
            self._cursor += 1
            return result
        except asyncio.CancelledError:
            self._incomplete = True
            raise
        except Exception as error:  # noqa: BLE001 - never echo model/source/repository details.
            self._incomplete = True
            if any(type(error) is native for native in _CAPACITY_FAILURES):
                # Retain the issued failure class for diagnosis, never its private
                # body or a permission to replay an incomplete model operation.
                capacity_failure = type(error)('model capacity admission failed')
                preserve_gateway_scope_diagnostic(capacity_failure, error)
        if capacity_failure is not None:
            raise capacity_failure from None
        raise DirectJournalError(_UNAVAILABLE) from None
