from __future__ import annotations

import ast
import hashlib
import json
from collections.abc import Mapping
from dataclasses import FrozenInstanceError, fields, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from uuid import UUID

import pytest

from agent_hub.plugins.handover_sources import SnapshotFile
from agent_hub.plugins.handover_verification import VerifiedMember, VerifiedPackageFacts
from agent_hub.plugins.source_scanner import (
    PluginScanLimits,
    PluginScanSubject,
    PluginSourceFinding,
    PluginSourceScanReport,
    scan_source,
)
from agent_hub.runtime.contracts import JsonValue, _canonical_json_bytes, _mutable_json


def file(path: str, content: bytes) -> SnapshotFile:
    return SnapshotFile(path, content, len(content), hashlib.sha256(content).hexdigest())


def manifest_file(
    entrypoint: str = "adapter.py", *, runtime: str = "python", dependencies: bool = False
) -> SnapshotFile:
    return file(
        "plugin.json",
        json.dumps(
            {
                "id": "example",
                "name": "Example",
                "version": "1.0.0",
                "package": {
                    "kind": "adapter_package",
                    "package_version": "1.0.0",
                    "adapter_id": "example",
                    "sdk_api_version": "1.0",
                    "runtime": runtime,
                    "entrypoint": entrypoint,
                    "isolation": "local_process",
                    "install_mode": "scan_only",
                    "dependencies": [
                        {"kind": "python", "source": "pypi", "name": "example", "version": "1.0.0"}
                    ]
                    if dependencies
                    else [],
                },
            }
        ).encode(),
    )


def subject(files: tuple[SnapshotFile, ...]) -> PluginScanSubject:
    return PluginScanSubject(
        tenant_id=UUID(int=1),
        run_id=UUID(int=2),
        registration_id=UUID(int=3),
        handover_id="handover-1",
        receipt_id="receipt-1",
        snapshot_id="snapshot-1",
        source_grant_id="grant-1",
        plan_hash="1" * 64,
        blob_sha256="2" * 64,
        source_grant_binding_sha256="3" * 64,
        registration_contract_sha256="4" * 64,
        plan_version=1,
        verified_package=VerifiedPackageFacts(
            plugin_id="example",
            plugin_version="1.0.0",
            source_manifest_sha256="5" * 64,
            frozen_manifest_sha256=next(
                (item.sha256 for item in files if item.path == "plugin.json"), "6" * 64
            ),
            archive_sha256="7" * 64,
            signature_payload_sha256="8" * 64,
            key_id="key-1",
            public_key_sha256="9" * 64,
            verified_at=datetime(2026, 10, 15, tzinfo=UTC),
            key_not_after=None,
            members=tuple(
                VerifiedMember(
                    item.path,
                    item.size_bytes,
                    "a" * 64 if item.path == "plugin.json" else item.sha256,
                )
                for item in files
            ),
        ),
    )


def scan(*files: SnapshotFile, limits: PluginScanLimits | None = None) -> PluginSourceScanReport:
    if files and not any(item.path == "plugin.json" for item in files):
        entrypoint = next((item.path for item in files if item.path.endswith(".py")), files[0].path)
        files = (*files, manifest_file(entrypoint))
    return scan_source(files, subject(files), limits=limits)


def member_rows(report: PluginSourceScanReport) -> tuple[Mapping[str, JsonValue], ...]:
    rows = report.body["members"]
    assert isinstance(rows, tuple)
    assert all(isinstance(row, Mapping) for row in rows)
    return cast(tuple[Mapping[str, JsonValue], ...], rows)


def test_ast_never_executes_top_level_decorators_defaults_or_imports(tmp_path: Path) -> None:
    marker = tmp_path / "must-not-exist"
    code = (
        "import source_scanner_nonexistent_target\n"
        f"open({str(marker)!r}, 'w').write('secret')\n"
        "@eval('raise SystemExit')\n"
        "def f(value=exec('raise SystemExit')):\n"
        "    return value\n"
        "raise RuntimeError('source must never execute')\n"
    ).encode()
    report = scan(file("adapter.py", code))
    assert report.coverage == "complete"
    assert {finding.category for finding in report.findings} >= {"import", "dynamic_execution"}
    assert not marker.exists()


