"""Pure package facts, not a source grant, user authorization, or install capability.

The caller must load the private snapshot and current tenant key itself. Final
DB integration must re-read grant/user/run/key state after the registration lock
in the commit session; a frozen key and the facts here cannot replace that check.
"""

from __future__ import annotations

import hashlib
import io
import json
import math
import re
import struct
import zipfile
import zlib
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from pydantic import ValidationError

from agent_hub.plugins.handover_sources import (
    FrozenSourceSnapshot,
    InvalidSource,
    SnapshotFile,
    SourceLimits,
    _checked_source,
    _safe_path,
    _validate_paths,
    _zip_files,
)
from agent_hub.plugins.handover_storage import AuthorizedSource
from agent_hub.skills.package import InvalidSkillPackage

if TYPE_CHECKING:
    from agent_hub.api.routers.admin import PluginArchiveManifest

_MAX_JSON_DEPTH = 64
_MAX_JSON_NODES = 20_000
_SIGNATURE_FIELDS = frozenset({"algorithm", "key_id", "value"})


class PackageVerificationError(ValueError):
    """A safe, payload/path-free failure at the pure verification boundary."""


def _utc(value: datetime) -> datetime:
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
        raise PackageVerificationError("verification timestamps must be timezone-aware")
    return value.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class FrozenSigningKey:
    """Caller-supplied current-key data; no tenant/DB freshness claim is made."""

    key_id: str
    algorithm: Literal["ed25519"]
    public_key: bytes = field(repr=False)
    trusted: Literal[True]
    not_before: datetime | None = None
    not_after: datetime | None = None

    def __post_init__(self) -> None:
        if (
            type(self.key_id) is not str
            or re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.:-]{0,127}", self.key_id) is None
            or type(self.algorithm) is not str
            or self.algorithm != "ed25519"
            or type(self.public_key) is not bytes
            or len(self.public_key) != 32
            or self.trusted is not True
        ):
            raise PackageVerificationError("an explicit trusted Ed25519 key is required")
        before = _utc(self.not_before) if self.not_before is not None else None
        after = _utc(self.not_after) if self.not_after is not None else None
        if before is not None and after is not None and after <= before:
            raise PackageVerificationError("invalid signing key window")


@dataclass(frozen=True, slots=True)
class VerifiedMember:
    path: str
    size_bytes: int
    sha256: str


@dataclass(frozen=True, slots=True)
class VerifiedPackageFacts:
    plugin_id: str
    plugin_version: str
    source_manifest_sha256: str
    frozen_manifest_sha256: str
    archive_sha256: str
    signature_payload_sha256: str
    key_id: str
    public_key_sha256: str
    verified_at: datetime
    key_not_after: datetime | None
    members: tuple[VerifiedMember, ...]


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise PackageVerificationError("duplicate manifest field")
        result[key] = value
    return result


def _reject_constant(value: str) -> object:
    raise PackageVerificationError("nonfinite manifest number")


def _manifest(data: bytes) -> dict[str, object]:
    # Bound nesting before json.loads can recurse. Delimiters inside strings do
    # not contribute, and json.loads remains responsible for syntax validation.
    depth = 0
    quoted = False
    escaped = False
    for byte in data:
        if quoted:
            if escaped:
                escaped = False
            elif byte == 92:
                escaped = True
            elif byte == 34:
                quoted = False
        elif byte == 34:
            quoted = True
        elif byte in (91, 123):
            depth += 1
            if depth > _MAX_JSON_DEPTH:
                raise PackageVerificationError("manifest nesting exceeds limit")
        elif byte in (93, 125):
            depth -= 1
    raw: object = json.loads(
        data.decode("utf-8"), object_pairs_hook=_unique_object, parse_constant=_reject_constant
    )
    if not isinstance(raw, dict):
        raise PackageVerificationError("manifest must be an object")
    pending: list[object] = [raw]
    nodes = 0
    while pending:
        value = pending.pop()
        nodes += 1
        if nodes > _MAX_JSON_NODES:
            raise PackageVerificationError("manifest structure exceeds limit")
        if isinstance(value, dict):
            pending.extend(value.keys())
            pending.extend(value.values())
        elif isinstance(value, list):
            pending.extend(value)
        elif isinstance(value, float) and not math.isfinite(value):
            raise PackageVerificationError("nonfinite manifest number")
        elif isinstance(value, str):
            value.encode("utf-8")
    return raw


