"""Network diagnostics retain finite kinds, never private transport bodies."""
import socket
from dataclasses import FrozenInstanceError, replace

import httpx
import pytest
from openai import APIConnectionError, APITimeoutError

from agent_hub.models.litellm_client import (
    ModelClientError,
    ModelResponseError,
    ModelTransportError,
    safe_model_client_error,
)
from agent_hub.models.provider_diagnostics import get_provider_diagnostic
from agent_hub.models.transport_diagnostics import (
    TransportFailureDiagnostic,
    TransportFailureKind,
    _capture_transport_diagnostic,
    _is_issued_transport_diagnostic,
    get_transport_diagnostic,
    preserve_transport_diagnostic,
)

SECRET = 'private-prompt api-key=DO_NOT_RETAIN https://secret.invalid/path'


def sdk_connection() -> APIConnectionError:
    return APIConnectionError(request=httpx.Request('POST', 'https://example.invalid'))


def test_wrapped_read_timeout_keeps_diagnostic_after_redaction() -> None:
    raw = APIConnectionError(request=httpx.Request('POST', 'https://example.invalid'))
    raw.__cause__ = httpx.ReadTimeout('private prompt api-key secret')
    safe = safe_model_client_error('test-deployment', raw, ('secret',))
    assert type(safe) is ModelTransportError
    assert safe.status_code is None
    assert safe.args == ("model transport failed for deployment 'test-deployment'",)
    diagnostic = get_transport_diagnostic(safe)
    assert diagnostic is not None and diagnostic.kind is TransportFailureKind.READ_TIMEOUT


@pytest.mark.parametrize(('raw', 'kind'), [
    (httpx.ReadTimeout(SECRET), TransportFailureKind.READ_TIMEOUT),
    (httpx.ConnectTimeout(SECRET), TransportFailureKind.CONNECT_TIMEOUT),
    (httpx.WriteTimeout(SECRET), TransportFailureKind.WRITE_TIMEOUT),
    (httpx.PoolTimeout(SECRET), TransportFailureKind.POOL_TIMEOUT),
    (httpx.TimeoutException(SECRET), TransportFailureKind.TIMEOUT),
    (TimeoutError(SECRET), TransportFailureKind.TIMEOUT),
    (httpx.ConnectError(SECRET), TransportFailureKind.CONNECT_ERROR),
    (httpx.ReadError(SECRET), TransportFailureKind.READ_ERROR),
    (httpx.WriteError(SECRET), TransportFailureKind.WRITE_ERROR),
    (httpx.RemoteProtocolError(SECRET), TransportFailureKind.REMOTE_PROTOCOL_ERROR),
    (httpx.ProxyError(SECRET), TransportFailureKind.PROXY_ERROR),
    (socket.gaierror(SECRET), TransportFailureKind.DNS_ERROR),
    (ConnectionError(SECRET), TransportFailureKind.CONNECTION_ERROR),
    (ConnectionResetError(SECRET), TransportFailureKind.CONNECTION_ERROR),
    (ConnectionRefusedError(SECRET), TransportFailureKind.CONNECTION_ERROR),
    (ConnectionAbortedError(SECRET), TransportFailureKind.CONNECTION_ERROR),
    (BrokenPipeError(SECRET), TransportFailureKind.CONNECTION_ERROR),
])
def test_finite_network_kinds_without_status(raw: Exception, kind: TransportFailureKind) -> None:
    safe = safe_model_client_error('test-deployment', raw, (SECRET,))
    assert type(safe) is ModelTransportError and safe.status_code is None
    diagnostic = get_transport_diagnostic(safe)
    assert diagnostic is not None and diagnostic.kind is kind
    assert SECRET not in repr(diagnostic) + repr(safe) + repr(safe.__dict__)
    assert safe.__cause__ is None and safe.__context__ is None and safe.__traceback__ is None
    assert not hasattr(diagnostic, '__dict__')


def test_sdk_timeout_default_and_more_precise_httpx_cause() -> None:
    raw = APITimeoutError(request=httpx.Request('POST', 'https://example.invalid'))
    generic = get_transport_diagnostic(safe_model_client_error('test', raw, ()))
    assert generic is not None and generic.kind is TransportFailureKind.TIMEOUT
    raw.__cause__ = httpx.WriteTimeout(SECRET)
    precise = get_transport_diagnostic(safe_model_client_error('test', raw, ()))
    assert precise is not None and precise.kind is TransportFailureKind.WRITE_TIMEOUT


def test_sdk_connection_without_cause_has_only_generic_connection_kind() -> None:
    safe = safe_model_client_error('test', sdk_connection(), ())
    diagnostic = get_transport_diagnostic(safe)
    assert diagnostic is not None and diagnostic.kind is TransportFailureKind.CONNECTION_ERROR