def test_every_python_member_including_non_entrypoint_is_scanned() -> None:
    report = scan(
        file("adapter.py", b"value = 1\n"),
        file("nested/unused.py", b"eval('secret')\n"),
    )
    assert report.coverage == "complete"
    assert [(f.rule_id, f.path, f.line, f.column) for f in report.findings] == [
        ("PY001", "nested/unused.py", 1, 0)
    ]
    assert [row["status"] for row in member_rows(report) if row["path"] != "plugin.json"] == [
        "scanned",
        "scanned",
    ]


@pytest.mark.parametrize(
    ("code", "category"),
    [
        (b"import builtins as b\nb.eval('secret')", "dynamic_execution"),
        (b"from builtins import exec as execute\nexecute('secret')", "dynamic_execution"),
        (b"compile('secret', 'private', 'exec')", "dynamic_execution"),
        (b"import subprocess as p\np.run(['secret'])", "process"),
        (b"from os import system as launch\nlaunch('secret')", "process"),
        (b"import subprocess\nlaunch = subprocess.Popen\nlaunch(['secret'])", "process"),
        (b"import requests as r\nr.get('https://secret.invalid')", "network"),
        (b"from urllib.request import urlopen as u\nu('secret')", "network"),
        (b"import socket as s\ns.socket()", "network"),
        (b"import httpx\nhttpx.Client().get('secret')", "network"),
        (b"open('secret', 'w')", "file_write"),
        (b"open('secret', mode='a')", "file_write"),
        (b"open('secret', mode=mode)", "file_write"),
        (b"from pathlib import Path as P\nP('secret').write_text('secret')", "file_write"),
        (b"import os as o\no.getenv('SECRET')", "environment"),
        (b"from os import environ as env\nvalue = env['SECRET']", "environment"),
        (b"import importlib as i\ni.import_module('secret')", "import"),
        (b"__import__('secret')", "import"),
    ],
)
def test_simple_aliases_and_calls_emit_fixed_signals(code: bytes, category: str) -> None:
    report = scan(file("adapter.py", code))
    assert category in {finding.category for finding in report.findings}
    assert all(
        finding.member_sha256 == hashlib.sha256(code).hexdigest() for finding in report.findings
    )


def test_comments_strings_and_read_only_open_are_not_call_signals() -> None:
    report = scan(file("adapter.py", b"# eval('secret')\ns = 'os.system()'\nopen('x', 'rb')\n"))
    assert report.coverage == "complete"
    assert report.findings == ()


@pytest.mark.parametrize("code", [b"def broken(:\n", b"\xff\xfe", b"value = '\x00'"])
def test_parse_failures_are_incomplete_without_exception_details(code: bytes) -> None:
    report = scan(file("bad.py", code), file("next.py", b"eval('private-secret')"))
    assert report.coverage == "incomplete"
    assert member_rows(report)[0]["reason"] == "parse_error"
    assert any(f.path == "next.py" for f in report.findings)
    assert "private-secret" not in _canonical_json_bytes(report.body).decode()


@pytest.mark.parametrize(
    "limits",
    [
        PluginScanLimits(max_nodes=2),
        PluginScanLimits(max_depth=2),
        PluginScanLimits(max_findings=1),
    ],
)
def test_budget_exhaustion_never_looks_complete(limits: PluginScanLimits) -> None:
    report = scan(file("adapter.py", b"eval('a')\nexec('b')\n"), limits=limits)
    assert report.coverage == "incomplete"
    assert len(report.findings) <= limits.max_findings


def test_node_budget_is_shared_across_members() -> None:
    report = scan(
        file("a.py", b"pass"), file("b.py", b"pass"), limits=PluginScanLimits(max_nodes=3)
    )
    assert report.coverage == "incomplete"
    assert member_rows(report)[1]["reason"] == "node_limit"


