from __future__ import annotations

import hashlib
import io
import os
import stat
import threading
import warnings
import zlib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Self

import pytest
from PIL import Image, PngImagePlugin

import agent_hub.multimodal.images as image_module
from agent_hub.multimodal.images import (
    FilesystemImageStore,
    ImageStoreCommitUncertain,
    sanitize_image,
)
from agent_hub.multimodal.types import ImageLimits, InvalidImage


def encoded_image(
    image_format: str = "PNG",
    *,
    size: tuple[int, int] = (8, 6),
    mode: str = "RGB",
    **save_options: object,
) -> bytes:
    image = Image.new(mode, size, (10, 20, 30, 255) if mode == "RGBA" else (10, 20, 30))
    output = io.BytesIO()
    image.save(output, format=image_format, **save_options)
    return output.getvalue()


def test_sanitizes_to_deterministic_metadata_free_png() -> None:
    pnginfo = PngImagePlugin.PngInfo()
    pnginfo.add_text("private", "metadata")
    raw = encoded_image("PNG", pnginfo=pnginfo)

    first = sanitize_image(raw, "image/png", "tenant-a")
    second = sanitize_image(raw, "image/png", "tenant-a")

    assert first.canonical_bytes == second.canonical_bytes
    assert first.source_sha256 == hashlib.sha256(raw).hexdigest()
    assert first.canonical_sha256 == hashlib.sha256(first.canonical_bytes).hexdigest()
    assert first.media_type == "image/png"
    assert first.original_format == "PNG"
    assert first.width == 8 and first.height == 6
    assert first.object_key != second.object_key
    assert first.object_key.startswith(f"tenants/{hashlib.sha256(b'tenant-a').hexdigest()}/")
    assert first.object_key.endswith(".png")
    with Image.open(io.BytesIO(first.canonical_bytes)) as canonical:
        canonical.load()
        assert canonical.format == "PNG" and canonical.info == {}


def test_applies_exif_orientation_and_strips_metadata() -> None:
    image = Image.new("RGB", (4, 2), (1, 2, 3))
    exif = Image.Exif()
    exif[274] = 6
    output = io.BytesIO()
    image.save(output, "JPEG", exif=exif, icc_profile=b"not-private")

    result = sanitize_image(output.getvalue(), "image/jpeg", "tenant")

    assert (result.width, result.height) == (2, 4)
    with Image.open(io.BytesIO(result.canonical_bytes)) as canonical:
        assert "exif" not in canonical.info and "icc_profile" not in canonical.info


def test_accepts_progressive_jpeg_with_multiple_scans() -> None:
    result = sanitize_image(
        encoded_image("JPEG", size=(16, 12), progressive=True),
        "image/jpeg",
        "tenant",
    )
    assert result.original_format == "JPEG" and (result.width, result.height) == (16, 12)


def test_copies_mutable_caller_buffer() -> None:
    source = bytearray(encoded_image())
    expected = hashlib.sha256(source).hexdigest()
    result = sanitize_image(memoryview(source), "image/png", "tenant")
    source[:] = b"x" * len(source)
    assert result.source_sha256 == expected
    assert hashlib.sha256(result.canonical_bytes).hexdigest() == result.canonical_sha256


@pytest.mark.parametrize("value", [None, "bytes", 1, object()])
def test_accepts_only_bytes_like_input(value: object) -> None:
    with pytest.raises(InvalidImage, match="image rejected"):
        sanitize_image(value, "image/png", "tenant")  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("declared", "payload"),
    [
        ("image/png ", encoded_image()),
        ("IMAGE/PNG", encoded_image()),
        ("image/gif", encoded_image("GIF")),
        ("image/png", encoded_image("JPEG")),
        ("image/jpeg", encoded_image("PNG")),
        ("image/png", b"MZ" + encoded_image()),
        ("image/png", b"#!/bin/sh\n" + encoded_image()),
        ("image/png", encoded_image() + b"MZ executable payload"),
        ("image/png", encoded_image()[:20]),
        ("image/png", b"<svg xmlns='http://www.w3.org/2000/svg'></svg>"),
    ],
)
def test_rejects_spoofed_unsupported_truncated_or_polyglot_images(
    declared: str, payload: bytes
) -> None:
    with pytest.raises(InvalidImage, match="^image rejected$") as captured:
        sanitize_image(payload, declared, "tenant")
    assert captured.value.__cause__ is None and captured.value.__context__ is None
    assert payload[:12].hex() not in repr(captured.value)


def test_rejects_animated_images() -> None:
    frames = [Image.new("RGB", (3, 3), color) for color in ("red", "blue")]
    output = io.BytesIO()
    frames[0].save(output, "WEBP", save_all=True, append_images=frames[1:], duration=10)
    with pytest.raises(InvalidImage, match="image rejected"):
        sanitize_image(output.getvalue(), "image/webp", "tenant")


