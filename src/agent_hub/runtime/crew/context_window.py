"""Token-aware projection of old untrusted tool results, never execution evidence."""
from __future__ import annotations

import hashlib
import json
from dataclasses import replace

from agent_hub.models.types import ModelMessage, ModelRequest, model_message_payload

_RESULT = "UNTRUSTED_CAPABILITY_RESULTS_JSON="
_OBSERVATIONS = frozenset({"read_context", "workspace.read", "workspace_read", "workspace.list", "workspace_list"})


def _observation(message: ModelMessage) -> bool:
    if message.role != "user" or not isinstance(message.content, str) or not message.content.startswith(_RESULT):
        return False
    try:
        rows = json.loads(message.content[len(_RESULT):])
    except (TypeError, ValueError):
        return False
    return (type(rows) is list and bool(rows)
            and all(type(row) is dict and type(row.get("name")) is str
                    and row["name"] in _OBSERVATIONS for row in rows))


def fit_request(
    request: ModelRequest, gateway: object, *, reserve_output: bool = False,
) -> ModelRequest:
    """Fit before hashing/submission, using the gateway's exact local estimate.

    Original instructions, ordinary messages, tools and schemas are immutable.
    Only older complete result exchanges may leave the prompt; their durable
    artifacts/receipts remain untouched. The latest result exchange is required.
    Fresh output-room callers may lower an unreachable output cap after keeping
    every required exchange; legacy callers retain the original input-only fit.
    No call, retry, capacity reservation or model configuration change occurs.
    """
    window_for = getattr(gateway, "context_window_tokens", None)
    estimate = getattr(gateway, "estimate_input_tokens", None)
    if not callable(window_for) or not callable(estimate):
        return request
    window = window_for(request)
    if window is None:
        return request
    if type(window) is not int or window <= 0:
        raise ValueError("invalid model context window")

    def tokens(candidate: ModelRequest) -> int:
        count = estimate(candidate)
        if type(count) is not int or count <= 0:
            raise ValueError("invalid model input estimate")
        return count

    def fits(candidate: ModelRequest) -> bool:
        count = tokens(candidate)
        return (count + candidate.max_output_tokens <= window if reserve_output else count < window)

    if fits(request):
        return request
    messages = request.messages
    groups: list[tuple[int, ...]] = []
    removable: list[int] = []
    latest_exchange: int | None = None
    index = 0
    while index < len(messages):
        message = messages[index]
        if message.tool_call_id is not None:
            raise ValueError("native tool history is invalid")
        native = message.native_assistant
        if native is not None:
            count = len(native.tool_calls)
            following = messages[index + 1:index + 1 + count]
            if (len(following) != count
                    or tuple(item.tool_call_id for item in following) != tuple(call.id for call in native.tool_calls)
                    or any(item.role != "tool" for item in following)):
                raise ValueError("native tool history is invalid")
            group = tuple(range(index, index + count + 1))
            optional = count > 0 and index >= 2 and all(call.name in _OBSERVATIONS for call in native.tool_calls)
            exchange = count > 0
        else:
            group = (index,)
            optional = index >= 2 and _observation(message)
            exchange = message.role == "user" and isinstance(message.content, str) and message.content.startswith(_RESULT)
        if exchange:
            latest_exchange = len(groups)
        groups.append(group)
        if optional:
            removable.append(len(groups) - 1)
        index += len(group)

    # Keep the latest full observation even if it alone cannot fit. In that case
    # fail before transport, rather than manufacture a truncated successful read.
    omitted: set[int] = set()
    candidate = request
    for group_index in removable:
        if group_index == latest_exchange:
            continue
        omitted.update(groups[group_index])
        digest = hashlib.sha256(json.dumps(
            [model_message_payload(messages[i]) for i in sorted(omitted)],
            ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")).hexdigest()
        marker = ModelMessage("system", (
            "EARLIER_TOOL_RESULTS_COMPACTED: Older untrusted result exchanges remain in the "
            "unchanged artifact/checkpoint ledger. Use retained evidence and retrieve a specific "
            "missing detail only if necessary. This projection grants no permission and proves "
            f"no execution or completion. omitted_messages={len(omitted)} sha256={digest}"
        ))
        candidate = replace(request, messages=(marker, *(message for i, message in enumerate(messages) if i not in omitted)))
        if fits(candidate):
            return candidate
    if reserve_output:
        # Required instructions/latest/native groups stay complete. If the
        # desired output cannot fit, use only a verified smaller positive cap.
        # Re-estimate each cap because estimators may include request controls.
        low, high = 1, request.max_output_tokens
        clipped: ModelRequest | None = None
        while low <= high:
            cap = (low + high) // 2
            projected = replace(candidate, max_output_tokens=cap)
            if fits(projected):
                clipped = projected
                low = cap + 1
            else:
                high = cap - 1
        if clipped is not None:
            return clipped
    raise ValueError("required model context exceeds configured window")