@pytest.mark.parametrize("path", ["tool.sh", "worker.js", "module.pyc", "native.pyd", "unknown"])
def test_unsupported_members_are_explicitly_uncovered(path: str) -> None:
    report = scan(file(path, b"uninspected executable payload"))
    assert report.coverage == "incomplete"
    assert report.findings == ()
    assert (
        next(row for row in member_rows(report) if row["path"] == path)["status"] == "unsupported"
    )


@pytest.mark.parametrize(
    "payload", [b"#!/bin/sh\necho secret", b"MZ\x00\x01", b"\x7fELF", b"\x00asm"]
)
def test_executable_signatures_cannot_hide_behind_document_extension(payload: bytes) -> None:
    assert scan(file("readme.txt", payload)).coverage == "incomplete"


def test_document_and_data_classification_is_explicit() -> None:
    report = scan(
        file("adapter.py", b"pass"),
        file("README.md", b"# Example"),
        file("data/values.csv", b"x,y\n1,2"),
    )
    assert report.coverage == "complete"
    assert {row["status"] for row in member_rows(report) if row["path"] != "adapter.py"} == {
        "nonexecuting"
    }
    assert "not proof" in str(report.body["limitations"])


def test_empty_coverage_is_incomplete() -> None:
    assert scan().coverage == "incomplete"


def test_stable_hash_order_and_observation_time_exclusion() -> None:
    files = (file("z.py", b"eval('secret')"), file("a.py", b"import os\nos.getenv('secret')"))
    original = subject(files)
    first = scan_source(files, original)
    observed = replace(
        original,
        verified_package=replace(
            original.verified_package,
            verified_at=original.verified_package.verified_at + timedelta(hours=1),
            key_not_after=datetime(2027, 1, 1, tzinfo=UTC),
            members=tuple(reversed(original.verified_package.members)),
        ),
    )
    second = scan_source(tuple(reversed(files)), observed)
    assert first == second
    assert first.report_sha256 == hashlib.sha256(_canonical_json_bytes(first.body)).hexdigest()
    assert [f.path for f in first.findings] == ["a.py", "a.py", "z.py"]
    assert b"verified_at" not in _canonical_json_bytes(first.body)
    assert b"key_not_after" not in _canonical_json_bytes(first.body)


@pytest.mark.parametrize(
    "name", [field.name for field in fields(PluginScanSubject) if field.name != "verified_package"]
)
def test_every_subject_field_is_bound_to_identity(name: str) -> None:
    files = (file("adapter.py", b"pass"),)
    original = subject(files)
    current = getattr(original, name)
    changed: Any = (
        UUID(int=42)
        if isinstance(current, UUID)
        else (current + 1 if isinstance(current, int) else "b" * 64)
    )
    assert (
        scan_source(files, replace(original, **{name: changed})).report_sha256
        != scan_source(files, original).report_sha256
    )


@pytest.mark.parametrize(
    "name",
    [
        "plugin_id",
        "plugin_version",
        "source_manifest_sha256",
        "archive_sha256",
        "signature_payload_sha256",
        "key_id",
        "public_key_sha256",
        "frozen_manifest_sha256",
    ],
)
def test_package_identity_fields_are_bound(name: str) -> None:
    files = (file("adapter.py", b"pass"),)
    original = subject(files)
    changes: dict[str, Any] = {name: "b" * 64}
    changed = replace(original, verified_package=replace(original.verified_package, **changes))
    assert scan_source(files, changed).report_sha256 != scan_source(files, original).report_sha256


