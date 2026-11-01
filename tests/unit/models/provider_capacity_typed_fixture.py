"""Typed, synthetic provider/TPM seams. No live network, Redis or credentials."""
from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Literal, Protocol, TypedDict, TypeGuard
from uuid import UUID

import httpx

from agent_hub.config.repository import ConfigRevision, ConfigStatus
from agent_hub.config.service import ConfigService
from agent_hub.models.litellm_client import LiteLLMClient
from agent_hub.models.provider_diagnostics import ProviderDiagnostic as DiagnosticValue
from agent_hub.models.provider_diagnostics import RequestShape
from agent_hub.security.secrets import SecretReference, SecretService


class DiagnosticsAPI(Protocol):
    ProviderDiagnostic: type[DiagnosticValue]

    def make_request_shape(self, payload: object, protocol: str, *, send_ordinal: int,
                           native_continuation: bool = False) -> RequestShape | None: ...

    def capture_error(self, error: object, shape: RequestShape | None,
                      previous: DiagnosticValue | None = None) -> DiagnosticValue | None: ...

    def capture_response(self, response: object, shape: RequestShape | None,
                         previous: DiagnosticValue | None = None) -> DiagnosticValue | None: ...

    def get_provider_diagnostic(self, error: object) -> DiagnosticValue | None: ...


def present[T](value: T | None) -> T:
    assert value is not None
    return value


def _dictionary(value: object) -> TypeGuard[dict[str, object]]:
    return isinstance(value, dict) and all(isinstance(key, str) for key in value)


def mutable(value: object) -> dict[str, object]:
    assert _dictionary(value)
    return value


def _sequence(value: object) -> TypeGuard[Sequence[object]]:
    return isinstance(value, list | tuple)


def sequence(value: object) -> Sequence[object]:
    assert _sequence(value)
    return value


def rows(value: object) -> list[dict[str, object]]:
    return [mutable(item) for item in sequence(value)]


def at(value: object, *path: str | int) -> object:
    for key in path:
        value = sequence(value)[key] if isinstance(key, int) else mutable(value)[key]
    return value


def integer(value: object) -> int:
    assert type(value) is int
    return value


def set_field(instance: object, field: str, value: object) -> None:
    """Exercise the real attribute/frozen-dataclass guard with malformed input."""
    setattr(instance, field, value)


type HTTPHandler = Callable[[httpx.Request], httpx.Response]


def mock_client(handle: HTTPHandler) -> LiteLLMClient:
    # Use the production SDK/httpx construction path with a MockTransport delegate.
    # Both protocols remain real adapters; only their network transport is synthetic.
    return LiteLLMClient(_network_delegate_factory=lambda: httpx.MockTransport(handle))


class DeepseekDefinition(TypedDict):
    provider: str
    model: str
    credential_ref: str
    quota_scope_id: str
    context_window_tokens: int
    max_output_tokens: int
    tpm: int
    rpm: int
    max_concurrency: int
    target_utilization: float
    capabilities: list[str]


class FixtureConfig(ConfigService):
    def __init__(self, document: dict[str, object]) -> None:
        self.document = document

    async def get_current(self, tenant_id: UUID) -> ConfigRevision:
        return ConfigRevision(id=UUID(int=1), tenant_id=tenant_id, version=1,
            status=ConfigStatus.PUBLISHED, document=self.document, created_by=None,
            created_at=datetime(2026, 10, 28, tzinfo=UTC))


class FixtureSecrets(SecretService):
    def __init__(self) -> None:
        pass

    async def resolve(self, tenant_id: UUID, reference: SecretReference | UUID | str) -> str:
        return 'fixture-key'


type Acquisition = tuple[Literal['acquire'], tuple[str, ...], float, int | Mapping[str, int]]


def _estimates(value: object) -> TypeGuard[Mapping[str, int]]:
    return isinstance(value, Mapping) and all(isinstance(k, str) and type(v) is int for k, v in value.items())


def _acquisition(value: object) -> TypeGuard[Acquisition]:
    if not isinstance(value, tuple) or len(value) != 4 or value[0] != 'acquire':
        return False
    return (isinstance(value[1], tuple) and all(isinstance(item, str) for item in value[1])
            and isinstance(value[2], int | float) and not isinstance(value[2], bool)
            and (type(value[3]) is int or _estimates(value[3])))


def acquisition(value: object) -> Acquisition:
    assert _acquisition(value)
    return value


def event_name(value: object) -> str:
    assert isinstance(value, tuple) and value and isinstance(value[0], str)
    return value[0]
