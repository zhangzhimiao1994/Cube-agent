from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator

_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_OBJECT_KEY = re.compile(
    r"^tenants/[a-f0-9]{64}/[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12}\.png$"
)
_SAFE_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,127}$")
_SAFE_STORE_NAMESPACE = re.compile(r"^[a-z0-9][a-z0-9_.:@-]{0,127}$")
_MAX_SAFE_IMAGE_DIMENSION = 16_384
_MAX_SAFE_IMAGE_PIXELS = 40_000_000


class InvalidImage(ValueError):
    """Stable rejection at the untrusted image boundary."""


class VisionAnalysisError(RuntimeError):
    """Stable, redacted vision pipeline failure."""


class VisionBusyError(VisionAnalysisError):
    """Bounded image worker admission is full."""


class VisionCleanupError(VisionAnalysisError):
    """Stable cleanup or recovery-recording failure."""


def _positive_int(name: str, value: object) -> None:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{name} must be a strict positive integer")


def _positive_number(name: str, value: object) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, int | float)
        or not math.isfinite(value)
        or value <= 0
    ):
        raise ValueError(f"{name} must be finite and positive")


@dataclass(frozen=True, slots=True)
class ImageLimits:
    max_raw_bytes: int = 20_000_000
    max_width: int = 8192
    max_height: int = 8192
    max_pixels: int = 40_000_000
    max_container_segments: int = 10_000
    max_decoded_bytes: int = 160_000_000
    max_compression_ratio: float = 500.0
    max_aspect_ratio: float = 100.0
    max_canonical_bytes: int = 20_000_000

    def __post_init__(self) -> None:
        for name in (
            "max_raw_bytes",
            "max_width",
            "max_height",
            "max_pixels",
            "max_container_segments",
            "max_decoded_bytes",
            "max_canonical_bytes",
        ):
            _positive_int(name, getattr(self, name))
        if self.max_width > _MAX_SAFE_IMAGE_DIMENSION:
            raise ValueError("max_width exceeds the hard safety ceiling")
        if self.max_height > _MAX_SAFE_IMAGE_DIMENSION:
            raise ValueError("max_height exceeds the hard safety ceiling")
        if self.max_pixels > _MAX_SAFE_IMAGE_PIXELS:
            raise ValueError("max_pixels exceeds the hard safety ceiling")
        _positive_number("max_compression_ratio", self.max_compression_ratio)
        _positive_number("max_aspect_ratio", self.max_aspect_ratio)


@dataclass(frozen=True, slots=True, repr=False)
class SanitizedImage:
    source_sha256: str
    canonical_sha256: str
    canonical_bytes: bytes = field(repr=False, compare=False)
    media_type: str
    original_format: str
    width: int
    height: int
    object_key: str

    def __post_init__(self) -> None:
        if _SHA256.fullmatch(self.source_sha256) is None:
            raise ValueError("source_sha256 must be a SHA-256 digest")
        if _SHA256.fullmatch(self.canonical_sha256) is None:
            raise ValueError("canonical_sha256 must be a SHA-256 digest")
        if type(self.canonical_bytes) is not bytes or not self.canonical_bytes:
            raise ValueError("canonical_bytes must be immutable nonempty bytes")
        if self.media_type != "image/png":
            raise ValueError("canonical media type must be image/png")
        if self.original_format not in {"JPEG", "PNG", "WEBP"}:
            raise ValueError("original format is unsupported")
        _positive_int("width", self.width)
        _positive_int("height", self.height)
        if _OBJECT_KEY.fullmatch(self.object_key) is None:
            raise ValueError("object key is invalid")

    def __repr__(self) -> str:
        return (
            "SanitizedImage("
            f"source_sha256={self.source_sha256!r}, canonical_sha256={self.canonical_sha256!r}, "
            f"media_type={self.media_type!r}, original_format={self.original_format!r}, "
            f"width={self.width!r}, height={self.height!r}, object_key={self.object_key!r})"
        )


SanitizedImage.__hash__ = None  # type: ignore[assignment]


@dataclass(frozen=True, slots=True)
class StoredImageObject:
    object_key: str
    byte_length: int
    content_type: str
    sha256: str

    def __post_init__(self) -> None:
        if _OBJECT_KEY.fullmatch(self.object_key) is None:
            raise ValueError("object key is invalid")
        _positive_int("byte_length", self.byte_length)
        if self.content_type != "image/png":
            raise ValueError("stored content type must be image/png")
        if _SHA256.fullmatch(self.sha256) is None:
            raise ValueError("sha256 must be a SHA-256 digest")


class ImageObjectStore(Protocol):
    """Put must reach a terminal commit/failure state before returning or raising."""

    store_id: str
    namespace: str

    async def put(
        self, tenant_id: str, object_key: str, data: bytes, content_type: str
    ) -> StoredImageObject: ...

    async def delete_by_object_key(self, object_key: str) -> None: ...


@dataclass(frozen=True, slots=True)
class ImageCleanupRecoveryItem:
    store_id: str
    namespace: str
    tenant_sha256: str
    object_key: str
    canonical_sha256: str
    reason: str

    def __post_init__(self) -> None:
        if _SAFE_ID.fullmatch(self.store_id) is None:
            raise ValueError("cleanup store_id must be a safe identifier")
        if _SAFE_STORE_NAMESPACE.fullmatch(self.namespace) is None:
            raise ValueError("cleanup namespace must be a safe identifier")
        if _SHA256.fullmatch(self.tenant_sha256) is None:
            raise ValueError("tenant_sha256 must be a SHA-256 digest")
        if _OBJECT_KEY.fullmatch(self.object_key) is None:
            raise ValueError("object key is invalid")
        if _SHA256.fullmatch(self.canonical_sha256) is None:
            raise ValueError("canonical_sha256 must be a SHA-256 digest")
        if self.reason not in {
            "put_failed",
            "put_cancelled",
            "stored_metadata_mismatch",
            "analysis_failed",
            "analysis_cancelled",
        }:
            raise ValueError("cleanup recovery reason is invalid")