def test_rules_parser_and_limits_are_bound(monkeypatch: pytest.MonkeyPatch) -> None:
    from agent_hub.plugins import source_scanner

    files = (file("adapter.py", b"pass"),)
    baseline = scan(*files)
    assert (
        scan(*files, limits=PluginScanLimits(max_nodes=999)).report_sha256 != baseline.report_sha256
    )
    monkeypatch.setattr(source_scanner, "RULESET_ID", "plugin-python-signals-test")
    changed = scan(*files)
    assert changed.report_sha256 != baseline.report_sha256
    monkeypatch.setattr(source_scanner, "PARSER_ID", "different-parser")
    assert scan(*files).report_sha256 != changed.report_sha256


def test_frozen_unsigned_manifest_is_not_compared_to_signed_manifest_digest() -> None:
    files = (manifest_file(), file("adapter.py", b"pass"))
    assert scan(*files).coverage == "complete"


@pytest.mark.parametrize("mutation", ["missing", "extra", "hash", "size", "frozen_manifest"])
def test_member_binding_mismatches_are_incomplete(mutation: str) -> None:
    files: tuple[SnapshotFile, ...] = (file("plugin.json", b"{}"), file("adapter.py", b"pass"))
    identity = subject(files)
    if mutation == "missing":
        files = files[:1]
    elif mutation == "extra":
        files = (*files, file("hidden.py", b"pass"))
    elif mutation == "hash":
        files = (files[0], replace(files[1], content=b"evil"))
    elif mutation == "size":
        files = (files[0], replace(files[1], size_bytes=99))
    else:
        identity = replace(
            identity,
            verified_package=replace(identity.verified_package, frozen_manifest_sha256="f" * 64),
        )
    assert scan_source(files, identity).coverage == "incomplete"


@pytest.mark.parametrize("name", ["max_nodes", "max_depth", "max_findings"])
@pytest.mark.parametrize("value", [True, False, 0, -1, 1.5, "3", 2**63])
def test_invalid_scalar_limits_are_rejected(name: str, value: object) -> None:
    with pytest.raises(ValueError, match="limits"):
        PluginScanLimits(**{name: cast(int, value)})


@pytest.mark.parametrize(
    "name,value",
    [
        ("max_nodes", 100_001),
        ("max_depth", 65),
        ("max_findings", 257),
        ("max_nodes", 2**63 - 1),
        ("max_depth", 2**63 - 1),
        ("max_findings", 2**63 - 1),
    ],
)
def test_trusted_limits_can_increase_up_to_int64(name: str, value: int) -> None:
    report = scan(file("adapter.py", b"pass"), limits=PluginScanLimits(**{name: value}))
    assert report.coverage == "complete"
    assert cast(Mapping[str, JsonValue], report.body["limits"])[name] == value


@pytest.mark.parametrize(
    "code,name,value,reason,expected_findings",
    [
        (b"pass\n" * 100_000, "max_nodes", 100_001, "node_limit", 0),
        (b"x = " + b"-" * 62 + b"1", "max_depth", 65, "depth_limit", 0),
        (b"eval('PRIVATE_CONSTANT')\n" * 257, "max_findings", 257, "finding_limit", 257),
    ],
    ids=["nodes", "depth", "findings"],
)
def test_default_exhaustion_can_be_resolved_by_explicit_increase(
    code: bytes, name: str, value: int, reason: str, expected_findings: int
) -> None:
    source = file("adapter.py", code)
    default = scan(source)
    assert default.coverage == "incomplete"
    assert reason in cast(tuple[JsonValue, ...], default.body["incomplete_reasons"])
    increased = scan(source, limits=PluginScanLimits(**{name: value}))
    assert increased.coverage == "complete"
    assert len(increased.findings) == expected_findings


def test_expanded_findings_report_freezes_stably_without_secret_echo() -> None:
    source = file("adapter.py", b"eval('PRIVATE_CONSTANT')\n" * 1_500)
    limits = PluginScanLimits(max_findings=1_500)
    first = scan(source, limits=limits)
    second = scan(source, limits=limits)
    assert first.coverage == "complete"
    assert len(first.findings) == 1_500
    assert first == second
    assert replace(first, body=dict(first.body)) == first
    encoded = _canonical_json_bytes(first.body)
    assert hashlib.sha256(encoded).hexdigest() == first.report_sha256
    assert b"PRIVATE_CONSTANT" not in encoded


