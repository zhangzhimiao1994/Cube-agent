from types import SimpleNamespace

import pytest

from agent_hub.harness.provider import (
    ProviderErrorEnvelope,
    ProviderStreamDelta,
    ProviderStreamNormalizer,
    estimate_prefix_cache_reuse,
    openai_compatible_stream_deltas,
)


def test_deepseek_normalizer_preserves_reasoning_separately_from_visible_text() -> None:
    normalizer = ProviderStreamNormalizer(provider="deepseek")

    events = tuple(
        normalizer.normalize(
            (
                ProviderStreamDelta(reasoning_content="先分析约束。"),
                ProviderStreamDelta(content="最终答案。"),
            )
        )
    )

    assert tuple(event.kind for event in events) == ("model.reasoning_delta", "model.text_delta")
    assert events[0].payload == {"text": "先分析约束。"}
    assert events[1].payload == {"text": "最终答案。"}


def test_normalizer_aggregates_streamed_tool_call_argument_fragments() -> None:
    normalizer = ProviderStreamNormalizer(provider="deepseek")

    events = tuple(
        normalizer.normalize(
            (
                ProviderStreamDelta(tool_call_id="call_1", tool_name="workspace_read"),
                ProviderStreamDelta(tool_call_id="call_1", tool_arguments_delta='{"path":"README'),
                ProviderStreamDelta(tool_call_id="call_1", tool_arguments_delta='.md"}'),
            )
        )
    )

    assert len(events) == 1
    assert events[0].kind == "tool.requested"
    assert events[0].payload == {
        "id": "call_1",
        "name": "workspace_read",
        "arguments": {"path": "README.md"},
    }


def test_normalizer_rejects_oversized_streamed_tool_arguments() -> None:
    normalizer = ProviderStreamNormalizer(provider="deepseek")

    with pytest.raises(ValueError, match="exceed"):
        tuple(
            normalizer.normalize(
                (
                    ProviderStreamDelta(tool_call_id="call_1", tool_name="workspace_read"),
                    ProviderStreamDelta(tool_call_id="call_1", tool_arguments_delta="x" * 40_000),
                    ProviderStreamDelta(tool_call_id="call_1", tool_arguments_delta="x" * 40_000),
                )
            )
        )


def test_openai_compatible_stream_chunks_feed_normalized_harness_events() -> None:
    normalizer = ProviderStreamNormalizer(provider="deepseek")
    chunks = (
        {
            "choices": [
                {
                    "delta": {
                        "reasoning_content": "先想清楚。",
                    }
                }
            ]
        },
        SimpleNamespace(
            choices=[
                SimpleNamespace(
                    delta=SimpleNamespace(
                        content="答案：",
                        tool_calls=[
                            SimpleNamespace(
                                index=0,
                                id="call_1",
                                function=SimpleNamespace(
                                    name="workspace_read",
                                    arguments='{"path":"README',
                                ),
                            )
                        ],
                    )
                )
            ]
        ),
        {
            "choices": [
                {
                    "delta": {
                        "content": "已读取。",
                        "tool_calls": [
                            {
                                "index": 0,
                                "function": {
                                    "arguments": '.md"}',
                                },
                            }
                        ],
                    }
                }
            ]
        },
    )

    events = tuple(normalizer.normalize(openai_compatible_stream_deltas(chunks)))

    assert tuple(event.kind for event in events) == (
        "model.reasoning_delta",
        "model.text_delta",
        "model.text_delta",
        "tool.requested",
    )
    assert events[0].payload == {"text": "先想清楚。"}
    assert events[1].payload == {"text": "答案："}
    assert events[2].payload == {"text": "已读取。"}
    assert events[3].payload == {
        "id": "call_1",
        "name": "workspace_read",
        "arguments": {"path": "README.md"},
    }


def test_prefix_cache_estimate_rewards_stable_prompt_prefixes() -> None:
    previous = (
        "system: stable policy\n"
        "tools: stable schemas\n"
        "memory: project summary\n"
        "user: first request\n"
    )
    current = (
        "system: stable policy\n"
        "tools: stable schemas\n"
        "memory: project summary\n"
        "user: second request\n"
    )

    estimate = estimate_prefix_cache_reuse(previous, current)

    assert estimate.reusable_prefix_bytes >= len("system: stable policy\ntools: stable schemas\n")
    assert 0 < estimate.reuse_ratio < 1
    assert estimate.useful is True


def test_provider_error_envelope_redacts_sensitive_values() -> None:
    envelope = ProviderErrorEnvelope.from_exception(
        provider="deepseek",
        error=RuntimeError("Authorization: Bearer sk-live-secret failed at api.deepseek.com"),
    )

    assert envelope.provider == "deepseek"
    assert envelope.safe_message == "provider request failed"
    assert "sk-live-secret" not in repr(envelope)
    assert "Authorization" not in repr(envelope)
