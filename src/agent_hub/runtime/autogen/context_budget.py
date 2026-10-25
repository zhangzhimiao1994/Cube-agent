"""Pure token-budget fitting for explicit AutoGen engineering history."""

import hashlib
import json
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
    while index < len(messages):
        message = messages[index]
        content = message.content
        if (message.native_assistant is not None or message.tool_call_id is not None
                or message.role not in {"system", "user", "assistant"}
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


def fit_request_history(request: ModelRequest, *, context_window_tokens: int) -> ModelRequest:
    """Fit input with a one-eighth reserve, removing only oldest complete history groups.

    The marker is an omission receipt, not a generated summary or execution evidence.
    Native/multimodal protocols are deliberately unsupported for compression.
    """
    if type(context_window_tokens) is not int or context_window_tokens <= 0:
        raise ContextBudgetExceeded()
    target = context_window_tokens - max(1, context_window_tokens // 8)
    estimator = ConservativeTokenEstimator()
    original_tokens = estimator.estimate_input(request)
    if original_tokens <= target:
        return request
    if request.native_tool_continuation:
        raise ContextBudgetExceeded()
    groups, pinned = _history_groups(request.messages)
    removed: set[int] = set()
    # Each complete group is considered once; pinned messages are never sliced or rewritten.
    for index, group in enumerate(groups):
        if index in pinned:
            continue
        removed.update(group)
        candidate = _replacement(request, removed)
        candidate_tokens = estimator.estimate_input(candidate)
        if candidate_tokens < original_tokens and candidate_tokens <= target:
            return candidate
    raise ContextBudgetExceeded()