@pytest.mark.parametrize("image_format", ["PNG", "JPEG", "WEBP"])
def test_rejects_container_aware_trailing_polyglots(image_format: str) -> None:
    raw = encoded_image(image_format)
    if image_format == "PNG":
        polyglot = raw + b"MZ-executable" + b"IEND\xaeB`\x82"
        mime = "image/png"
    elif image_format == "JPEG":
        polyglot = raw + b"MZ-executable" + b"\xff\xd9"
        mime = "image/jpeg"
    else:
        payload = b"MZ-executable"
        chunk = b"JUNK" + len(payload).to_bytes(4, "little") + payload
        if len(payload) % 2:
            chunk += b"\x00"
        polyglot = raw + chunk
        polyglot = polyglot[:4] + (len(polyglot) - 8).to_bytes(4, "little") + polyglot[8:]
        mime = "image/webp"
    with pytest.raises(InvalidImage, match="^image rejected$"):
        sanitize_image(polyglot, mime, "tenant")


@pytest.mark.parametrize(
    "limits",
    [
        ImageLimits(max_raw_bytes=20),
        ImageLimits(max_width=3),
        ImageLimits(max_height=3),
        ImageLimits(max_pixels=20),
        ImageLimits(max_container_segments=2),
        ImageLimits(max_aspect_ratio=1.1),
        ImageLimits(max_decoded_bytes=50),
        ImageLimits(max_compression_ratio=0.1),
        ImageLimits(max_canonical_bytes=20),
    ],
)
def test_enforces_resource_limits(limits: ImageLimits) -> None:
    with pytest.raises(InvalidImage, match="image rejected"):
        sanitize_image(encoded_image(size=(8, 6)), "image/png", "tenant", limits=limits)


@pytest.mark.parametrize(
    "field,value",
    [
        ("max_raw_bytes", True),
        ("max_width", 0),
        ("max_container_segments", 1.0),
        ("max_aspect_ratio", float("nan")),
        ("max_compression_ratio", float("inf")),
    ],
)
def test_limits_require_strict_finite_positive_values(field: str, value: object) -> None:
    with pytest.raises(ValueError):
        ImageLimits(**{field: value})  # type: ignore[arg-type]


def test_pillow_warning_does_not_control_acceptance_or_mutate_filters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_open = Image.open

    def warning_open(source: io.BytesIO) -> Image.Image:
        warnings.warn("bomb", Image.DecompressionBombWarning, stacklevel=2)
        return real_open(source)

    monkeypatch.setattr(Image, "open", warning_open)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", Image.DecompressionBombWarning)
        filters_before = list(warnings.filters)
        result = sanitize_image(encoded_image(), "image/png", "tenant")
        assert warnings.filters == filters_before
    assert result.width == 8


def test_sanitize_does_not_clobber_warning_filter_owned_by_another_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_open = Image.open
    production_opened = threading.Event()
    external_filter_set = threading.Event()
    sanitize_done = threading.Event()

    class ExternalWarning(Warning):
        pass

    def controlled_open(source: io.BytesIO) -> Image.Image:
        production_opened.set()
        assert external_filter_set.wait(timeout=5)
        return real_open(source)

    def external_warning_owner() -> bool:
        assert production_opened.wait(timeout=5)
        with warnings.catch_warnings():
            warnings.simplefilter("error", ExternalWarning)
            external_filter_set.set()
            assert sanitize_done.wait(timeout=5)
            try:
                warnings.warn("external", ExternalWarning, stacklevel=1)
            except ExternalWarning:
                return True
            return False

    monkeypatch.setattr(Image, "open", controlled_open)
    with ThreadPoolExecutor(max_workers=2) as executor:
        sanitization = executor.submit(
            sanitize_image, encoded_image(), "image/png", "tenant"
        )
        owner = executor.submit(external_warning_owner)
        assert sanitization.result(timeout=5).width == 8
        sanitize_done.set()
        assert owner.result(timeout=5)


def test_unsafe_png_header_dimensions_are_rejected_before_pillow_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = bytearray(encoded_image())
    dimensions = (8192).to_bytes(4, "big") * 2
    payload[16:24] = dimensions
    payload[29:33] = zlib.crc32(b"IHDR" + dimensions + payload[24:29]).to_bytes(4, "big")
    opens = 0

    def forbidden_open(source: io.BytesIO) -> Image.Image:
        nonlocal opens
        del source
        opens += 1
        raise AssertionError("Pillow must not see unsafe dimensions")

    monkeypatch.setattr(Image, "open", forbidden_open)
    with pytest.raises(InvalidImage, match="image rejected"):
        sanitize_image(bytes(payload), "image/png", "tenant")
    assert opens == 0


