from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import math
from typing import Annotated, Protocol
from urllib.parse import urlsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    field_validator,
)

from agent_hub.models.gateway import GatewayCompletion
from agent_hub.models.types import (
    ModelCapability,
    ModelMessage,
    ModelRequest,
    StructuredResponseSchema,
)
from agent_hub.multimodal.images import sanitize_image
from agent_hub.multimodal.types import (
    ImageAnalysisArtifact,
    ImageLimits,
    ImageObjectStore,
    ImageReferenceProvider,
    InvalidImage,
    OCRAdapter,
    OCRObservation,
    SanitizedImage,
    StoredImageObject,
    VisionAnalysisError,
    VisionAnalysisResult,
    VisionAuditEvent,
    VisionAuditSink,
)

_OCR_SUMMARY = "OCR-only text extraction; visual description unavailable"
_ObjectLabel = Annotated[str, StringConstraints(min_length=1, max_length=256)]


class VisionGateway(Protocol):
    async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion: ...


class _ModelVisionPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    source_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    summary: str = Field(min_length=1, max_length=2000)
    extracted_text: str | None = Field(max_length=20_000)
    objects: list[_ObjectLabel] = Field(max_length=100)
    confidence: float = Field(ge=0, le=1, allow_inf_nan=False)
    logical_model: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,127}$")
    deployment_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,127}$")

    @field_validator("objects")
    @classmethod
    def validate_objects(cls, values: list[str]) -> list[str]:
        if any(not value or value != value.strip() or len(value) > 256 for value in values):
            raise ValueError("objects must be bounded unpadded strings")
        return values


class NoopVisionAuditSink:
    async def record(self, event: VisionAuditEvent) -> None:
        del event


def _parse_payload(raw_response: str | None) -> _ModelVisionPayload | None:
    if not raw_response or len(raw_response) > 100_000:
        return None
    try:
        decoded = json.loads(raw_response)
        return _ModelVisionPayload.model_validate(decoded)
    except (json.JSONDecodeError, TypeError, ValueError, ValidationError):
        return None


def _valid_https_reference(reference: str, maximum: int) -> bool:
    if (
        type(reference) is not str
        or not reference
        or reference != reference.strip()
        or len(reference) > maximum
        or any(
            character.isspace() or ord(character) < 32 or ord(character) == 127
            for character in reference
        )
    ):
        return False
    try:
        parsed = urlsplit(reference)
        _ = parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme == "https"
        and parsed.hostname is not None
        and parsed.username is None
        and parsed.password is None
        and not parsed.fragment
    )


