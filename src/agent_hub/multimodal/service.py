from __future__ import annotations

import asyncio
import base64
import hashlib
import ipaddress
import json
import math
import re
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from enum import Enum
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
from agent_hub.multimodal.images import _await_task_uninterruptibly, sanitize_image
from agent_hub.multimodal.types import (
    ImageAnalysisArtifact,
    ImageCleanupRecoveryItem,
    ImageCleanupRecoverySink,
    ImageLimits,
    ImageObjectStore,
    ImageReferenceProvider,
    InvalidImage,
    OCRAdapter,
    OCRObservation,
    SanitizedImage,
    SignedImageReference,
    StoredImageObject,
    VisionAnalysisError,
    VisionAnalysisResult,
    VisionAuditEvent,
    VisionAuditSink,
    VisionBusyError,
    VisionCleanupError,
)

_OCR_SUMMARY = "OCR-only text extraction; visual description unavailable"
_ObjectLabel = Annotated[str, StringConstraints(min_length=1, max_length=256)]


class _CleanupOutcome(Enum):
    DELETED = "deleted"
    RECOVERY_QUEUED = "recovery_queued"
    RECOVERY_RECORDING_FAILED = "recovery_recording_failed"


def _clear_exception(error: BaseException) -> None:
    error.__traceback__ = None
    error.__context__ = None
    error.__cause__ = None


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
    except (json.JSONDecodeError, RecursionError, TypeError, ValueError, ValidationError):
        return None


type _TrustedAuthority = tuple[str, int]


def _normalize_hostname(hostname: str) -> str | None:
    try:
        normalized = hostname.rstrip(".").encode("idna").decode("ascii").lower()
    except (UnicodeError, ValueError):
        return None
    if not normalized or len(normalized) > 253:
        return None
    try:
        ipaddress.ip_address(normalized)
    except ValueError:
        pass
    else:
        return None
    labels = normalized.split(".")
    if (
        len(labels) < 2
        or any(
            not label
            or len(label) > 63
            or not label[0].isalnum()
            or not label[-1].isalnum()
            or any(not character.isalnum() and character != "-" for character in label)
            for label in labels
        )
        or labels[0] == "localhost"
        or normalized in {"localhost.localdomain", "nip.io", "sslip.io"}
        or normalized.endswith(
            (".local", ".internal", ".localhost", ".lan", ".nip.io", ".sslip.io")
        )
    ):
        return None
    return normalized


def _normalize_allowed_authority(value: object) -> _TrustedAuthority | None:
    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or len(value) > 300
        or any(character.isspace() or ord(character) < 32 for character in value)
    ):
        return None
    try:
        parsed = urlsplit(f"//{value}")
        parsed_port = parsed.port
    except ValueError:
        return None
    if (
        parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
    ):
        return None
    authority_text = parsed.netloc.rsplit("@", 1)[-1]
    has_explicit_port = ":" in authority_text
    if has_explicit_port:
        port_text = authority_text.rsplit(":", 1)[1]
        if re.fullmatch(r"[0-9]+", port_text) is None:
            return None
    port = 443 if parsed_port is None else parsed_port
    if port <= 0 or port > 65535 or (has_explicit_port and parsed_port is None):
        return None
    hostname = _normalize_hostname(parsed.hostname)
    return None if hostname is None else (hostname, port)


