from __future__ import annotations

import base64
import hashlib
import importlib.util
import io
import json
import stat
import struct
import zipfile
from contextlib import nullcontext
from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Literal, cast
from uuid import UUID

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from agent_hub.api.routers.admin import (
    _plugin_archive_manifest_from_archive,
    _plugin_signature_payload,
)
from agent_hub.plugins.handover_sources import FrozenSourceSnapshot, SnapshotFile, SourceLimits
from agent_hub.plugins.handover_storage import AuthorizedSource, WorkspaceSource

if TYPE_CHECKING:
    from agent_hub.plugins.handover_verification import FrozenSigningKey, VerifiedPackageFacts


NOW = datetime(2026, 10, 12, tzinfo=UTC)
CODE = b"raise RuntimeError('generated code must not execute')\n"
MEMBERS = {"adapter.py": CODE, "data/raw.bin": b"\x00\xff\x01"}


def manifest() -> dict[str, object]:
    return {
        "id": "example",
        "name": "Example",
        "version": "1.0.0",
        "enabled": False,
        "resource_config": {"allow": True, "count": 1, "sequence": [1, 2]},
        "package": {
            "kind": "adapter_package",
            "package_version": "1.0.0",
            "adapter_id": "example",
            "sdk_api_version": "1.0",
            "runtime": "python",
            "entrypoint": "adapter.py",
            "isolation": "local_process",
            "install_mode": "scan_only",
        },
    }