def test_existing_json_report_byte_limit_produces_explicit_incomplete() -> None:
    from agent_hub.runtime.contracts import _MAX_JSON_BYTES

    source = file("adapter.py", b"eval('PRIVATE_CONSTANT')\n" * 12_000)
    report = scan(source, limits=PluginScanLimits(max_findings=12_000))
    assert report.coverage == "incomplete"
    assert "report_byte_limit" in cast(tuple[JsonValue, ...], report.body["incomplete_reasons"])
    assert 256 < len(report.findings) < 12_000
    rows = cast(tuple[JsonValue, ...], report.body["findings"])
    assert len(rows) == len(report.findings)
    encoded = _canonical_json_bytes(report.body)
    assert len(encoded) <= _MAX_JSON_BYTES
    assert b"PRIVATE_CONSTANT" not in encoded


def test_unverified_body_limits_cannot_expand_report_freeze_budget() -> None:
    report = scan(file("adapter.py", b"eval('PRIVATE_CONSTANT')"))
    body = dict(report.body)
    body["findings"] = cast(tuple[JsonValue, ...], body["findings"]) * 1_500
    body["limits"] = {"max_nodes": 2**63 - 1, "max_depth": 2**63 - 1, "max_findings": 2**63 - 1}
    with pytest.raises(ValueError, match="structural limits"):
        replace(report, body=body)


def test_raised_scan_limits_preserve_source_safety_boundaries() -> None:
    limits = PluginScanLimits(max_nodes=2**63 - 1, max_depth=2**63 - 1, max_findings=2**63 - 1)
    large = scan(file("large.py", b" " * 512_001), limits=limits)
    assert large.coverage == "incomplete"
    assert member_rows(large)[0]["reason"] == "source_byte_limit"
    many = scan(*(file(f"part{i:03}.py", b"pass") for i in range(201)), limits=limits)
    assert many.coverage == "incomplete"
    assert "source_file_limit" in cast(tuple[JsonValue, ...], many.body["incomplete_reasons"])


def test_report_and_findings_are_deeply_immutable() -> None:
    report = scan(file("adapter.py", b"eval('secret')"))
    with pytest.raises(FrozenInstanceError):
        report.coverage = "incomplete"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        report.findings[0].line = 42  # type: ignore[misc]
    with pytest.raises(TypeError):
        cast(dict[str, JsonValue], report.body)["coverage"] = "incomplete"
    with pytest.raises(TypeError):
        cast(dict[str, JsonValue], member_rows(report)[0])["path"] = "changed"
    caller_body = cast(dict[str, JsonValue], _mutable_json(report.body))
    detached = replace(report, body=caller_body)
    caller_body["limitations"] = "changed"
    assert detached.body["limitations"] != "changed"
    assert isinstance(detached.findings[0], PluginSourceFinding)


def test_report_has_only_fixed_explanations_no_source_constants_or_error_text() -> None:
    report = scan(
        file("adapter.py", b"import os\nos.system('PRIVATE_CONSTANT')\n"),
        file("bad.py", b"def PRIVATE_SYNTAX_SECRET(:"),
    )
    serialized = json.dumps(_mutable_json(report.body))
    assert "PRIVATE_CONSTANT" not in serialized
    assert "PRIVATE_SYNTAX_SECRET" not in serialized
    assert "invalid syntax" not in serialized
    rows = report.body["findings"]
    assert isinstance(rows, tuple)
    assert all(isinstance(row, Mapping) and isinstance(row["explanation"], str) for row in rows)


def test_existing_source_byte_budgets_are_applied_before_parsing() -> None:
    report = scan(file("large.py", b" " * 512_001))
    assert report.coverage == "incomplete"
    assert report.findings == ()


