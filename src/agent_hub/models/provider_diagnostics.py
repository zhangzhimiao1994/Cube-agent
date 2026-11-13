"""Finite provider rejection evidence; never exception/body/request text or retry authority."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, replace
from typing import cast

import httpx
from openai import (
    APIStatusError,
    AuthenticationError,
    BadRequestError,
    ConflictError,
    InternalServerError,
    NotFoundError,
    PermissionDeniedError,
    RateLimitError,
    UnprocessableEntityError,
)

_ROLES = ("system", "developer", "user", "assistant", "tool")
_PROTOCOLS = frozenset({"chat_completions", "responses", "messages"})
_TOKEN_FIELDS = frozenset({"max_completion_tokens", "max_tokens", "max_output_tokens"})
_PROFILE_FIELDS = frozenset({"last_role", "last_content_kind", "last_content_empty", "last_assistant_prefix",
                             "empty_content_role_counts", "native_reasoning_field_count", "empty_native_reasoning_field_count"})
_CONTENT_KINDS = frozenset({"text", "null", "parts", "absent", "other"})
_CLASSES = frozenset({"unknown", "unsupported_parameter", "invalid_parameter_value", "context_window",
                      "tool_history", "structured_output", "policy", "model_route", "tool_choice_thinking", "assistant_turn"})
_PARAMETERS = _TOKEN_FIELDS | {"unknown", "tools", "tool_choice", "messages", "input", "response_format", "text", "model", "reasoning"}
_SDK_FAMILIES: dict[type[object], str] = {
    BadRequestError: "sdk_bad_request", AuthenticationError: "sdk_authentication",
    PermissionDeniedError: "sdk_permission", RateLimitError: "sdk_rate_limit",
    NotFoundError: "sdk_http_status", ConflictError: "sdk_http_status",
    UnprocessableEntityError: "sdk_http_status", InternalServerError: "sdk_http_status",
    APIStatusError: "sdk_http_status",
}
_FAMILIES = frozenset(_SDK_FAMILIES.values()) | {"http_status"}
_CODE_CLASSES = {
    "unsupported_parameter": "unsupported_parameter", "invalid_parameter_error": "invalid_parameter_value",
    "invalid_value": "invalid_parameter_value", "context_length_exceeded": "context_window",
    "invalid_tool_calls": "tool_history", "tool_call_response_missing": "tool_history",
    "invalid_json_schema": "structured_output", "content_policy_violation": "policy", "model_not_found": "model_route",
}
_THINKING_REJECTION = "The tool_choice parameter does not support being set to required or object in thinking mode"
# Full provider-owned validation signature, not a generic invalid_request_error
# or a claim about C7's unavailable original rejection body.
_ASSISTANT_TURN_REJECTION = (
    "The last message of deepseek-reasoner must be a user message, or an assistant "
    "message with prefix mode on (refer to https://api-docs.deepseek.com/guides/chat_prefix_completion)."
)
_MAX_BODY = 16384
_ISSUED_DIAGNOSTIC = object()


def _need(value: object) -> None:
    if not value:
        raise ValueError("provider diagnostic is invalid")


def _exact(value: object, keys: set[str]) -> dict[str, object]:
    _need(type(value) is dict and set(value) == keys)
    return cast(dict[str, object], value)


def _count(value: object, maximum: int = 1_000_000) -> bool:
    return type(value) is int and 0 <= value <= maximum


@dataclass(frozen=True, slots=True)
class RequestShape:
    protocol: str
    send_ordinal: int
    stream: bool
    role_counts: tuple[int, ...]
    tool_count: int
    tool_choice: str
    schema_present: bool
    native_continuation: bool
    native_reasoning_present: bool
    native_tool_call_count: int
    output_token_parameter: str
    output_tokens: int | None
    last_role: str | None = None
    last_content_kind: str | None = None
    last_content_empty: bool | None = None
    last_assistant_prefix: bool | None = None
    empty_content_role_counts: tuple[int, ...] | None = None
    native_reasoning_field_count: int | None = None
    empty_native_reasoning_field_count: int | None = None

    def __post_init__(self) -> None:
        self._validate()

    def _validate(self) -> None:
        _need(type(self.protocol) is str and self.protocol in _PROTOCOLS
              and type(self.send_ordinal) is int and self.send_ordinal in (1, 2))
        _need(all(type(v) is bool for v in (self.stream, self.schema_present, self.native_continuation, self.native_reasoning_present)))
        _need(type(self.role_counts) is tuple and len(self.role_counts) == 5
              and all(_count(v) for v in self.role_counts) and sum(self.role_counts) <= 1_000_000
              and _count(self.tool_count) and _count(self.native_tool_call_count))
        _need(type(self.tool_choice) is str and self.tool_choice in {"none", "auto", "required", "named", "unknown"})
        _need(type(self.output_token_parameter) is str and self.output_token_parameter in _TOKEN_FIELDS | {"none"})
        allowed_tokens = {"chat_completions": {"none", "max_tokens", "max_completion_tokens"},
                          "responses": {"none", "max_output_tokens"}, "messages": {"none", "max_tokens"}}
        _need(self.output_token_parameter in allowed_tokens[self.protocol])
        _need((self.output_token_parameter == "none" and self.output_tokens is None)
              or (self.output_token_parameter != "none" and _count(self.output_tokens) and self.output_tokens != 0))
        _need(self.last_role is None or type(self.last_role) is str and self.last_role in _ROLES
              and self.role_counts[_ROLES.index(self.last_role)] > 0)
        _need(self.last_content_kind is None or type(self.last_content_kind) is str and self.last_content_kind in _CONTENT_KINDS)
        _need(all(v is None or type(v) is bool for v in (self.last_content_empty, self.last_assistant_prefix)))
        if self.last_assistant_prefix is True:
            _need(self.last_role == "assistant")
        profile = (self.last_role, self.last_content_kind, self.last_content_empty, self.last_assistant_prefix,
                   self.empty_content_role_counts, self.native_reasoning_field_count, self.empty_native_reasoning_field_count)
        _need(all(v is None for v in profile) or all(v is not None for v in profile))
        if self.empty_content_role_counts is not None:
            empty = self.empty_content_role_counts
            _need(type(empty) is tuple and len(empty) == 5
                  and all(_count(v, maximum=n) for v, n in zip(empty, self.role_counts, strict=True)))
            _need(_count(self.native_reasoning_field_count, maximum=self.role_counts[3])
                  and _count(self.empty_native_reasoning_field_count, maximum=cast(int, self.native_reasoning_field_count)))
            if self.last_content_empty is True:
                _need(empty[_ROLES.index(cast(str, self.last_role))] > 0)
            else:
                index = _ROLES.index(cast(str, self.last_role))
                _need(empty[index] < self.role_counts[index])
            if self.last_content_kind in {"null", "absent"}:
                _need(self.last_content_empty is True)

    def to_payload(self) -> dict[str, object]:
        self._validate()
        return {"protocol": self.protocol, "send_ordinal": self.send_ordinal, "stream": self.stream,
                "message_count": sum(self.role_counts), "role_counts": dict(zip(_ROLES, self.role_counts)),
                "tool_count": self.tool_count, "tool_choice": self.tool_choice, "schema_present": self.schema_present,
                "native_continuation": self.native_continuation, "native_reasoning_present": self.native_reasoning_present,
                "native_tool_call_count": self.native_tool_call_count, "output_token_parameter": self.output_token_parameter,
                "output_tokens": self.output_tokens,
                **{key: value for key, value in (("last_role", self.last_role), ("last_content_kind", self.last_content_kind),
                                                ("last_content_empty", self.last_content_empty),
                                                ("last_assistant_prefix", self.last_assistant_prefix),
                                                ("empty_content_role_counts", None if self.empty_content_role_counts is None
                                                 else dict(zip(_ROLES, self.empty_content_role_counts))),
                                                ("native_reasoning_field_count", self.native_reasoning_field_count),
                                                ("empty_native_reasoning_field_count", self.empty_native_reasoning_field_count)) if value is not None}}

    @classmethod
    def from_payload(cls, value: object) -> RequestShape:
        _need(type(value) is dict)
        raw = cast(dict[str, object], value)
        _exact(raw, {"protocol", "send_ordinal", "stream", "message_count", "role_counts", "tool_count", "tool_choice",
                             "schema_present", "native_continuation", "native_reasoning_present", "native_tool_call_count",
                             "output_token_parameter", "output_tokens"} | (set(raw) & _PROFILE_FIELDS))
        _need(not (set(raw) & _PROFILE_FIELDS) or set(raw) & _PROFILE_FIELDS == _PROFILE_FIELDS)
        roles = _exact(raw["role_counts"], set(_ROLES))
        empty = None if raw.get("empty_content_role_counts") is None else _exact(raw["empty_content_role_counts"], set(_ROLES))
        shape = cls(cast(str, raw["protocol"]), cast(int, raw["send_ordinal"]), cast(bool, raw["stream"]),
                    tuple(cast(int, roles[k]) for k in _ROLES), cast(int, raw["tool_count"]), cast(str, raw["tool_choice"]),
                    cast(bool, raw["schema_present"]), cast(bool, raw["native_continuation"]), cast(bool, raw["native_reasoning_present"]),
                    cast(int, raw["native_tool_call_count"]), cast(str, raw["output_token_parameter"]), cast(int | None, raw["output_tokens"]),
                    cast(str | None, raw.get("last_role")), cast(str | None, raw.get("last_content_kind")),
                    cast(bool | None, raw.get("last_content_empty")), cast(bool | None, raw.get("last_assistant_prefix")),
                    None if empty is None else tuple(cast(int, empty[k]) for k in _ROLES),
                    cast(int | None, raw.get("native_reasoning_field_count")), cast(int | None, raw.get("empty_native_reasoning_field_count")))
        _need(type(raw["message_count"]) is int and raw["message_count"] == sum(shape.role_counts))
        return shape


def make_request_shape(payload: object, protocol: str, *, send_ordinal: int,
                       native_continuation: bool = False) -> RequestShape | None:
    """Count the final adapter payload, without retaining it; capture is optional."""
    try:
        _need(type(payload) is dict and type(protocol) is str and protocol in _PROTOCOLS)
        raw = cast(dict[str, object], payload)
        messages = raw.get("input" if protocol == "responses" else "messages", [])
        _need(type(messages) is list and len(messages) <= 1_000_000)
        message_list = cast(list[object], messages)
        roles = [0] * 5
        empty_roles = [0] * 5
        reasoning_fields, empty_reasoning_fields = 0, 0
        reasoning, calls = False, 0
        for message in message_list:
            _need(type(message) is dict)
            message = cast(dict[str, object], message)
            role = message.get("role")
            _need(type(role) is str and role in _ROLES)
            roles[_ROLES.index(cast(str, role))] += 1
            content = message.get("content")
            if content is None or type(content) in (str, list) and not content:
                empty_roles[_ROLES.index(cast(str, role))] += 1
            if role == "assistant" and "reasoning_content" in message:
                reasoning_fields += 1
                value = message["reasoning_content"]
                if value is None or type(value) in (str, list) and not value:
                    empty_reasoning_fields += 1
            reasoning = reasoning or bool(message.get("reasoning_content"))
            tool_calls = message.get("tool_calls", [])
            _need(type(tool_calls) is list)
            calls += len(cast(list[object], tool_calls))
        if protocol == "messages" and raw.get("system"):
            roles[0] += 1
        tools = raw.get("tools", [])
        _need(type(tools) is list)
        choice = raw.get("tool_choice")
        choice_kind = ("none" if choice is None else choice if type(choice) is str and choice in {"auto", "required", "none"}
                       else "named" if type(choice) is dict else "unknown")
        fields = [field for field in _TOKEN_FIELDS if field in raw]
        _need(len(fields) <= 1)
        token_field = fields[0] if fields else "none"
        tail = cast(dict[str, object], message_list[-1]) if message_list else None
        content = tail.get("content") if tail is not None else None
        content_kind = (None if tail is None else "absent" if "content" not in tail else "null" if content is None
                        else "text" if type(content) is str else "parts" if type(content) is list else "other")
        content_empty = (None if tail is None or content_kind == "other" else True if content is None
                         else len(cast(str | list[object], content)) == 0)
        prefix = tail.get("prefix", False) if tail is not None else None
        # A partial/unclassifiable profile is unknown, never guessed. Legacy
        # records retain their original payload with all seven fields omitted.
        profile_known = tail is not None and content_kind != "other" and type(prefix) is bool
        return RequestShape(protocol, send_ordinal, cast(bool, raw.get("stream", False)), tuple(roles), len(cast(list[object], tools)),
                            choice_kind, any(k in raw for k in ("response_format", "output_config", "text")),
                            native_continuation, reasoning, calls, token_field, cast(int | None, raw.get(token_field)),
                            cast(str, tail["role"]) if profile_known and tail is not None else None,
                            content_kind if profile_known else None, content_empty if profile_known else None,
                            cast(bool, prefix) if profile_known else None, tuple(empty_roles) if profile_known else None,
                            reasoning_fields if profile_known else None, empty_reasoning_fields if profile_known else None)
    except Exception:  # noqa: BLE001 - metadata extraction cannot affect the request.
        return None


@dataclass(frozen=True, slots=True)
class ProviderRejection:
    status_code: int
    exception_family: str
    rejection_class: str
    parameter_family: str
    classification_source: str
    body_state: str
    request_shape: RequestShape | None

    def __post_init__(self) -> None:
        self._validate()

    def _validate(self) -> None:
        _need(type(self.status_code) is int and 400 <= self.status_code <= 599)
        for value, allowed in ((self.exception_family, _FAMILIES), (self.rejection_class, _CLASSES),
                               (self.parameter_family, _PARAMETERS), (self.classification_source, {"structured_code", "exact_signature", "status_only"}),
                               (self.body_state, {"available", "absent", "oversized", "invalid", "unavailable"})):
            _need(type(value) is str and value in allowed)
        _need((self.classification_source == "status_only") == (self.rejection_class == "unknown"))
        _need((self.classification_source == "exact_signature") == (self.rejection_class in {"tool_choice_thinking", "assistant_turn"}))
        if self.classification_source == "exact_signature":
            _need(self.parameter_family == ("messages" if self.rejection_class == "assistant_turn" else "tool_choice"))
        if self.rejection_class == "assistant_turn":
            _need(self.status_code == 400)
        if self.body_state != "available":
            _need(self.classification_source == "status_only" and self.parameter_family == "unknown")
        if self.request_shape is not None:
            _need(type(self.request_shape) is RequestShape)
            self.request_shape._validate()

    def to_payload(self) -> dict[str, object]:
        self._validate()
        return {"status_code": self.status_code, "exception_family": self.exception_family, "rejection_class": self.rejection_class,
                "parameter_family": self.parameter_family, "classification_source": self.classification_source, "body_state": self.body_state,
                "request_shape": None if self.request_shape is None else self.request_shape.to_payload()}

    @classmethod
    def from_payload(cls, value: object) -> ProviderRejection:
        raw = _exact(value, {"status_code", "exception_family", "rejection_class", "parameter_family", "classification_source", "body_state", "request_shape"})
        return cls(cast(int, raw["status_code"]), cast(str, raw["exception_family"]), cast(str, raw["rejection_class"]),
                   cast(str, raw["parameter_family"]), cast(str, raw["classification_source"]), cast(str, raw["body_state"]),
                   None if raw["request_shape"] is None else RequestShape.from_payload(raw["request_shape"]))


@dataclass(frozen=True, slots=True)
class ProviderDiagnostic:
    rejections: tuple[ProviderRejection, ...]

    def __post_init__(self) -> None:
        self._validate()

    def _validate(self) -> None:
        _need(type(self.rejections) is tuple and 1 <= len(self.rejections) <= 2)
        previous = 0
        for item in self.rejections:
            _need(type(item) is ProviderRejection)
            item._validate()
            if item.request_shape is not None:
                _need(item.request_shape.send_ordinal > previous)
                previous = item.request_shape.send_ordinal
        if len(self.rejections) == 2:
            first, last = self.rejections
            _need(first.request_shape is not None and last.request_shape is not None
                  and first.request_shape.protocol == last.request_shape.protocol)

    @property
    def status_code(self) -> int:
        return self.rejections[-1].status_code

    def to_payload(self) -> dict[str, object]:
        self._validate()
        complete = all(r.request_shape is not None and r.request_shape.send_ordinal == i
                       for i, r in enumerate(self.rejections, 1))
        return {"schema_version": 1, "wire_history_complete": complete, "rejections": [r.to_payload() for r in self.rejections]}

    @classmethod
    def from_payload(cls, value: object) -> ProviderDiagnostic:
        raw = _exact(value, {"schema_version", "wire_history_complete", "rejections"})
        _need(type(raw["schema_version"]) is int and raw["schema_version"] == 1
              and type(raw["wire_history_complete"]) is bool and type(raw["rejections"]) is list
              and 1 <= len(cast(list[object], raw["rejections"])) <= 2)
        result = cls(tuple(ProviderRejection.from_payload(item) for item in cast(list[object], raw["rejections"])))
        _need(raw["wire_history_complete"] == result.to_payload()["wire_history_complete"])
        return result


def _attach_provider_diagnostic(error: BaseException, diagnostic: ProviderDiagnostic) -> None:
    """Issue only from a trusted wrapper constructor, never a public attribute alone."""
    try:
        _need(type(diagnostic) is ProviderDiagnostic)
        diagnostic._validate()
        namespace = object.__getattribute__(error, "__dict__")
        if type(namespace) is dict:
            namespace["_issued_provider_diagnostic"] = (_ISSUED_DIAGNOSTIC, error, diagnostic)
    except Exception:  # noqa: BLE001 - optional attachment never replaces a primary error.
        return


def get_provider_diagnostic(error: object) -> ProviderDiagnostic | None:
    try:
        if not isinstance(error, BaseException):
            return None
        namespace = object.__getattribute__(error, "__dict__")
        if type(namespace) is not dict:
            return None
        binding = namespace.get("_issued_provider_diagnostic")
        if type(binding) is not tuple or len(binding) != 3 or binding[0] is not _ISSUED_DIAGNOSTIC or binding[1] is not error:
            return None
        value = namespace.get("provider_diagnostic")
        if value is not binding[2]:
            return None
        if type(value) is ProviderDiagnostic:
            value._validate()
            if type(namespace.get("status_code")) is not int or namespace["status_code"] != value.status_code:
                return None
            return value
    except Exception:  # noqa: BLE001 - malformed evidence is absent, never logged with supplier data.
        return None
    return None


def _classify(body: object) -> tuple[str, str, str, str]:
    if body is None:
        return "unknown", "unknown", "status_only", "absent"
    if type(body) is not dict or len(body) > 32:
        return "unknown", "unknown", "status_only", "invalid"
    raw = cast(dict[str, object], body)
    if "error" in raw:
        if type(raw["error"]) is not dict or len(cast(dict[str, object], raw["error"])) > 32:
            return "unknown", "unknown", "status_only", "invalid"
        raw = cast(dict[str, object], raw["error"])
    if any(type(raw.get(k)) is str and len(cast(str, raw[k])) > 4096 for k in ("code", "type", "param", "message")):
        return "unknown", "unknown", "status_only", "oversized"
    code = raw.get("code") if raw.get("code") is not None else raw.get("type")
    category = _CODE_CLASSES.get(code, "unknown") if type(code) is str else "unknown"
    source = "status_only" if category == "unknown" else "structured_code"
    parameter = raw.get("param")
    family = "unknown"
    if type(parameter) is str and len(parameter) <= 256:
        match = re.fullmatch(r"([a-z_]+)(?:(?:\.[A-Za-z0-9_]+)|(?:\[[0-9]{1,6}\]))*", parameter)
        if match and match[1] in _PARAMETERS:
            family = match[1]
    message = raw.get("message")
    if type(message) is str and message in (
        _THINKING_REJECTION, _THINKING_REJECTION + ".",
        "<400> InternalError.Algo.InvalidParameter: " + _THINKING_REJECTION,
        "<400> InternalError.Algo.InvalidParameter: " + _THINKING_REJECTION + ".",
    ):
        category, family, source = "tool_choice_thinking", "tool_choice", "exact_signature"
    return category, family, source, "available"


def _diagnostic(status: object, family: str, body: object, shape: RequestShape | None,
                previous: ProviderDiagnostic | None = None, *, body_state: str | None = None) -> ProviderDiagnostic | None:
    if type(status) is not int or not 400 <= status <= 599:
        return None
    category, parameter, source, state = _classify(body)
    if status == 400 and state == "available" and type(body) is dict:
        raw = cast(dict[str, object], body)
        raw = cast(dict[str, object], raw["error"]) if "error" in raw else raw
        if (type(raw.get("message")) is str and raw["message"] == _ASSISTANT_TURN_REJECTION
                and type(raw.get("code")) is str and raw["code"] == "invalid_request_error"
                and type(raw.get("type")) is str and raw["type"] == "invalid_request_error"
                and (raw.get("param") is None or type(raw.get("param")) is str and raw["param"] == "messages")):
            category, parameter, source = "assistant_turn", "messages", "exact_signature"
    if body_state is not None:
        category, parameter, source, state = "unknown", "unknown", "status_only", body_state
    rejection = ProviderRejection(status, family, category, parameter, source, state, shape)
    prior: tuple[ProviderRejection, ...] = ()
    if type(previous) is ProviderDiagnostic and shape is not None and shape.send_ordinal == 2:
        previous._validate()
        if len(previous.rejections) == 1 and previous.rejections[0].request_shape is not None:
            prior = previous.rejections
    return ProviderDiagnostic((*prior, rejection))


def capture_error(error: object, shape: RequestShape | None,
                  previous: ProviderDiagnostic | None = None) -> ProviderDiagnostic | None:
    """Only exact trusted exception types; never str(error), unknown properties or I/O."""
    try:
        issued = get_provider_diagnostic(error)
        if issued is not None:
            return issued  # Rewrapping is idempotent; the original class/shape wins.
        if type(error) is httpx.HTTPStatusError:
            namespace = object.__getattribute__(error, "__dict__")
            return capture_response(namespace.get("response"), shape, previous)
        family = _SDK_FAMILIES.get(type(error))
        if family is None:
            return None
        namespace = object.__getattribute__(error, "__dict__")
        response = namespace.get("response")
        status = namespace.get("status_code")
        if type(response) is not httpx.Response or response.status_code != status:
            return None
        candidate = _diagnostic(status, family, namespace.get("body"), shape, previous)
        if candidate is None or candidate.rejections[-1].rejection_class != "assistant_turn":
            return candidate
        # SDK parsing can normalize duplicate keys and strip an oversized error
        # envelope. Retry authority requires the original already-received bytes
        # to pass the same bounded strict parser as HTTPStatusError evidence.
        received = capture_response(response, shape, previous)
        if received is None:
            return _diagnostic(status, family, None, shape, previous, body_state="unavailable")
        return ProviderDiagnostic((*received.rejections[:-1],
                                   replace(received.rejections[-1], exception_family=family)))
    except Exception:  # noqa: BLE001 - primary error classification must survive capture failure.
        return None


def capture_response(response: object, shape: RequestShape | None,
                     previous: ProviderDiagnostic | None = None) -> ProviderDiagnostic | None:
    try:
        if type(response) is not httpx.Response:
            return None
        status = response.status_code
        try:
            raw = response.content  # Already-received bytes only; no read()/network.
        except httpx.ResponseNotRead:
            return _diagnostic(status, "http_status", None, shape, previous, body_state="unavailable")
        if len(raw) > _MAX_BODY:
            return _diagnostic(status, "http_status", None, shape, previous, body_state="oversized")
        if not raw:
            return _diagnostic(status, "http_status", None, shape, previous)

        def pairs(items: list[tuple[str, object]]) -> dict[str, object]:
            result: dict[str, object] = {}
            for key, value in items:
                _need(key not in result)
                result[key] = value
            return result

        try:
            body = json.loads(raw, object_pairs_hook=pairs, parse_constant=lambda _: _need(False))
        except (ValueError, UnicodeError, RecursionError):
            return _diagnostic(status, "http_status", None, shape, previous, body_state="invalid")
        return _diagnostic(status, "http_status", body, shape, previous)
    except Exception:  # noqa: BLE001 - never replace a provider HTTP error with diagnostic parsing failure.
        return None
