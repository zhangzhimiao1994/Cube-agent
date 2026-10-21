"""Archive fixtures exercise validation only; they are not runnable SDKs."""

import gzip
import hashlib
import json
import tarfile
import time
from dataclasses import FrozenInstanceError, replace

import pytest

from agent_hub.skills.sandbox.toolchain_archive import (
    ArchiveLimits,
    GoArchiveFile,
    GoArchiveManifest,
    ToolchainArchiveError,
    inspect_go_archive,
)

LIMITS = ArchiveLimits(1_000_000, 2_000_000, 100)


def header(
    name: str,
    size: int = 0,
    kind: bytes = tarfile.REGTYPE,
    mode: int = 0o644,
    link: str = "",
) -> bytes:
    info = tarfile.TarInfo(name)
    info.size = size
    info.type = kind
    info.mode = mode
    info.linkname = link
    return info.tobuf(format=tarfile.USTAR_FORMAT)


def entry(name: str, data: bytes = b"", mode: int = 0o644) -> bytes:
    return header(name, len(data), mode=mode) + data + bytes((-len(data)) % 512)


def base() -> bytes:
    return (
        header("go/", kind=tarfile.DIRTYPE, mode=0o755)
        + entry("go/VERSION", b"go1.27.3\ntime 2026-10-01T00:00:00Z\n")
        + entry("go/bin/gofmt", b"fixture-not-executable-code", 0o755)
        + entry("go/bin/go", b"fixture-not-executable-code", 0o755)
    )


def packed(raw: bytes | None = None) -> bytes:
    return gzip.compress((base() if raw is None else raw) + bytes(1024), mtime=0)


def pax_path(path: str) -> bytes:
    suffix = (" path=" + path + "\n").encode("utf-8")
    length = len(suffix) + 1
    while length != len(str(length)) + len(suffix):
        length = len(str(length)) + len(suffix)
    return str(length).encode() + suffix


def pax_member(payload: bytes, *, member: bytes | None = None) -> bytes:
    return (header("PaxHeaders/placeholder", len(payload), kind=tarfile.XHDTYPE)
            + payload + bytes((-len(payload)) % 512)
            + (entry("go/placeholder.go", b"package main\n") if member is None else member))


def test_official_go_unicode_path_uses_bounded_local_pax() -> None:
    path = "go/test/fixedbugs/issue27836.dir/\u00defoo.go"
    manifest = inspect(packed(base() + pax_member(pax_path(path))))
    member = next(f for f in manifest.files if f.path == path)
    assert member.sha256 == hashlib.sha256(b"package main\n").hexdigest()
    assert "go/placeholder.go" not in {f.path for f in manifest.files}


@pytest.mark.parametrize("payload", [
    b"", b"20 path=go/a.go\n", b"019 path=go/a.go\n", b"20 linkpath=/etc/x\n",
    pax_path("../outside"), pax_path("/absolute"), pax_path("go/../outside"),
    pax_path("go/a.go") + pax_path("go/b.go"), b"9 size=0\n",
    pax_path("go/a.go\nbad"), pax_path("go/a\x00.go"),
])
def test_local_pax_has_one_exact_safe_path_and_no_other_overrides(payload: bytes) -> None:
    with pytest.raises(ToolchainArchiveError):
        inspect(packed(base() + pax_member(payload)))


def test_pax_is_not_global_recursive_or_dangling() -> None:
    value = pax_path("go/a.go")
    local = pax_member(value, member=b"")
    for extra in (local, local + local + entry("go/a.go", b""),
                  pax_member(value, member=header("go/a.go", kind=tarfile.DIRTYPE))):
        with pytest.raises(ToolchainArchiveError):
            inspect(packed(base() + extra))


def test_empty_and_implicit_directories_are_bound_and_bounded() -> None:
    plain = inspect(packed())
    value = inspect(packed(base() + header("go/empty/", kind=tarfile.DIRTYPE)))
    assert "go/empty" in value.directories
    assert value.tree_sha256 != plain.tree_sha256
    deep = base() + entry("go/a/b/c/d/e/source.go", b"package a\n")
    with pytest.raises(ToolchainArchiveError, match="directory_limit"):
        inspect(packed(deep), replace(LIMITS, max_directories=4))