class ImageCleanupRecoverySink(Protocol):
    """Reliably persist bounded compensation work before returning."""

    async def enqueue(self, item: ImageCleanupRecoveryItem) -> None: ...


class ContentTypeDetector(Protocol):
    def detect(self, data: bytes) -> str | None: ...


class OCRAdapter(Protocol):
    async def extract(self, image: bytes) -> OCRObservation: ...


class ImageReferenceProvider(Protocol):
    allowed_hosts: frozenset[str]

    async def reference(
        self, image: SanitizedImage, stored: StoredImageObject
    ) -> SignedImageReference: ...


class VisionAuditSink(Protocol):
    async def record(self, event: VisionAuditEvent) -> None: ...


@dataclass(frozen=True, slots=True, repr=False)
class SignedImageReference:
    url: str = field(repr=False)
    expires_at: datetime
    signed: bool
    provider_id: str

    def __post_init__(self) -> None:
        if type(self.url) is not str or not self.url or len(self.url) > 8192:
            raise ValueError("signed image URL must be nonempty and bounded")
        if (
            not isinstance(self.expires_at, datetime)
            or self.expires_at.tzinfo is None
            or self.expires_at.utcoffset() is None
        ):
            raise ValueError("signed image reference expiry must be timezone-aware")
        if self.signed is not True:
            raise ValueError("image reference must be signed")
        if _SAFE_ID.fullmatch(self.provider_id) is None:
            raise ValueError("image reference provider must be a safe identifier")


@dataclass(frozen=True, slots=True, repr=False)
class OCRObservation:
    text: str = field(repr=False)
    confidence: float
    text_oriented: bool

    def __post_init__(self) -> None:
        if type(self.text) is not str or not self.text or len(self.text) > 20_000:
            raise ValueError("OCR text must be nonempty and bounded")
        if (
            isinstance(self.confidence, bool)
            or not isinstance(self.confidence, int | float)
            or not math.isfinite(self.confidence)
            or not 0 <= self.confidence <= 1
        ):
            raise ValueError("OCR confidence must be finite and between zero and one")
        if type(self.text_oriented) is not bool:
            raise ValueError("text_oriented must be a boolean")


class ImageAnalysisArtifact(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    source_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    summary: str = Field(min_length=1, max_length=2000, repr=False)
    extracted_text: str | None = Field(default=None, max_length=20_000, repr=False)
    objects: list[str] = Field(max_length=100, repr=False)
    confidence: float = Field(ge=0, le=1, allow_inf_nan=False)
    logical_model: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,127}$")
    deployment_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,127}$")

    @field_validator("objects")
    @classmethod
    def validate_objects(cls, values: list[str]) -> list[str]:
        if any(not value or value != value.strip() or len(value) > 256 for value in values):
            raise ValueError("objects must be bounded unpadded strings")
        return values


@dataclass(frozen=True, slots=True, repr=False)
class VisionAnalysisResult:
    artifact: ImageAnalysisArtifact = field(repr=False)
    stored_object: StoredImageObject
    ocr_only: bool
    requires_review: bool

    def __post_init__(self) -> None:
        if not isinstance(self.artifact, ImageAnalysisArtifact):
            raise TypeError("artifact must be ImageAnalysisArtifact")
        if not isinstance(self.stored_object, StoredImageObject):
            raise TypeError("stored_object must be StoredImageObject")
        if type(self.ocr_only) is not bool or type(self.requires_review) is not bool:
            raise ValueError("result flags must be booleans")
        if self.ocr_only and not self.requires_review:
            raise ValueError("OCR-only results require review")


@dataclass(frozen=True, slots=True)
class VisionAuditEvent:
    tenant_sha256: str
    source_sha256: str
    canonical_sha256: str
    object_key: str
    detected_format: str
    width: int
    height: int
    logical_model: str
    deployment_id: str
    provider_id: str
    provider_model: str
    confidence: float
    ocr_only: bool
    requires_review: bool

    def __post_init__(self) -> None:
        for value in (self.tenant_sha256, self.source_sha256, self.canonical_sha256):
            if _SHA256.fullmatch(value) is None:
                raise ValueError("audit digests must be SHA-256")
        if _OBJECT_KEY.fullmatch(self.object_key) is None:
            raise ValueError("audit object key is invalid")
        if self.detected_format not in {"JPEG", "PNG", "WEBP"}:
            raise ValueError("audit format is unsupported")
        for value in (self.logical_model, self.deployment_id, self.provider_id):
            if _SAFE_ID.fullmatch(value) is None:
                raise ValueError("audit identifiers must be safe")
        if (
            not self.provider_model
            or len(self.provider_model) > 512
            or re.fullmatch(r"[A-Za-z0-9_./:-]+", self.provider_model) is None
        ):
            raise ValueError("audit provider model must be a bounded safe value")
        _positive_int("width", self.width)
        _positive_int("height", self.height)
        if not math.isfinite(self.confidence) or not 0 <= self.confidence <= 1:
            raise ValueError("audit confidence is invalid")
