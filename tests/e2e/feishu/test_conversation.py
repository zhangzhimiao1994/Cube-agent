from __future__ import annotations

import asyncio
import io
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from decimal import Decimal
from uuid import uuid4

import pytest
from PIL import Image

from agent_hub.channels.base import (
    AttachmentKind,
    Channel,
    ConversationType,
    InboundAttachment,
    InboundMessage,
)
from agent_hub.channels.feishu.media import (
    FeishuMediaError,
    FeishuMediaService,
    FeishuMediaTooLarge,
)
from agent_hub.channels.feishu.render import (
    FeishuFinalReply,
    FeishuRenderer,
    FeishuVisibleEvent,
    FeishuVisibleEventKind,
)
from agent_hub.models.gateway import GatewayCompletion
from agent_hub.models.types import ModelRequest, ModelResponse
from agent_hub.multimodal.images import MemoryImageStore
from agent_hub.multimodal.service import VisionService
from agent_hub.multimodal.types import ImageCleanupRecoveryItem, VisionAnalysisError

TENANT = "tenant_1"
RUN_ID = "run_1"


@dataclass(slots=True)
class FakeVisionGateway:
    last_request: ModelRequest | None = None
    fail: bool = False

    async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
        self.last_request = request
        if self.fail:
            raise RuntimeError("vision unavailable")
        payload = {
            "source_sha256": "0" * 64,
            "summary": "图片里有一只猫",
            "extracted_text": None,
            "objects": ["cat"],
            "confidence": 0.91,
            "logical_model": "ignored",
            "deployment_id": "ignored",
        }
        return GatewayCompletion(
            response=ModelResponse(text=json.dumps(payload)),
            deployment_id="vision_primary_1",
            logical_model="vision-primary",
            provider_id="testprovider",
            provider_model="testprovider/vision",
            cost_usd=Decimal(0),
        )


@dataclass(slots=True)
class RecordingRecoverySink:
    items: list[ImageCleanupRecoveryItem] = field(default_factory=list)

    async def enqueue(self, item: ImageCleanupRecoveryItem) -> None:
        self.items.append(item)


