from __future__ import annotations

import asyncio
import hashlib
import hmac
import io
import os
import re
import stat
import threading
import uuid
from enum import Enum
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError

from agent_hub.multimodal.types import (
    ContentTypeDetector,
    ImageCleanupRecoveryItem,
    ImageLimits,
    ImageObjectStore,
    InvalidImage,
    SanitizedImage,
    StoredImageObject,
    VisionCleanupError,
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
_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_MAX_STORED_IMAGE_BYTES = 20_000_000


async def _await_task_uninterruptibly[T](task: asyncio.Task[T]) -> T:
    first_cancellation: asyncio.CancelledError | None = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as cancellation:
            if first_cancellation is None:
                first_cancellation = cancellation
        except BaseException:
            if first_cancellation is None:
                raise
            break
    if first_cancellation is not None:
        if task.done() and not task.cancelled():
            task.exception()
        first_cancellation.__traceback__ = None
        raise first_cancellation from None
    return task.result()


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


def _valid_png_container(data: bytes, maximum_segments: int) -> bool:
    offset = 8
    chunks = 0
    while offset + 12 <= len(data):
        length = int.from_bytes(data[offset : offset + 4], "big")
        chunk_type = data[offset + 4 : offset + 8]
        end = offset + 12 + length
        if end > len(data):
            return False
        chunks += 1
        if chunks > maximum_segments:
            return False
        if chunks == 1 and (chunk_type != b"IHDR" or length != 13):
            return False
        if chunk_type == b"IEND":
            return length == 0 and end == len(data)
        offset = end
    return False


def _valid_jpeg_container(data: bytes, maximum_segments: int) -> bool:
    if not data.startswith(b"\xff\xd8"):
        return False
    offset = 2
    in_scan = False
    segments = 0
    while offset < len(data):
        segments += 1
        if segments > maximum_segments:
            return False
        if in_scan:
            marker_start = data.find(b"\xff", offset)
            if marker_start < 0 or marker_start + 1 >= len(data):
                return False
            code_offset = marker_start + 1
            while code_offset < len(data) and data[code_offset] == 0xFF:
                code_offset += 1
            if code_offset >= len(data):
                return False
            code = data[code_offset]
            if code == 0x00 or 0xD0 <= code <= 0xD7:
                offset = code_offset + 1
                continue
            offset = marker_start
            in_scan = False
            continue

        if data[offset] != 0xFF:
            return False
        marker_start = offset
        offset += 1
        while offset < len(data) and data[offset] == 0xFF:
            offset += 1
        if offset >= len(data):
            return False
        code = data[offset]
        offset += 1
        if code == 0xD9:
            return offset == len(data)
        if code in {0x00, 0xD8}:
            return False
        if code == 0x01 or 0xD0 <= code <= 0xD7:
            continue
        if offset + 2 > len(data):
            return False
        segment_length = int.from_bytes(data[offset : offset + 2], "big")
        if segment_length < 2 or offset + segment_length > len(data):
            return False
        offset += segment_length
        if code == 0xDA:
            in_scan = True
        if offset <= marker_start:  # pragma: no cover - defensive progress invariant
            return False
    return False


def _valid_webp_container(data: bytes, maximum_segments: int) -> bool:
    if (
        len(data) < 20
        or data[:4] != b"RIFF"
        or data[8:12] != b"WEBP"
        or int.from_bytes(data[4:8], "little") + 8 != len(data)
    ):
        return False
    allowed = {b"VP8 ", b"VP8L", b"VP8X", b"ALPH", b"ICCP", b"EXIF", b"XMP "}
    offset = 12
    image_chunks = 0
    chunks = 0
    while offset + 8 <= len(data):
        chunks += 1
        if chunks > maximum_segments:
            return False
        chunk_type = data[offset : offset + 4]
        length = int.from_bytes(data[offset + 4 : offset + 8], "little")
        payload_end = offset + 8 + length
        padded_end = payload_end + (length & 1)
        if chunk_type not in allowed or padded_end > len(data):
            return False
        if length & 1 and data[payload_end:padded_end] != b"\x00":
            return False
        if chunk_type in {b"VP8 ", b"VP8L"}:
            image_chunks += 1
        offset = padded_end
    return offset == len(data) and image_chunks == 1


def _has_exact_container_length(data: bytes, detected_mime: str, maximum_segments: int) -> bool:
    if detected_mime == "image/png":
        return _valid_png_container(data, maximum_segments)
    if detected_mime == "image/jpeg":
        return _valid_jpeg_container(data, maximum_segments)
    if detected_mime == "image/webp":
        return _valid_webp_container(data, maximum_segments)
    return False


def _png_dimensions(data: bytes) -> tuple[int, int] | None:
    if len(data) < 33 or data[12:16] != b"IHDR":
        return None
    width = int.from_bytes(data[16:20], "big")
    height = int.from_bytes(data[20:24], "big")
    return (width, height) if width and height else None


def _jpeg_dimensions(data: bytes, maximum_segments: int) -> tuple[int, int] | None:
    offset = 2
    dimensions: tuple[int, int] | None = None
    sof_markers = {
        0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
        0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF,
    }
    segments = 0
    while offset < len(data):
        segments += 1
        if segments > maximum_segments or data[offset] != 0xFF:
            return None
        offset += 1
        while offset < len(data) and data[offset] == 0xFF:
            offset += 1
        if offset >= len(data):
            return None
        code = data[offset]
        offset += 1
        if code == 0xD9:
            return dimensions
        if code in {0x00, 0xD8}:
            return None
        if code == 0x01 or 0xD0 <= code <= 0xD7:
            continue
        if offset + 2 > len(data):
            return None
        segment_length = int.from_bytes(data[offset : offset + 2], "big")
        if segment_length < 2 or offset + segment_length > len(data):
            return None
        if code in sof_markers:
            if segment_length < 8:
                return None
            height = int.from_bytes(data[offset + 3 : offset + 5], "big")
            width = int.from_bytes(data[offset + 5 : offset + 7], "big")
            candidate = (width, height)
            if not width or not height or (dimensions is not None and dimensions != candidate):
                return None
            dimensions = candidate
        if code == 0xDA:
            return dimensions
        offset += segment_length
    return None


def _webp_dimensions(data: bytes, maximum_segments: int) -> tuple[int, int] | None:
    offset = 12
    chunks = 0
    canvas: tuple[int, int] | None = None
    image_dimensions: tuple[int, int] | None = None
    while offset + 8 <= len(data):
        chunks += 1
        if chunks > maximum_segments:
            return None
        chunk_type = data[offset : offset + 4]
        length = int.from_bytes(data[offset + 4 : offset + 8], "little")
        payload_end = offset + 8 + length
        if payload_end > len(data):
            return None
        payload = data[offset + 8 : payload_end]
        candidate: tuple[int, int] | None = None
        if chunk_type == b"VP8X":
            if len(payload) != 10:
                return None
            canvas = (
                int.from_bytes(payload[4:7], "little") + 1,
                int.from_bytes(payload[7:10], "little") + 1,
            )
        elif chunk_type == b"VP8 ":
            if len(payload) < 10 or payload[3:6] != b"\x9d\x01\x2a":
                return None
            candidate = (
                int.from_bytes(payload[6:8], "little") & 0x3FFF,
                int.from_bytes(payload[8:10], "little") & 0x3FFF,
            )
        elif chunk_type == b"VP8L":
            if len(payload) < 5 or payload[0] != 0x2F:
                return None
            bits = int.from_bytes(payload[1:5], "little")
            candidate = ((bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1)
        if candidate is not None:
            if image_dimensions is not None or not candidate[0] or not candidate[1]:
                return None
            image_dimensions = candidate
        offset = payload_end + (length & 1)
    if image_dimensions is None or (canvas is not None and canvas != image_dimensions):
        return None
    return image_dimensions


def _container_dimensions(
    data: bytes, detected_mime: str, maximum_segments: int
) -> tuple[int, int] | None:
    if detected_mime == "image/png":
        return _png_dimensions(data)
    if detected_mime == "image/jpeg":
        return _jpeg_dimensions(data, maximum_segments)
    if detected_mime == "image/webp":
        return _webp_dimensions(data, maximum_segments)
    return None


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
            or not _has_exact_container_length(
                data, detected_mime or "", limits.max_container_segments
            )
        ):
            return None
        header_dimensions = _container_dimensions(
            data, detected_mime or "", limits.max_container_segments
        )
        if header_dimensions is None or not _dimensions_allowed(*header_dimensions, limits):
            return None
        with Image.open(io.BytesIO(data)) as probe:
            decoded_format = probe.format
            if decoded_format != expected_format:
                return None
            if getattr(probe, "n_frames", 1) != 1 or getattr(probe, "is_animated", False):
                return None
            if probe.size != header_dimensions or not _dimensions_allowed(
                probe.width, probe.height, limits
            ):
                return None
            probe.verify()

        with Image.open(io.BytesIO(data)) as decoded:
            if (
                decoded.format != decoded_format
                or getattr(decoded, "n_frames", 1) != 1
                or decoded.size != header_dimensions
            ):
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
                final_size = transposed.size
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
        with Image.open(io.BytesIO(canonical)) as verified:
            if (
                verified.format != "PNG"
                or getattr(verified, "n_frames", 1) != 1
                or verified.size != final_size
                or verified.info
            ):
                return None
            verified.load()
        return canonical, decoded_format, final_size[0], final_size[1]
    except (
        Image.DecompressionBombError,
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
    result = (
        None
        if copied is None
        else _sanitize_copied(
            copied, declared_mime, tenant_id, configured_limits, configured_detector
        )
    )
    del copied, declared_mime, tenant_id, configured_detector, configured_limits
    if result is None:
        raise InvalidImage("image rejected") from None
    return result


def _validated_store_match(
    tenant_id: str, object_key: str, data: bytes, content_type: str
) -> re.Match[str]:
    match = _OBJECT_KEY.fullmatch(object_key)
    if type(tenant_id) is not str or _TENANT_ID.fullmatch(tenant_id) is None:
        raise ValueError("invalid image object key or metadata")
    tenant_digest = hashlib.sha256(tenant_id.encode("utf-8")).hexdigest()
    if (
        match is None
        or match.group(1) != tenant_digest
        or content_type != "image/png"
        or type(data) is not bytes
        or not data
        or len(data) > _MAX_STORED_IMAGE_BYTES
    ):
        raise ValueError("invalid image object key or metadata")
    return match


def _effective_uid() -> int:
    get_effective_uid = getattr(os, "geteuid", None)
    if not callable(get_effective_uid):
        raise OSError("effective user identity is unavailable")
    return int(get_effective_uid())


def _os_flag(name: str) -> int:
    value = getattr(os, name)
    if not isinstance(value, int):
        raise TypeError("POSIX filesystem flag is unavailable")
    return value


class MemoryImageStore:
    """Portable test/development adapter with the same tenant-bound key contract."""

    def __init__(
        self,
        root: Path | str | None = None,
        *,
        store_id: str = "memory-image-store",
        namespace: str = "default",
    ) -> None:
        del root
        self.store_id = store_id
        self.namespace = namespace
        self._objects: dict[str, bytes] = {}

    async def put(
        self, tenant_id: str, object_key: str, data: bytes, content_type: str
    ) -> StoredImageObject:
        _validated_store_match(tenant_id, object_key, data, content_type)
        if object_key in self._objects:
            raise OSError("image object already exists") from None
        copied = bytes(data)
        self._objects[object_key] = copied
        return StoredImageObject(
            object_key=object_key,
            byte_length=len(copied),
            content_type=content_type,
            sha256=hashlib.sha256(copied).hexdigest(),
        )

    async def delete_by_object_key(
        self, object_key: str, expected_sha256: str
    ) -> None:
        if (
            type(object_key) is not str
            or _OBJECT_KEY.fullmatch(object_key) is None
            or type(expected_sha256) is not str
            or _SHA256.fullmatch(expected_sha256) is None
        ):
            raise OSError("image storage cleanup failed") from None
        current = self._objects.get(object_key)
        if current is None:
            return
        if not hmac.compare_digest(hashlib.sha256(current).hexdigest(), expected_sha256):
            raise OSError("image object hash mismatch") from None
        del self._objects[object_key]

    def contains(self, tenant_id: str, object_key: str) -> bool:
        digest = hashlib.sha256(tenant_id.encode("utf-8")).hexdigest()
        return object_key.startswith(f"tenants/{digest}/") and object_key in self._objects


async def replay_image_cleanup(
    item: ImageCleanupRecoveryItem, store: ImageObjectStore
) -> None:
    if item.store_id != store.store_id or item.namespace != store.namespace:
        raise VisionCleanupError("recovery item store mismatch") from None
    failed = False
    try:
        await store.delete_by_object_key(item.object_key, item.canonical_sha256)
    except (Exception, asyncio.CancelledError) as error:  # noqa: BLE001 - adapter boundary
        error.__traceback__ = None
        error.__context__ = None
        error.__cause__ = None
        del error
        failed = True
    if failed:
        raise VisionCleanupError("recovery cleanup failed") from None


class ImageStoreCommitUncertain(OSError):
    """A published target could not be durably rolled back."""

    def __init__(self, object_key: str) -> None:
        super().__init__("image store commit state is uncertain")
        self.object_key = object_key

    def __repr__(self) -> str:
        return f"ImageStoreCommitUncertain(object_key={self.object_key!r})"


class _DeleteResult(Enum):
    DELETED = "deleted"
    HASH_MISMATCH = "hash_mismatch"
    FAILED = "failed"


class FilesystemImageStore:
    """POSIX dirfd-based adapter that never follows filesystem links."""

    def __init__(self, root: Path | str) -> None:
        if os.name != "posix":
            raise RuntimeError("FilesystemImageStore requires POSIX dirfd semantics")
        required = ("O_DIRECTORY", "O_NOFOLLOW")
        if any(not hasattr(os, name) for name in required):
            raise RuntimeError("FilesystemImageStore requires POSIX dirfd semantics")
        configured_root = Path(root).absolute()
        if not configured_root.is_absolute():  # pragma: no cover - absolute() invariant
            raise ValueError("image store root must be absolute")
        self._fd_lock = threading.Lock()
        self.store_id = "filesystem-image-store"
        self.namespace = hashlib.sha256(os.fsencode(configured_root)).hexdigest()
        self._root_fd = self._open_secure_root(configured_root)

    @staticmethod
    def _directory_flags() -> int:
        return os.O_RDONLY | _os_flag("O_DIRECTORY") | _os_flag("O_NOFOLLOW")

    @staticmethod
    def _nofollow_flag() -> int:
        return _os_flag("O_NOFOLLOW")

    @classmethod
    def _open_secure_root(cls, root: Path) -> int:
        descriptor = os.open(root.anchor, cls._directory_flags())
        try:
            for part in root.parts[1:]:
                try:
                    os.mkdir(part, mode=0o700, dir_fd=descriptor)
                except FileExistsError:
                    pass
                child = os.open(part, cls._directory_flags(), dir_fd=descriptor)
                os.close(descriptor)
                descriptor = child
            cls._validate_directory(descriptor, private=True)
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise

    @staticmethod
    def _validate_directory(descriptor: int, *, private: bool) -> None:
        metadata = os.fstat(descriptor)
        if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != _effective_uid():
            raise OSError("unsafe image store directory")
        if private and metadata.st_mode & 0o077:
            raise OSError("unsafe image store directory permissions")

    @classmethod
    def _open_private_directory(cls, parent_fd: int, name: str, *, create: bool) -> int:
        if create:
            try:
                os.mkdir(name, mode=0o700, dir_fd=parent_fd)
            except FileExistsError:
                pass
        descriptor = os.open(name, cls._directory_flags(), dir_fd=parent_fd)
        try:
            cls._validate_directory(descriptor, private=True)
        except BaseException:
            os.close(descriptor)
            raise
        return descriptor

    def _duplicate_root_fd(self) -> int:
        with self._fd_lock:
            if self._root_fd < 0:
                raise OSError("image store is closed")
            return os.dup(self._root_fd)

    def close(self) -> None:
        with self._fd_lock:
            descriptor, self._root_fd = self._root_fd, -1
        if descriptor >= 0:
            os.close(descriptor)

    def __del__(self) -> None:
        descriptor = getattr(self, "_root_fd", -1)
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass
            self._root_fd = -1

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
            result = await _await_task_uninterruptibly(worker)
        finally:
            del worker
        if result is None:
            raise OSError("image storage failed") from None
        if isinstance(result, ValueError | ImageStoreCommitUncertain):
            raise result from None
        return result

    async def delete_by_object_key(
        self, object_key: str, expected_sha256: str
    ) -> None:
        worker = asyncio.create_task(
            asyncio.to_thread(self._delete_sync_safe, object_key, expected_sha256)
        )
        del object_key, expected_sha256
        try:
            result = await _await_task_uninterruptibly(worker)
        finally:
            del worker
        if result is _DeleteResult.HASH_MISMATCH:
            raise OSError("image object hash mismatch") from None
        if result is not _DeleteResult.DELETED:
            raise OSError("image storage cleanup failed") from None

    def _put_sync_safe(
        self, tenant_id: str, object_key: str, data: bytes, content_type: str
    ) -> StoredImageObject | ValueError | ImageStoreCommitUncertain | None:
        root_fd = tenants_fd = tenant_fd = -1
        temporary_name: str | None = None
        target_name: str | None = None
        target_created = False
        try:
            try:
                match = _validated_store_match(tenant_id, object_key, data, content_type)
            except ValueError as error:
                return error
            root_fd = self._duplicate_root_fd()
            tenants_fd = self._open_private_directory(root_fd, "tenants", create=True)
            tenant_fd = self._open_private_directory(tenants_fd, match.group(1), create=True)
            temporary_name = f".{uuid.uuid4()}.tmp"
            descriptor = os.open(
                temporary_name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | self._nofollow_flag(),
                0o600,
                dir_fd=tenant_fd,
            )
            try:
                metadata = os.fstat(descriptor)
                if (
                    not stat.S_ISREG(metadata.st_mode)
                    or metadata.st_uid != _effective_uid()
                    or metadata.st_mode & 0o177
                ):
                    raise OSError("unsafe temporary image object")
                view = memoryview(data)
                while view:
                    written = os.write(descriptor, view)
                    if written <= 0:
                        raise OSError("short image object write")
                    view = view[written:]
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            target_name = f"{match.group(2)}.png"
            os.link(
                temporary_name,
                target_name,
                src_dir_fd=tenant_fd,
                dst_dir_fd=tenant_fd,
                follow_symlinks=False,
            )
            target_created = True
            os.unlink(temporary_name, dir_fd=tenant_fd)
            temporary_name = None
            os.fsync(tenant_fd)
            return StoredImageObject(
                object_key=object_key,
                byte_length=len(data),
                content_type=content_type,
                sha256=hashlib.sha256(data).hexdigest(),
            )
        except (Exception, asyncio.CancelledError) as error:  # noqa: BLE001 - adapter boundary
            error.__traceback__ = None
            del error
            if target_created and target_name is not None:
                try:
                    os.unlink(target_name, dir_fd=tenant_fd)
                    os.fsync(tenant_fd)
                except (OSError, ValueError):
                    return ImageStoreCommitUncertain(object_key)
            return None
        finally:
            if temporary_name is not None and tenant_fd >= 0:
                try:
                    os.unlink(temporary_name, dir_fd=tenant_fd)
                except OSError:
                    pass
            for descriptor in (tenant_fd, tenants_fd, root_fd):
                if descriptor >= 0:
                    os.close(descriptor)

    def _delete_sync_safe(
        self, object_key: str, expected_sha256: str
    ) -> _DeleteResult:
        root_fd = tenants_fd = tenant_fd = target_fd = -1
        try:
            match = _OBJECT_KEY.fullmatch(object_key)
            if match is None or _SHA256.fullmatch(expected_sha256) is None:
                return _DeleteResult.FAILED
            tenant_digest = match.group(1)
            root_fd = self._duplicate_root_fd()
            try:
                tenants_fd = self._open_private_directory(root_fd, "tenants", create=False)
                tenant_fd = self._open_private_directory(tenants_fd, tenant_digest, create=False)
            except FileNotFoundError:
                return _DeleteResult.DELETED
            target_name = f"{match.group(2)}.png"
            try:
                target_fd = os.open(
                    target_name,
                    os.O_RDONLY | self._nofollow_flag(),
                    dir_fd=tenant_fd,
                )
            except FileNotFoundError:
                return _DeleteResult.DELETED
            metadata = os.fstat(target_fd)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != _effective_uid()
                or metadata.st_mode & 0o177
                or metadata.st_size > _MAX_STORED_IMAGE_BYTES
            ):
                return _DeleteResult.FAILED
            digest = hashlib.sha256()
            while True:
                chunk = os.read(target_fd, 128 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
            if not hmac.compare_digest(digest.hexdigest(), expected_sha256):
                return _DeleteResult.HASH_MISMATCH
            current = os.stat(target_name, dir_fd=tenant_fd, follow_symlinks=False)
            if (
                not stat.S_ISREG(current.st_mode)
                or current.st_dev != metadata.st_dev
                or current.st_ino != metadata.st_ino
            ):
                return _DeleteResult.FAILED
            os.unlink(target_name, dir_fd=tenant_fd)
            os.fsync(tenant_fd)
            return _DeleteResult.DELETED
        except (OSError, UnicodeError, ValueError):
            return _DeleteResult.FAILED
        finally:
            for descriptor in (target_fd, tenant_fd, tenants_fd, root_fd):
                if descriptor >= 0:
                    os.close(descriptor)
