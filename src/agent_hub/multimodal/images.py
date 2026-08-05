from __future__ import annotations

import asyncio
import hashlib
import io
import os
import re
import stat
import uuid
import warnings
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError

from agent_hub.multimodal.types import (
    ContentTypeDetector,
    ImageLimits,
    InvalidImage,
    SanitizedImage,
    StoredImageObject,
)

_ALLOWED_MIME_TO_FORMAT = {
    "image/jpeg": "JPEG",
    "image/png": "PNG",
    "image/webp": "WEBP",
}
_OBJECT_KEY = re.compile(
    r"^tenants/([a-f0-9]{64})/([0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12})\.png$"
)
_TENANT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@-]{0,255}$")


class StrictSignatureDetector:
    """Small dependency-free signature detector for the image allowlist."""

    def detect(self, data: bytes) -> str | None:
        if data.startswith(b"\x89PNG\r\n\x1a\n"):
            return "image/png"
        if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            return "image/webp"
        if len(data) >= 3 and data[:3] == b"\xff\xd8\xff":
            return "image/jpeg"
        return None


def _copy_input(raw: object, maximum: int) -> bytes | None:
    if not isinstance(raw, bytes | bytearray | memoryview):
        return None
    try:
        byte_length = raw.nbytes if isinstance(raw, memoryview) else len(raw)
        if byte_length == 0 or byte_length > maximum:
            return None
        return bytes(raw)
    except (TypeError, ValueError):
        return None


def _has_exact_container_length(data: bytes, detected_mime: str) -> bool:
    if detected_mime == "image/png":
        marker = b"IEND\xaeB`\x82"
        position = data.rfind(marker)
        return position >= 0 and position + len(marker) == len(data)
    if detected_mime == "image/webp":
        return len(data) >= 12 and int.from_bytes(data[4:8], "little") + 8 == len(data)
    if detected_mime == "image/jpeg":
        return data.endswith(b"\xff\xd9")
    return False


def _dimensions_allowed(width: int, height: int, limits: ImageLimits) -> bool:
    pixels = width * height
    return (
        width > 0
        and height > 0
        and width <= limits.max_width
        and height <= limits.max_height
        and pixels <= limits.max_pixels
        and max(width / height, height / width) <= limits.max_aspect_ratio
        and pixels * 4 <= limits.max_decoded_bytes
    )


def _decode_and_canonicalize(
    data: bytes,
    declared_mime: str,
    limits: ImageLimits,
    detector: ContentTypeDetector,
) -> tuple[bytes, str, int, int] | None:
    try:
        detected_mime = detector.detect(data)
        expected_format = _ALLOWED_MIME_TO_FORMAT.get(declared_mime)
        if (
            expected_format is None
            or detected_mime != declared_mime
            or not _has_exact_container_length(data, detected_mime or "")
        ):
            return None
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as probe:
                decoded_format = probe.format
                if decoded_format != expected_format:
                    return None
                if getattr(probe, "n_frames", 1) != 1 or getattr(probe, "is_animated", False):
                    return None
                if not _dimensions_allowed(probe.width, probe.height, limits):
                    return None
                probe.verify()

            with Image.open(io.BytesIO(data)) as decoded:
                if decoded.format != decoded_format or getattr(decoded, "n_frames", 1) != 1:
                    return None
                decoded.load()
                if not _dimensions_allowed(decoded.width, decoded.height, limits):
                    return None
                if decoded.width * decoded.height * 4 / len(data) > limits.max_compression_ratio:
                    return None
                transposed = ImageOps.exif_transpose(decoded)
                try:
                    if not _dimensions_allowed(transposed.width, transposed.height, limits):
                        return None
                    target_mode = "RGBA" if "A" in transposed.getbands() else "RGB"
                    converted = transposed.convert(target_mode)
                    try:
                        # Copy pixels into a new image so no decoder metadata or lazy source aliases survive.
                        clean = Image.new(target_mode, converted.size)
                        clean.frombytes(converted.tobytes())
                    finally:
                        converted.close()
                finally:
                    if transposed is not decoded:
                        transposed.close()

        output = io.BytesIO()
        try:
            clean.save(output, "PNG", optimize=False, compress_level=9)
            canonical = output.getvalue()
        finally:
            clean.close()
            output.close()
        if not canonical or len(canonical) > limits.max_canonical_bytes:
            return None
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(canonical)) as verified:
                if (
                    verified.format != "PNG"
                    or getattr(verified, "n_frames", 1) != 1
                    or verified.size != (transposed.width, transposed.height)
                    or verified.info
                ):
                    return None
                verified.load()
        return canonical, decoded_format, verified.width, verified.height
    except (
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
        UnidentifiedImageError,
        OSError,
        ValueError,
        TypeError,
        MemoryError,
    ):
        return None
    except Exception as error:  # noqa: BLE001 - injected detector failures are untrusted
        error.__traceback__ = None
        del error
        return None