def test_type_error_is_not_network_even_with_network_message_or_cause() -> None:
    raw = TypeError('ReadTimeout ' + SECRET)
    raw.__cause__ = httpx.ReadTimeout(SECRET)
    safe = safe_model_client_error('test', raw, (SECRET,))
    assert type(safe) is ModelClientError
    assert safe.args == ('model client internal failure',)
    assert get_transport_diagnostic(safe) is None
    wrapped = sdk_connection()
    wrapped.__cause__ = raw
    safe_wrapped = safe_model_client_error('test', wrapped, (SECRET,))
    assert type(safe_wrapped) is ModelClientError
    assert get_transport_diagnostic(safe_wrapped) is None


def test_custom_network_subclass_does_not_invent_fixed_category() -> None:
    class UntrustedTimeout(httpx.ReadTimeout):
        pass

    safe = safe_model_client_error('test', UntrustedTimeout(SECRET), (SECRET,))
    assert type(safe) is ModelTransportError  # Original error classification remains.
    assert get_transport_diagnostic(safe) is None


def test_hostile_exception_metaclass_cannot_replace_primary_safe_error() -> None:
    class HostileMeta(type):
        def __hash__(cls) -> int:
            raise AssertionError('untrusted class hash')

        def __eq__(cls, other: object) -> bool:
            raise AssertionError('untrusted class equality')

    class HostileNetwork(httpx.ReadError, metaclass=HostileMeta):
        pass

    raw = HostileNetwork(SECRET)
    target = RuntimeError('safe')
    _capture_transport_diagnostic(target, raw)
    assert get_transport_diagnostic(target) is None
    safe = safe_model_client_error('test', raw, (SECRET,))
    assert type(safe) is ModelTransportError
    assert get_transport_diagnostic(safe) is None
    assert SECRET not in str(safe)


@pytest.mark.parametrize(('depth', 'kind'), [
    (7, TransportFailureKind.READ_TIMEOUT),
    (8, TransportFailureKind.CONNECTION_ERROR),
    (64, TransportFailureKind.CONNECTION_ERROR),
])
def test_sdk_cause_walk_is_bounded(depth: int, kind: TransportFailureKind) -> None:
    raw: BaseException = httpx.ReadTimeout(SECRET)
    for _ in range(depth):
        wrapper = sdk_connection()
        wrapper.__cause__ = raw
        raw = wrapper
    safe = RuntimeError('safe')
    _capture_transport_diagnostic(safe, raw)
    diagnostic = get_transport_diagnostic(safe)
    assert diagnostic is not None and diagnostic.kind is kind


def test_cyclic_causes_terminate_without_holding_the_raw_error() -> None:
    raw = sdk_connection()
    raw.__cause__ = raw
    safe = RuntimeError('safe')
    _capture_transport_diagnostic(safe, raw)
    diagnostic = get_transport_diagnostic(safe)
    assert diagnostic is not None and diagnostic.kind is TransportFailureKind.CONNECTION_ERROR
    assert diagnostic._owner_id == id(safe)
    assert safe.__cause__ is None


def test_forged_or_copied_owner_bindings_are_rejected() -> None:
    safe = safe_model_client_error('test', httpx.ReadTimeout(SECRET), ())
    signed = get_transport_diagnostic(safe)
    assert signed is not None
    target = RuntimeError('target')
    original = safe.__dict__['_issued_transport_diagnostic']
    assert type(original) is tuple
    target.__dict__['_issued_transport_diagnostic'] = original
    assert get_transport_diagnostic(target) is None
    target.__dict__['_issued_transport_diagnostic'] = (original[0], target, signed)
    assert get_transport_diagnostic(target) is None  # Even relabeling the tuple cannot transfer it.
    unsigned = TransportFailureDiagnostic(TransportFailureKind.READ_TIMEOUT)
    target.__dict__['_issued_transport_diagnostic'] = (original[0], target, unsigned)
    assert get_transport_diagnostic(target) is None
    target.__dict__['_issued_transport_diagnostic'] = (
        original[0], target, replace(signed, kind=TransportFailureKind.CONNECT_ERROR))
    assert get_transport_diagnostic(target) is None
    target.__dict__['_issued_transport_diagnostic'] = (object(), target, signed)
    assert get_transport_diagnostic(target) is None


