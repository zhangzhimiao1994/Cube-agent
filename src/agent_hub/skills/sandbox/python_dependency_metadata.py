"""Bounded metadata values from verified wheel bytes; no IO or install authority.

An index is a DTO, not authentication or authorization. Root must verify its
owner/path and call require_catalog before trusting it, even with a cold cache.
Metadata belongs outside the existing flat wheel root.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import time
import zipfile
from dataclasses import dataclass, replace
from email.parser import BytesParser
from email.policy import compat32
from typing import ClassVar, cast

from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet

from agent_hub.capabilities.python_dependency_closure import PythonWheelMetadata
from agent_hub.capabilities.python_project_wheels import PythonWheelBinding
from agent_hub.skills.sandbox.python_input_fetch import WheelDownload, validate_wheel_bytes

_MAX_METADATA_BYTES = 256 * 1024
_MAX_FIELD_LENGTH = 4096
_MAX_REQUIRES_DIST = 1000
_MAX_RECORDS = 1000
_MAX_INDEX_BYTES = 8 * 1024 * 1024
_KEYS = frozenset({"schema_version", "extractor_version", "catalog_sha256", "records"})


class PythonDependencyMetadataError(ValueError):
    """Path-free failure at the metadata value boundary."""

    def __init__(self, code: str = "python_dependency_metadata_invalid") -> None:
        self.code = code
        super().__init__(code)


def _need(value: object) -> None:
    if not value:
        raise PythonDependencyMetadataError()


def _digest(value: object) -> None:
    _need(type(value) is str and re.fullmatch(r"[0-9a-f]{64}", value) is not None)


def _field(value: object) -> str:
    _need(type(value) is str)
    text = cast(str, value)
    _need(0 < len(text) <= _MAX_FIELD_LENGTH and all(32 <= ord(c) <= 126 for c in text)
          and bool(text.strip()))
    return text


def _check_deadline(deadline: float | None) -> None:
    if deadline is not None and time.monotonic() >= deadline:
        raise PythonDependencyMetadataError("python_dependency_metadata_timeout")


def extract_python_wheel_metadata(
    entry: WheelDownload, raw: bytes, *, deadline: float | None = None,
) -> PythonWheelMetadata:
    """Validate the whole archive first, then read its sole matching METADATA."""
    if deadline is None:
        validate_wheel_bytes(entry, raw)
    else:
        validate_wheel_bytes(entry, raw, deadline=deadline)
    try:
        _check_deadline(deadline)
        primary = "-".join(entry.filename.split("-")[:2]) + ".dist-info/METADATA"
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            _check_deadline(deadline)
            matches = [item for item in archive.infolist() if item.filename == primary]
            _need(len(matches) == 1 and 0 < matches[0].file_size <= _MAX_METADATA_BYTES)
            with archive.open(matches[0]) as stream:
                metadata = bytearray()
                while True:
                    _check_deadline(deadline)
                    chunk = stream.read(min(65536, _MAX_METADATA_BYTES + 1 - len(metadata)))
                    _check_deadline(deadline)
                    if not chunk:
                        break
                    metadata.extend(chunk)
                    _need(len(metadata) <= _MAX_METADATA_BYTES)
                metadata_bytes = bytes(metadata)
            _need(len(metadata_bytes) == matches[0].file_size
                  and len(metadata_bytes) <= _MAX_METADATA_BYTES)
        _check_deadline(deadline)
        document = BytesParser(policy=compat32).parsebytes(metadata_bytes)
        _check_deadline(deadline)
        _need(not document.defects)
        for key, expected in (("Name", entry.name), ("Version", entry.version)):
            _check_deadline(deadline)
            values = document.get_all(key, [])
            _need(len(values) == 1)
            actual = _field(values[0])
            if key == "Name":
                actual = re.sub(r"[-_.]+", "-", actual).lower()
            _need(actual == expected)
        python_values = document.get_all("Requires-Python", [])
        _need(len(python_values) <= 1)
        requires_python = _field(python_values[0]) if python_values else None
        if requires_python is not None:
            SpecifierSet(requires_python)
        dependencies = document.get_all("Requires-Dist", [])
        _need(len(dependencies) <= _MAX_REQUIRES_DIST)
        requires_dist = tuple(_field(value) for value in dependencies)
        for requirement in requires_dist:
            _check_deadline(deadline)
            _need(requirement.count("(") <= 32)
            Requirement(requirement)
        _check_deadline(deadline)
        return PythonWheelMetadata(
            wheel=PythonWheelBinding(entry.filename, entry.name, entry.version, entry.sha256),
            requires_python=requires_python, requires_dist=requires_dist,
        )
    except PythonDependencyMetadataError:
        raise
    except (ValueError, TypeError, AttributeError, KeyError, OSError, zipfile.BadZipFile,
            RecursionError, OverflowError):
        raise PythonDependencyMetadataError() from None


def _encoded(value: object) -> bytes:
    encoder = json.JSONEncoder(sort_keys=True, separators=(",", ":"), ensure_ascii=True,
                               allow_nan=False)
    raw = bytearray()
    for chunk in encoder.iterencode(value):
        _need(len(raw) + len(chunk) <= _MAX_INDEX_BYTES)
        raw.extend(chunk.encode("ascii"))
    return bytes(raw)


def _object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        _need(key not in result)
        result[key] = value
    return result


def _constant(value: str) -> object:
    raise PythonDependencyMetadataError()


@dataclass(frozen=True, slots=True)
class PythonDependencyMetadataIndex:
    """Canonical complete catalog metadata, usable without locally cached wheels."""

    catalog_sha256: str
    records: tuple[PythonWheelMetadata, ...]
    schema_version: ClassVar[int] = 1
    extractor_version: ClassVar[int] = 1

    def __post_init__(self) -> None:
        self.to_bytes()

    def to_payload(self) -> dict[str, object]:
        try:
            _digest(self.catalog_sha256)
            _need(type(self.records) is tuple and len(self.records) <= _MAX_RECORDS)
            previous: tuple[str, str, str, str] | None = None
            identities: set[tuple[str, str]] = set()
            filenames: set[str] = set()
            records: list[dict[str, object]] = []
            for record in self.records:
                _need(type(record) is PythonWheelMetadata)
                payload = record.to_payload()
                checked = PythonWheelMetadata.from_payload(payload)
                binding = checked.wheel
                key = (binding.name, binding.version, binding.filename, binding.sha256)
                _need(previous is None or previous < key)
                _need((binding.name, binding.version) not in identities
                      and binding.filename not in filenames)
                identities.add((binding.name, binding.version))
                filenames.add(binding.filename)
                previous = key
                records.append(payload)
            payload_index: dict[str, object] = {
                "schema_version": 1, "extractor_version": 1,
                "catalog_sha256": self.catalog_sha256, "records": records,
            }
            _encoded(payload_index)
            return payload_index
        except PythonDependencyMetadataError:
            raise
        except (ValueError, TypeError, AttributeError, KeyError, RecursionError, OverflowError):
            raise PythonDependencyMetadataError() from None

    @classmethod
    def from_payload(cls, value: object) -> PythonDependencyMetadataIndex:
        try:
            _need(type(value) is dict)
            item = cast(dict[str, object], value)
            _need(set(item) == _KEYS)
            for key in ("schema_version", "extractor_version"):
                _need(type(item[key]) is int and item[key] == 1)
            _digest(item["catalog_sha256"])
            _need(type(item["records"]) is list)
            values = cast(list[object], item["records"])
            _need(len(values) <= _MAX_RECORDS)
            _encoded(item)
            records = tuple(PythonWheelMetadata.from_payload(record) for record in values)
            return cls(cast(str, item["catalog_sha256"]), records)
        except PythonDependencyMetadataError:
            raise
        except (ValueError, TypeError, AttributeError, KeyError, RecursionError, OverflowError):
            raise PythonDependencyMetadataError() from None

    def to_bytes(self) -> bytes:
        return _encoded(self.to_payload())

    @classmethod
    def from_bytes(cls, raw: bytes) -> PythonDependencyMetadataIndex:
        try:
            _need(type(raw) is bytes and 0 < len(raw) <= _MAX_INDEX_BYTES)
            return cls.from_payload(json.loads(raw.decode("utf-8"), object_pairs_hook=_object,
                                               parse_constant=_constant))
        except PythonDependencyMetadataError:
            raise
        except (ValueError, TypeError, RecursionError, OverflowError):
            raise PythonDependencyMetadataError() from None

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.to_bytes()).hexdigest()

    def require_catalog(self, catalog: tuple[WheelDownload, ...], digest: str) -> None:
        """Match exact identities and the caller's opaque Root catalog digest."""
        try:
            self.to_payload()
            _digest(digest)
            _need(digest == self.catalog_sha256 and type(catalog) is tuple
                  and len(catalog) == len(self.records))
            expected: set[tuple[str, str, str, str]] = set()
            identities: set[tuple[str, str]] = set()
            filenames: set[str] = set()
            for entry in catalog:
                _need(type(entry) is WheelDownload)
                checked = replace(entry)
                _need((checked.name, checked.version) not in identities
                      and checked.filename not in filenames)
                identities.add((checked.name, checked.version))
                filenames.add(checked.filename)
                expected.add((checked.name, checked.version, checked.filename, checked.sha256))
            _need(expected == {(r.wheel.name, r.wheel.version, r.wheel.filename, r.wheel.sha256)
                               for r in self.records})
        except (ValueError, TypeError, AttributeError, KeyError, RecursionError, OverflowError):
            raise PythonDependencyMetadataError("python_dependency_metadata_catalog_mismatch") from None