def encoded(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()


def archive(raw: bytes, members: dict[str, bytes] | None = None) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zipped:
        zipped.writestr("plugin.json", raw)
        for path, content in (MEMBERS if members is None else members).items():
            zipped.writestr(path, content)
    return buffer.getvalue()


def frozen(
    raw: bytes | None = None, members: dict[str, bytes] | None = None
) -> FrozenSourceSnapshot:
    selected = dict(MEMBERS if members is None else members)
    selected["plugin.json"] = encoded(manifest()) if raw is None else raw
    files = tuple(
        SnapshotFile(path, content, len(content), hashlib.sha256(content).hexdigest())
        for path, content in sorted(selected.items())
    )
    return FrozenSourceSnapshot(
        authorized=AuthorizedSource(
            tenant_id=UUID("11111111-1111-4111-8111-111111111111"),
            run_id=UUID("22222222-2222-4222-8222-222222222222"),
            source=WorkspaceSource(
                project_id="project-a", session_id="session-a", paths=tuple(f.path for f in files)
            ),
            source_manifest_sha256="a" * 64,
        ),
        files=files,
        plugin_id="example",
        plugin_version="1.0.0",
        total_size_bytes=sum(f.size_bytes for f in files),
    )


def signed(
    private: Ed25519PrivateKey,
    value: dict[str, object] | None = None,
    members: dict[str, bytes] | None = None,
    key_id: str = "tenant-key",
) -> bytes:
    value = manifest() if value is None else value
    package = cast(dict[str, object], value["package"])
    signature: dict[str, object] = {"algorithm": "ed25519", "key_id": key_id, "value": "A" * 86}
    package["signature"] = signature
    unsigned = archive(encoded(value), members)
    payload = _plugin_signature_payload(_plugin_archive_manifest_from_archive(unsigned), unsigned)
    signature["value"] = base64.urlsafe_b64encode(private.sign(payload)).rstrip(b"=").decode()
    return archive(encoded(value), members)


def test_real_ed25519_matches_existing_protocol_and_exact_snapshot() -> None:
    assert importlib.util.find_spec("agent_hub.plugins.handover_verification") is not None
    from agent_hub.plugins.handover_verification import (
        FrozenSigningKey,
        verify_signed_snapshot_package,
    )

    private = Ed25519PrivateKey.generate()
    data = signed(private)
    snapshot = frozen()
    key = FrozenSigningKey(
        key_id="tenant-key",
        algorithm="ed25519",
        public_key=private.public_key().public_bytes_raw(),
        trusted=True,
    )
    facts = verify_signed_snapshot_package(snapshot=snapshot, archive_bytes=data, key=key, now=NOW)
    payload = _plugin_signature_payload(_plugin_archive_manifest_from_archive(data), data)
    assert facts.plugin_id == "example" and facts.plugin_version == "1.0.0"
    assert facts.archive_sha256 == hashlib.sha256(data).hexdigest()
    assert facts.signature_payload_sha256 == hashlib.sha256(payload).hexdigest()
    assert facts.public_key_sha256 == hashlib.sha256(key.public_key).hexdigest()
    assert facts.source_manifest_sha256 == snapshot.authorized.source_manifest_sha256
    assert facts.verified_at == NOW and facts.key_id == "tenant-key"
    assert tuple(item.path for item in facts.members) == (
        "adapter.py",
        "data/raw.bin",
        "plugin.json",
    )
    assert not hasattr(facts, "grant") and not hasattr(facts, "principal")


def signing_key(private: Ed25519PrivateKey, **changes: object) -> FrozenSigningKey:
    from agent_hub.plugins.handover_verification import FrozenSigningKey

    values: dict[str, object] = {
        "key_id": "tenant-key",
        "algorithm": "ed25519",
        "public_key": private.public_key().public_bytes_raw(),
        "trusted": True,
    }
    values.update(changes)
    return FrozenSigningKey(
        key_id=cast(str, values["key_id"]),
        algorithm=cast(Literal["ed25519"], values["algorithm"]),
        public_key=cast(bytes, values["public_key"]),
        trusted=cast(Literal[True], values["trusted"]),
        not_before=cast(datetime | None, values.get("not_before")),
        not_after=cast(datetime | None, values.get("not_after")),
    )


def verify(
    private: Ed25519PrivateKey,
    data: bytes,
    snapshot: FrozenSourceSnapshot | None = None,
    key: FrozenSigningKey | None = None,
    now: datetime = NOW,
    limits: SourceLimits | None = None,
) -> VerifiedPackageFacts:
    from agent_hub.plugins.handover_verification import verify_signed_snapshot_package

    return verify_signed_snapshot_package(
        snapshot=frozen() if snapshot is None else snapshot,
        archive_bytes=data,
        key=signing_key(private) if key is None else key,
        now=now,
        limits=limits,
    )


@pytest.mark.parametrize(
    "mutation",
    [
        "name",
        "version",
        "enabled",
        "bool_integer",
        "integer_float",
        "list_order",
        "missing_default",
        "added_default",
        "entrypoint",
        "permissions",
        "dependencies",
    ],
)
def test_valid_signature_cannot_authorize_manifest_changes(mutation: str) -> None:
    from agent_hub.plugins.handover_verification import PackageVerificationError

    private = Ed25519PrivateKey.generate()
    value = manifest()
    config = cast(dict[str, object], value["resource_config"])
    package = cast(dict[str, object], value["package"])
    if mutation == "name":
        value["name"] = "Changed"
    elif mutation == "version":
        value["version"] = "2.0.0"
        package["package_version"] = "2.0.0"
    elif mutation == "enabled":
        value["enabled"] = True
    elif mutation == "bool_integer":
        config["allow"] = 1
    elif mutation == "integer_float":
        config["count"] = 1.0
    elif mutation == "list_order":
        config["sequence"] = [2, 1]
    elif mutation == "missing_default":
        del value["enabled"]
    elif mutation == "added_default":
        value["description"] = ""
    elif mutation == "entrypoint":
        package["entrypoint"] = "data/raw.bin"
    elif mutation == "permissions":
        config["permissions"] = ["dangerous"]
    else:
        package["dependencies"] = []
    with pytest.raises(PackageVerificationError):
        verify(private, signed(private, value))


@pytest.mark.parametrize("mutation", ["changed_same_size", "missing", "extra", "renamed"])
def test_valid_signature_cannot_authorize_member_changes(mutation: str) -> None:
    from agent_hub.plugins.handover_verification import PackageVerificationError

    private = Ed25519PrivateKey.generate()
    members = dict(MEMBERS)
    if mutation == "changed_same_size":
        members["data/raw.bin"] = b"\x00\xfe\x01"
    elif mutation == "missing":
        del members["data/raw.bin"]
    elif mutation == "extra":
        members["hidden.bin"] = b"extra"
    else:
        members["data/renamed.bin"] = members.pop("data/raw.bin")
    with pytest.raises(PackageVerificationError):
        verify(private, signed(private, members=members))


def test_only_signature_fields_and_json_formatting_can_change() -> None:
    private = Ed25519PrivateKey.generate()
    original = manifest()
    package = cast(dict[str, object], original["package"])
    package["signature"] = {"algorithm": "ed25519", "key_id": "old-key", "value": "A" * 86}
    snapshot = frozen(json.dumps(original, indent=2).encode())
    assert verify(private, signed(private), snapshot).key_id == "tenant-key"


@pytest.mark.parametrize("bad", [False, 1, "true", None])
def test_key_trust_requires_literal_true(bad: object) -> None:
    from agent_hub.plugins.handover_verification import PackageVerificationError

    with pytest.raises(PackageVerificationError):
        signing_key(Ed25519PrivateKey.generate(), trusted=bad)


@pytest.mark.parametrize(
    "bad",
    [
        {"key_id": "unknown"},
        {"algorithm": "rsa"},
        {"public_key": b"short"},
        {"public_key": "A" * 32},
        {"key_id": "bad/key"},
        {"not_before": NOW.replace(tzinfo=None)},
        {"not_before": NOW, "not_after": NOW},
    ],
)
def test_frozen_key_dto_is_strict(bad: dict[str, object]) -> None:
    from agent_hub.plugins.handover_verification import PackageVerificationError

    private = Ed25519PrivateKey.generate()
    with pytest.raises(PackageVerificationError):
        key = signing_key(private, **bad)
        verify(private, signed(private), key=key)


@pytest.mark.parametrize("mutation", ["trust", "id", "public_key", "algorithm", "window"])
def test_forged_frozen_key_is_revalidated(mutation: str) -> None:
    from agent_hub.plugins.handover_verification import PackageVerificationError

    private = Ed25519PrivateKey.generate()
    key = signing_key(private)
    name, value = {
        "trust": ("trusted", 1),
        "id": ("key_id", "unknown"),
        "public_key": ("public_key", Ed25519PrivateKey.generate().public_key().public_bytes_raw()),
        "algorithm": ("algorithm", "rsa"),
        "window": ("not_after", NOW.replace(tzinfo=None)),
    }[mutation]
    object.__setattr__(key, name, value)
    with pytest.raises(PackageVerificationError):
        verify(private, signed(private), key=key)


@pytest.mark.parametrize("delta,success", [(-1, False), (0, True), (59, True), (60, False)])
def test_key_window_uses_one_caller_clock(delta: int, success: bool) -> None:
    from agent_hub.plugins.handover_verification import PackageVerificationError

    private = Ed25519PrivateKey.generate()
    key = signing_key(private, not_before=NOW, not_after=NOW + timedelta(seconds=60))
    data = signed(private)
    clock = NOW + timedelta(seconds=delta)
    if success:
        facts = verify(private, data, key=key, now=clock)
        assert facts.verified_at == clock and facts.key_not_after == NOW + timedelta(seconds=60)
    else:
        with pytest.raises(PackageVerificationError):
            verify(private, data, key=key, now=clock)


def test_timezone_offsets_are_normalized_and_naive_now_is_rejected() -> None:
    from agent_hub.plugins.handover_verification import PackageVerificationError

    private = Ed25519PrivateKey.generate()
    data = signed(private)
    offset = NOW.astimezone(timezone(timedelta(hours=8)))
    assert verify(private, data, now=offset).verified_at == NOW
    with pytest.raises(PackageVerificationError):
        verify(private, data, now=NOW.replace(tzinfo=None))


@pytest.mark.parametrize(
    "mutation", ["unsigned", "null", "extra", "algorithm", "invalid", "wrong_key"]
)
def test_unsigned_unknown_envelope_and_invalid_real_crypto_fail_closed(mutation: str) -> None:
    from agent_hub.plugins.handover_verification import PackageVerificationError

    private = Ed25519PrivateKey.generate()
    data = signed(private)
    with zipfile.ZipFile(io.BytesIO(data)) as zipped:
        value = cast(dict[str, object], json.loads(zipped.read("plugin.json")))
    package = cast(dict[str, object], value["package"])
    signature = cast(dict[str, object], package["signature"])
    if mutation == "unsigned":
        del package["signature"]
    elif mutation == "null":
        package["signature"] = None
    elif mutation == "extra":
        signature["proof"] = "not-authority"
    elif mutation == "algorithm":
        signature["algorithm"] = "rsa"
    elif mutation == "invalid":
        signature["value"] = "A" * 86
    key = signing_key(Ed25519PrivateKey.generate() if mutation == "wrong_key" else private)
    with pytest.raises(PackageVerificationError):
        verify(private, archive(encoded(value)), key=key)


@pytest.mark.parametrize("side", ["snapshot", "archive"])
@pytest.mark.parametrize(
    "mutation",
    [
        "duplicate",
        "escaped_duplicate",
        "nested_duplicate",
        "nan",
        "infinity",
        "overflow",
        "deep",
        "nodes",
        "invalid_utf8",
        "surrogate",
        "not_object",
    ],
)
def test_raw_manifest_is_strict_before_legacy_dto_parsing(side: str, mutation: str) -> None:
    from agent_hub.plugins.handover_verification import PackageVerificationError

    private = Ed25519PrivateKey.generate()
    data = signed(private)
    with zipfile.ZipFile(io.BytesIO(data)) as zipped:
        raw = zipped.read("plugin.json")
    bad = {
        "duplicate": raw.replace(b'"id":"example"', b'"id":"evil","id":"example"'),
        "escaped_duplicate": raw.replace(b'"id":"example"', b'"\\u0069d":"evil","id":"example"'),
        "nested_duplicate": raw.replace(b'"allow":true', b'"allow":false,"allow":true'),
        "nan": raw.replace(b'"count":1', b'"count":NaN'),
        "infinity": raw.replace(b'"count":1', b'"count":Infinity'),
        "overflow": raw.replace(b'"count":1', b'"count":1e999'),
        "deep": raw.replace(b'"count":1', b'"count":' + b"[" * 65 + b"0" + b"]" * 65),
        "nodes": raw.replace(b'"count":1', b'"count":[' + b"0," * 20_001 + b"0]"),
        "invalid_utf8": b"\xff",
        "surrogate": raw.replace(b'"name":"Example"', b'"name":"\\ud800"'),
        "not_object": b"[]",
    }[mutation]
    with pytest.raises(PackageVerificationError) as caught:
        verify(
            private,
            data if side == "snapshot" else archive(bad),
            snapshot=frozen(bad) if side == "snapshot" else frozen(),
        )
    assert str(caught.value) == "signed snapshot package verification failed"


@pytest.mark.parametrize(
    "mutation",
    [
        "bytes",
        "sha",
        "size",
        "total",
        "id",
        "version",
        "selection",
        "source_hash",
        "unsorted",
    ],
)
def test_untrusted_snapshot_dto_cannot_supply_false_file_or_provenance_facts(mutation: str) -> None:
    from agent_hub.plugins.handover_verification import PackageVerificationError

    private = Ed25519PrivateKey.generate()
    snapshot = frozen()
    first = snapshot.files[0]
    if mutation in {"bytes", "sha", "size"}:
        if mutation == "bytes":
            first = replace(first, content=b"changed")
        elif mutation == "sha":
            first = replace(first, sha256="b" * 64)
        else:
            first = replace(first, size_bytes=True)
        snapshot = replace(snapshot, files=(first, *snapshot.files[1:]))
    elif mutation == "total":
        snapshot = replace(snapshot, total_size_bytes=snapshot.total_size_bytes + 1)
    elif mutation == "id":
        snapshot = replace(snapshot, plugin_id="changed")
    elif mutation == "version":
        snapshot = replace(snapshot, plugin_version="2.0.0")
    elif mutation == "unsorted":
        snapshot = replace(snapshot, files=tuple(reversed(snapshot.files)))
    elif mutation == "selection":
        source = WorkspaceSource(
            project_id="project-a", session_id="session-a", paths=("plugin.json",)
        )
        snapshot = replace(
            snapshot, authorized=snapshot.authorized.model_copy(update={"source": source})
        )
    else:
        snapshot = replace(
            snapshot,
            authorized=snapshot.authorized.model_copy(update={"source_manifest_sha256": "bad"}),
        )
    with pytest.raises(PackageVerificationError):
        verify(private, signed(private), snapshot)


@pytest.mark.parametrize(
    "limit",
    [
        SourceLimits(max_archive_bytes=1),
        SourceLimits(max_files=2),
        SourceLimits(max_file_bytes=2),
        SourceLimits(max_total_bytes=2),
    ],
)
def test_archive_and_snapshot_safety_limits(limit: SourceLimits) -> None:
    from agent_hub.plugins.handover_verification import PackageVerificationError

    private = Ed25519PrivateKey.generate()
    with pytest.raises(PackageVerificationError):
        verify(private, signed(private), limits=limit)


@pytest.mark.parametrize(
    "mutation",
    [
        "symlink",
        "device",
        "duplicate",
        "case_alias",
        "backslash",
        "percent",
        "escape",
        "absolute",
        "windows_reserved",
        "nested_manifest",
        "directory_extra",
        "dir_file_alias",
        "dir_case_alias",
        "nul_name",
        "encrypted",
        "bzip2",
        "ratio",
        "prefix",
        "trailing",
        "broken",
    ],
)
def test_zip_container_cannot_hide_unsafe_or_unmatched_members(mutation: str) -> None:
    from agent_hub.plugins.handover_verification import PackageVerificationError

    private = Ed25519PrivateKey.generate()
    data = signed(private)
    with zipfile.ZipFile(io.BytesIO(data)) as zipped:
        raw = zipped.read("plugin.json")
    buffer = io.BytesIO()
    method = zipfile.ZIP_BZIP2 if mutation == "bzip2" else zipfile.ZIP_DEFLATED
    with zipfile.ZipFile(buffer, "w", method) as zipped:
        zipped.writestr("plugin.json", raw)
        for path, content in MEMBERS.items():
            if path == "adapter.py" and mutation in {"symlink", "device"}:
                info = zipfile.ZipInfo(path)
                info.create_system = 3
                info.external_attr = (
                    (stat.S_IFLNK if mutation == "symlink" else stat.S_IFCHR) | 0o600
                ) << 16
                zipped.writestr(info, content)
            elif path == "adapter.py" and mutation == "nul_name":
                zipped.writestr("adapter.pyX", content)
            else:
                zipped.writestr(path, content)
        extra = {
            "duplicate": "adapter.py",
            "case_alias": "ADAPTER.py",
            "backslash": "x\\y",
            "percent": "x%2fy",
            "escape": "../evil",
            "absolute": "/evil",
            "windows_reserved": "CON",
            "nested_manifest": "nested/plugin.json",
            "directory_extra": "hidden/",
            "ratio": "ratio.bin",
            "dir_file_alias": "adapter.py/",
            "dir_case_alias": "ADAPTER.py/",
        }.get(mutation)
        if extra is not None:
            with pytest.warns(UserWarning) if mutation == "duplicate" else nullcontext():
                zipped.writestr(extra, b"x" * 100_000 if mutation == "ratio" else b"x")
    data = buffer.getvalue()
    if mutation == "nul_name":
        data = data.replace(b"adapter.pyX", b"adapter.py\x00")
    elif mutation == "encrypted":
        mutable = bytearray(data)
        local = mutable.index(b"PK\x03\x04")
        central = mutable.index(b"PK\x01\x02")
        for offset in (local + 6, central + 8):
            flags = struct.unpack_from("<H", mutable, offset)[0]
            struct.pack_into("<H", mutable, offset, flags | 1)
        data = bytes(mutable)
    elif mutation == "prefix":
        data = b"hidden prefix" + data
    elif mutation == "trailing":
        data += b"hidden trailer"
    elif mutation == "broken":
        data = b"not a zip"
    with pytest.raises(PackageVerificationError):
        verify(private, data)


def test_facts_and_members_are_immutable() -> None:
    private = Ed25519PrivateKey.generate()
    facts = verify(private, signed(private))
    with pytest.raises(FrozenInstanceError):
        facts.key_id = "changed"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        facts.members[0].sha256 = "b" * 64  # type: ignore[misc]


def test_verification_never_reads_paths_or_executes_generated_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import builtins
    import socket
    import subprocess

    from agent_hub.api.routers import admin
    from agent_hub.plugins import package_builder
    from agent_hub.plugins.handover_verification import verify_signed_snapshot_package

    private = Ed25519PrivateKey.generate()
    data = signed(private)
    snapshot = frozen()

    def forbidden(*args: object, **kwargs: object) -> object:
        raise AssertionError("verification performed a forbidden side effect")

    monkeypatch.setattr(Path, "open", forbidden)
    monkeypatch.setattr(builtins, "open", forbidden)
    monkeypatch.setattr(builtins, "exec", forbidden)
    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(package_builder, "build_signed_plugin_archive", forbidden)
    monkeypatch.setattr(admin, "_stored_plugin_package_metadata", forbidden)
    facts = verify_signed_snapshot_package(
        snapshot=snapshot, archive_bytes=data, key=signing_key(private), now=NOW
    )
    assert facts.members[0].sha256 == hashlib.sha256(CODE).hexdigest()


def test_bounded_compressed_package_does_not_depend_on_synthetic_stored_zip_size() -> None:
    private = Ed25519PrivateKey.generate()
    block = b"".join(hashlib.sha256(str(i).encode()).digest() for i in range(512))
    members = {"adapter.py": CODE, **{f"data/{i}.bin": (block * 31)[:499_790] for i in range(4)}}
    snapshot = frozen(members=members)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_STORED) as zipped:
        for file in snapshot.files:
            zipped.writestr(file.path, file.content)
    data = signed(private, members=members)
    assert snapshot.total_size_bytes < 2_000_000 < len(buffer.getvalue())
    with zipfile.ZipFile(io.BytesIO(data)) as zipped:
        assert sum(item.file_size for item in zipped.infolist()) <= 2_000_000
    assert verify(private, data, snapshot).archive_sha256 == hashlib.sha256(data).hexdigest()


