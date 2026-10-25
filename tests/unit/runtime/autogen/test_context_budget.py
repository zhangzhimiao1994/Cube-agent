"""Token-based history fitting preserves task and complete tool evidence."""

import hashlib
import json
from dataclasses import fields, replace

import pytest

from agent_hub.models.gateway import ConservativeTokenEstimator
from agent_hub.models.native_messages import NativeAssistantMessage, NativeToolCall
from agent_hub.models.types import (
    ModelCapability,
    ModelMessage,
    ModelRequest,
    StructuredResponseSchema,
    ToolDefinition,
    model_message_payload,
)
from agent_hub.runtime.autogen.context_budget import ContextBudgetExceeded, fit_request_history

CALLS = "EXPLICIT_CAPABILITY_CALLS_JSON="
RESULTS = "UNTRUSTED_CAPABILITY_RESULTS_JSON="
MARKER = "COMPRESSED_ENGINEERING_HISTORY_JSON="
ESTIMATOR = ConservativeTokenEstimator()


def pair(index: int, text: str = "code " * 2200) -> tuple[ModelMessage, ModelMessage]:
    return (
        ModelMessage("assistant", CALLS + json.dumps([{
            "id": f"call-{index}", "name": "workspace_write_text",
            "arguments": json.dumps({"path": f"{index}.py", "content": text}),
        }], ensure_ascii=False)),
        ModelMessage("user", RESULTS + json.dumps([{
            "call_id": f"call-{index}", "name": "workspace_write_text",
            "content": '{ "status": "failed", "receipt": "unaltered" }', "is_error": True,
        }], ensure_ascii=False)),
    )


def request(messages: tuple[ModelMessage, ...] | None = None) -> ModelRequest:
    if messages is None:
        messages = (
            ModelMessage("system", "Keep permissions and verify before delivery."),
            ModelMessage("user", "Build the integer CLI, test it, deliver the project."),
            *(message for index in range(10) for message in pair(index)),
        )
    return ModelRequest(
        logical_model="engineering", messages=messages, timeout_seconds=177,
        max_output_tokens=100, allow_fallback=False,
        required_capabilities=frozenset({
            ModelCapability.TOOL_CALLING, ModelCapability.STRUCTURED_OUTPUT,
        }),
        tools=(ToolDefinition("workspace_write_text", "Write within workspace", {
            "type": "object", "properties": {"content": {"type": "string"}},
        }),),
        required_tool="workspace_write_text",
        response_schema=StructuredResponseSchema("delivery", {
            "type": "object", "properties": {"done": {"type": "boolean"}},
        }),
    )


def marker_payload(fitted: ModelRequest) -> dict[str, object]:
    markers = [message for message in fitted.messages
               if isinstance(message.content, str) and message.content.startswith(MARKER)]
    assert len(markers) == 1
    assert markers[0].role == "user"
    payload: object = json.loads(str(markers[0].content)[len(MARKER):])
    assert isinstance(payload, dict)
    return payload


def test_22_messages_fit_by_actual_tokens_not_message_count() -> None:
    original = request((ModelMessage("system", "policy"), ModelMessage("user", "Build CLI"),
                        *(message for index in range(10)
                          for message in pair(index, "code " * 6000))))
    assert len(original.messages) == 22
    assert len(original.messages) < 128
    assert ESTIMATOR.estimate_input(original) > 65536
    fitted = fit_request_history(original, context_window_tokens=65536)
    assert ESTIMATOR.estimate_input(fitted) <= 57344
    assert fitted is not original
    assert len(fitted.messages) < 22


def test_original_task_latest_failure_and_every_system_are_unchanged() -> None:
    original = request()
    extra_system = ModelMessage("system", "Never expand permission scope.")
    latest_instruction = ModelMessage("user", "Fix the exact failing test; do not fake results.")
    original = replace(original, messages=(
        *original.messages[:8], extra_system, *original.messages[8:], latest_instruction,
    ))
    fitted = fit_request_history(original, context_window_tokens=8000)
    for message in (original.messages[0], original.messages[1], extra_system,
                    *original.messages[-3:]):
        assert any(item is message for item in fitted.messages)


