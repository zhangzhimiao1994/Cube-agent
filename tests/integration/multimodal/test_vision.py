from __future__ import annotations

import asyncio
import io
import json
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest
from PIL import Image

from agent_hub.models.gateway import GatewayCompletion
from agent_hub.models.types import ModelCapability, ModelRequest, ModelResponse
from agent_hub.multimodal.images import FilesystemImageStore, sanitize_image
from agent_hub.multimodal.service import VisionService
from agent_hub.multimodal.types import (
    ImageAnalysisArtifact,
    OCRObservation,
    SanitizedImage,
    SignedImageReference,
    StoredImageObject,
    VisionAnalysisError,
    VisionAuditEvent,
)


def png_bytes() -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (5, 4), "red").save(output, "PNG")
    return output.getvalue()


class GatewayStub:
    def __init__(
        self,
        payload: str | None,
        *,
        error: Exception | None = None,
        actual_logical_model: str = "vision-primary",
        deployment_id: str = "vision-deployment",
        provider_id: str = "openai",
        provider_model: str = "openai/gpt-4o-mini",
    ) -> None:
        self.payload = payload
        self.error = error
        self.actual_logical_model = actual_logical_model
        self.deployment_id = deployment_id
        self.provider_id = provider_id
        self.provider_model = provider_model
        self.requests: list[ModelRequest] = []

    async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return GatewayCompletion(
            response=ModelResponse(text=self.payload),
            deployment_id=self.deployment_id,
            logical_model=self.actual_logical_model,
            provider_id=self.provider_id,
            provider_model=self.provider_model,
        )


class OCRStub:
    def __init__(self, observation: OCRObservation) -> None:
        self.observation = observation

    async def extract(self, image: bytes) -> OCRObservation:
        del image
        return self.observation


class AuditStub:
    def __init__(self, *, fail: bool = False) -> None:
        self.events: list[VisionAuditEvent] = []
        self.fail = fail

    async def record(self, event: VisionAuditEvent) -> None:
        self.events.append(event)
        if self.fail:
            raise RuntimeError("private audit failure")


class ReferenceStub:
    def __init__(self, reference: object) -> None:
        self.reference_value = reference

    async def reference(
        self, image: SanitizedImage, stored: StoredImageObject
    ) -> SignedImageReference:
        del image, stored
        return cast(SignedImageReference, self.reference_value)


def response(confidence: float = 0.9, **extra: object) -> str:
    body: dict[str, object] = {
        "source_sha256": "f" * 64,
        "summary": "A red square",
        "extracted_text": None,
        "objects": ["square"],
        "confidence": confidence,
        "logical_model": "hallucinated",
        "deployment_id": "hallucinated",
    }
    body.update(extra)
    return json.dumps(body)


async def test_vision_uses_gateway_capabilities_schema_and_trusted_provenance(tmp_path: Path) -> None:
    gateway = GatewayStub(response())
    audit = AuditStub()
    result = await VisionService(
        gateway, FilesystemImageStore(tmp_path), audit_sink=audit
    ).analyze(png_bytes(), "image/png", "tenant-a", "vision-primary")
    assert result.artifact.source_sha256 != "f" * 64
    assert result.artifact.logical_model == "vision-primary"
    assert result.artifact.deployment_id == "vision-deployment"
    assert not result.ocr_only and not result.requires_review
    request = gateway.requests[0]
    assert request.required_capabilities == frozenset(
        {ModelCapability.VISION, ModelCapability.STRUCTURED_OUTPUT}
    )
    assert request.response_schema is not None
    schema = request.response_schema.schema
    assert schema["additionalProperties"] is False
    properties = schema["properties"]
    assert isinstance(properties, Mapping)
    source_schema = properties["source_sha256"]
    objects_schema = properties["objects"]
    assert isinstance(source_schema, Mapping) and "pattern" in source_schema
    assert isinstance(objects_schema, Mapping)
    item_schema = objects_schema["items"]
    assert isinstance(item_schema, Mapping) and item_schema["maxLength"] == 256
    required = schema["required"]
    assert isinstance(required, tuple)
    required_fields = {item for item in required if isinstance(item, str)}
    assert required_fields == {
        "source_sha256", "summary", "extracted_text", "objects", "confidence",
        "logical_model", "deployment_id",
    }
    content = request.messages[0].content
    assert not isinstance(content, str)
    image_part = content[1]["image_url"]
    assert isinstance(image_part, Mapping)
    assert str(image_part["url"]).startswith("data:image/png;base64,")
    assert len(audit.events) == 1
    assert "data:image" not in repr(audit.events[0])
    assert "canonical_bytes" not in repr(result)
    assert len(list(tmp_path.rglob("*.png"))) == 1