def _without_signature(raw: dict[str, object], *, required: bool) -> dict[str, object]:
    package = raw.get("package")
    if not isinstance(package, dict):
        raise PackageVerificationError("adapter package metadata is required")
    if "signature" not in package:
        if required:
            raise PackageVerificationError("signed package is required")
    else:
        signature = package["signature"]
        if not isinstance(signature, dict) or set(signature) != _SIGNATURE_FIELDS:
            raise PackageVerificationError("invalid signature envelope")
        if any(type(value) is not str for value in signature.values()):
            raise PackageVerificationError("invalid signature envelope")
    return {**raw, "package": {key: value for key, value in package.items() if key != "signature"}}


def _same_json(left: object, right: object) -> bool:
    if type(left) is not type(right):
        return False
    if isinstance(left, dict) and isinstance(right, dict):
        return left.keys() == right.keys() and all(_same_json(v, right[k]) for k, v in left.items())
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(_same_json(a, b) for a, b in zip(left, right))
    if isinstance(left, float) and isinstance(right, float):
        return left.hex() == right.hex()
    return bool(left == right)


def _checked_snapshot(
    snapshot: FrozenSourceSnapshot, limits: SourceLimits
) -> tuple[FrozenSourceSnapshot, SnapshotFile]:
    if (
        type(snapshot) is not FrozenSourceSnapshot
        or type(snapshot.authorized) is not AuthorizedSource
        or type(snapshot.files) is not tuple
        or type(snapshot.total_size_bytes) is not int
        or type(snapshot.plugin_id) is not str
        or type(snapshot.plugin_version) is not str
        or type(snapshot.authorized.source_manifest_sha256) is not str
        or any(type(item) is not SnapshotFile for item in snapshot.files)
    ):
        raise PackageVerificationError("invalid frozen snapshot")
    authorized = AuthorizedSource.model_validate(snapshot.authorized.model_dump(mode="python"))
    snapshot = replace(
        snapshot, authorized=authorized, files=tuple(replace(item) for item in snapshot.files)
    )
    source = _checked_source(authorized.source)
    paths = tuple(item.path for item in snapshot.files)
    if any(type(item.path) is not str or type(item.sha256) is not str for item in snapshot.files):
        raise PackageVerificationError("invalid frozen member fields")
    _validate_paths(paths, limits)
    if paths != tuple(sorted(paths)) or (source.kind == "workspace" and paths != source.paths):
        raise PackageVerificationError("frozen member selection is inconsistent")
    total = 0
    for item in snapshot.files:
        if (
            type(item.content) is not bytes
            or type(item.size_bytes) is not int
            or item.size_bytes != len(item.content)
            or item.size_bytes > limits.max_file_bytes
            or hashlib.sha256(item.content).hexdigest() != item.sha256
        ):
            raise PackageVerificationError("frozen member identity is inconsistent")
        total += item.size_bytes
    if total != snapshot.total_size_bytes or total > limits.max_total_bytes:
        raise PackageVerificationError("frozen total is inconsistent or exceeds limit")
    manifests = [item for item in snapshot.files if item.path == "plugin.json"]
    if len(manifests) != 1:
        raise PackageVerificationError("original root manifest is required")
    return snapshot, manifests[0]


def _archive_files(data: bytes, limits: SourceLimits) -> tuple[SnapshotFile, ...]:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        members = archive.infolist()
        if not members or len(members) > limits.max_files:
            raise PackageVerificationError("archive member count exceeds limit")
        # This capability accepts file-only ZIPs. Directories have no frozen
        # byte identity, and ZipInfo otherwise silently truncates NUL names.
        if any(member.is_dir() or member.orig_filename != member.filename for member in members):
            raise PackageVerificationError("archive has non-frozen or ambiguous entries")
        end = len(data) - 22 - len(archive.comment)
        if end < 0 or not data.startswith(b"PK\x03\x04"):
            raise PackageVerificationError("invalid ZIP envelope")
        signature, disk, central_disk, disk_count, count, size, offset, comment_size = (
            struct.unpack_from("<4s4H2IH", data, end)
        )
        if (
            signature != b"PK\x05\x06"
            or disk != 0
            or central_disk != 0
            or disk_count != count
            or count != len(members)
            or comment_size != len(archive.comment)
            or offset + size != end
            or min(member.header_offset for member in members) != 0
        ):
            raise PackageVerificationError("ZIP envelope is prefixed, trailing or unsupported")
        _check_local_records(data, members, offset)
    return _zip_files(data, limits)