def test_configured_pixel_limit_cannot_exceed_hard_safety_ceiling() -> None:
    with pytest.raises(ValueError, match="max_pixels"):
        ImageLimits(max_pixels=40_000_001)


def test_header_and_decoder_dimensions_must_match(monkeypatch: pytest.MonkeyPatch) -> None:
    real_open = Image.open
    first = True

    class MismatchedProbe:
        def __init__(self, image: Image.Image) -> None:
            self._image = image
            self.format = image.format
            self.width = image.width + 1
            self.height = image.height
            self.size = (self.width, self.height)
            self.n_frames = 1
            self.is_animated = False

        def __enter__(self) -> Self:
            return self

        def __exit__(self, *args: object) -> None:
            del args
            self._image.close()

        def verify(self) -> None:
            self._image.verify()

    def mismatched_open(source: io.BytesIO) -> Image.Image | MismatchedProbe:
        nonlocal first
        image = real_open(source)
        if first:
            first = False
            return MismatchedProbe(image)
        return image

    monkeypatch.setattr(Image, "open", mismatched_open)
    with pytest.raises(InvalidImage, match="image rejected"):
        sanitize_image(encoded_image(), "image/png", "tenant")


def test_injected_detector_failure_is_redacted() -> None:
    class FailingDetector:
        def detect(self, data: bytes) -> str | None:
            del data
            raise RuntimeError("private detector detail")

    with pytest.raises(InvalidImage, match="^image rejected$") as captured:
        sanitize_image(encoded_image(), "image/png", "tenant", detector=FailingDetector())
    assert captured.value.__cause__ is None and captured.value.__context__ is None


def test_invalid_image_traceback_does_not_retain_full_payload() -> None:
    payload = b"private-pixels-" * 100
    with pytest.raises(InvalidImage) as captured:
        sanitize_image(payload, "image/png", "tenant")
    locals_dump: list[str] = []
    traceback = captured.value.__traceback__
    while traceback is not None:
        if traceback.tb_frame.f_globals.get("__name__", "").startswith("agent_hub"):
            locals_dump.extend(repr(value) for value in traceback.tb_frame.f_locals.values())
        traceback = traceback.tb_next
    assert payload.decode() not in " ".join(locals_dump)


def test_tenant_identifier_is_bounded_and_never_appears_in_object_key() -> None:
    tenant = "Customer-Secret_1"
    result = sanitize_image(encoded_image(), "image/png", tenant)
    assert tenant not in result.object_key
    assert ".." not in result.object_key
    for invalid in ("", " padded", "../tenant", "tenant/name", "x" * 257, "line\nbreak"):
        with pytest.raises(InvalidImage):
            sanitize_image(encoded_image(), "image/png", invalid)


def test_sanitized_image_repr_hides_bytes_and_is_unhashable() -> None:
    result = sanitize_image(encoded_image(), "image/png", "tenant")
    assert "canonical_bytes" not in repr(result)
    with pytest.raises(TypeError):
        hash(result)


@pytest.mark.skipif(os.name != "posix", reason="POSIX dirfd adapter")
async def test_filesystem_store_writes_atomically_with_private_permissions(tmp_path: Path) -> None:
    store = FilesystemImageStore(tmp_path)
    digest = hashlib.sha256(b"tenant").hexdigest()
    key = f"tenants/{digest}/123e4567-e89b-42d3-a456-426614174000.png"
    stored = await store.put("tenant", key, b"canonical", "image/png")
    target = tmp_path.joinpath(*key.split("/"))

    assert target.read_bytes() == b"canonical"
    assert stored.object_key == key and stored.byte_length == 9
    assert stored.content_type == "image/png"
    if os.name == "posix":
        assert os.stat(target).st_mode & 0o777 == 0o600

    await store.delete("tenant", key)
    assert not target.exists()


@pytest.mark.skipif(os.name != "posix", reason="POSIX dirfd adapter")
async def test_filesystem_store_delete_is_tenant_bound(tmp_path: Path) -> None:
    store = FilesystemImageStore(tmp_path)
    digest = hashlib.sha256(b"tenant").hexdigest()
    key = f"tenants/{digest}/123e4567-e89b-42d3-a456-426614174000.png"
    await store.put("tenant", key, b"canonical", "image/png")
    with pytest.raises(OSError, match="cleanup failed"):
        await store.delete("different-tenant", key)
    assert tmp_path.joinpath(*key.split("/")).exists()