class VisionService:
    """Sanitize, persist, analyze through the gateway, and safely audit an image."""

    def __init__(
        self,
        gateway: VisionGateway,
        object_store: ImageObjectStore,
        *,
        limits: ImageLimits | None = None,
        ocr_adapter: OCRAdapter | None = None,
        reference_provider: ImageReferenceProvider | None = None,
        audit_sink: VisionAuditSink | None = None,
        review_threshold: float = 0.7,
        max_reference_length: int = 30_000_000,
        max_concurrent_image_tasks: int = 4,
    ) -> None:
        if (
            isinstance(review_threshold, bool)
            or not isinstance(review_threshold, int | float)
            or not math.isfinite(review_threshold)
            or not 0 <= review_threshold <= 1
        ):
            raise ValueError("review_threshold must be finite and between zero and one")
        if type(max_reference_length) is not int or max_reference_length <= 0:
            raise ValueError("max_reference_length must be a strict positive integer")
        if type(max_concurrent_image_tasks) is not int or max_concurrent_image_tasks <= 0:
            raise ValueError("max_concurrent_image_tasks must be a strict positive integer")
        self._gateway = gateway
        self._object_store = object_store
        self._limits = limits or ImageLimits()
        self._ocr = ocr_adapter
        self._reference_provider = reference_provider
        self._audit = audit_sink or NoopVisionAuditSink()
        self._review_threshold = float(review_threshold)
        self._max_reference_length = max_reference_length
        self._image_slots = asyncio.Semaphore(max_concurrent_image_tasks)

    async def analyze(
        self,
        raw: bytes | bytearray | memoryview,
        declared_mime: str,
        tenant_id: str,
        logical_model: str = "vision-primary",
    ) -> VisionAnalysisResult:
        immutable = bytes(raw) if isinstance(raw, bytes | bytearray | memoryview) else raw
        del raw
        sanitization = asyncio.create_task(
            self._sanitize_input(immutable, declared_mime, tenant_id)
        )
        del immutable
        try:
            sanitized = await sanitization
        except asyncio.CancelledError:
            del sanitization
            raise
        del sanitization
        if sanitized is None:
            raise InvalidImage("image rejected") from None
        del declared_mime
        tenant_digest = hashlib.sha256(tenant_id.encode("utf-8")).hexdigest()
        try:
            stored = await self._object_store.put(
                tenant_id,
                sanitized.object_key,
                sanitized.canonical_bytes,
                sanitized.media_type,
            )
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 - adapter details are untrusted
            error.__traceback__ = None
            del error, tenant_id
            raise VisionAnalysisError("image storage failed") from None
        del tenant_id
        if (
            not isinstance(stored, StoredImageObject)
            or stored.object_key != sanitized.object_key
            or stored.content_type != sanitized.media_type
            or stored.sha256 != sanitized.canonical_sha256
            or stored.byte_length != len(sanitized.canonical_bytes)
        ):
            raise VisionAnalysisError("image storage failed") from None

        reference = await self._image_reference(sanitized, stored)
        request = self._request(logical_model, reference)
        completion: GatewayCompletion | None = None
        try:
            completion = await self._gateway.complete_with_context(request)
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 - gateway adapter details are redacted
            error.__traceback__ = None
            del error
        del request, reference

        payload = None if completion is None else _parse_payload(completion.response.text)
        artifact = self._trusted_artifact(payload, sanitized, logical_model, completion)
        primary_unavailable = artifact is None
        low_confidence = artifact is not None and artifact.confidence < self._review_threshold

        if self._ocr is not None and (primary_unavailable or low_confidence):
            ocr = await self._safe_ocr(sanitized.canonical_bytes)
            if ocr is not None and ocr.text_oriented:
                artifact = ImageAnalysisArtifact(
                    source_sha256=sanitized.source_sha256,
                    summary=_OCR_SUMMARY,
                    extracted_text=ocr.text,
                    objects=[],
                    confidence=float(ocr.confidence),
                    logical_model=logical_model,
                    deployment_id="local-ocr",
                )
                result = VisionAnalysisResult(artifact, stored, True, True)
                await self._audit_result(result, sanitized, tenant_digest, "local-ocr")
                return result

        if artifact is None:
            del completion, payload
            raise VisionAnalysisError("vision analysis failed") from None
        result = VisionAnalysisResult(
            artifact=artifact,
            stored_object=stored,
            ocr_only=False,
            requires_review=artifact.confidence < self._review_threshold,
        )
        await self._audit_result(result, sanitized, tenant_digest, "model-gateway")
        return result

    async def _image_reference(
        self, sanitized: SanitizedImage, stored: StoredImageObject
    ) -> str:
        if self._reference_provider is None:
            encoded = await asyncio.to_thread(
                lambda value: base64.b64encode(value).decode("ascii"),
                sanitized.canonical_bytes,
            )
            reference = f"data:image/png;base64,{encoded}"
            if len(reference) <= self._max_reference_length:
                return reference
            del encoded, reference
            raise VisionAnalysisError("image reference failed") from None
        try:
            reference = await self._reference_provider.reference(sanitized, stored)
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 - provider details are redacted
            error.__traceback__ = None
            del error
            raise VisionAnalysisError("image reference failed") from None
        if not _valid_https_reference(reference, self._max_reference_length):
            del reference
            raise VisionAnalysisError("image reference failed") from None
        return reference

    async def _sanitize_input(
        self, data: bytes | bytearray | memoryview, declared_mime: str, tenant_id: str
    ) -> SanitizedImage | None:
        async with self._image_slots:
            worker = asyncio.create_task(
                asyncio.to_thread(
                    sanitize_image,
                    data,
                    declared_mime,
                    tenant_id,
                    limits=self._limits,
                )
            )
            try:
                result = await asyncio.shield(worker)
            except asyncio.CancelledError:
                await asyncio.gather(worker, return_exceptions=True)
                del data, worker
                raise
            except InvalidImage as error:
                error.__traceback__ = None
                del data, error, worker
                return None
            except Exception as error:  # noqa: BLE001 - sanitize boundary is redacted
                error.__traceback__ = None
                del data, error, worker
                return None
        del data, worker
        return result

    @staticmethod
    def _request(logical_model: str, reference: str) -> ModelRequest:
        schema = _ModelVisionPayload.model_json_schema()
        return ModelRequest(
            logical_model=logical_model,
            messages=(
                ModelMessage(
                    role="user",
                    content=(
                        {
                            "type": "text",
                            "text": (
                                "Analyze only the attached canonical image. Return exactly the "
                                "requested JSON object; do not infer provenance fields."
                            ),
                        },
                        {"type": "image_url", "image_url": {"url": reference, "detail": "high"}},
                    ),
                ),
            ),
            required_capabilities=frozenset(
                {ModelCapability.VISION, ModelCapability.STRUCTURED_OUTPUT}
            ),
            response_schema=StructuredResponseSchema(name="VisionAnalysis", schema=schema),
        )

    @staticmethod
    def _trusted_artifact(
        payload: _ModelVisionPayload | None,
        sanitized: SanitizedImage,
        logical_model: str,
        completion: GatewayCompletion | None,
    ) -> ImageAnalysisArtifact | None:
        if payload is None or completion is None:
            return None
        try:
            return ImageAnalysisArtifact(
                source_sha256=sanitized.source_sha256,
                summary=payload.summary,
                extracted_text=payload.extracted_text,
                objects=payload.objects,
                confidence=payload.confidence,
                logical_model=logical_model,
                deployment_id=completion.deployment_id,
            )
        except (TypeError, ValueError, ValidationError):
            return None

    async def _safe_ocr(self, canonical: bytes) -> OCRObservation | None:
        if self._ocr is None:
            return None
        try:
            observation = await self._ocr.extract(canonical)
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 - OCR details are untrusted
            error.__traceback__ = None
            del error
            return None
        return observation if isinstance(observation, OCRObservation) else None

    async def _audit_result(
        self,
        result: VisionAnalysisResult,
        sanitized: SanitizedImage,
        tenant_digest: str,
        provider_id: str,
    ) -> None:
        event = VisionAuditEvent(
            tenant_sha256=tenant_digest,
            source_sha256=sanitized.source_sha256,
            canonical_sha256=sanitized.canonical_sha256,
            object_key=sanitized.object_key,
            detected_format=sanitized.original_format,
            width=sanitized.width,
            height=sanitized.height,
            logical_model=result.artifact.logical_model,
            deployment_id=result.artifact.deployment_id,
            provider_id=provider_id,
            confidence=result.artifact.confidence,
            ocr_only=result.ocr_only,
            requires_review=result.requires_review,
        )
        try:
            await self._audit.record(event)
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 - audit details are untrusted
            error.__traceback__ = None
            del error, event
            raise VisionAnalysisError("vision audit failed") from None