async def test_fallback_artifact_and_audit_use_actual_gateway_provenance(tmp_path: Path) -> None:
    gateway = GatewayStub(
        response(),
        actual_logical_model="vision-backup",
        deployment_id="backup-deployment",
        provider_id="anthropic",
        provider_model="anthropic/claude-vision",
    )
    audit = AuditStub()
    result = await VisionService(
        gateway, FilesystemImageStore(tmp_path), audit_sink=audit
    ).analyze(png_bytes(), "image/png", "tenant", "vision-primary")

    assert result.artifact.logical_model == "vision-backup"
    assert result.artifact.deployment_id == "backup-deployment"
    assert audit.events[0].logical_model == "vision-backup"
    assert audit.events[0].deployment_id == "backup-deployment"
    assert audit.events[0].provider_id == "anthropic"
    assert audit.events[0].provider_model == "anthropic/claude-vision"


@pytest.mark.parametrize(
    "payload",
    [None, "", "not-json-private-output", "{}", response(extra_field="forbidden"),
     response(summary="x" * 2001), response(confidence=float("nan"))],
)
async def test_malformed_model_response_is_safely_rejected(tmp_path: Path, payload: str | None) -> None:
    service = VisionService(GatewayStub(payload), FilesystemImageStore(tmp_path))
    with pytest.raises(VisionAnalysisError, match="vision analysis failed") as captured:
        await service.analyze(png_bytes(), "image/png", "tenant", "vision-primary")
    assert captured.value.__cause__ is None and captured.value.__context__ is None
    assert "private-output" not in repr(captured.value)


async def test_low_confidence_requires_review(tmp_path: Path) -> None:
    result = await VisionService(
        GatewayStub(response(confidence=0.2)), FilesystemImageStore(tmp_path),
        review_threshold=0.7,
    ).analyze(png_bytes(), "image/png", "tenant", "vision-primary")
    assert result.requires_review and not result.ocr_only


async def test_ocr_fallback_is_fixed_and_non_fabricating_for_text_image(tmp_path: Path) -> None:
    ocr = OCRStub(OCRObservation(text="invoice 123", confidence=0.8, text_oriented=True))
    service = VisionService(
        GatewayStub(None, error=RuntimeError("provider detail")),
        FilesystemImageStore(tmp_path), ocr_adapter=ocr,
    )
    result = await service.analyze(png_bytes(), "image/png", "tenant", "vision-primary")
    assert result.ocr_only and result.requires_review
    assert result.artifact.summary == "OCR-only text extraction; visual description unavailable"
    assert result.artifact.extracted_text == "invoice 123"
    assert result.artifact.objects == []
    assert result.artifact.deployment_id == "local-ocr"


async def test_non_text_oriented_ocr_cannot_replace_vision(tmp_path: Path) -> None:
    ocr = OCRStub(OCRObservation(text="noise", confidence=0.8, text_oriented=False))
    service = VisionService(
        GatewayStub(None, error=RuntimeError("down")), FilesystemImageStore(tmp_path),
        ocr_adapter=ocr,
    )
    with pytest.raises(VisionAnalysisError, match="vision analysis failed"):
        await service.analyze(png_bytes(), "image/png", "tenant", "vision-primary")


async def test_failed_analysis_removes_stored_object(tmp_path: Path) -> None:
    service = VisionService(GatewayStub("not-json"), FilesystemImageStore(tmp_path))
    with pytest.raises(VisionAnalysisError, match="vision analysis failed"):
        await service.analyze(png_bytes(), "image/png", "tenant", "vision-primary")
    assert list(tmp_path.rglob("*.png")) == []


async def test_audit_failure_is_fail_closed(tmp_path: Path) -> None:
    service = VisionService(
        GatewayStub(response()), FilesystemImageStore(tmp_path), audit_sink=AuditStub(fail=True)
    )
    with pytest.raises(VisionAnalysisError, match="vision audit failed"):
        await service.analyze(png_bytes(), "image/png", "tenant", "vision-primary")
    assert list(tmp_path.rglob("*.png")) == []


async def test_cancellation_propagates(tmp_path: Path) -> None:
    class CancellingGateway(GatewayStub):
        async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
            del request
            raise asyncio.CancelledError("caller")

    raw = png_bytes()
    canonical = sanitize_image(raw, "image/png", "tenant").canonical_bytes
    service = VisionService(CancellingGateway(response()), FilesystemImageStore(tmp_path))
    with pytest.raises(asyncio.CancelledError, match="caller") as captured:
        await service.analyze(raw, "image/png", "tenant", "vision-primary")
    assert captured.value.args == ("caller",)
    assert list(tmp_path.rglob("*.png")) == []
    traceback = captured.value.__traceback__
    while traceback is not None:
        if traceback.tb_frame.f_globals.get("__name__", "").startswith("agent_hub"):
            for value in traceback.tb_frame.f_locals.values():
                assert not isinstance(value, SanitizedImage)
                assert value != raw and value != canonical
        traceback = traceback.tb_next