def _check_local_records(data: bytes, members: list[zipfile.ZipInfo], central_start: int) -> None:
    # zipfile validates/decompresses indexed members, but ignores unindexed
    # local records. Require complete contiguous coverage before its readers.
    ordered = sorted(members, key=lambda member: member.header_offset)
    for index, member in enumerate(ordered):
        signature, _, flags, method, _, _, crc, compressed, size, name_size, extra_size = (
            struct.unpack_from("<4s5H3I2H", data, member.header_offset)
        )
        if (
            signature != b"PK\x03\x04"
            or flags != member.flag_bits
            or method != member.compress_type
        ):
            raise PackageVerificationError("ZIP local and central metadata disagree")
        values = (member.CRC, member.compress_size, member.file_size)
        if (crc, compressed, size) != values and not (
            flags & 8 and all(a in (0, b) for a, b in zip((crc, compressed, size), values))
        ):
            raise PackageVerificationError("ZIP local sizes are unsupported or inconsistent")
        body_end = member.header_offset + 30 + name_size + extra_size + member.compress_size
        next_start = ordered[index + 1].header_offset if index + 1 < len(ordered) else central_start
        if flags & 8:
            descriptor = data[body_end:next_start]
            if len(descriptor) == 16 and descriptor[:4] == b"PK\x07\x08":
                descriptor = descriptor[4:]
            if len(descriptor) != 12 or struct.unpack("<III", descriptor) != values:
                raise PackageVerificationError("ZIP descriptor or local record coverage is invalid")
        elif body_end != next_start:
            raise PackageVerificationError("ZIP contains unindexed or overlapping local bytes")


def _contract(raw: dict[str, object], files: tuple[SnapshotFile, ...]) -> PluginArchiveManifest:
    from agent_hub.api.routers.admin import (
        SUPPORTED_PLUGIN_PACKAGE_SDK_API_VERSIONS,
        PluginArchiveManifest,
        _validate_plugin_package_contract,
        _validate_plugin_package_metadata,
    )

    # Reuse the package schema/contract on already strictly parsed raw data. Do
    # not rebuild a STORED source ZIP: its headers are not frozen file bytes.
    if any(
        file.path.rsplit("/", 1)[-1] == "plugin.json" and file.path != "plugin.json"
        for file in files
    ):
        raise PackageVerificationError("package manifest member selection is ambiguous")
    _validate_plugin_package_metadata(raw)
    manifest = PluginArchiveManifest.model_validate(raw)
    package = manifest.package
    _validate_plugin_package_contract(package, archive_paths=frozenset(file.path for file in files))
    if (
        package is None
        or package.kind != "adapter_package"
        or package.entrypoint is None
        or package.sdk_api_version not in SUPPORTED_PLUGIN_PACKAGE_SDK_API_VERSIONS
        or package.package_version != manifest.version
    ):
        raise PackageVerificationError("a supported adapter package is required")
    _safe_path(package.entrypoint)
    return manifest


def verify_signed_snapshot_package(
    *,
    snapshot: FrozenSourceSnapshot,
    archive_bytes: bytes,
    key: FrozenSigningKey,
    now: datetime,
    limits: SourceLimits | None = None,
) -> VerifiedPackageFacts:
    """Verify bounded ZIP bytes against a frozen source, without any side effects.

    Only the three signature-envelope fields may be added/replaced. Original
    raw JSON values/presence/types and all non-manifest file bytes remain fixed.
    Provenance is checked structurally and returned, not independently granted.
    """
    try:
        return _verify(snapshot, archive_bytes, key, now, limits)
    except (
        ValueError,
        TypeError,
        AttributeError,
        UnicodeError,
        RecursionError,
        InvalidSignature,
        InvalidSource,
        InvalidSkillPackage,
        ValidationError,
        zipfile.BadZipFile,
        NotImplementedError,
        OSError,
        zlib.error,
        struct.error,
        EOFError,
    ):
        raise PackageVerificationError("signed snapshot package verification failed") from None