@pytest.mark.parametrize(
    "code",
    [
        b"from pathlib import Path\nPath('secret').open('w')",
        b"from pathlib import Path\np = Path('secret')\np.open('a')",
        b"open(*args)",
    ],
)
def test_path_open_and_unpacked_open_cannot_hide_possible_writes(code: bytes) -> None:
    report = scan(file("adapter.py", code))
    assert "file_write" in {finding.category for finding in report.findings}


def test_source_file_count_budget_is_incomplete() -> None:
    report = scan(*(file(f"part{i:03}.py", b"pass") for i in range(201)))
    assert report.coverage == "incomplete"
    assert "source_file_limit" in cast(tuple[JsonValue, ...], report.body["incomplete_reasons"])


@pytest.mark.parametrize("error", [RecursionError, MemoryError])
def test_parser_resource_failure_is_sanitized_incomplete(
    monkeypatch: pytest.MonkeyPatch, error: type[Exception]
) -> None:
    def fail(*args: object, **kwargs: object) -> object:
        raise error("PRIVATE_PARSER_DETAIL")

    with monkeypatch.context() as scoped:
        scoped.setattr(ast, "parse", fail)
        report = scan(file("adapter.py", b"pass"))
    assert report.coverage == "incomplete"
    assert report.findings == ()
    assert "PRIVATE_PARSER_DETAIL" not in _canonical_json_bytes(report.body).decode()


@pytest.mark.parametrize("entrypoint", ["main.txt", "main", "main.json", "nested/main.csv"])
@pytest.mark.parametrize("dependencies", [False, True], ids=["direct", "dependency-bootstrap"])
def test_python_entrypoints_cannot_be_classified_as_documents(
    entrypoint: str, dependencies: bool
) -> None:
    files = (
        manifest_file(entrypoint, dependencies=dependencies),
        file(entrypoint, b"import subprocess\nsubprocess.run(['PRIVATE_CONSTANT'])"),
    )
    report = scan_source(files, subject(files))
    assert report.coverage == "incomplete"
    row = next(row for row in member_rows(report) if row["path"] == entrypoint)
    assert row["status"] == "unsupported"
    assert row["reason"] == "unsupported_python_entrypoint"
    assert "unsupported_python_entrypoint" in cast(
        tuple[JsonValue, ...], report.body["incomplete_reasons"]
    )
    assert b"PRIVATE_CONSTANT" not in _canonical_json_bytes(report.body)


@pytest.mark.parametrize("dependencies", [False, True])
def test_python_source_entrypoint_and_dynamic_import_remain_scanned(dependencies: bool) -> None:
    report = scan(
        manifest_file("main.py", dependencies=dependencies),
        file("main.py", b"__import__('PRIVATE_DEPENDENCY')"),
    )
    assert report.coverage == "complete"
    row = next(row for row in member_rows(report) if row["path"] == "main.py")
    assert row["status"] == "scanned"
    assert [finding.rule_id for finding in report.findings] == ["PY002"]
    assert "Dependencies" in str(report.body["limitations"])
    assert b"PRIVATE_DEPENDENCY" not in _canonical_json_bytes(report.body)


@pytest.mark.parametrize("runtime", ["node", "container", "mcp_remote", "none"])
def test_non_python_runtime_cannot_claim_complete_python_coverage(runtime: str) -> None:
    report = scan(manifest_file("main.txt", runtime=runtime), file("main.txt", b"PRIVATE_CONSTANT"))
    assert report.coverage == "incomplete"
    row = next(row for row in member_rows(report) if row["path"] == "main.txt")
    assert row["status"] == "unsupported"
    assert row["reason"] == "unsupported_runtime"


@pytest.mark.parametrize(
    "raw",
    [
        b"{}",
        b"[]",
        b"{PRIVATE_SYNTAX",
        b'{"id":"a","id":"b"}',
        manifest_file().content.replace(b'"entrypoint": "adapter.py"', b'"entrypoint": null'),
    ],
)
def test_missing_or_invalid_execution_metadata_is_incomplete(raw: bytes) -> None:
    report = scan(file("plugin.json", raw), file("adapter.py", b"pass"))
    assert report.coverage == "incomplete"
    assert b"PRIVATE_SYNTAX" not in _canonical_json_bytes(report.body)


