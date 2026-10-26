"""Actual wheel archives and opaque catalog bindings; no Root or fetch doubles."""

from __future__ import annotations

import hashlib
import importlib
import io
import json
import zipfile
from dataclasses import FrozenInstanceError, replace
from typing import Any

import pytest

from agent_hub.skills.sandbox.python_input_fetch import WheelDownload, validate_wheel_bytes

CATALOG_SHA = "a" * 64


@pytest.fixture
def api() -> Any:
    return importlib.import_module("agent_hub.skills.sandbox.python_dependency_metadata")


def wheel(
    *, name: str = "demo-pkg", version: str = "1.0", headers: bytes = b"",
    metadata: bytes | None = None, extra: tuple[tuple[str, bytes], ...] = (),
) -> tuple[WheelDownload, bytes]:
    distribution = name.replace("-", "_")
    primary = f"{distribution}-{version}.dist-info"
    filename = f"{distribution}-{version}-py3-none-any.whl"
    if metadata is None:
        metadata = (f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n".encode()
                    + headers + b"\n")
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path, body in (
            (distribution + "/__init__.py", b"raise AssertionError('never import wheel')\n"),
            (primary + "/METADATA", metadata),
            (primary + "/WHEEL", (b"Wheel-Version: 1.0\nRoot-Is-Purelib: true\n"
                                  b"Tag: py3-none-any\n\n")),
            (primary + "/RECORD", b""),
            *extra,
        ):
            archive.writestr(zipfile.ZipInfo(path), body)
    raw = output.getvalue()
    return WheelDownload(filename, name, version, hashlib.sha256(raw).hexdigest(), len(raw),
                         "https://files.pythonhosted.org/packages/ab/cd/" + filename), raw


def record(api: Any, *, name: str = "demo-pkg", version: str = "1.0") -> Any:
    entry, raw = wheel(name=name, version=version)
    return api.extract_python_wheel_metadata(entry, raw)


def index(api: Any) -> Any:
    return api.PythonDependencyMetadataIndex(CATALOG_SHA, (record(api),))


def test_extracts_actual_identity_requirements_markers_and_extras(api: Any) -> None:
    entry, raw = wheel(headers=(b"Requires-Python: >=3.10,<4\n"
        b"Requires-Dist: packaging>=23; python_version >= '3.10'\n"
        b"Requires-Dist: helper[fast]==2.0; extra == 'test'\n"), extra=(
            ("demo_pkg/_vendor/other.dist-info/METADATA", b"Requires-Dist: forged\n"),))
    validate_wheel_bytes(entry, raw)
    metadata = api.extract_python_wheel_metadata(entry, raw)
    assert metadata.wheel.to_payload() == {
        "filename": entry.filename, "name": "demo-pkg", "version": "1.0",
        "sha256": hashlib.sha256(raw).hexdigest(),
    }
    assert metadata.requires_python == ">=3.10,<4"
    assert metadata.requires_dist == (
        "packaging>=23; python_version >= '3.10'", "helper[fast]==2.0; extra == 'test'",
    )


def test_absent_dependency_headers_are_explicit_empty_values(api: Any) -> None:
    metadata = record(api)
    assert metadata.requires_python is None
    assert metadata.requires_dist == ()


@pytest.mark.parametrize("case", ["sha", "size", "name", "version", "truncated", "type"])
def test_extract_requires_actual_validated_bytes(api: Any, case: str) -> None:
    entry, raw = wheel()
    data: object = raw
    if case == "sha":
        entry = replace(entry, sha256="0" * 64)
    elif case == "size":
        entry = replace(entry, size_bytes=len(raw) + 1)
    elif case in {"name", "version"}:
        entry, _ = wheel(name="other-pkg") if case == "name" else wheel(version="2.0")
        entry = replace(entry, sha256=hashlib.sha256(raw).hexdigest(), size_bytes=len(raw))
    elif case == "truncated":
        raw = raw[:-1]
        entry = replace(entry, sha256=hashlib.sha256(raw).hexdigest(), size_bytes=len(raw))
        data = raw
    else:
        data = bytearray(raw)
    with pytest.raises(ValueError):
        api.extract_python_wheel_metadata(entry, data)


@pytest.mark.parametrize("headers", [
    b"Name: demo-pkg\n", b"Version: 1.0\n",
    b"Requires-Python: >=3\nrequires-python: <4\n",
    b"Requires-Python: \n", b"Requires-Python: nonsense\n",
    b"Requires-Dist: \n", b"Requires-Dist: helper=>2\n",
    b"Requires-Dist: helper\x00\n", b"Requires-Dist: helper\x7f\n",
    b"Requires-Python: >=3\x01\n", b"Requires-Dist: helper\n injected\n",
    b"Requires-Dist: helper\t>=2\n",
])
def test_rejects_duplicate_identity_or_unsafe_dependency_headers(api: Any, headers: bytes) -> None:
    entry, raw = wheel(headers=headers)
    with pytest.raises(ValueError):
        api.extract_python_wheel_metadata(entry, raw)


