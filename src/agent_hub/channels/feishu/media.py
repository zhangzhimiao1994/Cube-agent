from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import quote

import httpx

from agent_hub.channels.base import AttachmentKind, InboundAttachment, InboundMessage
from agent_hub.channels.feishu.settings import FeishuSettings
from agent_hub.multimodal.images import StrictSignatureDetector
from agent_hub.multimodal.types import VisionAnalysisResult

_LOGGER = logging.getLogger(__name__)
_DEFAULT_FEISHU_API_BASE = "https://open.feishu.cn/open-apis"


class FeishuMediaError(RuntimeError):
    def __init__(self, message: str, *, diagnostics: dict[str, str] | None = None) -> None:
        super().__init__(message)
        self.diagnostics = diagnostics or {}


class FeishuMediaTooLarge(FeishuMediaError):
    pass


class FeishuMediaClient(Protocol):
    async def tenant_access_token(self, tenant_external_id: str) -> str: ...

    def download_resource(
        self,
        *,
        message_id: str,
        resource_key: str,
        tenant_access_token: str,
        resource_type: str = "image",
    ) -> AsyncIterator[bytes]: ...


class FeishuOpenAPIMediaClient:
    def __init__(
        self,
        *,
        settings: FeishuSettings,
        api_base: str = _DEFAULT_FEISHU_API_BASE,
        timeout_seconds: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._settings = settings
        self._api_base = api_base.rstrip("/")
        self._timeout_seconds = timeout_seconds
        self._transport = transport

    async def tenant_access_token(self, tenant_external_id: str) -> str:
        del tenant_external_id
        url = f"{self._api_base}/auth/v3/tenant_access_token/internal"
        async with self._client() as client:
            response = await client.post(
                url,
                json={
                    "app_id": self._settings.app_id,
                    "app_secret": self._settings.app_secret_value(),
                },
            )
        if response.status_code >= 400:
            raise FeishuMediaError(f"token request failed status={response.status_code}")
        payload = _json_object(response)
        if payload.get("code") != 0:
            raise FeishuMediaError(f"token request rejected code={payload.get('code')}")
        token = payload.get("tenant_access_token")
        if not isinstance(token, str) or not token:
            raise FeishuMediaError("token response is missing tenant_access_token")
        return token

    async def download_resource(
        self,
        *,
        message_id: str,
        resource_key: str,
        tenant_access_token: str,
        resource_type: str = "image",
    ) -> AsyncIterator[bytes]:
        url = (
            f"{self._api_base}/im/v1/messages/{quote(message_id, safe='')}"
            f"/resources/{quote(resource_key, safe='')}"
        )
        async with self._client() as client, client.stream(
            "GET",
            url,
            params={"type": resource_type},
            headers={"Authorization": f"Bearer {tenant_access_token}"},
        ) as response:
            if response.status_code >= 400:
                raise FeishuMediaError(
                    f"media download request failed status={response.status_code}"
                )
            async for chunk in response.aiter_bytes():
                yield chunk

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=self._timeout_seconds,
            transport=self._transport,
        )


class VisionAnalyzer(Protocol):
    async def analyze(
        self,
        raw: bytes | bytearray | memoryview,
        declared_mime: str,
        tenant_id: str,
        logical_model: str = "vision-primary",
    ) -> VisionAnalysisResult: ...


@dataclass(frozen=True, slots=True)
class FeishuImageAnalysis:
    result: VisionAnalysisResult
    channel_metadata: dict[str, str]