def test_archive_deadline_interrupts_validation_and_padding(monkeypatch: pytest.MonkeyPatch) -> None:
    from agent_hub.skills.sandbox import toolchain_archive

    raw = packed(base() + bytes(2 * 1024**2))
    calls = 0

    def clock() -> float:
        nonlocal calls
        calls += 1
        return calls / 1000

    monkeypatch.setattr(time, "monotonic", clock)
    with pytest.raises(ToolchainArchiveError, match="archive_timeout"):
        toolchain_archive.inspect_go_archive(raw, expected_sha256=hashlib.sha256(raw).hexdigest(),
            expected_size_bytes=len(raw), limits=replace(LIMITS, max_expanded_bytes=4 * 1024**2),
            deadline=0.01)
    assert calls < 50


def inspect(data: bytes, limits: ArchiveLimits = LIMITS) -> GoArchiveManifest:
    return inspect_go_archive(
        data,
        expected_sha256=hashlib.sha256(data).hexdigest(),
        expected_size_bytes=len(data),
        limits=limits,
    )


def reject(data: bytes, code: str, limits: ArchiveLimits = LIMITS) -> None:
    with pytest.raises(ToolchainArchiveError) as error:
        inspect(data, limits)
    assert error.value.code == code
    assert str(error.value) == code
    assert error.value.__cause__ is None


