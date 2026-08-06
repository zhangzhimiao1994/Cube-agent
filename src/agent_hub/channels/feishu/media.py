from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Protocol

from agent_hub.channels.base import AttachmentKind, InboundAttachment, InboundMessage
from agent_hub.multimodal.images import StrictSignatureDetector
from agent_hub.multimodal.types import VisionAnalysisResult


class FeishuMediaError(RuntimeError):
    pass


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
    ) -> AsyncIterator[bytes]: ...


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
    ) -> None:
        if max_download_bytes <= 0:
            raise ValueError("max_download_bytes must be positive")
        self._client = client
        self._analyzer = analyzer
        self._max_download_bytes = max_download_bytes
        self._detector = StrictSignatureDetector()

    async def analyze_images(
        self,
        message: InboundMessage,
    ) -> tuple[FeishuImageAnalysis, ...]:
        analyses: list[FeishuImageAnalysis] = []
        for attachment in message.attachments:
            if attachment.kind is not AttachmentKind.IMAGE:
                continue
            analyses.append(await self._analyze_one(message, attachment))
        return tuple(analyses)

    async def _analyze_one(
        self,
        message: InboundMessage,
        attachment: InboundAttachment,
    ) -> FeishuImageAnalysis:
        token = await self._client.tenant_access_token(message.tenant_external_id)
        raw = await self._download_bounded(
            message_id=message.message_id,
            resource_key=attachment.external_key,
            tenant_access_token=token,
        )
        detected_mime = self._detector.detect(raw)
        declared_mime = attachment.declared_mime or detected_mime
        if detected_mime is None or declared_mime != detected_mime:
            raise FeishuMediaError("image MIME mismatch")
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
    ) -> bytes:
        chunks: list[bytes] = []
        total = 0
        async for chunk in self._client.download_resource(
            message_id=message_id,
            resource_key=resource_key,
            tenant_access_token=tenant_access_token,
        ):
            if type(chunk) is not bytes or not chunk:
                raise FeishuMediaError("invalid media chunk")
            total += len(chunk)
            if total > self._max_download_bytes:
                raise FeishuMediaTooLarge("media exceeds limit")
            chunks.append(chunk)
        if total == 0:
            raise FeishuMediaError("empty media")
        return b"".join(chunks)


__all__ = [
    "FeishuImageAnalysis",
    "FeishuMediaClient",
    "FeishuMediaError",
    "FeishuMediaService",
    "FeishuMediaTooLarge",
    "VisionAnalyzer",
]
