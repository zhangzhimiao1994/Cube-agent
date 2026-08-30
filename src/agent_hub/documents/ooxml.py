from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from xml.sax.saxutils import escape
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

OOXML_TIMESTAMP = (1980, 1, 1, 0, 0, 0)


def xml_text(value: object) -> str:
    text = str(value)
    if any(not _is_valid_xml_text_character(ord(character)) for character in text):
        raise ValueError("text contains characters that are not valid XML")
    return escape(text, {'"': "&quot;", "'": "&apos;"})


def write_ooxml_package(parts: Mapping[str, str], output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(output, "w", compression=ZIP_DEFLATED) as package:
        for name in sorted(parts):
            _validate_part_name(name)
            info = ZipInfo(name, date_time=OOXML_TIMESTAMP)
            info.compress_type = ZIP_DEFLATED
            info.external_attr = 0o600 << 16
            package.writestr(info, parts[name].encode("utf-8"))


def xml_document(body: str) -> str:
    return f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n{body}'


def _validate_part_name(name: str) -> None:
    if not name or name.startswith("/") or "\\" in name:
        raise ValueError(f"unsafe OOXML part name: {name!r}")
    if any(segment in {"", ".", ".."} for segment in name.split("/")):
        raise ValueError(f"unsafe OOXML part name: {name!r}")


def _is_valid_xml_text_character(codepoint: int) -> bool:
    if codepoint in {0x09, 0x0A, 0x0D}:
        return True
    if codepoint < 0x20:
        return False
    if 0xD800 <= codepoint <= 0xDFFF:
        return False
    if codepoint > 0x10FFFF:
        return False
    if 0xFDD0 <= codepoint <= 0xFDEF:
        return False
    return codepoint & 0xFFFE != 0xFFFE
