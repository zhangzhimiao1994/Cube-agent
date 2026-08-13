from __future__ import annotations

import asyncio
import re
from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

import httpx

from agent_hub.multimodal.video_providers import (
    GeneratedVideoArtifact,
    VideoProviderGenerationError,
)

MiniMaxGeneratedVideo = GeneratedVideoArtifact

_SAFE_FILENAME = re.compile(r"^[A-Za-z0-9._-]{1,160}$")


class MiniMaxVideoGenerationError(VideoProviderGenerationError):
    """Safe provider error for MiniMax video generation failures."""


class MiniMaxVideoGenerationClient:
    def __init__(
        self,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout_seconds: float = 60,
        poll_interval_seconds: float = 5,
        max_polls: int = 240,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if poll_interval_seconds < 0:
            raise ValueError("poll_interval_seconds must be nonnegative")
        if max_polls <= 0:
            raise ValueError("max_polls must be positive")
        self._transport = transport
        self._timeout_seconds = timeout_seconds
        self._poll_interval_seconds = poll_interval_seconds
        self._max_polls = max_polls

    async def generate_text_to_video(
        self,
        *,
        api_key: str,
        api_base: str,
        model: str,
        prompt: str,
        output_dir: Path,
        duration: int = 6,
        resolution: str = "768P",
    ) -> MiniMaxGeneratedVideo:
        api_key = _required_string(api_key, "api_key")
        model = _required_string(model, "model")
        prompt = _required_string(prompt, "prompt")
        base = _normalized_base(api_base)
        output_dir.mkdir(parents=True, exist_ok=True)
        async with self._client() as client:
            task_id = await self._submit(
                client,
                base=base,
                api_key=api_key,
                model=model,
                prompt=prompt,
                duration=duration,
                resolution=resolution,
            )
            file_id = await self._poll(client, base=base, api_key=api_key, task_id=task_id)
            download_url, filename = await self._retrieve_file(
                client,
                base=base,
                api_key=api_key,
                file_id=file_id,
            )
            stored_path, mime_type = await self._download(
                client,
                download_url=download_url,
                output_dir=output_dir,
                filename=filename,
            )
        return MiniMaxGeneratedVideo(
            path=stored_path,
            uri=stored_path.as_uri(),
            provider="minimax",
            model=model,
            task_id=task_id,
            file_id=file_id,
            mime_type=mime_type,
        )

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=self._timeout_seconds,
            transport=self._transport,
            follow_redirects=True,
        )

    async def _submit(
        self,
        client: httpx.AsyncClient,
        *,
        base: str,
        api_key: str,
        model: str,
        prompt: str,
        duration: int,
        resolution: str,
    ) -> str:
        response = await client.post(
            f"{base}/video_generation",
            headers=_headers(api_key),
            json={
                "model": model,
                "prompt": prompt,
                "duration": duration,
                "resolution": resolution,
                "prompt_optimizer": True,
            },
        )
        payload = _json_object(response, "MiniMax video submit failed")
        _raise_for_provider_failure(payload, "MiniMax video submit failed")
        task_id = payload.get("task_id")
        if not isinstance(task_id, str) or not task_id.strip():
            raise MiniMaxVideoGenerationError("MiniMax video submit response missing task_id")
        return task_id.strip()

    async def _poll(
        self,
        client: httpx.AsyncClient,
        *,
        base: str,
        api_key: str,
        task_id: str,
    ) -> str:
        for attempt in range(self._max_polls):
            response = await client.get(
                f"{base}/query/video_generation",
                headers=_headers(api_key),
                params={"task_id": task_id},
            )
            payload = _json_object(response, "MiniMax video query failed")
            _raise_for_provider_failure(payload, "MiniMax video query failed")
            status = str(payload.get("status") or payload.get("task_status") or "").strip()
            if status == "Success":
                file_id = payload.get("file_id")
                if not isinstance(file_id, str) or not file_id.strip():
                    raise MiniMaxVideoGenerationError("MiniMax video query response missing file_id")
                return file_id.strip()
            if status in {"Fail", "Failed", "Error"}:
                raise MiniMaxVideoGenerationError("MiniMax video task failed")
            if attempt < self._max_polls - 1 and self._poll_interval_seconds:
                await asyncio.sleep(self._poll_interval_seconds)
        raise MiniMaxVideoGenerationError("MiniMax video task polling timed out")

    async def _retrieve_file(
        self,
        client: httpx.AsyncClient,
        *,
        base: str,
        api_key: str,
        file_id: str,
    ) -> tuple[str, str | None]:
        response = await client.get(
            f"{base}/files/retrieve",
            headers=_headers(api_key),
            params={"file_id": file_id},
        )
        payload = _json_object(response, "MiniMax file retrieve failed")
        _raise_for_provider_failure(payload, "MiniMax file retrieve failed")
        file_payload = payload.get("file")
        if not isinstance(file_payload, Mapping):
            file_payload = payload
        download_url = file_payload.get("download_url")
        if not isinstance(download_url, str) or not download_url.strip():
            raise MiniMaxVideoGenerationError("MiniMax file retrieve response missing download_url")
        filename = file_payload.get("filename")
        return download_url.strip(), filename if isinstance(filename, str) else None

    async def _download(
        self,
        client: httpx.AsyncClient,
        *,
        download_url: str,
        output_dir: Path,
        filename: str | None,
    ) -> tuple[Path, str]:
        response = await client.get(download_url)
        if response.status_code >= 400:
            raise MiniMaxVideoGenerationError("MiniMax file download failed")
        suffix = _filename_suffix(filename, download_url) or ".mp4"
        original_name = _safe_filename(filename)
        stem = PurePosixPath(original_name).stem if original_name else "minimax-video"
        name = f"{stem}-{uuid4().hex}{suffix}"
        target = (output_dir / name).resolve()
        root = output_dir.resolve()
        if root not in target.parents:
            raise MiniMaxVideoGenerationError("MiniMax file target path is unsafe")
        target.write_bytes(response.content)
        mime_type = response.headers.get("content-type", "").split(";", 1)[0].strip()
        return target, mime_type or "video/mp4"