def _sanitize_copied(
    data: bytes,
    declared_mime: object,
    tenant_id: object,
    limits: ImageLimits,
    detector: ContentTypeDetector,
) -> SanitizedImage | None:
    if (
        not data
        or len(data) > limits.max_raw_bytes
        or type(declared_mime) is not str
        or declared_mime not in _ALLOWED_MIME_TO_FORMAT
        or type(tenant_id) is not str
        or _TENANT_ID.fullmatch(tenant_id) is None
    ):
        return None
    decoded = _decode_and_canonicalize(data, declared_mime, limits, detector)
    if decoded is None:
        return None
    canonical, original_format, width, height = decoded
    tenant_digest = hashlib.sha256(tenant_id.encode("utf-8")).hexdigest()
    return SanitizedImage(
        source_sha256=hashlib.sha256(data).hexdigest(),
        canonical_sha256=hashlib.sha256(canonical).hexdigest(),
        canonical_bytes=canonical,
        media_type="image/png",
        original_format=original_format,
        width=width,
        height=height,
        object_key=f"tenants/{tenant_digest}/{uuid.uuid4()}.png",
    )


def sanitize_image(
    raw: bytes | bytearray | memoryview,
    declared_mime: str,
    tenant_id: str,
    *,
    limits: ImageLimits | None = None,
    detector: ContentTypeDetector | None = None,
) -> SanitizedImage:
    configured_limits = limits if isinstance(limits, ImageLimits) else ImageLimits()
    copied = _copy_input(raw, configured_limits.max_raw_bytes)
    del raw
    configured_detector = detector or StrictSignatureDetector()
    result = None if copied is None else _sanitize_copied(
        copied, declared_mime, tenant_id, configured_limits, configured_detector
    )
    del copied, declared_mime, tenant_id, configured_detector, configured_limits
    if result is None:
        raise InvalidImage("image rejected") from None
    return result


class FilesystemImageStore:
    """Local production adapter with tenant-bound keys and atomic private files."""

    def __init__(self, root: Path | str) -> None:
        self._configured_root = Path(root).absolute()
        self._root = self._configured_root.resolve(strict=False)

    async def put(
        self, tenant_id: str, object_key: str, data: bytes, content_type: str
    ) -> StoredImageObject:
        copied = bytes(data) if type(data) is bytes else b""
        del data
        worker = asyncio.create_task(
            asyncio.to_thread(self._put_sync_safe, tenant_id, object_key, copied, content_type)
        )
        del copied, tenant_id, object_key, content_type
        try:
            result = await asyncio.shield(worker)
        except asyncio.CancelledError:
            await asyncio.shield(worker)
            raise
        if result is None:
            raise OSError("image storage failed") from None
        if isinstance(result, ValueError):
            raise result from None
        return result

    def _put_sync_safe(
        self, tenant_id: str, object_key: str, data: bytes, content_type: str
    ) -> StoredImageObject | ValueError | None:
        try:
            match = _OBJECT_KEY.fullmatch(object_key)
            if _TENANT_ID.fullmatch(tenant_id) is None:
                return ValueError("invalid image object key or metadata")
            tenant_digest = hashlib.sha256(tenant_id.encode("utf-8")).hexdigest()
            if (
                match is None
                or match.group(1) != tenant_digest
                or content_type != "image/png"
                or not data
            ):
                return ValueError("invalid image object key or metadata")
            for candidate in (self._configured_root, *self._configured_root.parents):
                if candidate.exists() and candidate.is_symlink():
                    return None
            self._configured_root.mkdir(parents=True, exist_ok=True)
            if self._configured_root.resolve(strict=True) != self._root:
                return None
            parent = self._root / "tenants" / match.group(1)
            current = self._root
            for part in ("tenants", match.group(1)):
                current = current / part
                if current.exists() and current.is_symlink():
                    return None
                current.mkdir(mode=0o700, exist_ok=True)
            resolved_parent = parent.resolve(strict=True)
            if resolved_parent != self._root / "tenants" / match.group(1):
                return None
            target = resolved_parent / f"{match.group(2)}.png"
            if target.exists() or target.is_symlink():
                return None
            temporary = resolved_parent / f".{uuid.uuid4()}.tmp"
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                with os.fdopen(descriptor, "wb", closefd=True) as output:
                    output.write(data)
                    output.flush()
                    os.fsync(output.fileno())
                os.chmod(temporary, stat.S_IRUSR | stat.S_IWUSR)
                os.replace(temporary, target)
            finally:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass
            return StoredImageObject(
                object_key=object_key,
                byte_length=len(data),
                content_type=content_type,
                sha256=hashlib.sha256(data).hexdigest(),
            )
        except (OSError, UnicodeError, ValueError):
            return None