async def test_reference_cancellation_cleans_up_and_preserves_identity(tmp_path: Path) -> None:
    cancellation = asyncio.CancelledError("reference-cancelled")

    class CancellingReference:
        async def reference(
            self, image: SanitizedImage, stored: StoredImageObject
        ) -> SignedImageReference:
            del image, stored
            raise cancellation

    service = VisionService(
        GatewayStub(response()),
        FilesystemImageStore(tmp_path),
        reference_provider=CancellingReference(),
    )
    with pytest.raises(asyncio.CancelledError) as captured:
        await service.analyze(png_bytes(), "image/png", "tenant", "vision-primary")
    assert captured.value is cancellation
    assert list(tmp_path.rglob("*.png")) == []


async def test_audit_cancellation_cleans_up_and_preserves_identity(tmp_path: Path) -> None:
    cancellation = asyncio.CancelledError("audit-cancelled")

    class CancellingAudit:
        async def record(self, event: VisionAuditEvent) -> None:
            del event
            raise cancellation

    service = VisionService(
        GatewayStub(response()),
        FilesystemImageStore(tmp_path),
        audit_sink=CancellingAudit(),
    )
    with pytest.raises(asyncio.CancelledError) as captured:
        await service.analyze(png_bytes(), "image/png", "tenant", "vision-primary")
    assert captured.value is cancellation
    assert list(tmp_path.rglob("*.png")) == []


async def test_data_url_ceiling_fails_safely(tmp_path: Path) -> None:
    service = VisionService(
        GatewayStub(response()), FilesystemImageStore(tmp_path), max_reference_length=30
    )
    with pytest.raises(VisionAnalysisError, match="image reference failed") as captured:
        await service.analyze(png_bytes(), "image/png", "tenant", "vision-primary")
    locals_repr: list[str] = []
    traceback = captured.value.__traceback__
    while traceback is not None:
        if traceback.tb_frame.f_globals.get("__name__", "").startswith("agent_hub"):
            locals_repr.extend(repr(value) for value in traceback.tb_frame.f_locals.values())
        traceback = traceback.tb_next
    assert "data:image/png;base64" not in " ".join(locals_repr)


@pytest.mark.parametrize(
    "reference",
    [
        "http://trusted.example/image",
        "https://user@trusted.example/image",
        "https://trusted.example/image#fragment",
        "https://trusted.example/image with space",
        "https://trusted.example/line\nbreak",
        "https://127.0.0.1/image?sig=value",
        "https://169.254.169.254/image?sig=value",
        "https://[::1]/image?sig=value",
        "https://bucket.local/image?sig=value",
    ],
)
async def test_reference_provider_must_return_strict_https_url(
    tmp_path: Path, reference: str
) -> None:
    service = VisionService(
        GatewayStub(response()),
        FilesystemImageStore(tmp_path),
        reference_provider=ReferenceStub(
            SignedImageReference(
                url=reference,
                expires_at=datetime.now(UTC) + timedelta(minutes=1),
                signed=True,
                provider_id="signed-store",
            )
        ),
    )
    with pytest.raises(VisionAnalysisError, match="image reference failed"):
        await service.analyze(png_bytes(), "image/png", "tenant", "vision-primary")


@pytest.mark.parametrize("seconds", [-1, 301])
async def test_signed_reference_must_be_unexpired_and_short_lived(
    tmp_path: Path, seconds: int
) -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    signed = SignedImageReference(
        url="https://public.example/image?sig=value",
        expires_at=now + timedelta(seconds=seconds),
        signed=True,
        provider_id="signed-store",
    )
    service = VisionService(
        GatewayStub(response()),
        FilesystemImageStore(tmp_path),
        reference_provider=ReferenceStub(signed),
        utc_now=lambda: now,
    )
    with pytest.raises(VisionAnalysisError, match="image reference failed"):
        await service.analyze(png_bytes(), "image/png", "tenant", "vision-primary")
    assert list(tmp_path.rglob("*.png")) == []