def test_manifest_binds_sorted_paths_sizes_hashes_and_modes() -> None:
    data = packed(base() + entry("go/src/extra.txt", b"hello"))
    manifest = inspect(data)
    assert manifest.archive_sha256 == hashlib.sha256(data).hexdigest()
    assert manifest.archive_size_bytes == len(data)
    assert isinstance(manifest.files, tuple)
    assert [file.path for file in manifest.files] == [
        "go/VERSION",
        "go/bin/go",
        "go/bin/gofmt",
        "go/src/extra.txt",
    ]
    assert manifest.files[-1] == GoArchiveFile(
        "go/src/extra.txt", 5, hashlib.sha256(b"hello").hexdigest(), False
    )
    assert manifest.expanded_bytes == sum(file.size_bytes for file in manifest.files)
    encoded = json.dumps(
        {"directories": list(manifest.directories), "files": [
            {
                "path": file.path,
                "size_bytes": file.size_bytes,
                "sha256": file.sha256,
                "executable": file.executable,
            }
            for file in manifest.files
        ]},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("ascii")
    assert manifest.tree_sha256 == hashlib.sha256(encoded).hexdigest()
    with pytest.raises(FrozenInstanceError):
        manifest.expanded_bytes = 0  # type: ignore[misc]
    assert not hasattr(manifest, "__dict__")
    assert not hasattr(manifest.files[0], "__dict__")
    assert not hasattr(LIMITS, "__dict__")


def test_tree_hash_ignores_archive_order_but_not_file_mode_or_content() -> None:
    original = inspect(packed())
    reordered = inspect(
        packed(
            entry("go/bin/go", b"fixture-not-executable-code", 0o755)
            + entry("go/bin/gofmt", b"fixture-not-executable-code", 0o755)
            + entry("go/VERSION", b"go1.27.3\ntime 2026-10-01T00:00:00Z\n")
        )
    )
    assert original.tree_sha256 == reordered.tree_sha256
    assert original.archive_sha256 != reordered.archive_sha256
    assert original.tree_sha256 != inspect(packed(base() + entry("go/extra", b"x"))).tree_sha256
    assert (
        inspect(packed(base() + entry("go/extra", b"x"))).tree_sha256
        != inspect(packed(base() + entry("go/extra", b"x", 0o755))).tree_sha256
    )


@pytest.mark.parametrize("digest", ["0" * 64, "not-a-digest", "A" * 64, "a" * 63])
def test_rejects_unpinned_digest(digest: str) -> None:
    data = packed()
    with pytest.raises(ToolchainArchiveError, match="^archive_identity$"):
        inspect_go_archive(
            data, expected_sha256=digest, expected_size_bytes=len(data), limits=LIMITS
        )


def test_fixed_size_and_archive_byte_limit() -> None:
    data = packed()
    with pytest.raises(ToolchainArchiveError, match="^archive_identity$"):
        inspect_go_archive(
            data,
            expected_sha256=hashlib.sha256(data).hexdigest(),
            expected_size_bytes=len(data) + 1,
            limits=LIMITS,
        )
    reject(data, "archive_limit", replace(LIMITS, max_archive_bytes=len(data) - 1))
    assert inspect(data, replace(LIMITS, max_archive_bytes=len(data)))


@pytest.mark.parametrize(
    "field",
    ["max_archive_bytes", "max_expanded_bytes", "max_entries", "max_path_bytes", "max_depth"],
)
@pytest.mark.parametrize("value", [0, -1, True])
def test_limits_are_positive_integers(field: str, value: int) -> None:
    with pytest.raises(ToolchainArchiveError, match="^invalid_limits$"):
        inspect(packed(), replace(LIMITS, **{field: value}))


def test_expansion_limit_includes_headers_padding_and_end_markers() -> None:
    raw = base() + bytes(1024)
    data = gzip.compress(raw)
    assert inspect(data, replace(LIMITS, max_expanded_bytes=len(raw)))
    reject(data, "expanded_limit", replace(LIMITS, max_expanded_bytes=len(raw) - 1))
    reject(
        gzip.compress(raw + bytes(200_000)),
        "expanded_limit",
        replace(LIMITS, max_expanded_bytes=len(raw)),
    )


def test_entry_budget_counts_directories_too() -> None:
    assert inspect(packed(), replace(LIMITS, max_entries=4))
    reject(packed(), "entry_limit", replace(LIMITS, max_entries=3))


@pytest.mark.parametrize(
    "path",
    [
        "/go/evil",
        "../go/evil",
        "go/../evil",
        "go/./evil",
        "go//evil",
        "other/evil",
        "go\\evil",
        "go/evil\n",
        "go/evil\t",
        "go/evil\x7f",
        "go/evil\u0085",
        "go/evil\u200b",
        "go/e\u0301",
        "go/evil/",
        "go",
        "go/a/../../escape",
    ],
)
def test_noncanonical_paths_are_rejected(path: str) -> None:
    reject(packed(base() + entry(path)), "unsafe_path")


def test_path_depth_and_utf8_byte_budgets() -> None:
    reject(packed(base() + entry("go/a/b/c")), "path_limit", replace(LIMITS, max_depth=3))
    reject(
        packed(base() + entry("go/" + "a" * 30)), "path_limit", replace(LIMITS, max_path_bytes=20)
    )
    reject(
        packed(base() + entry("go/" + "\u00e9" * 9)),
        "path_limit",
        replace(LIMITS, max_path_bytes=20),
    )


@pytest.mark.parametrize(
    "first,second",
    [
        (entry("go/a"), entry("go/a")),
        (header("go/a/", kind=tarfile.DIRTYPE), header("go/a/", kind=tarfile.DIRTYPE)),
        (entry("go/a"), header("go/a/", kind=tarfile.DIRTYPE)),
        (entry("go/A"), entry("go/a")),
        (entry("go/A/x"), entry("go/a/y")),
        (entry("go/a"), entry("go/a/b")),
        (entry("go/a/b"), entry("go/a")),
        (entry("go/a"), header("go/a/b/", kind=tarfile.DIRTYPE)),
    ],
)
def test_duplicate_case_and_file_parent_conflicts(first: bytes, second: bytes) -> None:
    reject(packed(base() + first + second), "path_conflict")


def test_explicit_directory_after_implicit_parent_is_valid() -> None:
    assert inspect(packed(base() + entry("go/src/a") + header("go/src/", kind=tarfile.DIRTYPE)))


@pytest.mark.parametrize(
    "kind",
    [
        tarfile.SYMTYPE,
        tarfile.LNKTYPE,
        tarfile.CHRTYPE,
        tarfile.BLKTYPE,
        tarfile.FIFOTYPE,
        tarfile.GNUTYPE_SPARSE,
        tarfile.XHDTYPE,
        tarfile.XGLTYPE,
        tarfile.GNUTYPE_LONGNAME,
        tarfile.GNUTYPE_LONGLINK,
        b"7",
        b"Z",
    ],
)
def test_special_entries_are_rejected_without_reading_payload(kind: bytes) -> None:
    # A huge declared extension body must never be buffered/parsed by tarfile.
    reject(packed(base() + header("go/secret-input", 8_000_000_000, kind)), "entry_type")


def test_directory_data_and_regular_link_metadata_are_rejected() -> None:
    reject(packed(base() + header("go/a/", 1, tarfile.DIRTYPE)), "tar_format")
    reject(packed(base() + header("go/a", link="secret")), "tar_format")


@pytest.mark.parametrize("name", ["go/bin/go", "go/bin/gofmt", "go/VERSION"])
def test_required_files_must_exist_as_regular_files(name: str) -> None:
    remaining = b"".join(
        entry(path, b"v", 0o755)
        for path in ("go/bin/go", "go/bin/gofmt", "go/VERSION")
        if path != name
    )
    reject(packed(remaining + header(name + "/", kind=tarfile.DIRTYPE)), "required_file")


@pytest.mark.parametrize("name", ["go/bin/go", "go/bin/gofmt"])
def test_required_binaries_need_executable_mode(name: str) -> None:
    raw = b"".join(
        entry(path, b"v", 0o644 if path == name else 0o755)
        for path in ("go/bin/go", "go/bin/gofmt", "go/VERSION")
    )
    reject(packed(raw), "required_file")


def test_version_content_is_bounded_but_not_catalog_validated_here() -> None:
    binaries = entry("go/bin/go", b"x", 0o755) + entry("go/bin/gofmt", b"x", 0o755)
    manifest = inspect(packed(binaries + entry("go/VERSION", b"go1.99.100")))
    assert manifest.version == "go1.99.100"
    assert replace(manifest, version="go1.27.3").tree_sha256 == manifest.tree_sha256
    reject(packed(binaries + entry("go/VERSION", b"x" * 4096)), "version_format")
    reject(packed(binaries + header("go/VERSION", 4097)), "version_limit")


@pytest.mark.parametrize(
    "content",
    [
        b"go1.27.3",
        b"go1.27.3\n",
        b"go1.27.3\ntime 2026-10-01T00:00:00Z",
        b"go1.27.3\ntime 2026-10-01T00:00:00.123456789Z\n",
        b"go1.27.3\ntime 2026-10-01T08:00:00+08:00\n",
    ],
)
def test_version_exposes_first_line_and_accepts_valid_optional_build_time(content: bytes) -> None:
    binaries = entry("go/bin/go", b"x", 0o755) + entry("go/bin/gofmt", b"x", 0o755)
    assert inspect(packed(binaries + entry("go/VERSION", content))).version == "go1.27.3"


@pytest.mark.parametrize(
    "content",
    [
        b"",
        b"go1.27",
        b"go1.27.3rc1",
        b" go1.27.3",
        b"go1.27.3 ",
        b"go1.027.3",
        b"go1.27.03",
        b"go2.0.0",
        b"go1.27.3\r\n",
        b"go1.27.3\n\n",
        b"go1.27.3\ntime bogus",
        b"go1.27.3\ntime 2026-02-30T00:00:00Z",
        b"go1.27.3\ntime 2026-10-01T25:00:00Z",
        b"go1.27.3\ntime 2026-10-01T00:00:00",
        b"go1.27.3\ntime 2026-10-01T00:00:00+00:99",
        b"go1.27.3\ntime 2026-10-01T00:00:00+24:00",
        b"go1.27.3\ntime 2026-10-01T00:00:00Z\ntrailing",
        b"go1.27.3\ntime 2026-10-01T00:00:00Z\n\n",
        b"go1.27.3\x00",
        b"\xff",
    ],
)
def test_malformed_version_and_build_time_fail_closed(content: bytes) -> None:
    binaries = entry("go/bin/go", b"x", 0o755) + entry("go/bin/gofmt", b"x", 0o755)
    reject(packed(binaries + entry("go/VERSION", content)), "version_format")


@pytest.mark.parametrize("suffix", [b"x", bytes(512), packed(), gzip.compress(b"")])
def test_gzip_trailing_data_or_concatenated_members_are_rejected(suffix: bytes) -> None:
    reject(packed() + suffix, "gzip_format")


@pytest.mark.parametrize("cut", [1, 4, 8, 20])
def test_truncated_gzip_is_rejected(cut: int) -> None:
    reject(packed()[:-cut], "gzip_format")


def test_gzip_crc_is_validated() -> None:
    data = bytearray(packed())
    data[-8] ^= 1
    reject(bytes(data), "gzip_format")


@pytest.mark.parametrize(
    "raw",
    [
        base(),
        base() + bytes(512),
        base() + bytes(1024) + b"x",
        base() + bytes(1024) + entry("go/hidden"),
        base() + bytes(1025),
        base() + bytes(512) + header("go/hidden"),
    ],
)
def test_missing_end_markers_and_nonzero_or_unaligned_tar_trailers(raw: bytes) -> None:
    reject(gzip.compress(raw), "tar_format")


def test_zero_tar_record_padding_is_valid() -> None:
    assert inspect(gzip.compress(base() + bytes(1024 + 512 * 20)))


def test_file_padding_and_header_checksum_are_checked() -> None:
    raw = base() + header("go/extra", 1) + b"x" + b"y" + bytes(510)
    reject(packed(raw), "tar_format")
    damaged = bytearray(base())
    damaged[20] ^= 1
    reject(packed(bytes(damaged)), "tar_format")


def test_file_declared_size_cannot_exceed_expansion_budget() -> None:
    reject(packed(base() + header("go/extra", 8_000_000_000)), "expanded_limit")


def test_ustar_prefix_paths_are_preserved() -> None:
    path = "go/src/" + "long-component/" * 8 + "file.go"
    manifest = inspect(packed(base() + entry(path, b"package source")))
    assert any(file.path == path for file in manifest.files)


def test_error_constructor_does_not_echo_unknown_input() -> None:
    error = ToolchainArchiveError("secret-input")
    assert error.code == "invalid_archive"
    assert str(error) == "invalid_archive"


def test_large_regular_file_is_hashed_across_stream_chunks() -> None:
    payload = bytes(range(256)) * 2048 + b"tail"
    manifest = inspect(packed(base() + entry("go/src/large", payload)))
    file = next(file for file in manifest.files if file.path == "go/src/large")
    assert file.size_bytes == len(payload)
    assert file.sha256 == hashlib.sha256(payload).hexdigest()


def test_large_pax_header_is_rejected_before_extension_parsing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("must not invoke TarFile's extension parser")

    monkeypatch.setattr(tarfile, "open", forbidden)
    monkeypatch.setattr(tarfile.TarFile, "next", forbidden)
    reject(packed(base() + header("go/pax", 8_000_000_000, tarfile.XHDTYPE)), "entry_type")


def rechecksum(block: bytearray) -> bytes:
    block[148:156] = b"        "
    block[148:156] = f"{sum(block):06o}\0 ".encode("ascii")
    return bytes(block)


@pytest.mark.parametrize(
    "offset,replacement",
    [
        (124, b"\x80" + bytes(11)),
        (124, b"000001\x000001\x00"),
        (257, b"unknown!"),
        (10, b"\0hidden"),
        (500, b"hidden"),
        (100, b"0004755\0"),
    ],
)
def test_noncanonical_headers_are_rejected(offset: int, replacement: bytes) -> None:
    block = bytearray(header("go/extra-long-name"))
    block[offset : offset + len(replacement)] = replacement
    reject(packed(base() + rechecksum(block)), "tar_format")


def test_invalid_utf8_path_is_not_replacement_decoded() -> None:
    block = bytearray(header("go/extra"))
    block[3] = 0xFF
    reject(packed(base() + rechecksum(block)), "tar_format")


def test_gnu_ordinary_headers_accepted_but_no_sparse_extension_fields() -> None:
    block = bytearray(header("go/extra"))
    block[257:265] = b"ustar  \0"
    assert inspect(packed(base() + rechecksum(block)))
    block[386:398] = b"00000000001\0"
    reject(packed(base() + rechecksum(block)), "tar_format")


def test_empty_archive_still_requires_toolchain_files() -> None:
    reject(gzip.compress(bytes(1024)), "required_file")


def test_absolute_archive_size_is_checked_before_decompression() -> None:
    reject(b"not gzip", "archive_limit", replace(LIMITS, max_archive_bytes=1))


def test_regular_file_cannot_consume_missing_payload_or_terminator() -> None:
    reject(gzip.compress(base() + header("go/extra", 1024) + b"truncated"), "tar_format")


@pytest.mark.parametrize("data", [b"", b"no gzip header", gzip.compress(b"not a tar")])
def test_malformed_streams_are_finite_errors(data: bytes) -> None:
    with pytest.raises(ToolchainArchiveError) as error:
        inspect(data)
    assert error.value.code in {"gzip_format", "tar_format"}
    assert str(error.value) == error.value.code