def test_absent_manifest_cannot_prove_execution_coverage() -> None:
    files = (file("adapter.py", b"pass"),)
    assert scan_source(files, subject(files)).coverage == "incomplete"


@pytest.mark.parametrize("field", ["id", "version"])
def test_manifest_identity_must_match_exact_verified_package(field: str) -> None:
    raw = cast(dict[str, object], json.loads(manifest_file().content))
    raw[field] = "other"
    files = (file("plugin.json", json.dumps(raw).encode()), file("adapter.py", b"pass"))
    report = scan_source(files, subject(files))
    assert report.coverage == "incomplete"
    assert "manifest_identity_mismatch" in cast(
        tuple[JsonValue, ...], report.body["incomplete_reasons"]
    )


def test_unbound_manifest_cannot_select_a_document_as_safe() -> None:
    original = (manifest_file("main.txt"), file("main.txt", b"pass"), file("adapter.py", b"pass"))
    changed = (manifest_file("adapter.py"), *original[1:])
    report = scan_source(changed, subject(original))
    assert report.coverage == "incomplete"
    row = next(row for row in member_rows(report) if row["path"] == "main.txt")
    assert row["status"] != "nonexecuting"


@pytest.mark.parametrize("entrypoint", ["main.txt", "main", "main.json"])
def test_real_verified_signed_non_python_suffix_entrypoint_is_incomplete(
    tmp_path: Path, entrypoint: str
) -> None:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    from agent_hub.plugins.handover_sources import FrozenSourceSnapshot
    from agent_hub.plugins.handover_storage import AuthorizedSource, WorkspaceSource
    from agent_hub.plugins.handover_verification import (
        FrozenSigningKey,
        verify_signed_snapshot_package,
    )
    from agent_hub.plugins.package_builder import build_signed_plugin_archive

    raw = cast(dict[str, object], json.loads(manifest_file(entrypoint).content))
    cast(dict[str, object], raw["package"])["signature"] = {
        "algorithm": "ed25519",
        "key_id": "key-1",
        "value": "A" * 86,
    }
    files = tuple(
        sorted(
            (
                file("plugin.json", json.dumps(raw).encode()),
                file(entrypoint, b"import subprocess\nsubprocess.run(['PRIVATE_CONSTANT'])"),
            ),
            key=lambda item: item.path,
        )
    )
    identity = subject(files)
    for item in files:
        (tmp_path / item.path).write_bytes(item.content)
    private = Ed25519PrivateKey.generate()
    archive = tmp_path / "signed.zip"
    build_signed_plugin_archive(
        source_dir=tmp_path, output_path=archive, private_key=private, key_id="key-1"
    )
    snapshot = FrozenSourceSnapshot(
        authorized=AuthorizedSource(
            tenant_id=identity.tenant_id,
            run_id=identity.run_id,
            source=WorkspaceSource(
                project_id="project-a",
                session_id="session-a",
                paths=tuple(item.path for item in files),
            ),
            source_manifest_sha256=identity.verified_package.source_manifest_sha256,
        ),
        files=files,
        plugin_id="example",
        plugin_version="1.0.0",
        total_size_bytes=sum(item.size_bytes for item in files),
    )
    facts = verify_signed_snapshot_package(
        snapshot=snapshot,
        archive_bytes=archive.read_bytes(),
        key=FrozenSigningKey(
            key_id="key-1",
            algorithm="ed25519",
            public_key=private.public_key().public_bytes_raw(),
            trusted=True,
        ),
        now=identity.verified_package.verified_at,
    )
    report = scan_source(files, replace(identity, verified_package=facts))
    assert report.coverage == "incomplete"
    row = next(row for row in member_rows(report) if row["path"] == entrypoint)
    assert row["status"] == "unsupported"