@dataclass(slots=True)
class FakeMediaClient:
    resources: dict[str, bytes]
    token_requests: list[str] = field(default_factory=list)
    cancel: bool = False

    async def tenant_access_token(self, tenant_external_id: str) -> str:
        self.token_requests.append(tenant_external_id)
        return f"token_for_{tenant_external_id}"

    async def download_resource(
        self,
        *,
        message_id: str,
        resource_key: str,
        tenant_access_token: str,
    ) -> AsyncIterator[bytes]:
        del message_id, tenant_access_token
        if self.cancel:
            raise asyncio.CancelledError
        data = self.resources[resource_key]
        midpoint = max(1, len(data) // 2)
        yield data[:midpoint]
        yield data[midpoint:]


@dataclass(slots=True)
class FeishuConversationFixture:
    media: FeishuMediaService
    renderer: FeishuRenderer
    last_reply_text: str = ""

    async def send_private_image(self, filename: str = "cat.png") -> None:
        message = inbound_image_message(
            InboundAttachment(
                kind=AttachmentKind.IMAGE,
                external_key=filename,
                filename=filename,
                declared_mime="image/png",
            )
        )
        analyses = await self.media.analyze_images(message)
        summary = analyses[0].result.artifact.summary
        self.last_reply_text = self.renderer.final_answer(
            FeishuFinalReply(text=f"图片分析：{summary}")
        )[0]

    async def send(self, text: str) -> None:
        del text
        self.last_reply_text = self.renderer.final_answer(
            FeishuFinalReply(text="评估完成。")
        )[0]


def png_bytes() -> bytes:
    output = io.BytesIO()
    with Image.new("RGB", (1, 1), "white") as image:
        image.save(output, format="PNG")
    return output.getvalue()


def inbound_image_message(*attachments: InboundAttachment) -> InboundMessage:
    return InboundMessage(
        channel=Channel.FEISHU,
        tenant_external_id=TENANT,
        sender_external_id="ou_1",
        conversation_external_id="oc_1",
        message_id=f"om_{uuid4().hex}",
        event_id=f"evt_{uuid4().hex}",
        conversation_type=ConversationType.PRIVATE,
        text="[image]",
        mentions_bot=False,
        attachments=attachments,
        received_at=__import__("datetime").datetime.now(__import__("datetime").UTC),
    )


def media_service(
    client: FakeMediaClient,
    gateway: FakeVisionGateway,
    *,
    max_download_bytes: int = 20_000_000,
) -> FeishuMediaService:
    return FeishuMediaService(
        client=client,
        analyzer=VisionService(
            gateway,
            MemoryImageStore(),
            cleanup_recovery_sink=RecordingRecoverySink(),
        ),
        max_download_bytes=max_download_bytes,
    )


async def test_image_message_routes_to_vision() -> None:
    vision_gateway = FakeVisionGateway()
    feishu_fixture = FeishuConversationFixture(
        media=media_service(FakeMediaClient({"cat.png": png_bytes()}), vision_gateway),
        renderer=FeishuRenderer(),
    )

    await feishu_fixture.send_private_image("cat.png")

    assert vision_gateway.last_request is not None
    assert {capability.value for capability in vision_gateway.last_request.required_capabilities} == {
        "vision",
        "structured_output",
    }
    assert "图片" in feishu_fixture.last_reply_text


async def test_multiple_images_are_downloaded_with_tenant_token() -> None:
    client = FakeMediaClient({"a.png": png_bytes(), "b.png": png_bytes()})
    service = media_service(client, FakeVisionGateway())
    message = inbound_image_message(
        InboundAttachment(
            kind=AttachmentKind.IMAGE,
            external_key="a.png",
            declared_mime="image/png",
        ),
        InboundAttachment(
            kind=AttachmentKind.IMAGE,
            external_key="b.png",
            declared_mime="image/png",
        ),
    )

    analyses = await service.analyze_images(message)

    assert len(analyses) == 2
    assert client.token_requests == [TENANT, TENANT]
    assert analyses[0].channel_metadata["resource_key"] == "a.png"


async def test_invalid_or_oversized_image_is_rejected() -> None:
    with pytest.raises(FeishuMediaError, match="MIME"):
        await media_service(
            FakeMediaClient({"bad.png": b"not image"}),
            FakeVisionGateway(),
        ).analyze_images(
            inbound_image_message(
                InboundAttachment(
                    kind=AttachmentKind.IMAGE,
                    external_key="bad.png",
                    declared_mime="image/png",
                )
            )
        )

    with pytest.raises(FeishuMediaTooLarge):
        await media_service(
            FakeMediaClient({"cat.png": png_bytes()}),
            FakeVisionGateway(),
            max_download_bytes=1,
        ).analyze_images(
            inbound_image_message(
                InboundAttachment(
                    kind=AttachmentKind.IMAGE,
                    external_key="cat.png",
                    declared_mime="image/png",
                )
            )
        )


async def test_media_errors_include_channel_diagnostics() -> None:
    message = inbound_image_message(
        InboundAttachment(
            kind=AttachmentKind.IMAGE,
            external_key="bad.png",
            declared_mime="image/png",
        )
    )

    with pytest.raises(FeishuMediaError) as error_info:
        await media_service(
            FakeMediaClient({"bad.png": b"not image"}),
            FakeVisionGateway(),
        ).analyze_images(message)

    assert error_info.value.diagnostics == {
        "channel": "feishu",
        "message_id": message.message_id,
        "resource_key": "bad.png",
        "tenant_key": TENANT,
        "attachment_kind": "image",
        "reason": "image MIME mismatch",
    }


async def test_media_download_cancellation_propagates() -> None:
    with pytest.raises(asyncio.CancelledError):
        await media_service(
            FakeMediaClient({"cat.png": png_bytes()}, cancel=True),
            FakeVisionGateway(),
        ).analyze_images(
            inbound_image_message(
                InboundAttachment(
                    kind=AttachmentKind.IMAGE,
                    external_key="cat.png",
                    declared_mime="image/png",
                )
            )
        )


async def test_vision_failure_is_redacted() -> None:
    with pytest.raises(VisionAnalysisError, match="vision analysis failed"):
        await media_service(
            FakeMediaClient({"cat.png": png_bytes()}),
            FakeVisionGateway(fail=True),
        ).analyze_images(
            inbound_image_message(
                InboundAttachment(
                    kind=AttachmentKind.IMAGE,
                    external_key="cat.png",
                    declared_mime="image/png",
                )
            )
        )


def test_progress_updates_are_coalesced() -> None:
    now = 10.0
    renderer = FeishuRenderer(monotonic=lambda: now, progress_interval_seconds=5)

    assert renderer.progress(RUN_ID, "planning") == "进度：planning"
    now = 12.0
    assert renderer.progress(RUN_ID, "still planning") is None
    now = 16.0
    assert renderer.progress(RUN_ID, "running") == "进度：running"


async def test_default_reply_hides_internal_discussion() -> None:
    feishu_fixture = FeishuConversationFixture(
        media=media_service(FakeMediaClient({"cat.png": png_bytes()}), FakeVisionGateway()),
        renderer=FeishuRenderer(),
    )

    await feishu_fixture.send("/discuss evaluate proposal")

    assert "最终结论" in feishu_fixture.last_reply_text
    assert "internal_turn" not in feishu_fixture.last_reply_text


def test_details_filters_internal_events_and_requires_permission() -> None:
    renderer = FeishuRenderer()
    events = (
        FeishuVisibleEvent(FeishuVisibleEventKind.DISCUSSION, "agent debated option A"),
        FeishuVisibleEvent(FeishuVisibleEventKind.TOOL, "calculator returned 4"),
        FeishuVisibleEvent(FeishuVisibleEventKind.INTERNAL_TURN, "hidden chain"),
    )

    assert renderer.details(events, can_view_details=False) == ("没有权限查看 details。",)
    details = "\n".join(renderer.details(events, can_view_details=True))

    assert "discussion" in details
    assert "calculator" in details
    assert "internal_turn" not in details
    assert "hidden chain" not in details


def test_final_answer_splits_and_includes_citations_and_warnings() -> None:
    renderer = FeishuRenderer(max_message_chars=80)
    chunks = renderer.final_answer(
        FeishuFinalReply(
            text="结论。" * 40,
            citations=("doc1",),
            warnings=("需要人工复核",),
        )
    )

    assert len(chunks) > 1
    assert "引用" in "\n".join(chunks)
    assert "需要人工复核" in "\n".join(chunks)