def test_json_float_sign_is_preserved_outside_signature_envelope() -> None:
    from agent_hub.plugins.handover_verification import PackageVerificationError

    private = Ed25519PrivateKey.generate()
    original = manifest()
    cast(dict[str, object], original["resource_config"])["count"] = 0.0
    candidate = manifest()
    cast(dict[str, object], candidate["resource_config"])["count"] = -0.0
    with pytest.raises(PackageVerificationError):
        verify(private, signed(private, candidate), frozen(encoded(original)))


def test_snapshot_member_hash_cannot_be_a_caller_equality_hook() -> None:
    from agent_hub.plugins.handover_verification import PackageVerificationError

    class ForgedDigest(str):
        def __eq__(self, other: object) -> bool:
            return True

        def __ne__(self, other: object) -> bool:
            return False

    private = Ed25519PrivateKey.generate()
    snapshot = frozen()
    original = snapshot.files[-1]
    snapshot = replace(
        snapshot, files=(*snapshot.files[:-1], replace(original, sha256=ForgedDigest("bad")))
    )
    with pytest.raises(PackageVerificationError):
        verify(private, signed(private), snapshot)


@pytest.mark.parametrize("mutation", ["json", "member", "directory", "unsigned"])
def test_legacy_signature_reader_is_not_called_before_strict_checks(
    monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    from agent_hub.api.routers import admin
    from agent_hub.plugins.handover_verification import PackageVerificationError

    private = Ed25519PrivateKey.generate()
    data = signed(private)
    with zipfile.ZipFile(io.BytesIO(data)) as zipped:
        raw = zipped.read("plugin.json")
    members = dict(MEMBERS)
    if mutation == "json":
        raw = raw.replace(b'"id":"example"', b'"id":"evil","id":"example"')
    elif mutation == "member":
        members["data/raw.bin"] = b"\x00\xfe\x01"
    elif mutation == "directory":
        members["extra/"] = b""
    else:
        raw = encoded(manifest())
    data = archive(raw, members)

    def forbidden(*args: object, **kwargs: object) -> object:
        raise AssertionError("legacy signature reader ran before strict validation")

    monkeypatch.setattr(admin, "_plugin_archive_signature_files", forbidden)
    with pytest.raises(PackageVerificationError):
        verify(private, data)


@pytest.mark.parametrize("mutation", ["archive_type", "key_type", "snapshot_type", "limits"])
def test_boundary_rejects_wrong_internal_types_and_forged_limits(mutation: str) -> None:
    from agent_hub.plugins.handover_verification import (
        FrozenSigningKey,
        PackageVerificationError,
        verify_signed_snapshot_package,
    )

    private = Ed25519PrivateKey.generate()
    data = signed(private)
    limits = SourceLimits()
    if mutation == "limits":
        object.__setattr__(limits, "max_total_bytes", 2_000_001)
    with pytest.raises(PackageVerificationError):
        verify_signed_snapshot_package(
            snapshot=cast(FrozenSourceSnapshot, {}) if mutation == "snapshot_type" else frozen(),
            archive_bytes=cast(bytes, bytearray(data)) if mutation == "archive_type" else data,
            key=cast(FrozenSigningKey, {}) if mutation == "key_type" else signing_key(private),
            now=NOW,
            limits=limits,
        )


def test_facts_use_detached_validated_snapshot_not_later_caller_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agent_hub.api.routers import admin
    from agent_hub.api.routers.admin import PluginArchiveManifest

    private = Ed25519PrivateKey.generate()
    data = signed(private)
    snapshot = frozen()
    payload = admin._plugin_signature_payload

    def during_payload(manifest: PluginArchiveManifest, archive_bytes: bytes) -> bytes:
        object.__setattr__(snapshot.authorized, "source_manifest_sha256", "b" * 64)
        return payload(manifest, archive_bytes)

    monkeypatch.setattr(admin, "_plugin_signature_payload", during_payload)
    assert verify(private, data, snapshot).source_manifest_sha256 == "a" * 64


def test_zip_unindexed_local_member_is_not_snapshot_equivalent() -> None:
    from agent_hub.plugins.handover_verification import PackageVerificationError

    private = Ed25519PrivateKey.generate()
    data = signed(private)
    with zipfile.ZipFile(io.BytesIO(data)) as zipped:
        raw = zipped.read("plugin.json")
    data = archive(raw, {**MEMBERS, "hidden.bin": b"not frozen"})
    end = len(data) - 22
    central_size, central_start = struct.unpack_from("<II", data, end + 12)
    records: list[bytes] = []
    cursor = central_start
    while cursor < end:
        name_size, extra_size, comment_size = struct.unpack_from("<HHH", data, cursor + 28)
        record_end = cursor + 46 + name_size + extra_size + comment_size
        name = data[cursor + 46 : cursor + 46 + name_size]
        if name != b"hidden.bin":
            records.append(data[cursor:record_end])
        cursor = record_end
    central = b"".join(records)
    assert len(central) < central_size
    trailer = bytearray(data[end:])
    struct.pack_into("<HHI", trailer, 8, len(records), len(records), len(central))
    data = data[:central_start] + central + bytes(trailer)
    # The ordinary ZIP reader ignores the real, still-present local member.
    with zipfile.ZipFile(io.BytesIO(data)) as zipped:
        assert set(zipped.namelist()) == {"plugin.json", *MEMBERS}
        assert all(zipped.read(name) for name in zipped.namelist())
    with pytest.raises(PackageVerificationError):
        verify(private, data)


def test_actual_streaming_zip_descriptors_preserve_protocol_compatibility() -> None:
    class StreamingBuffer(io.BytesIO):
        def seek(self, offset: int, whence: int = 0) -> int:
            raise io.UnsupportedOperation("stream is not seekable")

    private = Ed25519PrivateKey.generate()
    data = signed(private)
    with zipfile.ZipFile(io.BytesIO(data)) as zipped:
        raw = zipped.read("plugin.json")
    buffer = StreamingBuffer()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zipped:
        zipped.writestr("plugin.json", raw)
        for path, content in MEMBERS.items():
            zipped.writestr(path, content)
    data = buffer.getvalue()
    with zipfile.ZipFile(io.BytesIO(data)) as zipped:
        assert all(member.flag_bits & 8 for member in zipped.infolist())
    assert verify(private, data).archive_sha256 == hashlib.sha256(data).hexdigest()