@pytest.mark.parametrize("case", ["length", "count", "metadata_bytes"])
def test_metadata_limits_fail_instead_of_truncating(api: Any, case: str) -> None:
    headers = {
        "length": b"Requires-Dist: helper; extra == '" + b"x" * 8192 + b"'\n",
        "count": b"Requires-Dist: helper\n" * 1001,
        "metadata_bytes": b"Summary: " + b"x" * (256 * 1024) + b"\n",
    }[case]
    entry, raw = wheel(headers=headers)
    with pytest.raises(ValueError):
        api.extract_python_wheel_metadata(entry, raw)


def test_marker_nesting_is_bounded_before_requirement_parser_recursion(api: Any) -> None:
    marker = b"(" * 40 + b"python_version >= '3.10'" + b")" * 40
    entry, raw = wheel(headers=b"Requires-Dist: helper; " + marker + b"\n")
    with pytest.raises(ValueError):
        api.extract_python_wheel_metadata(entry, raw)


def test_duplicate_metadata_members_and_foreign_dist_info_are_invalid(api: Any) -> None:
    with pytest.warns(UserWarning):
        entry, raw = wheel(extra=(("demo_pkg-1.0.dist-info/METADATA", b"Name: forged\n"),))
    with pytest.raises(ValueError):
        api.extract_python_wheel_metadata(entry, raw)
    entry, raw = wheel(extra=(("foreign-1.0.dist-info/METADATA", b"Name: forged\n"),))
    with pytest.raises(ValueError):
        api.extract_python_wheel_metadata(entry, raw)


def test_actual_nonmetadata_member_crc_is_validated_before_extraction(api: Any) -> None:
    entry, raw = wheel()
    changed = raw.replace(b"never import wheel", b"never import wheeX")
    entry = replace(entry, sha256=hashlib.sha256(changed).hexdigest())
    with pytest.raises(ValueError):
        api.extract_python_wheel_metadata(entry, changed)


def test_index_roundtrip_digest_and_cold_catalog_without_files(api: Any) -> None:
    entry, raw = wheel()
    value = api.PythonDependencyMetadataIndex(CATALOG_SHA,
        (api.extract_python_wheel_metadata(entry, raw),))
    payload = value.to_payload()
    assert set(payload) == {"schema_version", "extractor_version", "catalog_sha256", "records"}
    assert payload["schema_version"] == payload["extractor_version"] == 1
    assert value.schema_version == value.extractor_version == 1
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=True, allow_nan=False).encode("ascii")
    assert value.to_bytes() == encoded
    assert value.sha256 == hashlib.sha256(encoded).hexdigest()
    assert api.PythonDependencyMetadataIndex.from_payload(payload) == value
    restored = api.PythonDependencyMetadataIndex.from_bytes(encoded)
    assert restored == value
    del raw
    assert restored.require_catalog((entry,), CATALOG_SHA) is None
    with pytest.raises((FrozenInstanceError, AttributeError)):
        value.catalog_sha256 = "b" * 64
    assert not hasattr(value, "__dict__")


@pytest.mark.parametrize("case", [
    "missing", "extra", "duplicate", "hash", "version", "filename", "digest", "list",
])
def test_catalog_requires_exact_complete_identity_and_opaque_digest(api: Any, case: str) -> None:
    entry, _ = wheel()
    catalog: object = (entry,)
    digest = CATALOG_SHA
    if case == "missing":
        catalog = ()
    elif case == "extra":
        catalog = (entry, wheel(name="other-pkg")[0])
    elif case == "duplicate":
        catalog = (entry, entry)
    elif case == "hash":
        catalog = (replace(entry, sha256="b" * 64),)
    elif case == "version":
        catalog = (wheel(version="2.0")[0],)
    elif case == "filename":
        catalog = (replace(entry, filename=entry.filename.replace("demo_pkg", "Demo_Pkg"),
                           url=entry.url.replace("demo_pkg", "Demo_Pkg")),)
    elif case == "digest":
        digest = "b" * 64
    else:
        catalog = [entry]
    with pytest.raises(ValueError):
        index(api).require_catalog(catalog, digest)


def test_catalog_order_is_irrelevant_but_index_order_is_canonical(api: Any) -> None:
    first, second = record(api, name="aaa"), record(api, name="zzz")
    value = api.PythonDependencyMetadataIndex(CATALOG_SHA, (first, second))
    assert value.require_catalog((wheel(name="zzz")[0], wheel(name="aaa")[0]), CATALOG_SHA) is None
    with pytest.raises(ValueError):
        api.PythonDependencyMetadataIndex(CATALOG_SHA, (second, first))