def test_preserve_reissues_only_kind_and_keeps_first_target_diagnostic() -> None:
    first = safe_model_client_error('test', httpx.ReadTimeout(SECRET), ())
    second = safe_model_client_error('test', httpx.ConnectTimeout(SECRET), ())
    target = RuntimeError('safe wrapper')
    preserve_transport_diagnostic(target, first)
    copied = get_transport_diagnostic(target)
    assert copied is not None and copied.kind is TransportFailureKind.READ_TIMEOUT
    assert copied is not get_transport_diagnostic(first)
    preserve_transport_diagnostic(target, second)
    assert get_transport_diagnostic(target) is copied
    preserve_transport_diagnostic(target, object())
    assert get_transport_diagnostic(target) is copied
    with pytest.raises(FrozenInstanceError):
        copied.__setattr__('kind', TransportFailureKind.CONNECT_ERROR)


def test_bare_metadata_requires_issuer_enum_and_positive_owner_without_error() -> None:
    safe = safe_model_client_error('test', httpx.ReadTimeout(SECRET), ())
    diagnostic = get_transport_diagnostic(safe)
    assert diagnostic is not None and _is_issued_transport_diagnostic(diagnostic)
    assert not _is_issued_transport_diagnostic(replace(diagnostic))
    assert not _is_issued_transport_diagnostic(TransportFailureDiagnostic(TransportFailureKind.READ_TIMEOUT))
    assert not _is_issued_transport_diagnostic(object.__new__(TransportFailureDiagnostic))
    assert not _is_issued_transport_diagnostic({'kind': 'read_timeout'})
    object.__setattr__(diagnostic, '_owner_id', 0)
    assert not _is_issued_transport_diagnostic(diagnostic)
    assert get_transport_diagnostic(safe) is None


def test_bare_metadata_rejects_string_kind_even_with_original_issuer() -> None:
    safe = safe_model_client_error('test', httpx.ReadTimeout(SECRET), ())
    diagnostic = get_transport_diagnostic(safe)
    assert diagnostic is not None
    object.__setattr__(diagnostic, 'kind', 'read_timeout')
    assert not _is_issued_transport_diagnostic(diagnostic)
    assert get_transport_diagnostic(safe) is None


def test_malicious_attributes_and_descriptors_are_not_evaluated() -> None:
    class Trap(RuntimeError):
        def __getattribute__(self, name: str) -> object:
            raise AssertionError('untrusted attribute access')

        @property
        def __dict__(self) -> dict[str, object]:
            raise AssertionError('untrusted dictionary descriptor')

        @__dict__.setter
        def __dict__(self, value: dict[str, object]) -> None:
            raise AssertionError('untrusted dictionary setter')

        @property
        def __cause__(self) -> BaseException | None:
            raise AssertionError('untrusted cause descriptor')

        @__cause__.setter
        def __cause__(self, value: BaseException | None) -> None:
            raise AssertionError('untrusted cause setter')

    trap = Trap(SECRET)
    assert get_transport_diagnostic(trap) is None
    assert get_transport_diagnostic(object()) is None
    safe = safe_model_client_error('test', httpx.ReadTimeout(SECRET), ())
    preserve_transport_diagnostic(trap, safe)
    diagnostic = get_transport_diagnostic(trap)
    assert diagnostic is not None and diagnostic.kind is TransportFailureKind.READ_TIMEOUT


def test_known_http_status_and_provider_diagnostic_survive_rewrapping() -> None:
    request = httpx.Request('POST', 'https://example.invalid')
    response = httpx.Response(503, request=request, json={'error': {'type': 'server_error'}})
    raw = httpx.HTTPStatusError(SECRET, request=request, response=response)
    safe = safe_model_client_error('test', raw, (SECRET,))
    assert type(safe) is ModelTransportError and safe.status_code == 503
    provider = get_provider_diagnostic(safe)
    assert provider is not None
    assert get_transport_diagnostic(safe) is None  # HTTP status alone is not a network category.
    network = safe_model_client_error('test', httpx.ReadTimeout(SECRET), ())
    preserve_transport_diagnostic(safe, network)
    again = safe_model_client_error('test', safe, ())
    assert type(again) is ModelTransportError
    assert again.args == safe.args and again.status_code == 503
    assert get_provider_diagnostic(again) == provider
    diagnostic = get_transport_diagnostic(again)
    assert diagnostic is not None and diagnostic.kind is TransportFailureKind.READ_TIMEOUT


def test_response_error_keeps_public_args_status_and_signed_diagnostic() -> None:
    source = ModelResponseError('model response rejected', status_code=400)
    network = safe_model_client_error('test', httpx.ReadError(SECRET), ())
    preserve_transport_diagnostic(source, network)
    safe = safe_model_client_error('test', source, ())
    assert type(safe) is ModelResponseError and safe.status_code == 400
    assert safe.args == ('model response rejected',)
    diagnostic = get_transport_diagnostic(safe)
    assert diagnostic is not None and diagnostic.kind is TransportFailureKind.READ_ERROR
