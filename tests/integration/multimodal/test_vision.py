from __future__ import annotations

import asyncio
import io
import json
import threading
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
    def __init__(
        self,
        reference: object,
        *,
        allowed_hosts: frozenset[str] = frozenset({"public.example", "trusted.example"}),
    ) -> None:
        self.reference_value = reference
        self.allowed_hosts = allowed_hosts

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
        allowed_hosts = frozenset({"public.example"})

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


async def _wait_for_thread_event(event: threading.Event) -> None:
    while not event.is_set():
        await asyncio.sleep(0)


class BlockingFilesystemStore(FilesystemImageStore):
    def __init__(self, root: Path, *, block_put: bool = False, block_delete: bool = True) -> None:
        super().__init__(root)
        self.put_started = threading.Event()
        self.put_finished = threading.Event()
        self.put_release = threading.Event()
        self.delete_started = threading.Event()
        self.delete_release = threading.Event()
        if not block_put:
            self.put_release.set()
        if not block_delete:
            self.delete_release.set()

    def _put_sync_safe(
        self, tenant_id: str, object_key: str, data: bytes, content_type: str
    ) -> StoredImageObject | ValueError | None:
        self.put_started.set()
        self.put_release.wait()
        result = super()._put_sync_safe(tenant_id, object_key, data, content_type)
        self.put_finished.set()
        return result

    def _delete_sync_safe(self, tenant_id: str, object_key: str) -> bool:
        self.delete_started.set()
        self.delete_release.wait()
        return super()._delete_sync_safe(tenant_id, object_key)


async def test_repeated_put_and_cleanup_cancellation_cannot_leave_late_file(
    tmp_path: Path,
) -> None:
    store = BlockingFilesystemStore(tmp_path, block_put=True)
    task = asyncio.create_task(
        VisionService(GatewayStub(response()), store).analyze(
            png_bytes(), "image/png", "tenant", "vision-primary"
        )
    )
    await _wait_for_thread_event(store.put_started)
    task.cancel("first-cancel")
    await asyncio.sleep(0)
    assert not task.done()
    task.cancel("second-cancel")
    await asyncio.sleep(0)
    delete_started_before_put_release = store.delete_started.is_set()
    store.put_release.set()
    await _wait_for_thread_event(store.delete_started)
    put_finished_before_delete = store.put_finished.is_set()
    task.cancel("third-cancel")
    await asyncio.sleep(0)
    task.cancel("fourth-cancel")
    store.delete_release.set()

    with pytest.raises(asyncio.CancelledError) as captured:
        await task
    assert captured.value.args == ("first-cancel",)
    assert task.cancelled()
    assert not delete_started_before_put_release
    assert put_finished_before_delete
    assert list(tmp_path.rglob("*.png")) == []


async def test_repeated_model_phase_cancellation_cannot_interrupt_cleanup(
    tmp_path: Path,
) -> None:
    gateway_started = asyncio.Event()

    class BlockingGateway(GatewayStub):
        async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
            del request
            gateway_started.set()
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

    store = BlockingFilesystemStore(tmp_path)
    task = asyncio.create_task(
        VisionService(BlockingGateway(response()), store).analyze(
            png_bytes(), "image/png", "tenant", "vision-primary"
        )
    )
    await gateway_started.wait()
    task.cancel("model-cancel")
    await _wait_for_thread_event(store.delete_started)
    task.cancel("cleanup-cancel-two")
    await asyncio.sleep(0)
    task.cancel("cleanup-cancel-three")
    store.delete_release.set()

    with pytest.raises(asyncio.CancelledError) as captured:
        await task
    assert captured.value.args == ("model-cancel",)
    assert task.cancelled()
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


@pytest.mark.parametrize(
    ("allowed_hosts", "url"),
    [
        (frozenset({"trusted.example"}), "https://evil.example/image?sig=value"),
        (frozenset({"trusted.example"}), "https://trusted.example.evil/image?sig=value"),
        (frozenset({"trusted.example"}), "https://sub.trusted.example/image?sig=value"),
        (frozenset({"trusted.example"}), "https://trusted.example:8443/image?sig=value"),
        (frozenset({"trusted.example"}), "https://anything.nip.io/image?sig=value"),
        (frozenset({"trusted.example"}), "https://localhost.example/image?sig=value"),
    ],
)
async def test_signed_reference_host_must_exactly_match_trusted_allowlist(
    tmp_path: Path, allowed_hosts: frozenset[str], url: str
) -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    service = VisionService(
        GatewayStub(response()),
        FilesystemImageStore(tmp_path),
        reference_provider=ReferenceStub(
            SignedImageReference(
                url=url,
                expires_at=now + timedelta(seconds=60),
                signed=True,
                provider_id="signed-store",
            ),
            allowed_hosts=allowed_hosts,
        ),
        utc_now=lambda: now,
    )
    with pytest.raises(VisionAnalysisError, match="image reference failed"):
        await service.analyze(png_bytes(), "image/png", "tenant", "vision-primary")
    assert list(tmp_path.rglob("*.png")) == []


