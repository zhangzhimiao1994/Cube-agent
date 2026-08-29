from __future__ import annotations

from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any, cast

from agent_hub.harness.streaming import (
    openai_compatible_stream_events,
    transport_openai_compatible_stream_events,
)
from agent_hub.models.types import Deployment, ModelMessage, ModelRequest


class AsyncChunkStream:
    def __init__(self, chunks: list[object]) -> None:
        self._chunks = list(chunks)

    def __aiter__(self) -> AsyncChunkStream:
        return self

    async def __anext__(self) -> object:
        if not self._chunks:
            raise StopAsyncIteration
        return self._chunks.pop(0)


class CloseTrackingAsyncChunkStream(AsyncChunkStream):
    def __init__(self, chunks: list[object]) -> None:
        super().__init__(chunks)
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True


class FakeTransport:
    def __init__(self, chunks: list[object]) -> None:
        self.stream = CloseTrackingAsyncChunkStream(chunks)
        self.calls: list[tuple[Deployment, ModelRequest, str]] = []

    def stream_openai_compatible_chunks(
        self,
        deployment: Deployment,
        request: ModelRequest,
        api_key: str,
    ) -> AsyncIterator[object]:
        self.calls.append((deployment, request, api_key))
        return self.stream


def deployment() -> Deployment:
    return Deployment(
        id="primary-1",
        logical_model="primary",
        provider_model="deepseek/deepseek-chat",
        api_base="https://proxy.example.com/v1",
    )


def request() -> ModelRequest:
    return ModelRequest(
        logical_model="primary",
        messages=(ModelMessage(role="user", content="hello"),),
    )


async def test_openai_compatible_async_chunks_feed_incremental_harness_events() -> None:
    chunks = AsyncChunkStream(
        [
            {"choices": [{"delta": {"reasoning_content": "plan"}}]},
            SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content="answer"))]),
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_1",
                                    "function": {
                                        "name": "workspace_read",
                                        "arguments": '{"path":"README',
                                    },
                                }
                            ]
                        }
                    }
                ]
            },
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "function": {"arguments": '.md"}'},
                                }
                            ]
                        }
                    }
                ]
            },
        ]
    )
    events = openai_compatible_stream_events(chunks, provider="deepseek")

    first = await events.__anext__()
    second = await events.__anext__()
    remaining = [event async for event in events]

    assert first.kind == "model.reasoning_delta"
    assert first.payload == {"text": "plan"}
    assert second.kind == "model.text_delta"
    assert second.payload == {"text": "answer"}
    assert len(remaining) == 1
    assert remaining[0].kind == "tool.requested"
    assert remaining[0].payload == {
        "id": "call_1",
        "name": "workspace_read",
        "arguments": {"path": "README.md"},
    }


async def test_openai_compatible_stream_events_close_underlying_chunks_on_early_stop() -> None:
    chunks = CloseTrackingAsyncChunkStream(
        [
            {"choices": [{"delta": {"content": "hello"}}]},
            {"choices": [{"delta": {"content": "later"}}]},
        ]
    )
    events = openai_compatible_stream_events(chunks, provider="deepseek")

    first = await events.__anext__()
    await cast(Any, events).aclose()

    assert first.kind == "model.text_delta"
    assert chunks.closed is True


async def test_openai_compatible_stream_events_keep_interleaved_tool_calls_separate() -> None:
    events = [
        event
        async for event in openai_compatible_stream_events(
            AsyncChunkStream(
                [
                    {
                        "choices": [
                            {
                                "delta": {
                                    "tool_calls": [
                                        {
                                            "index": 0,
                                            "id": "call_a",
                                            "function": {
                                                "name": "workspace_read",
                                                "arguments": '{"path":"README',
                                            },
                                        },
                                        {
                                            "index": 1,
                                            "id": "call_b",
                                            "function": {
                                                "name": "web_search",
                                                "arguments": '{"query":"harness',
                                            },
                                        },
                                    ]
                                }
                            }
                        ]
                    },
                    {
                        "choices": [
                            {
                                "delta": {
                                    "tool_calls": [
                                        {
                                            "index": 1,
                                            "function": {"arguments": ' stream"}'},
                                        },
                                        {
                                            "index": 0,
                                            "function": {"arguments": '.md"}'},
                                        },
                                    ]
                                }
                            }
                        ]
                    },
                ]
            ),
            provider="deepseek",
        )
    ]

    assert [event.payload for event in events] == [
        {
            "id": "call_a",
            "name": "workspace_read",
            "arguments": {"path": "README.md"},
        },
        {
            "id": "call_b",
            "name": "web_search",
            "arguments": {"query": "harness stream"},
        },
    ]


async def test_transport_stream_bridge_uses_litellm_chunk_method() -> None:
    transport = FakeTransport(
        [
            {"choices": [{"delta": {"content": "hello"}}]},
        ]
    )
    model_deployment = deployment()
    model_request = request()

    events = [
        event
        async for event in transport_openai_compatible_stream_events(
            transport,
            model_deployment,
            model_request,
            "runtime-key",
            provider="deepseek",
        )
    ]

    assert transport.calls == [(model_deployment, model_request, "runtime-key")]
    assert len(events) == 1
    assert events[0].kind == "model.text_delta"
    assert events[0].payload == {"text": "hello"}


async def test_transport_stream_bridge_closes_underlying_chunks_on_early_stop() -> None:
    transport = FakeTransport(
        [
            {"choices": [{"delta": {"content": "hello"}}]},
            {"choices": [{"delta": {"content": "later"}}]},
        ]
    )

    events = transport_openai_compatible_stream_events(
        transport,
        deployment(),
        request(),
        "runtime-key",
        provider="deepseek",
    )
    first = await events.__anext__()
    await cast(Any, events).aclose()

    assert first.kind == "model.text_delta"
    assert transport.stream.closed is True