def test_only_messages_change_and_retained_args_receipts_and_schema_are_byte_exact() -> None:
    original = request()
    before = tuple(model_message_payload(item) for item in original.messages)
    fitted = fit_request_history(original, context_window_tokens=8000)
    for field in fields(original):
        if field.name != "messages":
            assert getattr(fitted, field.name) == getattr(original, field.name)
    assert fitted.tools is original.tools
    assert fitted.response_schema is original.response_schema
    assert fitted.messages[-2] is original.messages[-2]
    assert fitted.messages[-1] is original.messages[-1]
    assert tuple(model_message_payload(item) for item in original.messages) == before


def test_marker_is_only_count_and_stable_sha_of_removed_messages() -> None:
    original = request()
    fitted = fit_request_history(original, context_window_tokens=8000)
    removed = [item for item in original.messages
               if not any(item is kept for kept in fitted.messages)]
    raw = json.dumps([model_message_payload(item) for item in removed],
                     ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                     allow_nan=False).encode("utf-8")
    assert marker_payload(fitted) == {
        "count": len(removed), "sha256": hashlib.sha256(raw).hexdigest(),
    }
    assert fitted.messages == fit_request_history(original, context_window_tokens=8000).messages
    assert "code code" not in str(marker_payload(fitted))


def test_oldest_complete_pairs_are_removed_first_without_orphans() -> None:
    original = request()
    fitted = fit_request_history(original, context_window_tokens=8000)
    retained = [item for item in fitted.messages
                if isinstance(item.content, str) and item.content.startswith(CALLS)]
    original_calls = list(original.messages[2::2])
    assert 0 < len(retained) < len(original_calls)
    assert retained == original_calls[-len(retained):]
    for index, item in enumerate(fitted.messages):
        if isinstance(item.content, str) and item.content.startswith(CALLS):
            assert str(fitted.messages[index + 1].content).startswith(RESULTS)


def test_fit_is_idempotent_and_already_fit_returns_identical_object() -> None:
    original = request()
    assert fit_request_history(original, context_window_tokens=100000) is original
    fitted = fit_request_history(original, context_window_tokens=8000)
    assert fit_request_history(fitted, context_window_tokens=8000) is fitted