@pytest.mark.parametrize(
    "key",
    ["../escape.png", "/absolute.png", "tenants/a/../../escape", "tenants/a/not-uuid.png"],
)
async def test_filesystem_store_rejects_untrusted_keys(tmp_path: Path, key: str) -> None:
    if os.name != "posix":
        pytest.skip("POSIX dirfd adapter")
    with pytest.raises(ValueError, match="object key"):
        await FilesystemImageStore(tmp_path).put("tenant", key, b"data", "image/png")


@pytest.mark.skipif(os.name != "posix", reason="POSIX dirfd adapter")
async def test_filesystem_store_rejects_symlink_traversal(tmp_path: Path) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()
    digest = hashlib.sha256(b"tenant").hexdigest()
    tenant_dir = tmp_path / "tenants"
    tenant_dir.mkdir()
    try:
        (tenant_dir / digest).symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks unavailable")
    key = f"tenants/{digest}/123e4567-e89b-42d3-a456-426614174000.png"
    with pytest.raises(OSError, match="image storage failed"):
        await FilesystemImageStore(tmp_path).put("tenant", key, b"data", "image/png")
    assert list(outside.iterdir()) == []


@pytest.mark.skipif(os.name != "posix", reason="POSIX dirfd adapter")
async def test_filesystem_store_rejects_configured_symlink_root(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    linked_root = tmp_path / "linked-root"
    try:
        linked_root.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks unavailable")
    digest = hashlib.sha256(b"tenant").hexdigest()
    key = f"tenants/{digest}/123e4567-e89b-42d3-a456-426614174000.png"
    with pytest.raises(OSError, match="image storage failed"):
        await FilesystemImageStore(linked_root).put("tenant", key, b"data", "image/png")
    assert list(outside.iterdir()) == []


@pytest.mark.skipif(os.name == "posix", reason="non-POSIX only")
def test_filesystem_store_fails_closed_off_posix(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="POSIX"):
        FilesystemImageStore(tmp_path)


@pytest.mark.skipif(os.name != "posix", reason="POSIX dirfd adapter")
@pytest.mark.parametrize("failure", ["temp_unlink", "directory_fsync", "result"])
async def test_filesystem_store_rolls_back_every_post_publish_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    store = FilesystemImageStore(tmp_path)
    digest = hashlib.sha256(b"tenant").hexdigest()
    key = f"tenants/{digest}/123e4567-e89b-42d3-a456-426614174000.png"
    target = tmp_path.joinpath(*key.split("/"))
    real_unlink = os.unlink
    real_fsync = os.fsync

    def injected_unlink(path: str, *, dir_fd: int | None = None) -> None:
        if failure == "temp_unlink" and path.startswith("."):
            raise OSError("injected")
        real_unlink(path, dir_fd=dir_fd)

    def injected_fsync(descriptor: int) -> None:
        if failure == "directory_fsync" and stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise OSError("injected")
        real_fsync(descriptor)

    class FailingStoredImageObject:
        def __init__(self, **kwargs: object) -> None:
            del kwargs
            raise RuntimeError("injected")

    monkeypatch.setattr(os, "unlink", injected_unlink)
    monkeypatch.setattr(os, "fsync", injected_fsync)
    if failure == "result":
        monkeypatch.setattr(image_module, "StoredImageObject", FailingStoredImageObject)
    with pytest.raises((OSError, RuntimeError)):
        await store.put("tenant", key, b"canonical", "image/png")
    assert not target.exists()
    store.close()


@pytest.mark.skipif(os.name != "posix", reason="POSIX dirfd adapter")
async def test_filesystem_store_reports_commit_uncertain_when_rollback_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    baseline_descriptors = len(os.listdir("/proc/self/fd"))
    store = FilesystemImageStore(tmp_path)
    digest = hashlib.sha256(b"tenant").hexdigest()
    key = f"tenants/{digest}/123e4567-e89b-42d3-a456-426614174000.png"
    target_name = key.rsplit("/", 1)[1]
    real_unlink = os.unlink
    real_fsync = os.fsync

    def injected_unlink(path: str, *, dir_fd: int | None = None) -> None:
        if path == target_name:
            raise OSError("injected rollback failure")
        real_unlink(path, dir_fd=dir_fd)

    def injected_fsync(descriptor: int) -> None:
        if stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise OSError("injected publish failure")
        real_fsync(descriptor)

    with monkeypatch.context() as injected:
        injected.setattr(os, "unlink", injected_unlink)
        injected.setattr(os, "fsync", injected_fsync)
        with pytest.raises(ImageStoreCommitUncertain) as captured:
            await store.put("tenant", key, b"canonical", "image/png")
    assert captured.value.object_key == key
    assert "canonical" not in repr(captured.value)
    await store.delete("tenant", key)
    store.close()
    assert len(os.listdir("/proc/self/fd")) == baseline_descriptors
