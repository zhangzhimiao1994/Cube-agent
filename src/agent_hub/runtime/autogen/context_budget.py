"""Pure token-budget fitting for explicit AutoGen engineering history."""

import hashlib
import json
from collections.abc import Callable
from dataclasses import replace
from typing import cast

from agent_hub.models.gateway import ConservativeTokenEstimator
from agent_hub.models.types import ModelMessage, ModelRequest, model_message_payload

_CALLS = "EXPLICIT_CAPABILITY_CALLS_JSON="
_RESULTS = "UNTRUSTED_CAPABILITY_RESULTS_JSON="
_MARKER = "COMPRESSED_ENGINEERING_HISTORY_JSON="
_DATA_PREFIXES = ("UNTRUSTED_", "EXPLICIT_", "COMPRESSED_DISCUSSION_HISTORY_JSON=", _MARKER)


class ContextBudgetExceeded(ValueError):
    """The pinned context cannot be safely represented inside the input budget."""

    def __init__(self) -> None:
        super().__init__("Request history cannot fit the context budget safely.")


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ContextBudgetExceeded()
        result[key] = value
    return result


def _rows(content: str, prefix: str) -> list[dict[str, object]]:
    try:
        value: object = json.loads(content[len(prefix):], object_pairs_hook=_unique_object)
    except (ValueError, RecursionError, OverflowError):
        raise ContextBudgetExceeded() from None
    if not isinstance(value, list) or not value or not all(isinstance(row, dict) for row in value):
        raise ContextBudgetExceeded()
    return cast(list[dict[str, object]], value)


def _validate_pair(call: ModelMessage, result: ModelMessage) -> None:
    if (call.role != "assistant" or result.role != "user"
            or not isinstance(call.content, str) or not isinstance(result.content, str)
            or not result.content.startswith(_RESULTS)):
        raise ContextBudgetExceeded()
    calls = _rows(call.content, _CALLS)
    results = _rows(result.content, _RESULTS)
    expected: dict[str, str] = {}
    for row in calls:
        identifier, name = row.get("id"), row.get("name")
        if (set(row) != {"id", "name", "arguments"}
                or not isinstance(identifier, str) or not identifier
                or identifier in expected or not isinstance(name, str) or not name
                or not isinstance(row["arguments"], str)):
            raise ContextBudgetExceeded()
        expected[identifier] = name
    seen: set[str] = set()
    for row in results:
        identifier = row.get("call_id")
        if (set(row) != {"call_id", "name", "content", "is_error"}
                or not isinstance(identifier, str) or identifier in seen
                or identifier not in expected or row["name"] != expected[identifier]
                or not isinstance(row["content"], str) or type(row["is_error"]) is not bool):
            raise ContextBudgetExceeded()
        seen.add(identifier)
    if seen != set(expected):
        raise ContextBudgetExceeded()