class FeishuMediaService:
    def __init__(
        self,
        *,
        client: FeishuMediaClient,
        analyzer: VisionAnalyzer,
        max_download_bytes: int = 20_000_000,
        log_service: object | None = None,
    ) -> None:
        if max_download_bytes <= 0:
            raise ValueError("max_download_bytes must be positive")
        self._client = client
        self._analyzer = analyzer
        self._max_download_bytes = max_download_bytes
        self._detector = StrictSignatureDetector()
        self._log_service = log_service

    async def analyze_images(
        self,
        message: InboundMessage,
    ) -> tuple[FeishuImageAnalysis, ...]:
        analyses: list[FeishuImageAnalysis] = []
        for attachment in message.attachments:
            if attachment.kind is not AttachmentKind.IMAGE:
                continue
            try:
                analyses.append(await self._analyze_one(message, attachment))
            except FeishuMediaError as error:
                await log_feishu_media_failure(
                    log_service=self._log_service,
                    error=error,
                )
                raise
        return tuple(analyses)

    async def _analyze_one(
        self,
        message: InboundMessage,
        attachment: InboundAttachment,
    ) -> FeishuImageAnalysis:
        token = await self._client.tenant_access_token(message.tenant_external_id)
        diagnostics = _diagnostics(message, attachment)
        raw = await self._download_bounded(
            message_id=message.message_id,
            resource_key=attachment.external_key,
            tenant_access_token=token,
            diagnostics=diagnostics,
        )
        detected_mime = self._detector.detect(raw)
        declared_mime = attachment.declared_mime or detected_mime
        if detected_mime is None or declared_mime != detected_mime:
            raise FeishuMediaError(
                "image MIME mismatch",
                diagnostics={**diagnostics, "reason": "image MIME mismatch"},
            )
        result = await self._analyzer.analyze(
            raw,
            detected_mime,
            message.tenant_external_id,
        )
        return FeishuImageAnalysis(
            result=result,
            channel_metadata={
                "channel": message.channel.value,
                "message_id": message.message_id,
                "resource_key": attachment.external_key,
                "detected_mime": detected_mime,
            },
        )

    async def _download_bounded(
        self,
        *,
        message_id: str,
        resource_key: str,
        tenant_access_token: str,
        diagnostics: dict[str, str],
    ) -> bytes:
        chunks: list[bytes] = []
        total = 0
        async for chunk in self._client.download_resource(
            message_id=message_id,
            resource_key=resource_key,
            tenant_access_token=tenant_access_token,
        ):
            if type(chunk) is not bytes or not chunk:
                raise FeishuMediaError(
                    "invalid media chunk",
                    diagnostics={**diagnostics, "reason": "invalid media chunk"},
                )
            total += len(chunk)
            if total > self._max_download_bytes:
                raise FeishuMediaTooLarge(
                    "media exceeds limit",
                    diagnostics={**diagnostics, "reason": "media exceeds limit"},
                )
            chunks.append(chunk)
        if total == 0:
            raise FeishuMediaError(
                "empty media",
                diagnostics={**diagnostics, "reason": "empty media"},
            )
        return b"".join(chunks)


def _diagnostics(message: InboundMessage, attachment: InboundAttachment) -> dict[str, str]:
    return {
        "channel": message.channel.value,
        "message_id": message.message_id,
        "resource_key": attachment.external_key,
        "tenant_key": message.tenant_external_id,
        "attachment_kind": attachment.kind.value,
    }


def _json_object(response: httpx.Response) -> dict[str, object]:
    try:
        payload = response.json()
    except ValueError as error:
        raise FeishuMediaError("Feishu response is not JSON") from error
    if not isinstance(payload, dict):
        raise FeishuMediaError("Feishu response is not an object")
    return payload


async def log_feishu_media_failure(
    *,
    log_service: object | None,
    error: FeishuMediaError,
) -> None:
    _LOGGER.warning(
        "feishu_media_failed error_type=%s reason=%s",
        type(error).__name__,
        str(error) or "Feishu media failed",
    )
    if log_service is None:
        return
    recorder = getattr(log_service, "record_log", None)
    if recorder is None:
        return
    details = {**error.diagnostics, "error_type": type(error).__name__}
    try:
        await recorder(
            category="channel_error",
            level="error",
            title="Feishu media analysis failed",
            message=str(error) or "Feishu media failed",
            source="channels.feishu.media",
            details=details,
        )
    except Exception:
        _LOGGER.exception("feishu_media_failure_log_failed")


__all__ = [
    "FeishuImageAnalysis",
    "FeishuMediaClient",
    "FeishuMediaError",
    "FeishuMediaService",
    "FeishuMediaTooLarge",
    "FeishuOpenAPIMediaClient",
    "VisionAnalyzer",
    "log_feishu_media_failure",
]
