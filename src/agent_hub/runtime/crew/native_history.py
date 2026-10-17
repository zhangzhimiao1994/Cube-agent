"""Private Chat history grouping; never an artifact or an authorization source."""
from __future__ import annotations

import json
from collections.abc import Mapping, Sequence

from agent_hub.models.types import ModelMessage, ModelResponse, model_message_payload


def _mutable(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _mutable(item) for key, item in value.items()}
    if isinstance(value, tuple | list):
        return [_mutable(item) for item in value]
    return value


def validate_response(response: ModelResponse, aliases: Mapping[str, str]) -> None:
    native = response.native_assistant
    if native is None:
        return
    if len(native.tool_calls) != len(response.tool_calls):
        raise ValueError("native response binding is invalid")
    for wire, call in zip(native.tool_calls, response.tool_calls, strict=True):
        if (wire.id != call.id or aliases.get(wire.name, wire.name) != call.name
                or json.loads(wire.arguments) != _mutable(call.arguments)):
            raise ValueError("native response binding is invalid")
    if native.content != response.text:
        raise ValueError("native response binding is invalid")


def tool_result_messages(
    response: ModelResponse, results: Sequence[Mapping[str, object]],
) -> tuple[ModelMessage, ...]:
    native = response.native_assistant
    if native is None or len(results) > len(response.tool_calls):
        raise ValueError("native result history is invalid")
    if tuple(call.id for call in response.tool_calls) != tuple(call.id for call in native.tool_calls):
        raise ValueError("native result history is invalid")
    messages = [ModelMessage("assistant", native.content or "", native_assistant=native)]
    for index, call in enumerate(response.tool_calls):
        if index < len(results):
            result = results[index]
            if result.get("name") != call.name:
                raise ValueError("native result history is invalid")
        else:
            result = {"name": call.name, "result": {"status": "not_executed"}}
        messages.append(ModelMessage(
            "tool", "UNTRUSTED_CAPABILITY_RESULTS_JSON=" + json.dumps(
                [result], ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"),
            ), tool_call_id=call.id,
        ))
    return tuple(messages)


def compact_messages(
    messages: Sequence[ModelMessage], *, max_prompt_bytes: int, message_limit: int,
) -> tuple[ModelMessage, ...]:
    current = tuple(messages)
    groups: list[tuple[int, ...]] = []
    index = 0
    latest_native: int | None = None
    while index < len(current):
        message = current[index]
        if message.tool_call_id is not None:
            raise ValueError("native tool history is invalid")
        native = message.native_assistant
        if native is None:
            groups.append((index,))
            index += 1
            continue
        count = len(native.tool_calls)
        following = current[index + 1:index + 1 + count]
        if (len(following) != count or tuple(item.tool_call_id for item in following)
                != tuple(call.id for call in native.tool_calls)
                or any(item.role != "tool" for item in following)):
            raise ValueError("native tool history is invalid")
        latest_native = len(groups)
        groups.append(tuple(range(index, index + count + 1)))
        index += count + 1

    sizes = [len(json.dumps(model_message_payload(message), ensure_ascii=False, allow_nan=False,
                            sort_keys=True, separators=(",", ":")).encode("utf-8")) for message in current]
    if len(current) <= message_limit and sum(sizes) <= max_prompt_bytes:
        return current
    # Retain anchors, the last complete native exchange and its following controls.
    required_groups = {i for i, group in enumerate(groups) if any(j < 2 for j in group)}
    if latest_native is not None:
        required_groups.update(range(latest_native, len(groups)))
    controls: dict[str, int] = {}
    for i, group in enumerate(groups):
        if len(group) != 1:
            continue
        message = current[group[0]]
        if message.role == "system" and isinstance(message.content, str):
            prefix = message.content.split(":", 1)[0]
            if prefix in {
                "WORKSPACE_OBSERVATION_STALL", "CAPABILITY_ARGUMENT_CORRECTION",
                "WORKSPACE_MAINTENANCE_DELIVERY_CONTINUATION", "WORKSPACE_DELIVERY_CONTINUATION",
                "CAPABILITY_RESULT_CONTINUATION",
            }:
                controls[prefix] = i
    required_groups.update(controls.values())
    retained = {index for i in required_groups for index in groups[i]}
    total = sum(sizes[index] for index in retained)
    if len(retained) > message_limit or total > max_prompt_bytes:
        raise ValueError("native tool context exceeds bounded capacity")
    for group in reversed(groups):
        if any(index in retained for index in group):
            continue
        size = sum(sizes[index] for index in group)
        if len(retained) + len(group) <= message_limit and total + size <= max_prompt_bytes:
            retained.update(group)
            total += size
    return tuple(current[index] for index in sorted(retained))
