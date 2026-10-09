from __future__ import annotations

import zipfile
from collections.abc import Mapping
from io import BytesIO

import pytest

from agent_hub.harness import project_scale_runner as runner

_SOURCE = b"export function total(values) { return values.reduce((a, b) => a + b, 0); }\n"
_GENERAL_MARKER = "workspace_bundle: contains placeholder or stub markers"
_SOURCE_MARKER = "workspace_bundle: source contains placeholder or stub markers"


def _files(extra: Mapping[str, bytes] | None = None) -> dict[str, bytes]:
    files = {
        "README.md": b"# Own task API\n",
        "src/main.js": _SOURCE,
        "tests/main.test.js": b"assert.equal(total([1, 2]), 3);\n",
        "VERIFICATION.md": (
            b"- npm run build: passed exit 0\n- npm test: passed exit 0\n"
            b"- interaction smoke: passed\n"
        ),
    }
    files.update(extra or {})
    return files


def _bundle(files: Mapping[str, bytes], *, stored: bool = False) -> bytes:
    output = BytesIO()
    compression = zipfile.ZIP_STORED if stored else zipfile.ZIP_DEFLATED
    with zipfile.ZipFile(output, "w", compression=compression) as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return output.getvalue()


def _executed(bundle: bytes) -> runner._EvidenceCheck:
    # Only the quality boundary is exercised; no project commands are executed.
    return runner._executed_capability_quality(
        bundle, runner._EvidenceCheck(passed=True, reasons=())
    )


@pytest.mark.parametrize("marker", runner._PLACEHOLDER_MARKERS)
def test_every_source_marker_after_legacy_text_limit_is_rejected(marker: str) -> None:
    bundle = _bundle(_files({
        "src/late.js": _SOURCE + b" " * 120_001 + marker.upper().encode(),
    }))
    assert _GENERAL_MARKER in runner._workspace_bundle_project_quality_reasons(bundle)
    result = _executed(bundle)
    assert not result.passed
    assert _SOURCE_MARKER in result.reasons


@pytest.mark.parametrize("split", range(1, len(b"not implemented")))
def test_marker_across_byte_chunk_boundary_is_rejected(split: int) -> None:
    content = b" " * (2 * 65_536 - split) + b"not implemented"
    result = _executed(_bundle(_files({"src/late.js": content})))
    assert not result.passed
    assert _SOURCE_MARKER in result.reasons


@pytest.mark.parametrize("utf8_start", (65_534, 65_535, 131_070, 131_071))
def test_utf8_codepoint_split_does_not_hide_later_marker(utf8_start: int) -> None:
    content = b" " * utf8_start + "\u4e2d".encode() + b" " * 65_536 + b"COMING SOON"
    assert _SOURCE_MARKER in _executed(_bundle(_files({"src/late.js": content}))).reasons


@pytest.mark.parametrize("path", (
    "docs/note.md", "styles/main.css", "assets/data.json", "note.txt", "scripts/build.js",
))
def test_text_scan_rejects_late_marker_but_executed_source_filter_is_preserved(path: str) -> None:
    bundle = _bundle(_files({path: b" " * 120_001 + b"coming soon"}))
    assert _GENERAL_MARKER in runner._workspace_bundle_project_quality_reasons(bundle)
    assert _executed(bundle).passed


def test_marker_fragments_in_different_files_do_not_match() -> None:
    bundle = _bundle(_files({"src/a.js": b"// coming", "src/b.js": b" soon\n"}))
    assert runner._workspace_bundle_project_quality_reasons(bundle) == ()
    assert _executed(bundle).passed


def test_normal_bundle_passes_without_changing_legacy_text_excerpt_limit() -> None:
    bundle = _bundle(_files({"src/late.js": _SOURCE + b" " * 130_000}))
    assert runner._workspace_bundle_project_quality_reasons(bundle) == ()
    assert _executed(bundle).passed
    with zipfile.ZipFile(BytesIO(bundle)) as archive:
        assert len(runner._workspace_bundle_source_text(archive, ["src/late.js"])) == 120_000


@pytest.mark.parametrize("path", ("src/large.js", "assets/large.bin"))
def test_per_file_limit_is_inclusive_and_oversize_fails_closed(path: str) -> None:
    limit = runner._GENERATED_PROJECT_MAX_FILE_BYTES
    assert _executed(_bundle(_files({path: b" " * limit}))).passed
    bundle = _bundle(_files({path: b" " * (limit + 1)}))
    assert runner._workspace_bundle_project_quality_reasons(bundle)
    assert not _executed(bundle).passed


def _total_budget_files(extra_byte: int) -> dict[str, bytes]:
    files = _files()
    remaining = runner._GENERATED_PROJECT_MAX_TOTAL_BYTES - sum(map(len, files.values()))
    index = 0
    while remaining > 0:
        size = min(remaining, runner._GENERATED_PROJECT_MAX_FILE_BYTES)
        files[f"assets/{index}.bin"] = b" " * size
        remaining -= size
        index += 1
    if extra_byte:
        files["assets/extra.bin"] = b"x"
    return files