@pytest.mark.parametrize("case", [
    "unknown", "missing", "schema_bool", "schema_float", "schema_old", "extractor_bool",
    "extractor_old", "bad_digest", "uppercase_digest", "records_tuple", "duplicate", "unsorted",
    "record_unknown", "wheel_unknown", "record_missing", "record_type", "nonfinite",
])
def test_payload_boundary_is_strict(api: Any, case: str) -> None:
    value: dict[str, Any] = index(api).to_payload()
    if case == "unknown":
        value["authority"] = True
    elif case == "missing":
        del value["catalog_sha256"]
    elif case.startswith("schema_"):
        value["schema_version"] = {"bool": True, "float": 1.0, "old": 2}[case[7:]]
    elif case.startswith("extractor_"):
        value["extractor_version"] = True if case.endswith("bool") else 2
    elif case in {"bad_digest", "uppercase_digest"}:
        value["catalog_sha256"] = "x" if case == "bad_digest" else "A" * 64
    elif case == "records_tuple":
        value["records"] = tuple(value["records"])
    elif case == "duplicate":
        value["records"] *= 2
    elif case == "unsorted":
        value["records"] = [record(api, name="zzz").to_payload(),
                            record(api, name="aaa").to_payload()]
    elif case == "record_unknown":
        value["records"][0]["authority"] = True
    elif case == "wheel_unknown":
        value["records"][0]["wheel"]["url"] = "https://untrusted.invalid"
    elif case == "record_missing":
        del value["records"][0]["requires_dist"]
    elif case == "record_type":
        value["records"][0] = None
    else:
        value["schema_version"] = float("nan")
    with pytest.raises(ValueError):
        api.PythonDependencyMetadataIndex.from_payload(value)


@pytest.mark.parametrize("raw", [
    b'{"schema_version":1,"schema_version":1}',
    b'{"schema_version":NaN}', b'{"schema_version":Infinity}', b'{"schema_version":-Infinity}',
    b"[]", b"null", b"{", b"\xff", b"[" * 2000 + b"]" * 2000,
])
def test_wire_rejects_malformed_duplicate_and_nonfinite_json(api: Any, raw: bytes) -> None:
    with pytest.raises(ValueError):
        api.PythonDependencyMetadataIndex.from_bytes(raw)


def test_nested_duplicate_json_keys_are_rejected_even_when_values_match(api: Any) -> None:
    raw: bytes = index(api).to_bytes()
    duplicated = raw.replace(b'"requires_python":null',
                             b'"requires_python":null,"requires_python":null')
    assert duplicated != raw
    with pytest.raises(ValueError):
        api.PythonDependencyMetadataIndex.from_bytes(duplicated)


def test_wire_and_payload_have_total_byte_limits_without_partial_results(api: Any) -> None:
    with pytest.raises(ValueError):
        api.PythonDependencyMetadataIndex.from_bytes(b" " * (8 * 1024 * 1024 + 1))
    values = index(api).to_payload()
    values["records"] = values["records"] * 1001
    with pytest.raises(ValueError):
        api.PythonDependencyMetadataIndex.from_payload(values)


def test_total_encoded_budget_applies_to_otherwise_valid_distinct_records(api: Any) -> None:
    record_type = importlib.import_module(
        "agent_hub.capabilities.python_dependency_closure").PythonWheelMetadata
    values = []
    for number in range(150):
        binding = record(api, name=f"pkg{number:03d}").wheel
        values.append(record_type(binding, None,
            ("helper; extra == '" + "x" * 4000 + "'",) * 15))
    with pytest.raises(ValueError):
        api.PythonDependencyMetadataIndex(CATALOG_SHA, tuple(values))
    payload = {"schema_version": 1, "extractor_version": 1, "catalog_sha256": CATALOG_SHA,
               "records": [item.to_payload() for item in values]}
    with pytest.raises(ValueError):
        api.PythonDependencyMetadataIndex.from_payload(payload)


def test_all_serialization_boundaries_revalidate_forged_values(api: Any) -> None:
    value = index(api)
    object.__setattr__(value, "catalog_sha256", "forged")
    for operation in (value.to_payload, value.to_bytes, lambda: value.sha256):
        with pytest.raises(ValueError):
            operation()


@pytest.mark.parametrize("digest,records", [(True, ()), ("a" * 64, []), ("a" * 64, (None,))])
def test_constructor_has_same_strict_value_contract(api: Any, digest: object, records: object) -> None:
    with pytest.raises(ValueError):
        api.PythonDependencyMetadataIndex(digest, records)