def _valid_https_reference(
    reference: str, maximum: int, allowed_authorities: frozenset[_TrustedAuthority]
) -> bool:
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
        parsed_port = parsed.port
    except ValueError:
        return False
    hostname = parsed.hostname
    if hostname is None or "\\" in reference:
        return False
    authority_text = parsed.netloc.rsplit("@", 1)[-1]
    has_explicit_port = ":" in authority_text
    if has_explicit_port:
        port_text = authority_text.rsplit(":", 1)[1]
        if re.fullmatch(r"[0-9]+", port_text) is None:
            return False
    port = 443 if parsed_port is None else parsed_port
    if port <= 0 or port > 65535 or (has_explicit_port and parsed_port is None):
        return False
    normalized_host = _normalize_hostname(hostname)
    authority = None if normalized_host is None else (normalized_host, port)
    return (
        parsed.scheme == "https"
        and authority in allowed_authorities
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
        cleanup_recovery_sink: ImageCleanupRecoverySink,
        limits: ImageLimits | None = None,
        ocr_adapter: OCRAdapter | None = None,
        reference_provider: ImageReferenceProvider | None = None,
        audit_sink: VisionAuditSink | None = None,
        review_threshold: float = 0.7,
        max_reference_length: int = 30_000_000,
        max_concurrent_image_tasks: int = 4,
        max_active_image_tasks: int | None = None,
        max_waiting_image_tasks: int = 16,
        cleanup_attempts: int = 3,
        cleanup_backoff_seconds: float = 0.01,
        max_signed_url_ttl_seconds: float = 300,
        utc_now: Callable[[], datetime] = lambda: datetime.now(UTC),
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
        active_limit = (
            max_concurrent_image_tasks if max_active_image_tasks is None else max_active_image_tasks
        )
        if type(active_limit) is not int or active_limit <= 0:
            raise ValueError("max_concurrent_image_tasks must be a strict positive integer")
        if type(max_waiting_image_tasks) is not int or max_waiting_image_tasks < 0:
            raise ValueError("max_waiting_image_tasks must be a strict nonnegative integer")
        if type(cleanup_attempts) is not int or cleanup_attempts <= 0:
            raise ValueError("cleanup_attempts must be a strict positive integer")
        if (
            isinstance(cleanup_backoff_seconds, bool)
            or not isinstance(cleanup_backoff_seconds, int | float)
            or not math.isfinite(cleanup_backoff_seconds)
            or cleanup_backoff_seconds < 0
        ):
            raise ValueError("cleanup_backoff_seconds must be finite and nonnegative")
        if (
            isinstance(max_signed_url_ttl_seconds, bool)
            or not isinstance(max_signed_url_ttl_seconds, int | float)
            or not math.isfinite(max_signed_url_ttl_seconds)
            or max_signed_url_ttl_seconds <= 0
        ):
            raise ValueError("max_signed_url_ttl_seconds must be finite and positive")
        self._gateway = gateway
        self._object_store = object_store
        self._cleanup_recovery_sink = cleanup_recovery_sink
        self._limits = limits or ImageLimits()
        self._ocr = ocr_adapter
        self._reference_provider = reference_provider
        self._reference_allowed_authorities: frozenset[_TrustedAuthority] = frozenset()
        if reference_provider is not None:
            try:
                configured_hosts = reference_provider.allowed_hosts
            except Exception as error:  # noqa: BLE001 - invalid injected configuration
                error.__traceback__ = None
                del error
                raise ValueError("reference provider allowlist is invalid") from None
            if type(configured_hosts) is not frozenset or not 1 <= len(configured_hosts) <= 32:
                raise ValueError("reference provider allowlist is invalid")
            normalized_hosts = {_normalize_allowed_authority(host) for host in configured_hosts}
            if None in normalized_hosts:
                raise ValueError("reference provider allowlist is invalid")
            self._reference_allowed_authorities = frozenset(
                authority for authority in normalized_hosts if authority is not None
            )
        self._audit = audit_sink or NoopVisionAuditSink()
        self._review_threshold = float(review_threshold)
        self._max_reference_length = max_reference_length
        self._image_slots = asyncio.Semaphore(active_limit)
        self._admission_lock = threading.Lock()
        self._max_active_image_tasks = active_limit
        self._max_waiting_image_tasks = max_waiting_image_tasks
        self._max_admitted_image_tasks = active_limit + max_waiting_image_tasks
        self._admitted_image_tasks = 0
        self._active_image_tasks = 0
        self._waiting_image_tasks = 0
        self._cleanup_attempts = cleanup_attempts
        self._cleanup_backoff_seconds = float(cleanup_backoff_seconds)
        self._max_signed_url_ttl_seconds = float(max_signed_url_ttl_seconds)
        self._utc_now = utc_now

    async def analyze(
        self,
        raw: bytes | bytearray | memoryview,
        declared_mime: str,
        tenant_id: str,
        logical_model: str = "vision-primary",
    ) -> VisionAnalysisResult:
        if not isinstance(raw, bytes | bytearray | memoryview):
            del raw
            raise InvalidImage("image rejected") from None
        try:
            raw_size = raw.nbytes if isinstance(raw, memoryview) else len(raw)
        except (TypeError, ValueError, OverflowError):
            del raw
            raise InvalidImage("image rejected") from None
        if raw_size == 0 or raw_size > self._limits.max_raw_bytes:
            del raw
            raise InvalidImage("image rejected") from None
        await self._acquire_image_slot()
        try:
            try:
                immutable = bytes(raw)
            except (TypeError, ValueError, OverflowError, MemoryError):
                del raw
                raise InvalidImage("image rejected") from None
            del raw
            sanitized = await self._sanitize_input(immutable, declared_mime, tenant_id)
            del immutable
        finally:
            self._release_image_slot()
        if sanitized is None:
            raise InvalidImage("image rejected") from None
        del declared_mime
        object_key = sanitized.object_key
        tenant_digest = hashlib.sha256(tenant_id.encode("utf-8")).hexdigest()
        stored, storage_cancellation = await self._store_image(sanitized, tenant_id)
        if storage_cancellation is not None:
            canonical_sha256 = sanitized.canonical_sha256
            del sanitized, stored
            outcome = await self._cleanup_object(
                object_key,
                tenant_digest,
                canonical_sha256,
                "put_cancelled",
                storage_cancellation,
            )
            del canonical_sha256, object_key, tenant_id, tenant_digest
            if outcome is _CleanupOutcome.RECOVERY_RECORDING_FAILED:
                storage_cancellation.add_note("image cleanup recovery recording failed")
            elif outcome is _CleanupOutcome.RECOVERY_QUEUED:
                storage_cancellation.add_note("image cleanup recovery queued")
            _clear_exception(storage_cancellation)
            raise storage_cancellation from None
        if stored is None:
            canonical_sha256 = sanitized.canonical_sha256
            del sanitized, stored
            outcome = await self._cleanup_object(
                object_key,
                tenant_digest,
                canonical_sha256,
                "put_failed",
            )
            del canonical_sha256, object_key, tenant_id, tenant_digest
            if outcome is _CleanupOutcome.RECOVERY_RECORDING_FAILED:
                raise VisionCleanupError("image cleanup recovery recording failed") from None
            if outcome is _CleanupOutcome.RECOVERY_QUEUED:
                raise VisionCleanupError("image cleanup failed; recovery queued") from None
            raise VisionAnalysisError("image storage failed") from None
        if not self._stored_matches(stored, sanitized):
            canonical_sha256 = sanitized.canonical_sha256
            del sanitized, stored
            outcome = await self._cleanup_object(
                object_key,
                tenant_digest,
                canonical_sha256,
                "stored_metadata_mismatch",
            )
            del canonical_sha256, object_key, tenant_id, tenant_digest
            if outcome is _CleanupOutcome.RECOVERY_RECORDING_FAILED:
                raise VisionCleanupError("image cleanup recovery recording failed") from None
            if outcome is _CleanupOutcome.RECOVERY_QUEUED:
                raise VisionCleanupError("image cleanup failed; recovery queued") from None
            raise VisionAnalysisError("image storage failed") from None

        try:
            result, failure, cancellation = await self._analyze_stored(
                sanitized, stored, logical_model, tenant_digest
            )
        except asyncio.CancelledError as unexpected_cancellation:
            unexpected_cancellation.__traceback__ = None
            result, failure, cancellation = None, None, unexpected_cancellation
        except Exception as error:  # noqa: BLE001 - fail closed around injected components
            error.__traceback__ = None
            del error
            result, failure, cancellation = None, "vision analysis failed", None
        if result is not None:
            del sanitized
            del tenant_id, object_key, stored
            return result

        canonical_sha256 = sanitized.canonical_sha256
        del sanitized
        outcome = await self._cleanup_object(
            object_key,
            tenant_digest,
            canonical_sha256,
            "analysis_cancelled" if cancellation is not None else "analysis_failed",
            cancellation,
        )
        del canonical_sha256, tenant_id, object_key, stored
        if cancellation is not None:
            if outcome is _CleanupOutcome.RECOVERY_RECORDING_FAILED:
                cancellation.add_note("image cleanup recovery recording failed")
            elif outcome is _CleanupOutcome.RECOVERY_QUEUED:
                cancellation.add_note("image cleanup recovery queued")
            _clear_exception(cancellation)
            raise cancellation from None
        if outcome is _CleanupOutcome.RECOVERY_RECORDING_FAILED:
            raise VisionCleanupError("image cleanup recovery recording failed") from None
        if outcome is _CleanupOutcome.RECOVERY_QUEUED:
            raise VisionCleanupError("image cleanup failed; recovery queued") from None
        raise VisionAnalysisError(failure or "vision analysis failed") from None

    async def _acquire_image_slot(self) -> None:
        with self._admission_lock:
            if self._admitted_image_tasks >= self._max_admitted_image_tasks:
                raise VisionBusyError("vision image workers busy") from None
            self._admitted_image_tasks += 1
            self._waiting_image_tasks = (
                self._admitted_image_tasks - self._active_image_tasks
            )
        try:
            await self._image_slots.acquire()
        except BaseException:
            with self._admission_lock:
                self._admitted_image_tasks -= 1
                self._waiting_image_tasks = (
                    self._admitted_image_tasks - self._active_image_tasks
                )
            raise
        with self._admission_lock:
            self._active_image_tasks += 1
            self._waiting_image_tasks = (
                self._admitted_image_tasks - self._active_image_tasks
            )

    def _release_image_slot(self) -> None:
        with self._admission_lock:
            self._active_image_tasks -= 1
            self._admitted_image_tasks -= 1
            self._waiting_image_tasks = (
                self._admitted_image_tasks - self._active_image_tasks
            )
        self._image_slots.release()

    async def _store_image(
        self, sanitized: SanitizedImage, tenant_id: str
    ) -> tuple[StoredImageObject | None, asyncio.CancelledError | None]:
        put_task = asyncio.create_task(
            self._object_store.put(
                tenant_id,
                sanitized.object_key,
                sanitized.canonical_bytes,
                sanitized.media_type,
            )
        )
        try:
            stored = await _await_task_uninterruptibly(put_task)
        except asyncio.CancelledError as cancellation:
            stored = None
            if not put_task.cancelled():
                try:
                    settled = put_task.result()
                    if isinstance(settled, StoredImageObject):
                        stored = settled
                except BaseException as put_error:  # noqa: BLE001 - inspect terminal put
                    put_error.__traceback__ = None
                    del put_error
            cancellation.__traceback__ = None
            del sanitized, tenant_id, put_task
            return stored, cancellation
        except Exception as error:  # noqa: BLE001 - adapter details are untrusted
            error.__traceback__ = None
            del error, sanitized, tenant_id, put_task
            return None, None
        del sanitized, tenant_id, put_task
        return stored, None

    @staticmethod
    def _stored_matches(stored: StoredImageObject, sanitized: SanitizedImage) -> bool:
        return (
            isinstance(stored, StoredImageObject)
            and stored.object_key == sanitized.object_key
            and stored.content_type == sanitized.media_type
            and stored.sha256 == sanitized.canonical_sha256
            and stored.byte_length == len(sanitized.canonical_bytes)
        )

    async def _cleanup_object(
        self,
        object_key: str,
        tenant_digest: str,
        canonical_sha256: str,
        reason: str,
        original_cancellation: asyncio.CancelledError | None = None,
    ) -> _CleanupOutcome:
        cleanup = asyncio.create_task(
            self._cleanup_or_enqueue_recovery(
                object_key,
                tenant_digest,
                canonical_sha256,
                reason,
            )
        )
        del object_key, tenant_digest, canonical_sha256, reason
        cleanup_cancellation: asyncio.CancelledError | None = None
        try:
            outcome = await _await_task_uninterruptibly(cleanup)
        except asyncio.CancelledError as cancellation:
            cleanup_cancellation = cancellation
            try:
                outcome = cleanup.result()
            except BaseException as cleanup_error:  # noqa: BLE001 - terminal task inspection
                _clear_exception(cleanup_error)
                del cleanup_error
                outcome = _CleanupOutcome.RECOVERY_RECORDING_FAILED
        except BaseException as cleanup_error:  # noqa: BLE001 - terminal task inspection
            _clear_exception(cleanup_error)
            del cleanup_error
            outcome = _CleanupOutcome.RECOVERY_RECORDING_FAILED
        del cleanup
        if cleanup_cancellation is not None:
            _clear_exception(cleanup_cancellation)
            if original_cancellation is None:
                if outcome is _CleanupOutcome.RECOVERY_RECORDING_FAILED:
                    cleanup_cancellation.add_note(
                        "image cleanup recovery recording failed"
                    )
                elif outcome is _CleanupOutcome.RECOVERY_QUEUED:
                    cleanup_cancellation.add_note("image cleanup recovery queued")
                raise cleanup_cancellation from None
            del cleanup_cancellation
        return outcome

    async def _cleanup_or_enqueue_recovery(
        self,
        object_key: str,
        tenant_digest: str,
        canonical_sha256: str,
        reason: str,
    ) -> _CleanupOutcome:
        for attempt in range(self._cleanup_attempts):
            try:
                await self._object_store.delete_by_object_key(
                    object_key, canonical_sha256
                )
            except (Exception, asyncio.CancelledError) as error:  # noqa: BLE001 - adapter
                _clear_exception(error)
                del error
                if attempt + 1 < self._cleanup_attempts and self._cleanup_backoff_seconds:
                    await asyncio.sleep(self._cleanup_backoff_seconds)
            else:
                return _CleanupOutcome.DELETED
        recording_failed = False
        item: ImageCleanupRecoveryItem | None = None
        try:
            item = ImageCleanupRecoveryItem(
                store_id=self._object_store.store_id,
                namespace=self._object_store.namespace,
                tenant_sha256=tenant_digest,
                object_key=object_key,
                canonical_sha256=canonical_sha256,
                reason=reason,
            )
            await self._cleanup_recovery_sink.enqueue(item)
        except (Exception, asyncio.CancelledError) as error:  # noqa: BLE001 - recovery sink
            _clear_exception(error)
            del error
            recording_failed = True
        del item
        if recording_failed:
            return _CleanupOutcome.RECOVERY_RECORDING_FAILED
        return _CleanupOutcome.RECOVERY_QUEUED

    async def _analyze_stored(
        self,
        sanitized: SanitizedImage,
        stored: StoredImageObject,
        requested_logical_model: str,
        tenant_digest: str,
    ) -> tuple[
        VisionAnalysisResult | None,
        str | None,
        asyncio.CancelledError | None,
    ]:
        reference, cancellation = await self._image_reference(sanitized, stored)
        if cancellation is not None:
            del sanitized, stored
            return None, None, cancellation
        if reference is None:
            del sanitized, stored
            return None, "image reference failed", None
        try:
            request = self._request(requested_logical_model, reference)
        except (TypeError, ValueError):
            del sanitized, stored, reference
            return None, "vision analysis failed", None
        completion: GatewayCompletion | None = None
        try:
            completion = await self._gateway.complete_with_context(request)
        except asyncio.CancelledError as gateway_cancellation:
            gateway_cancellation.__traceback__ = None
            del sanitized, stored, request, reference
            return None, None, gateway_cancellation
        except Exception as error:  # noqa: BLE001 - gateway details are redacted
            error.__traceback__ = None
            del error
        del request, reference

        payload = None if completion is None else _parse_payload(completion.response.text)
        artifact = self._trusted_artifact(payload, sanitized, completion)
        primary_unavailable = artifact is None
        low_confidence = artifact is not None and artifact.confidence < self._review_threshold

        if self._ocr is not None and (primary_unavailable or low_confidence):
            try:
                ocr = await self._safe_ocr(sanitized.canonical_bytes)
            except asyncio.CancelledError as ocr_cancellation:
                ocr_cancellation.__traceback__ = None
                del sanitized, stored, completion, payload
                return None, None, ocr_cancellation
            if ocr is not None and ocr.text_oriented:
                artifact = ImageAnalysisArtifact(
                    source_sha256=sanitized.source_sha256,
                    summary=_OCR_SUMMARY,
                    extracted_text=ocr.text,
                    objects=[],
                    confidence=float(ocr.confidence),
                    logical_model=requested_logical_model,
                    deployment_id="local-ocr",
                )
                result = VisionAnalysisResult(artifact, stored, True, True)
                audit_failure, audit_cancellation = await self._audit_outcome(
                    result, sanitized, tenant_digest, "local-ocr", "local-ocr"
                )
                del sanitized, stored, completion, payload, ocr
                if audit_cancellation is not None:
                    return None, None, audit_cancellation
                if audit_failure:
                    return None, "vision audit failed", None
                return result, None, None

        if artifact is None or completion is None:
            del sanitized, stored, completion, payload
            return None, "vision analysis failed", None
        result = VisionAnalysisResult(
            artifact=artifact,
            stored_object=stored,
            ocr_only=False,
            requires_review=artifact.confidence < self._review_threshold,
        )
        audit_failure, audit_cancellation = await self._audit_outcome(
            result,
            sanitized,
            tenant_digest,
            completion.provider_id,
            completion.provider_model,
        )
        del sanitized, stored, completion, payload
        if audit_cancellation is not None:
            return None, None, audit_cancellation
        if audit_failure:
            return None, "vision audit failed", None
        return result, None, None

    async def _image_reference(
        self, sanitized: SanitizedImage, stored: StoredImageObject
    ) -> tuple[str | None, asyncio.CancelledError | None]:
        if self._reference_provider is None:
            try:
                encoding = asyncio.create_task(
                    asyncio.to_thread(
                        lambda value: base64.b64encode(value).decode("ascii"),
                        sanitized.canonical_bytes,
                    )
                )
                encoded = await _await_task_uninterruptibly(encoding)
            except asyncio.CancelledError as cancellation:
                cancellation.__traceback__ = None
                del sanitized, stored, encoding
                return None, cancellation
            del encoding
            reference = f"data:image/png;base64,{encoded}"
            if len(reference) <= self._max_reference_length:
                del sanitized, stored, encoded
                return reference, None
            del sanitized, stored, encoded, reference
            return None, None
        try:
            signed_reference = await self._reference_provider.reference(sanitized, stored)
        except asyncio.CancelledError as cancellation:
            cancellation.__traceback__ = None
            del sanitized, stored
            return None, cancellation
        except Exception as error:  # noqa: BLE001 - provider details are redacted
            error.__traceback__ = None
            del error, sanitized, stored
            return None, None
        try:
            now = self._utc_now()
        except Exception as error:  # noqa: BLE001 - injected clock details are redacted
            error.__traceback__ = None
            del error, sanitized, stored, signed_reference
            return None, None
        valid_expiry = False
        if (
            isinstance(now, datetime)
            and now.tzinfo is not None
            and now.utcoffset() is not None
            and isinstance(signed_reference, SignedImageReference)
        ):
            ttl = (signed_reference.expires_at - now).total_seconds()
            valid_expiry = 0 < ttl <= self._max_signed_url_ttl_seconds
        if (
            not isinstance(signed_reference, SignedImageReference)
            or not valid_expiry
            or not _valid_https_reference(
                signed_reference.url,
                min(self._max_reference_length, 8192),
                self._reference_allowed_authorities,
            )
        ):
            del sanitized, stored, signed_reference
            return None, None
        url = signed_reference.url
        del sanitized, stored, signed_reference
        return url, None

    async def _sanitize_input(
        self, data: bytes | bytearray | memoryview, declared_mime: str, tenant_id: str
    ) -> SanitizedImage | None:
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
            result = await _await_task_uninterruptibly(worker)
        except asyncio.CancelledError:
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
                logical_model=completion.logical_model,
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

    async def _audit_outcome(
        self,
        result: VisionAnalysisResult,
        sanitized: SanitizedImage,
        tenant_digest: str,
        provider_id: str,
        provider_model: str,
    ) -> tuple[bool, asyncio.CancelledError | None]:
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
            provider_model=provider_model,
            confidence=result.artifact.confidence,
            ocr_only=result.ocr_only,
            requires_review=result.requires_review,
        )
        try:
            await self._audit.record(event)
        except asyncio.CancelledError as cancellation:
            cancellation.__traceback__ = None
            del result, sanitized, event
            return False, cancellation
        except Exception as error:  # noqa: BLE001 - audit details are untrusted
            error.__traceback__ = None
            del error, result, sanitized, event
            return True, None
        del result, sanitized, event
        return False, None