def _history_groups(messages: tuple[ModelMessage, ...]) -> tuple[list[tuple[int, ...]], set[int]]:
    groups: list[tuple[int, ...]] = []
    pinned: set[int] = set()
    user_groups: list[int] = []
    latest_pair: int | None = None
    plain_group: int | None = None
    index = 0
    native_ids: set[str] = set()
    while index < len(messages):
        message = messages[index]
        content = message.content
        native = message.native_assistant
        if native is not None:
            calls = native.tool_calls
            identifiers = tuple(call.id for call in calls)
            following = messages[index + 1:index + 1 + len(calls)]
            if (message.role != 'assistant' or message.tool_call_id is not None
                    or len(set(identifiers)) != len(identifiers)
                    or native_ids.intersection(identifiers)
                    or len(following) != len(calls)
                    or tuple(item.tool_call_id for item in following) != identifiers
                    or any(item.role != 'tool' or item.native_assistant is not None
                           or not isinstance(item.content, str) for item in following)):
                raise ContextBudgetExceeded()
            native_ids.update(identifiers)
            group = len(groups)
            groups.append(tuple(range(index, index + len(calls) + 1)))
            if calls:
                latest_pair = group
            else:
                pinned.add(group)  # A standalone native message/signature is mandatory.
            plain_group = None
            index += len(calls) + 1
            continue
        if (message.tool_call_id is not None or message.role not in {"system", "user", "assistant"}
                or not isinstance(content, str)):
            raise ContextBudgetExceeded()
        group = len(groups)
        if message.role == "system":
            groups.append((index,))
            pinned.add(group)
            plain_group = None
        elif content.startswith(_CALLS):
            if index + 1 >= len(messages):
                raise ContextBudgetExceeded()
            _validate_pair(message, messages[index + 1])
            groups.append((index, index + 1))
            latest_pair = group
            plain_group = None
            index += 1
        elif content.startswith(_RESULTS):
            raise ContextBudgetExceeded()
        elif content.startswith(_DATA_PREFIXES):
            # Unknown data protocols and existing receipts are never treated as user tasks.
            groups.append((index,))
            pinned.add(group)
            plain_group = None
        elif message.role == "assistant" and plain_group is not None:
            groups[plain_group] = (*groups[plain_group], index)
        else:
            groups.append((index,))
            plain_group = group
            if message.role == "user":
                user_groups.append(group)
        index += 1
    if not user_groups:
        raise ContextBudgetExceeded()
    pinned.update((user_groups[0], user_groups[-1]))
    if latest_pair is not None:
        pinned.add(latest_pair)
    return groups, pinned


def _replacement(request: ModelRequest, removed: set[int]) -> ModelRequest:
    payload = [model_message_payload(message) for index, message in enumerate(request.messages)
               if index in removed]
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                     allow_nan=False).encode("utf-8")
    marker = ModelMessage("user", _MARKER + json.dumps({
        "count": len(removed), "sha256": hashlib.sha256(raw).hexdigest(),
    }, sort_keys=True, separators=(",", ":")))
    first = min(removed)
    messages: list[ModelMessage] = []
    for index, message in enumerate(request.messages):
        if index == first:
            messages.append(marker)
        if index not in removed:
            messages.append(message)
    return replace(request, messages=tuple(messages))


def fit_request_history(request: ModelRequest, *, context_window_tokens: int | None,
                        admission_token_limit: int | None = None,
                        estimate_input: Callable[[ModelRequest], int] | None = None) -> ModelRequest:
    """Fit input with a one-eighth reserve, removing only oldest complete history groups.

    The marker is an omission receipt, not a generated summary or execution evidence.
    Native assistant/tool exchanges are indivisible, including private signatures.
    The latest complete group is mandatory; orphan/incomplete/multimodal groups fail closed.
    """
    from agent_hub.runtime.request_admission import input_target

    if (context_window_tokens is not None
            and (type(context_window_tokens) is not int or context_window_tokens <= 0)):
        raise ContextBudgetExceeded()
    target = input_target(context_window_tokens, admission_token_limit)
    if target is None:
        raise ContextBudgetExceeded()
    def tokens(candidate: ModelRequest) -> int:
        value = (estimate_input(candidate) if estimate_input is not None
                 else ConservativeTokenEstimator().estimate_input(candidate))
        if type(value) is not int or value <= 0:
            raise ContextBudgetExceeded()
        return value

    native = request.native_tool_continuation or any(
        message.native_assistant is not None or message.tool_call_id is not None for message in request.messages)
    validated = _history_groups(request.messages) if native else None
    original_tokens = tokens(request)
    if original_tokens <= target:
        return request
    groups, pinned = validated if validated is not None else _history_groups(request.messages)
    removed: set[int] = set()
    # Each complete group is considered once; pinned messages are never sliced or rewritten.
    for index, group in enumerate(groups):
        if index in pinned:
            continue
        removed.update(group)
        candidate = _replacement(request, removed)
        candidate_tokens = tokens(candidate)
        if candidate_tokens < original_tokens and candidate_tokens <= target:
            return candidate
    if admission_token_limit is not None and (
            context_window_tokens is None or admission_token_limit < context_window_tokens):
        from agent_hub.models.capacity import CapacityRequestTooLarge

        raise CapacityRequestTooLarge()
    raise ContextBudgetExceeded()
