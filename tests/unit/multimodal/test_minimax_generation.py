from __future__ import annotations

import json

import httpx
import pytest

from agent_hub.multimodal.minimax import MiniMaxVideoGenerationClient


@pytest.mark.asyncio
async def test_minimax_video_client_polls_downloads_and_stores_file(tmp_path) -> None:  # type: ignore[no-untyped-def]
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/v1/video_generation":
            payload = json.loads(request.content.decode("utf-8"))
            assert payload == {
                "model": "MiniMax-Hailuo-02",
                "prompt": "a quiet product shot [Static shot]",
                "duration": 6,
                "resolution": "768P",
                "prompt_optimizer": True,
            }
            return httpx.Response(200, json={"task_id": "task-1", "base_resp": {"status_code": 0}})
        if request.url.path == "/v1/query/video_generation":
            assert request.url.params["task_id"] == "task-1"
            return httpx.Response(200, json={"status": "Success", "file_id": "file-1"})
        if request.url.path == "/v1/files/retrieve":
            assert request.url.params["file_id"] == "file-1"
            return httpx.Response(
                200,
                json={
                    "file": {
                        "file_id": "file-1",
                        "filename": "output_aigc.mp4",
                        "download_url": "https://download.example/video.mp4",
                    }
                },
            )
        if request.url.host == "download.example":
            return httpx.Response(200, content=b"fake-mp4-bytes")
        return httpx.Response(404)

    client = MiniMaxVideoGenerationClient(
        transport=httpx.MockTransport(handler),
        poll_interval_seconds=0,
    )

    artifact = await client.generate_text_to_video(
        api_key="sk-live",
        api_base="https://api.minimax.io/v1",
        model="MiniMax-Hailuo-02",
        prompt="a quiet product shot [Static shot]",
        output_dir=tmp_path,
        duration=6,
        resolution="768P",
    )

    assert artifact.kind == "video"
    assert artifact.provider == "minimax"
    assert artifact.task_id == "task-1"
    assert artifact.file_id == "file-1"
    assert artifact.mime_type == "video/mp4"
    assert artifact.path.read_bytes() == b"fake-mp4-bytes"
    assert artifact.path.name.endswith(".mp4")
    assert artifact.path.name.startswith("MiniMax-Hailuo-02_")
    timestamp = artifact.path.stem.removeprefix("MiniMax-Hailuo-02_")
    assert len(timestamp) == 15
    assert timestamp[8] == "-"
    assert timestamp.replace("-", "").isdigit()
    assert artifact.uri == artifact.path.as_uri()
    assert [request.url.path for request in requests] == [
        "/v1/video_generation",
        "/v1/query/video_generation",
        "/v1/files/retrieve",
        "/video.mp4",
    ]
