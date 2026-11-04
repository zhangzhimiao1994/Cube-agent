"""Issuer-bound network kinds; never receipts, request data or retry authority."""
import socket
from contextlib import suppress
from dataclasses import dataclass, field
from enum import StrEnum

import httpx
from openai import APIConnectionError, APITimeoutError


class TransportFailureKind(StrEnum):
    READ_TIMEOUT = 'read_timeout'
    CONNECT_TIMEOUT = 'connect_timeout'
    WRITE_TIMEOUT = 'write_timeout'
    POOL_TIMEOUT = 'pool_timeout'
    TIMEOUT = 'timeout'
    CONNECT_ERROR = 'connect_error'
    READ_ERROR = 'read_error'
    WRITE_ERROR = 'write_error'
    REMOTE_PROTOCOL_ERROR = 'remote_protocol_error'
    PROXY_ERROR = 'proxy_error'
    DNS_ERROR = 'dns_error'
    CONNECTION_ERROR = 'connection_error'


_ISSUER = object()
_ATTRIBUTE = '_issued_transport_diagnostic'
_MAX_CAUSE_DEPTH = 8
_ERROR_DICT = BaseException.__dict__['__dict__']
_ERROR_CAUSE = BaseException.__dict__['__cause__']
_KINDS: tuple[tuple[type[BaseException], TransportFailureKind], ...] = (
    (httpx.ReadTimeout, TransportFailureKind.READ_TIMEOUT),
    (httpx.ConnectTimeout, TransportFailureKind.CONNECT_TIMEOUT),
    (httpx.WriteTimeout, TransportFailureKind.WRITE_TIMEOUT),
    (httpx.PoolTimeout, TransportFailureKind.POOL_TIMEOUT),
    (httpx.TimeoutException, TransportFailureKind.TIMEOUT),
    (TimeoutError, TransportFailureKind.TIMEOUT),
    (httpx.ConnectError, TransportFailureKind.CONNECT_ERROR),
    (httpx.ReadError, TransportFailureKind.READ_ERROR),
    (httpx.WriteError, TransportFailureKind.WRITE_ERROR),
    (httpx.RemoteProtocolError, TransportFailureKind.REMOTE_PROTOCOL_ERROR),
    (httpx.ProxyError, TransportFailureKind.PROXY_ERROR),
    (socket.gaierror, TransportFailureKind.DNS_ERROR),
    (ConnectionError, TransportFailureKind.CONNECTION_ERROR),
    (ConnectionAbortedError, TransportFailureKind.CONNECTION_ERROR),
    (ConnectionRefusedError, TransportFailureKind.CONNECTION_ERROR),
    (ConnectionResetError, TransportFailureKind.CONNECTION_ERROR),
    (BrokenPipeError, TransportFailureKind.CONNECTION_ERROR),
)


@dataclass(frozen=True, slots=True)
class TransportFailureDiagnostic:
    kind: TransportFailureKind
    _issuer: object | None = field(default=None, init=False, repr=False, compare=False)
    _owner_id: int = field(default=0, init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if type(self.kind) is not TransportFailureKind:
            raise ValueError('invalid transport diagnostic')


def _namespace(error: BaseException) -> dict[str, object] | None:
    # Base descriptors bypass injected __getattribute__/__dict__ properties.
    namespace: object = _ERROR_DICT.__get__(error)
    return namespace if isinstance(namespace, dict) else None


def _exception_cause(error: BaseException) -> BaseException | None:
    cause: object = _ERROR_CAUSE.__get__(error)
    return cause if isinstance(cause, BaseException) else None


def _is_issued_transport_diagnostic(value: object) -> bool:
    """Validate bare metadata without retaining or accepting an error reference."""
    with suppress(BaseException):  # Optional metadata never replaces a primary outcome.
        return (type(value) is TransportFailureDiagnostic and value._issuer is _ISSUER
                and type(value.kind) is TransportFailureKind
                and type(value._owner_id) is int and value._owner_id > 0)
    return False


def get_transport_diagnostic(error: object) -> TransportFailureDiagnostic | None:
    with suppress(BaseException):  # Optional metadata never replaces a primary outcome.
        if not isinstance(error, BaseException):
            return None
        namespace = _namespace(error)
        binding = None if namespace is None else namespace.get(_ATTRIBUTE)
        if (type(binding) is not tuple or len(binding) != 3
                or binding[0] is not _ISSUER or binding[1] is not error):
            return None
        diagnostic = binding[2]
        if (type(diagnostic) is not TransportFailureDiagnostic
                or not _is_issued_transport_diagnostic(diagnostic)
                or diagnostic._owner_id != id(error)):
            return None
        return diagnostic
    return None


def _issue(target: BaseException, kind: TransportFailureKind) -> None:
    with suppress(BaseException):  # Optional metadata never replaces a primary outcome.
        namespace = _namespace(target)
        if namespace is None or get_transport_diagnostic(target) is not None:
            return
        diagnostic = TransportFailureDiagnostic(kind)
        object.__setattr__(diagnostic, '_issuer', _ISSUER)
        object.__setattr__(diagnostic, '_owner_id', id(target))
        namespace[_ATTRIBUTE] = (_ISSUER, target, diagnostic)


def preserve_transport_diagnostic(target: BaseException, source: object) -> None:
    """Reissue only a validated kind for the target; never copy an owner binding."""
    diagnostic = get_transport_diagnostic(source)
    if diagnostic is not None:
        _issue(target, diagnostic.kind)


def _network_kind(error: BaseException) -> TransportFailureKind | None:
    current: BaseException | None = error
    seen: set[int] = set()
    fallback: TransportFailureKind | None = None
    for _ in range(_MAX_CAUSE_DEPTH):
        if current is None or id(current) in seen:
            break
        seen.add(id(current))
        for error_type, kind in _KINDS:
            if type(current) is error_type:
                return kind
        # Only exact official SDK wrappers allow looking through a cause. An
        # unrelated TypeError with a network-looking message/cause is no proof.
        if type(current) is APITimeoutError:
            fallback = fallback or TransportFailureKind.TIMEOUT
        elif type(current) is APIConnectionError:
            fallback = fallback or TransportFailureKind.CONNECTION_ERROR
        else:
            break
        current = _exception_cause(current)
    return fallback


def _capture_transport_diagnostic(target: BaseException, source: BaseException) -> None:
    with suppress(BaseException):  # Optional metadata never replaces a primary outcome.
        preserve_transport_diagnostic(target, source)
        if get_transport_diagnostic(target) is None:
            kind = _network_kind(source)
            if kind is not None:
                _issue(target, kind)