def _verify(
    snapshot: FrozenSourceSnapshot,
    data: bytes,
    key: FrozenSigningKey,
    now: datetime,
    limits: SourceLimits | None,
) -> VerifiedPackageFacts:
    # Revalidate even frozen dataclasses: object.__setattr__/deserialization and
    # model_copy are not proof that a caller supplied a valid internal value.
    if type(key) is not FrozenSigningKey:
        raise PackageVerificationError("a frozen current signing key is required")
    key = replace(key)
    current = _utc(now)
    if (key.not_before is not None and current < _utc(key.not_before)) or (
        key.not_after is not None and current >= _utc(key.not_after)
    ):
        raise PackageVerificationError("signing key is not currently active")
    if limits is not None and type(limits) is not SourceLimits:
        raise PackageVerificationError("invalid verification limits")
    checked_limits = SourceLimits() if limits is None else replace(limits)
    if type(data) is not bytes or not data or len(data) > checked_limits.max_archive_bytes:
        raise PackageVerificationError("archive is invalid or exceeds limit")
    snapshot, original = _checked_snapshot(snapshot, checked_limits)
    frozen_raw = _manifest(original.content)
    frozen_without_signature = _without_signature(frozen_raw, required=False)
    original_manifest = _contract(frozen_raw, snapshot.files)
    identity = (original_manifest.id, original_manifest.version)
    if (
        identity != (snapshot.plugin_id, snapshot.plugin_version)
        or (frozen_raw.get("id"), frozen_raw.get("version")) != identity
    ):
        raise PackageVerificationError("frozen package identity is inconsistent")

    # Source ZIP validation bounds every member before existing legacy package
    # helpers read it; do not give TAR/ZIP wrapper aliases to those helpers.
    files = _archive_files(data, checked_limits)
    manifests = [item for item in files if item.path == "plugin.json"]
    if len(manifests) != 1:
        raise PackageVerificationError("signed root manifest is required")
    raw = _manifest(manifests[0].content)
    if not _same_json(frozen_without_signature, _without_signature(raw, required=True)):
        raise PackageVerificationError("manifest differs from frozen source")
    actual = tuple((f.path, f.size_bytes, f.sha256) for f in files if f.path != "plugin.json")
    expected = tuple(
        (f.path, f.size_bytes, f.sha256) for f in snapshot.files if f.path != "plugin.json"
    )
    if actual != expected:
        raise PackageVerificationError("archive members differ from frozen source")
    manifest = _contract(raw, files)

    from agent_hub.api.routers.admin import (
        _decode_base64url_bytes,
        _plugin_signature_payload,
    )

    signature = manifest.package.signature if manifest.package is not None else None
    if signature is None or signature.key_id != key.key_id or signature.algorithm != key.algorithm:
        raise PackageVerificationError("signature does not match the current signing key")
    signature_bytes = _decode_base64url_bytes(
        signature.value, expected_length=64, label="signature"
    )
    payload = _plugin_signature_payload(manifest, data)
    Ed25519PublicKey.from_public_bytes(key.public_key).verify(signature_bytes, payload)
    return VerifiedPackageFacts(
        plugin_id=manifest.id,
        plugin_version=manifest.version,
        source_manifest_sha256=snapshot.authorized.source_manifest_sha256,
        frozen_manifest_sha256=original.sha256,
        archive_sha256=hashlib.sha256(data).hexdigest(),
        signature_payload_sha256=hashlib.sha256(payload).hexdigest(),
        key_id=key.key_id,
        public_key_sha256=hashlib.sha256(key.public_key).hexdigest(),
        verified_at=current,
        key_not_after=_utc(key.not_after) if key.not_after is not None else None,
        members=tuple(VerifiedMember(f.path, f.size_bytes, f.sha256) for f in files),
    )