def test_small_max_output_does_not_shrink_one_eighth_input_reserve() -> None:
    original = request()
    window = ESTIMATOR.estimate_input(original) + 110
    assert ESTIMATOR.estimate_input(original) < window - original.max_output_tokens
    fitted = fit_request_history(original, context_window_tokens=window)
    assert fitted is not original
    assert ESTIMATOR.estimate_input(fitted) <= window - max(1, window // 8)
    assert fitted.max_output_tokens == 100


@pytest.mark.parametrize("text", ["English ", "\u4e2d\u6587\u4ee3\u7801", "\U0001f680\u6d4b\u8bd5\u00e9"])
def test_multibyte_estimates_and_hashes(text: str) -> None:
    original = request((ModelMessage("user", "Original task"),
                        *pair(0, text * 10000), *pair(1, "latest")))
    fitted = fit_request_history(original, context_window_tokens=2000)
    assert ESTIMATOR.estimate_input(fitted) <= 1750
    assert fitted.messages[-2:] == original.messages[-2:]
    assert marker_payload(fitted)["count"] == 2


@pytest.mark.parametrize("index", [0, 1, -1])
def test_oversized_pinned_task_system_or_latest_result_rejected(index: int) -> None:
    original = request()
    messages = list(original.messages)
    if index == -1:
        messages[-2:] = pair(99, "critical " * 20000)
    else:
        messages[index] = ModelMessage(messages[index].role, "SECRET " * 20000)
    original = replace(original, messages=tuple(messages))
    with pytest.raises(ContextBudgetExceeded) as caught:
        fit_request_history(original, context_window_tokens=8000)
    assert str(caught.value) == "Request history cannot fit the context budget safely."
    assert "SECRET" not in str(caught.value)


@pytest.mark.parametrize("broken", ["orphan", "missing", "mismatch", "duplicate", "malformed"])
def test_unpaired_or_invalid_explicit_protocol_fails_closed(broken: str) -> None:
    calls, results = pair(8)
    messages: tuple[ModelMessage, ...]
    if broken == "orphan":
        messages = (results,)
    elif broken == "missing":
        messages = (calls,)
    elif broken == "mismatch":
        messages = (calls, pair(9)[1])
    elif broken == "duplicate":
        rows = json.loads(str(results.content)[len(RESULTS):])
        messages = (calls, ModelMessage("user", RESULTS + json.dumps(rows * 2)))
    else:
        messages = (ModelMessage("assistant", CALLS + "{bad-secret"),)
    original = request((ModelMessage("user", "task"), *pair(0, "old " * 9000),
                        *messages, *pair(10, "last")))
    assert ESTIMATOR.estimate_input(original) > 4000
    with pytest.raises(ContextBudgetExceeded):
        fit_request_history(original, context_window_tokens=4000)


def test_feedback_is_not_misidentified_as_original_user_task() -> None:
    original = request((ModelMessage("system", "policy"), *pair(0), *pair(1)))
    with pytest.raises(ContextBudgetExceeded):
        fit_request_history(original, context_window_tokens=4000)


def test_old_plain_turn_is_removed_as_a_complete_group() -> None:
    old_user = ModelMessage("user", "old follow-up " * 4000)
    old_assistant = ModelMessage("assistant", "old response " * 2000)
    original = request((ModelMessage("user", "original task"),
                        ModelMessage("assistant", "initial plan"),
                        old_user, old_assistant,
                        ModelMessage("user", "latest instruction"), *pair(1, "last")))
    fitted = fit_request_history(original, context_window_tokens=2000)
    assert old_user not in fitted.messages and old_assistant not in fitted.messages
    assert fitted.messages[0] is original.messages[0]
    assert fitted.messages[-3:] == original.messages[-3:]


@pytest.mark.parametrize("role", ["native_assistant", "native_tool_message", "tool"])
def test_unknown_native_protocol_is_not_deleted(role: str) -> None:
    original = request((ModelMessage("user", "task"), ModelMessage(role, "secret " * 9000),
                        *pair(1, "last")))
    with pytest.raises(ContextBudgetExceeded):
        fit_request_history(original, context_window_tokens=2000)


def test_real_native_call_result_history_fails_closed_without_rewriting() -> None:
    native = NativeAssistantMessage("chat", None, (NativeToolCall("one", "write", "{}"),))
    original = replace(request((ModelMessage("user", "task"),)),
                       native_tool_continuation=True, messages=(
                           ModelMessage("user", "task"),
                           ModelMessage("assistant", "", native_assistant=native),
                           ModelMessage("tool", "receipt " * 9000, tool_call_id="one"),
                       ))
    with pytest.raises(ContextBudgetExceeded):
        fit_request_history(original, context_window_tokens=2000)


def test_no_substantive_reduction_fails_instead_of_adding_marker() -> None:
    original = request((ModelMessage("user", "task"), ModelMessage("assistant", "tiny"),
                        ModelMessage("user", "last")))
    with pytest.raises(ContextBudgetExceeded):
        fit_request_history(original, context_window_tokens=50)


@pytest.mark.parametrize("window", [0, -1, True, 1.5])
def test_invalid_window_has_fixed_safe_failure(window: int) -> None:
    with pytest.raises(ContextBudgetExceeded):
        fit_request_history(request(), context_window_tokens=window)


def test_first_real_user_task_after_tool_data_is_pinned() -> None:
    first_task = ModelMessage("user", "Original request after imported tool history")
    original = request((*pair(0, "old " * 9000), first_task, *pair(1, "old " * 9000),
                        ModelMessage("user", "latest instruction"), *pair(2, "last")))
    fitted = fit_request_history(original, context_window_tokens=2500)
    assert any(message is first_task for message in fitted.messages)
    assert fitted.messages[-3:] == original.messages[-3:]


def test_unknown_protocol_data_is_pinned_not_treated_as_user_instruction() -> None:
    data = ModelMessage("user", "UNTRUSTED_OTHER_JSON=" + "sensitive " * 9000)
    original = request((data, ModelMessage("user", "task"), *pair(0, "old " * 9000),
                        *pair(1, "last")))
    with pytest.raises(ContextBudgetExceeded):
        fit_request_history(original, context_window_tokens=2500)


def test_multicall_pair_stays_complete_with_out_of_order_results() -> None:
    call_a, result_a = pair(10, "last a")
    call_b, result_b = pair(11, "last b")
    calls = json.loads(str(call_a.content)[len(CALLS):]) + json.loads(
        str(call_b.content)[len(CALLS):])
    results = json.loads(str(result_b.content)[len(RESULTS):]) + json.loads(
        str(result_a.content)[len(RESULTS):])
    latest = (ModelMessage("assistant", CALLS + json.dumps(calls)),
              ModelMessage("user", RESULTS + json.dumps(results)))
    original = request((ModelMessage("user", "task"), *pair(0, "old " * 9000), *latest))
    fitted = fit_request_history(original, context_window_tokens=2500)
    assert fitted.messages[-2] is latest[0]
    assert fitted.messages[-1] is latest[1]


@pytest.mark.parametrize("corruption", ["duplicate-key", "wrong-role", "missing-result",
                                       "duplicate-call", "wrong-name", "non-string-arguments"])
def test_ambiguous_or_partial_call_batches_are_never_compressed(corruption: str) -> None:
    calls, results = pair(1)
    bad: tuple[ModelMessage, ...] = (calls, results)
    if corruption == "duplicate-key":
        bad = (ModelMessage("assistant", CALLS + '[{"id":"a","id":"b"}]'), results)
    elif corruption == "wrong-role":
        bad = (ModelMessage("user", calls.content), results)
    elif corruption == "missing-result":
        rows = json.loads(str(calls.content)[len(CALLS):])
        extra = json.loads(str(pair(2)[0].content)[len(CALLS):])
        bad = (ModelMessage("assistant", CALLS + json.dumps(rows + extra)), results)
    elif corruption == "duplicate-call":
        rows = json.loads(str(calls.content)[len(CALLS):])
        bad = (ModelMessage("assistant", CALLS + json.dumps(rows * 2)), results)
    elif corruption == "wrong-name":
        bad = (calls, ModelMessage("user", str(results.content).replace(
            "workspace_write_text", "workspace_read")))
    else:
        rows = json.loads(str(calls.content)[len(CALLS):])
        rows[0]["arguments"] = {"path": "not a raw argument string"}
        bad = (ModelMessage("assistant", CALLS + json.dumps(rows)), results)
    original = request((ModelMessage("user", "task"), *pair(0, "old " * 9000),
                        *bad, *pair(3, "last")))
    assert ESTIMATOR.estimate_input(original) > 2500
    with pytest.raises(ContextBudgetExceeded):
        fit_request_history(original, context_window_tokens=2500)


def test_opaque_multimodal_critical_history_is_not_deleted() -> None:
    opaque = ModelMessage("user", ({"type": "text", "text": "important " * 9000},))
    original = request((ModelMessage("user", "task"), opaque, *pair(0, "last")))
    with pytest.raises(ContextBudgetExceeded):
        fit_request_history(original, context_window_tokens=2000)


def test_independent_complete_rounds_can_reuse_provider_call_identifier() -> None:
    latest = pair(0, "latest failure")
    original = request((ModelMessage("user", "task"), *pair(0, "old " * 9000), *latest))
    fitted = fit_request_history(original, context_window_tokens=2500)
    assert fitted.messages[-2] is latest[0]
    assert fitted.messages[-1] is latest[1]
    assert marker_payload(fitted)["count"] == 2
    assert ESTIMATOR.estimate_input(fitted) <= 2188