def _headers(api_key: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }


def _json_object(response: httpx.Response, message: str) -> dict[str, Any]:
    if response.status_code >= 400:
        raise MiniMaxVideoGenerationError(message)
    try:
        payload = response.json()
    except ValueError:
        raise MiniMaxVideoGenerationError(f"{message}: malformed JSON") from None
    if not isinstance(payload, dict):
        raise MiniMaxVideoGenerationError(f"{message}: malformed JSON")
    return payload


def _raise_for_provider_failure(payload: Mapping[str, Any], message: str) -> None:
    base_resp = payload.get("base_resp")
    if isinstance(base_resp, Mapping):
        status_code = base_resp.get("status_code")
        if status_code not in (None, 0, "0"):
            status_msg = base_resp.get("status_msg")
            suffix = f": {status_msg}" if isinstance(status_msg, str) and status_msg else ""
            raise MiniMaxVideoGenerationError(
                f"{message}{suffix}",
                provider_code=str(status_code),
            )
    code = payload.get("code")
    if code not in (None, 0, "0"):
        raise MiniMaxVideoGenerationError(message, provider_code=str(code))


def _required_string(value: str, field_name: str) -> str:
    if type(value) is not str or not value.strip():
        raise ValueError(f"{field_name} must be nonblank")
    return value.strip()


def _normalized_base(api_base: str) -> str:
    value = _required_string(api_base, "api_base").rstrip("/")
    if value.endswith("/v1"):
        return value
    return f"{value}/v1"


def _safe_filename(filename: str | None) -> str | None:
    if filename is None:
        return None
    name = PurePosixPath(filename).name
    if _SAFE_FILENAME.fullmatch(name) is None:
        return None
    return name


def _filename_suffix(filename: str | None, url: str) -> str | None:
    if filename:
        suffix = PurePosixPath(filename).suffix.lower()
        if suffix:
            return suffix
    suffix = PurePosixPath(urlsplit(url).path).suffix.lower()
    return suffix or None


__all__ = [
    "MiniMaxGeneratedVideo",
    "MiniMaxVideoGenerationClient",
    "MiniMaxVideoGenerationError",
]