async def test_valid_short_lived_signed_reference_is_passed_to_gateway(tmp_path: Path) -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    url = "https://public.example/image?sig=value"
    gateway = GatewayStub(response())
    service = VisionService(
        gateway,
        FilesystemImageStore(tmp_path),
        reference_provider=ReferenceStub(
            SignedImageReference(
                url=url,
                expires_at=now + timedelta(seconds=60),
                signed=True,
                provider_id="signed-store",
            )
        ),
        utc_now=lambda: now,
    )
    result = await service.analyze(png_bytes(), "image/png", "tenant", "vision-primary")
    content = gateway.requests[0].messages[0].content
    assert not isinstance(content, str)
    image_part = content[1]["image_url"]
    assert isinstance(image_part, Mapping) and image_part["url"] == url
    assert result.stored_object.object_key


async def test_async_invalid_image_traceback_does_not_retain_payload(tmp_path: Path) -> None:
    payload = b"private-async-image" * 100
    service = VisionService(GatewayStub(response()), FilesystemImageStore(tmp_path))
    with pytest.raises(ValueError, match="image rejected") as captured:
        await service.analyze(payload, "image/png", "tenant", "vision-primary")
    locals_repr: list[str] = []
    traceback = captured.value.__traceback__
    while traceback is not None:
        if traceback.tb_frame.f_globals.get("__name__", "").startswith("agent_hub"):
            locals_repr.extend(repr(value) for value in traceback.tb_frame.f_locals.values())
        traceback = traceback.tb_next
    assert payload.decode() not in " ".join(locals_repr)


async def test_model_failure_traceback_does_not_retain_sanitized_image_or_bytes(
    tmp_path: Path,
) -> None:
    canonical = sanitize_image(png_bytes(), "image/png", "tenant").canonical_bytes
    service = VisionService(GatewayStub("not-json"), FilesystemImageStore(tmp_path))
    with pytest.raises(VisionAnalysisError, match="vision analysis failed") as captured:
        await service.analyze(png_bytes(), "image/png", "tenant", "vision-primary")
    traceback = captured.value.__traceback__
    while traceback is not None:
        if traceback.tb_frame.f_globals.get("__name__", "").startswith("agent_hub"):
            for value in traceback.tb_frame.f_locals.values():
                assert not isinstance(value, SanitizedImage)
                assert value != canonical
        traceback = traceback.tb_next


async def test_bare_https_reference_provider_is_not_a_trusted_signed_contract(
    tmp_path: Path,
) -> None:
    service = VisionService(
        GatewayStub(response()),
        FilesystemImageStore(tmp_path),
        reference_provider=ReferenceStub("https://public.example/image?sig=value"),
    )
    with pytest.raises(VisionAnalysisError, match="image reference failed"):
        await service.analyze(png_bytes(), "image/png", "tenant", "vision-primary")
    assert list(tmp_path.rglob("*.png")) == []


async def test_recursive_json_is_a_stable_analysis_failure(tmp_path: Path) -> None:
    recursive = "[" * 2000 + "0" + "]" * 2000
    service = VisionService(GatewayStub(recursive), FilesystemImageStore(tmp_path))
    with pytest.raises(VisionAnalysisError, match="^vision analysis failed$") as captured:
        await service.analyze(png_bytes(), "image/png", "tenant", "vision-primary")
    assert captured.value.__cause__ is None and captured.value.__context__ is None
    assert list(tmp_path.rglob("*.png")) == []


def test_artifact_repr_hides_model_generated_content() -> None:
    artifact = ImageAnalysisArtifact(
        source_sha256="a" * 64,
        summary="private summary",
        extracted_text="private extracted text",
        objects=["private object"],
        confidence=0.5,
        logical_model="vision-primary",
        deployment_id="vision-deployment",
    )
    exposed = repr(artifact)
    assert "private summary" not in exposed
    assert "private extracted text" not in exposed
    assert "private object" not in exposed


def test_signed_reference_contract_is_strict_and_hides_url() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    reference = SignedImageReference(
        url="https://public.example/image?private-signature",
        expires_at=now,
        signed=True,
        provider_id="signed-store",
    )
    assert "private-signature" not in repr(reference)
    with pytest.raises(ValueError, match="signed"):
        SignedImageReference(
            url="https://public.example/image",
            expires_at=now,
            signed=False,
            provider_id="signed-store",
        )
    with pytest.raises(ValueError, match="timezone-aware"):
        SignedImageReference(
            url="https://public.example/image",
            expires_at=datetime(2026, 1, 1),  # noqa: DTZ001 - exercises naive rejection
            signed=True,
            provider_id="signed-store",
        )
    with pytest.raises(ValueError, match="timezone-aware"):
        SignedImageReference(
            url="https://public.example/image",
            expires_at="tomorrow",  # type: ignore[arg-type]
            signed=True,
            provider_id="signed-store",
        )