@pytest.mark.parametrize(
    ("allowed_host", "url"),
    [
        ("TRUSTED.EXAMPLE", "https://trusted.example:443/image?sig=value"),
        ("trusted.example:8443", "https://TRUSTED.EXAMPLE:8443/image?sig=value"),
        ("bücher.example", "https://BÜCHER.example/image?sig=value"),
        ("sub.trusted.example", "https://sub.trusted.example/image?sig=value"),
    ],
)
async def test_trusted_reference_authority_normalization_is_exact(
    tmp_path: Path, allowed_host: str, url: str
) -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    gateway = GatewayStub(response())
    result = await VisionService(
        gateway,
        FilesystemImageStore(tmp_path),
        reference_provider=ReferenceStub(
            SignedImageReference(
                url=url,
                expires_at=now + timedelta(seconds=60),
                signed=True,
                provider_id="signed-store",
            ),
            allowed_hosts=frozenset({allowed_host}),
        ),
        utc_now=lambda: now,
    ).analyze(png_bytes(), "image/png", "tenant", "vision-primary")
    assert result.artifact.summary == "A red square"


@pytest.mark.parametrize(
    "allowed_host",
    [
        "trusted.example:0",
        "trusted.example:",
        "trusted.example:-1",
        "trusted.example:65536",
        "trusted.example:not-a-port",
    ],
)
def test_reference_allowlist_rejects_invalid_or_empty_ports(
    tmp_path: Path, allowed_host: str
) -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    with pytest.raises(ValueError, match="allowlist"):
        VisionService(
            GatewayStub(response()),
            FilesystemImageStore(tmp_path),
            reference_provider=ReferenceStub(
                SignedImageReference(
                    url="https://trusted.example/image?sig=value",
                    expires_at=now + timedelta(seconds=60),
                    signed=True,
                    provider_id="signed-store",
                ),
                allowed_hosts=frozenset({allowed_host}),
            ),
            utc_now=lambda: now,
        )


@pytest.mark.parametrize(
    "url",
    [
        "https://trusted.example:0/image?sig=value",
        "https://trusted.example:/image?sig=value",
        "https://trusted.example:-1/image?sig=value",
        "https://trusted.example:65536/image?sig=value",
        "https://trusted.example:not-a-port/image?sig=value",
    ],
)
async def test_signed_reference_rejects_invalid_or_empty_ports(tmp_path: Path, url: str) -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    service = VisionService(
        GatewayStub(response()),
        FilesystemImageStore(tmp_path),
        reference_provider=ReferenceStub(
            SignedImageReference(
                url=url,
                expires_at=now + timedelta(seconds=60),
                signed=True,
                provider_id="signed-store",
            ),
            allowed_hosts=frozenset({"trusted.example"}),
        ),
        utc_now=lambda: now,
    )
    with pytest.raises(VisionAnalysisError, match="image reference failed"):
        await service.analyze(png_bytes(), "image/png", "tenant", "vision-primary")


@pytest.mark.parametrize(
    ("allowed_host", "url"),
    [
        ("trusted.example", "https://trusted.example/image?sig=value"),
        ("trusted.example", "https://trusted.example:443/image?sig=value"),
        ("trusted.example:443", "https://trusted.example/image?sig=value"),
    ],
)
async def test_default_and_explicit_https_port_are_equivalent(
    tmp_path: Path, allowed_host: str, url: str
) -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    result = await VisionService(
        GatewayStub(response()),
        FilesystemImageStore(tmp_path),
        reference_provider=ReferenceStub(
            SignedImageReference(
                url=url,
                expires_at=now + timedelta(seconds=60),
                signed=True,
                provider_id="signed-store",
            ),
            allowed_hosts=frozenset({allowed_host}),
        ),
        utc_now=lambda: now,
    ).analyze(png_bytes(), "image/png", "tenant", "vision-primary")
    assert result.artifact.summary == "A red square"


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