def test_total_byte_limit_is_inclusive_and_oversize_fails_closed() -> None:
    assert _executed(_bundle(_total_budget_files(0))).passed
    bundle = _bundle(_total_budget_files(1))
    assert runner._workspace_bundle_project_quality_reasons(bundle)
    assert not _executed(bundle).passed


@pytest.mark.parametrize("bad_content", (b"\xff", b"\xe4\xb8"))
def test_invalid_or_truncated_utf8_text_fails_closed(bad_content: bytes) -> None:
    bundle = _bundle(_files({"src/late.js": b" " * 120_001 + bad_content}))
    assert not _executed(bundle).passed


def test_binary_content_is_not_decoded_or_matched_as_source() -> None:
    bundle = _bundle(_files({"assets/opaque.bin": b"\xffcoming soon\xe4\xb8"}))
    assert runner._workspace_bundle_project_quality_reasons(bundle) == ()
    assert _executed(bundle).passed


@pytest.mark.parametrize("path", ("src/bad.js", "assets/bad.bin"))
def test_corrupt_member_fails_closed_even_when_not_selected_as_text(path: str) -> None:
    bundle = bytearray(_bundle(_files({path: b" " * 130_000}), stored=True))
    with zipfile.ZipFile(BytesIO(bundle)) as archive:
        info = archive.getinfo(path)
        offset = info.header_offset
        name_length = int.from_bytes(bundle[offset + 26:offset + 28], "little")
        extra_length = int.from_bytes(bundle[offset + 28:offset + 30], "little")
        bundle[offset + 30 + name_length + extra_length + 129_999] ^= 1
    assert not _executed(bytes(bundle)).passed


@pytest.mark.parametrize("bundle", (b"not a zip", b"PK\x03\x04"))
def test_invalid_or_truncated_archive_fails_closed(bundle: bytes) -> None:
    assert not _executed(bundle).passed


@pytest.mark.parametrize("source_only", (False, True))
def test_scanner_uses_only_bounded_chunk_reads(
    monkeypatch: pytest.MonkeyPatch, source_only: bool
) -> None:
    bundle = _bundle(_files({"src/late.js": b" " * 131_067 + b"coming soon"}))
    original_read = zipfile.ZipExtFile.read
    sizes: list[int] = []

    def bounded_read(stream: zipfile.ZipExtFile, size: int | None = -1) -> bytes:
        assert type(size) is int and 0 < size <= 65_536
        sizes.append(size)
        return original_read(stream, size)

    def forbidden_member_read(*args: object, **kwargs: object) -> bytes:
        pytest.fail("placeholder scanner must not use whole-member archive.read")

    monkeypatch.setattr(zipfile.ZipExtFile, "read", bounded_read)
    monkeypatch.setattr(zipfile.ZipFile, "read", forbidden_member_read)
    with zipfile.ZipFile(BytesIO(bundle)) as archive:
        assert runner._workspace_bundle_has_placeholder(archive, source_only=source_only)
    assert sizes and max(sizes) == 65_536


def test_scanner_does_not_stop_validating_after_a_marker_is_found() -> None:
    bundle = _bundle(_files({
        "src/late.js": b"coming soon" + b" " * 130_000 + b"\xe4\xb8",
    }))
    result = _executed(bundle)
    assert not result.passed
    assert "workspace_bundle: invalid or unreadable zip bundle" in result.reasons


@pytest.mark.parametrize("source_only", (False, True))
def test_file_count_limit_is_inclusive_and_excess_fails_closed(source_only: bool) -> None:
    files = {f"assets/{i}.bin": b"" for i in range(runner._GENERATED_PROJECT_MAX_FILES)}
    with zipfile.ZipFile(BytesIO(_bundle(files))) as archive:
        assert not runner._workspace_bundle_has_placeholder(archive, source_only=source_only)
    files["assets/extra.bin"] = b""
    with (
        zipfile.ZipFile(BytesIO(_bundle(files))) as archive,
        pytest.raises(RuntimeError, match="too many files"),
    ):
        runner._workspace_bundle_has_placeholder(archive, source_only=source_only)


@pytest.mark.parametrize("path", ("src/late.js", "assets/opaque.bin"))
def test_member_open_failure_never_becomes_a_quality_pass(
    monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    bundle = _bundle(_files({path: b" " * 130_000}))
    original_open = zipfile.ZipFile.open

    def fail_open(
        archive: zipfile.ZipFile,
        name: str | zipfile.ZipInfo,
        mode: str = "r",
        pwd: bytes | None = None,
        *,
        force_zip64: bool = False,
    ) -> object:
        if (name.filename if isinstance(name, zipfile.ZipInfo) else name) == path:
            raise OSError("own fixture read failure")
        return original_open(archive, name, mode, pwd, force_zip64=force_zip64)

    monkeypatch.setattr(zipfile.ZipFile, "open", fail_open)
    assert not _executed(bundle).passed
