"""File evidence must be read and decoded, never trusted from caller flags."""
from __future__ import annotations

import hashlib
import importlib
import os
import sys
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from PIL import Image, ImageDraw


def _module() -> Any:
    return importlib.import_module("agent_hub.harness.browser_evidence")


def _descriptor(path: str, data: bytes) -> dict[str, object]:
    return {"path": path, "size_bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}


def _png(*, visible: bool = True, transparent: bool = False) -> bytes:
    image = Image.new("RGBA", (8, 6), (255, 255, 255, 0 if transparent else 255))
    if visible:
        ImageDraw.Draw(image).rectangle((0, 0, 3, 5), fill=(10, 50, 90, 0 if transparent else 255))
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def test_read_evidence_checks_actual_bytes_and_descriptor(tmp_path: Path) -> None:
    data = b"operator-owned observation"
    (tmp_path / "trace.json").write_bytes(data)
    descriptor = _descriptor("trace.json", data)
    assert _module()._read_evidence_file(tmp_path, descriptor, max_bytes=128) == data


@pytest.mark.skipif(sys.platform != "win32", reason="Windows pathname/handle ctime semantics")
def test_read_evidence_accepts_stable_distinct_windows_ctime_sources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    data = b"stable native Windows opened object"
    (tmp_path / "trace.json").write_bytes(data)
    fstat = os.fstat

    def distinct_handle_ctime(fd: int) -> Any:
        info = fstat(fd)
        return SimpleNamespace(st_dev=info.st_dev, st_ino=info.st_ino, st_size=info.st_size,
                               st_mtime_ns=info.st_mtime_ns, st_ctime_ns=info.st_ctime_ns + 1,
                               st_nlink=info.st_nlink, st_mode=info.st_mode,
                               st_file_attributes=getattr(info, "st_file_attributes", 0))

    monkeypatch.setattr(os, "fstat", distinct_handle_ctime)
    assert _module()._read_evidence_file(
        tmp_path, _descriptor("trace.json", data), max_bytes=128,
    ) == data


@pytest.mark.parametrize("path", ["", ".", "../x", "/x", "a\\x", "a//x", "a/./x", "C:/x"])
def test_read_evidence_rejects_noncanonical_paths(tmp_path: Path, path: str) -> None:
    with pytest.raises(ValueError):
        _module()._read_evidence_file(tmp_path, _descriptor(path, b"x"), max_bytes=128)


@pytest.mark.parametrize("change", ["size", "digest", "extra", "bool", "budget", "missing"])
def test_read_evidence_rejects_false_metadata(tmp_path: Path, change: str) -> None:
    data = b"actual"
    (tmp_path / "trace.json").write_bytes(data)
    descriptor = _descriptor("trace.json", data)
    maximum = 128
    if change == "size":
        descriptor["size_bytes"] = len(data) + 1
    elif change == "digest":
        descriptor["sha256"] = "0" * 64
    elif change == "extra":
        descriptor["passed"] = True
    elif change == "bool":
        descriptor["size_bytes"] = True
    elif change == "budget":
        maximum = len(data) - 1
    else:
        (tmp_path / "trace.json").unlink()
    with pytest.raises(ValueError):
        _module()._read_evidence_file(tmp_path, descriptor, max_bytes=maximum)


def test_read_evidence_rejects_actual_hardlink(tmp_path: Path) -> None:
    data = b"same bytes do not grant ownership"
    source = tmp_path / "original.json"
    source.write_bytes(data)
    os.link(source, tmp_path / "alias.json")
    with pytest.raises(ValueError):
        _module()._read_evidence_file(tmp_path, _descriptor("alias.json", data), max_bytes=128)
    assert source.read_bytes() == data


def test_read_evidence_rejects_path_replacement_during_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    data = b"unchanged bytes, foreign inode"
    source = tmp_path / "trace.json"
    replacement = tmp_path / "replacement.json"
    source.write_bytes(data)
    replacement.write_bytes(data)
    read = os.read
    replaced = False

    def replacing_read(fd: int, size: int) -> bytes:
        nonlocal replaced
        result = read(fd, size)
        if not replaced:
            replaced = True
            replacement.replace(source)
        return result

    monkeypatch.setattr(os, "read", replacing_read)
    with pytest.raises(ValueError):
        _module()._read_evidence_file(tmp_path, _descriptor("trace.json", data), max_bytes=128)


@pytest.mark.parametrize("data", [b'{"x":1,"x":2}', b'{"x":{"y":1,"y":2}}', b'{"x":NaN}', b'{"x":Infinity}', b'{"x":1e999}', b'{"x":[-1e999]}', b'[]', b'\xff'])
def test_evidence_json_rejects_ambiguous_or_invalid_documents(data: bytes) -> None:
    with pytest.raises(ValueError):
        _module()._parse_json_evidence(data)


def test_evidence_json_reads_a_real_object() -> None:
    assert _module()._parse_json_evidence(b'{"actual":[1,"observed"]}') == {"actual": [1, "observed"]}


def test_png_metrics_derive_from_actual_pixels() -> None:
    data = _png()
    assert _module()._png_observation(data, width=8, height=6) == {
        "width": 8, "height": 6, "sha256": hashlib.sha256(data).hexdigest(), "nonblank": True,
    }


@pytest.mark.parametrize("case", ["uniform", "transparent", "wrong_dimensions", "bool_dimensions", "not_png", "truncated"])
def test_png_metrics_reject_false_render_proof(case: str) -> None:
    data = _png(visible=case != "uniform", transparent=case == "transparent")
    width: int = 8
    if case == "wrong_dimensions":
        width = 9
    elif case == "bool_dimensions":
        width = True
    elif case == "not_png":
        data = b"not an image"
    elif case == "truncated":
        data = data[:30]
    with pytest.raises(ValueError):
        _module()._png_observation(data, width=width, height=6)


def _scope() -> dict[str, object]:
    return {
        "execution_identity": {
            "execution_id": "synthetic-execution", "base_url": "https://operator.invalid",
            "tenant_id": "20000000-0000-0000-0000-000000000001",
            "user_id": "30000000-0000-0000-0000-000000000001",
            "model_profile": {"primary": "synthetic-profile"},
        },
        "case_id": "small:create", "project_id": "project-a", "conversation_id": "conversation-a",
        "run_id": "run-a", "workspace_session_id": "session-a",
    }


def _manifest() -> dict[str, tuple[int, str]]:
    return {"server.js": (17, hashlib.sha256(b"synthetic server!").hexdigest()),
            "index.html": (3, hashlib.sha256(b"app").hexdigest())}


def _validate(root: Path, descriptor: object, *, device: str = "desktop") -> dict[str, object]:
    validator = getattr(_module(), "validate_case_browser_bundle", None)
    assert callable(validator), "full file-backed validator missing"
    return cast(dict[str, object], validator(
        descriptor, evidence_root=root, expected_scope=_scope(),
        validated_manifest=_manifest(), device=device,
    ))


@pytest.mark.parametrize("device", ["desktop", "mobile"])
def test_complete_real_file_bundle_returns_unchanged_deep_copy(tmp_path: Path, device: str) -> None:
    import copy

    from tests.unit.harness.browser_evidence_fixture import build_device_bundle

    descriptor = build_device_bundle(tmp_path, scope=_scope(), validated_manifest=_manifest(), device=device)
    expected = copy.deepcopy(descriptor)
    result = _validate(tmp_path, descriptor, device=device)
    assert result == expected
    assert result is not descriptor
    assert result["checks"] is not descriptor["checks"]
    assert result["bundle_file"] is not descriptor["bundle_file"]


def _json_file(root: Path, reference: Any) -> Any:
    import json

    return json.loads((root / reference["path"]).read_bytes())


def _rewrite_file(root: Path, reference: Any, value: object) -> dict[str, object]:
    import json

    data = value if isinstance(value, bytes) else json.dumps(value, allow_nan=False).encode()
    (root / reference["path"]).write_bytes(data)
    return _descriptor(reference["path"], data)


def _rewrite_role(root: Path, descriptor: Any, role: str, change: Any) -> None:
    bundle = _json_file(root, descriptor["bundle_file"])
    document = _json_file(root, bundle["files"][role])
    change(document)
    bundle["files"][role] = _rewrite_file(root, bundle["files"][role], document)
    descriptor["bundle_file"] = _rewrite_file(root, descriptor["bundle_file"], bundle)


def _replace(document: Any, path: str, value: object) -> None:
    parts = path.split(".")
    for part in parts[:-1]:
        document = document[int(part)] if isinstance(document, list) else document[part]
    document[int(parts[-1]) if isinstance(document, list) else parts[-1]] = value


_ROLE_REJECTIONS: list[tuple[str, str, object]] = [
    ("render", "schema_version", True), ("render", "extra", "legacy"),
    ("render", "scope.run_id", "foreign"), ("render", "scope.execution_identity.model_profile", {}),
    ("render", "preview_identity.runtime_handle", "c" * 32),
    ("render", "device", "mobile"), ("render", "viewport.width", 1441),
    ("render", "viewport.height", True), ("render", "observed_at", "2026-10-05T00:00:00"),
    ("render", "observed_at", "2000-01-01T00:00:00+00:00"),
    ("render", "content_url_sha256", "X" * 64), ("render", "title", " "),
    ("render", "content_text", ""), ("render", "content_text", "x" * 16385),
    ("render", "visible_controls", []), ("render", "visible_controls.0.selector", ""),
    ("render", "visible_controls.0.text", ""),
    ("render", "visible_controls.0.bounds.x", -1),
    ("render", "visible_controls.0.bounds.y", 1000),
    ("render", "visible_controls.0.bounds.width", 0),
    ("render", "visible_controls.0.bounds.height", True),
    ("render", "console_errors", ["crash"]), ("render", "page_errors", ["page failed"]),
    ("render", "assets", [{"path": "img/a.png", "resource_type": "image", "status": 404, "loaded": True}]),
    ("render", "assets", [{"path": "img/a.png", "resource_type": "image", "status": 200, "loaded": False}]),
    ("render", "assets", [{"path": "https://foreign.invalid/a", "resource_type": "image", "status": 200, "loaded": True}]),
    ("render", "assets", [{"path": "../a", "resource_type": "image", "status": 200, "loaded": True}]),
    ("business", "schema_version", 1.0), ("business", "scope.case_id", "small:other"),
    ("business", "preview_identity.preview_id", "10000000-0000-0000-0000-000000000002"),
    ("business", "unique_value", "preexisting-value"),
    ("business", "unique_value", ""), ("business", "selectors.success", []),
    ("business", "selectors.success", [0]), ("business", "selectors.id_field", ""),
    ("business", "selectors.before_records", ["missing"]),
    ("business", "selectors.mutation_record", ["success"]),
    ("business", "before.method", "POST"), ("business", "before.status", 302),
    ("business", "before.path", "/api/records"), ("business", "before.path", "api//records"),
    ("business", "before.path", "api/%2e%2e/secret"),
    ("business", "before.body.records", {}), ("business", "before.body.records", ["input echo"]),
    ("business", "mutation.method", "GET"), ("business", "mutation.status", 400),
    ("business", "mutation.status", True), ("business", "mutation.body.success", False),
    ("business", "mutation.body.success", 1), ("business", "mutation.input_value", "echo"),
    ("business", "mutation.control.visible", False),
    ("business", "mutation.control.selector", "#foreign-control"),
    ("business", "mutation.body.record.id", ""),
    ("business", "readback.method", "POST"), ("business", "readback.cache", "default"),
    ("business", "readback.after_reload", False), ("business", "readback.status", 304),
    ("business", "readback.body.success", False),
    ("business", "readback.body.record.id", "foreign-record"),
    ("business", "readback.body.record.value", "input echo"),
    ("business", "readback.observed_at", "2000-01-01T00:00:00+00:00"),
    ("provenance", "identity.user_id", "30000000-0000-0000-0000-000000000002"),
    ("provenance", "snapshot_manifest.manifest_sha256", "0" * 64),
    ("provenance", "snapshot_manifest.file_count", 1),
    ("provenance", "snapshot_manifest.total_bytes", 21),
    ("provenance", "snapshot_manifest.selection_policy", "static-display-root-v1"),
    ("provenance", "captured_at", "2100-01-01T00:00:00+00:00"),
    ("cleanup", "preview_identity.source.sha256", "0" * 64),
    ("cleanup", "cleanup_record.identity.project_id", "foreign-project"),
    ("cleanup", "cleanup_record.cleanup_receipt", None),
    ("cleanup", "cleanup_record.cleanup_receipt.schema_version", True),
    ("cleanup", "cleanup_record.cleanup_receipt.status", "unknown"),
    ("cleanup", "cleanup_record.cleanup_receipt.coverage", "unknown"),
    ("cleanup", "cleanup_record.cleanup_receipt.unobserved", []),
    ("cleanup", "cleanup_record.cleanup_receipt.observations", []),
    ("cleanup", "cleanup_record.cleanup_receipt.observations.0.observed_at", "2000-01-01T00:00:00+00:00"),
    ("cleanup", "cleanup_record.cleanup_receipt.observations.0.reason_code", "not_attempted"),
    ("cleanup", "cleanup_record.cleanup_receipt.observations.0.result", "unknown"),
    ("cleanup", "cleanup_record.cleanup_receipt.requested_at", "2000-01-01T00:00:00+00:00"),
    ("cleanup", "revocation.method", "POST"), ("cleanup", "revocation.status", 200),
    ("cleanup", "revocation.content_url_sha256", "0" * 64),
    ("cleanup", "revocation.observed_at", "2000-01-01T00:00:00+00:00"),
]


@pytest.mark.parametrize("role,path,value", _ROLE_REJECTIONS,
                         ids=[f"{role}-{path}-{index}" for index, (role, path, _) in enumerate(_ROLE_REJECTIONS)])
def test_bundle_rejects_file_backed_false_observations(
    tmp_path: Path, role: str, path: str, value: object,
) -> None:
    from tests.unit.harness.browser_evidence_fixture import build_device_bundle

    descriptor = build_device_bundle(tmp_path, scope=_scope(), validated_manifest=_manifest(), device="desktop")
    _rewrite_role(tmp_path, descriptor, role, lambda doc: _replace(doc, path, value))
    with pytest.raises(ValueError):
        _validate(tmp_path, descriptor)


@pytest.mark.parametrize("case", [
    "preexisting", "stale_readback", "scope_replay", "owner_replay", "profile_replay",
    "manifest", "manifest_empty", "device", "bundle_legacy", "descriptor_legacy",
    "tamper_bundle", "tamper_render", "tamper_business", "tamper_provenance", "tamper_cleanup",
    "tamper_viewport_png", "path_role_alias", "missing_file", "hardlink", "root_escape",
    "uniform_png", "wrong_dimensions", "transparent_png", "broken_png", "frame_missing",
    "bundle_budget", "json_budget", "assets_budget", "controls_budget", "body_records_budget",
    "duplicate_json", "nonfinite_json", "descriptor_extra", "descriptor_bool", "checks_extra",
    "checks_false", "wrong_evidence_ref", "descriptor_stale", "static_preview",
])
def test_bundle_rejects_replay_alias_tamper_and_exhaustion(tmp_path: Path, case: str) -> None:
    import copy

    from tests.unit.harness.browser_evidence_fixture import build_device_bundle

    descriptor: Any = build_device_bundle(tmp_path, scope=_scope(), validated_manifest=_manifest(), device="desktop")
    bundle = _json_file(tmp_path, descriptor["bundle_file"])
    expected_scope: Any = _scope()
    manifest = _manifest()
    device = "desktop"
    if case == "preexisting":
        def preexisting(doc: Any) -> None:
            doc["before"]["body"]["records"] = [{"id": "old", "value": doc["unique_value"]}]
        _rewrite_role(tmp_path, descriptor, "business", preexisting)
    elif case == "stale_readback":
        def stale(doc: Any) -> None:
            doc["readback"]["observed_at"] = doc["mutation"]["observed_at"]
        _rewrite_role(tmp_path, descriptor, "business", stale)
    elif case == "scope_replay":
        expected_scope["run_id"] = "different-run"
    elif case == "owner_replay":
        expected_scope["execution_identity"]["tenant_id"] = "20000000-0000-0000-0000-000000000002"
    elif case == "profile_replay":
        expected_scope["execution_identity"]["model_profile"] = {"primary": "other"}
    elif case == "manifest":
        manifest["extra.js"] = (1, "a" * 64)
    elif case == "manifest_empty":
        manifest = {}
    elif case == "device":
        device = "mobile"
    elif case.startswith("tamper_"):
        role = case.removeprefix("tamper_")
        ref = descriptor["bundle_file"] if role == "bundle" else bundle["files"][role]
        target = tmp_path / ref["path"]
        target.write_bytes(target.read_bytes() + b" ")
    elif case == "path_role_alias":
        bundle["files"]["business"] = copy.deepcopy(bundle["files"]["render"])
    elif case == "missing_file":
        (tmp_path / bundle["files"]["render"]["path"]).unlink()
    elif case == "hardlink":
        target = tmp_path / bundle["files"]["render"]["path"]
        os.link(target, tmp_path / "render-alias")
    elif case == "root_escape":
        bundle["files"]["render"]["path"] = "../foreign.json"
    elif case in {"uniform_png", "wrong_dimensions", "transparent_png", "broken_png"}:
        size = (1439, 960) if case == "wrong_dimensions" else (1440, 960)
        with Image.new("RGBA", size, (255, 255, 255, 0 if case == "transparent_png" else 255)) as image:
            if case != "uniform_png":
                ImageDraw.Draw(image).rectangle((0, 0, 700, 959), fill=(10, 30, 90, 0 if case == "transparent_png" else 255))
            output = BytesIO()
            image.save(output, format="PNG")
        data = b"not PNG" if case == "broken_png" else output.getvalue()
        bundle["files"]["viewport_png"] = _rewrite_file(tmp_path, bundle["files"]["viewport_png"], data)
    elif case == "frame_missing":
        _rewrite_role(tmp_path, descriptor, "render", lambda doc: doc.update(frame={
            "bounds": {"x": 0, "y": 0, "width": 300, "height": 400}, "width": 300, "height": 400,
        }))
    elif case == "bundle_budget":
        descriptor["bundle_file"] = _rewrite_file(tmp_path, descriptor["bundle_file"], b" " * 65537)
    elif case == "json_budget":
        bundle["files"]["render"] = _rewrite_file(tmp_path, bundle["files"]["render"], b" " * (2 * 1024 * 1024 + 1))
    elif case in {"assets_budget", "controls_budget", "body_records_budget"}:
        if case == "assets_budget":
            value: Any = [{"path": "app.js", "resource_type": "script", "status": 200, "loaded": True}] * 513
            _rewrite_role(tmp_path, descriptor, "render", lambda doc: doc.update(assets=value))
        elif case == "controls_budget":
            _rewrite_role(tmp_path, descriptor, "render", lambda doc: doc.update(visible_controls=doc["visible_controls"] * 257))
        else:
            _rewrite_role(tmp_path, descriptor, "business", lambda doc: doc["before"]["body"].update(records=[{}] * 4097))
    elif case in {"duplicate_json", "nonfinite_json"}:
        data = b'{"schema_version":1,"schema_version":1}' if case == "duplicate_json" else b'{"bad":1e999}'
        bundle["files"]["business"] = _rewrite_file(tmp_path, bundle["files"]["business"], data)
    elif case == "descriptor_legacy":
        descriptor["schema_version"] = 1
    elif case == "bundle_legacy":
        bundle["schema_version"] = 1
    elif case == "descriptor_extra":
        descriptor["legacy"] = True
    elif case == "descriptor_bool":
        descriptor["schema_version"] = True
    elif case == "checks_extra":
        descriptor["checks"]["legacy"] = True
    elif case == "checks_false":
        descriptor["checks"]["preview_interaction"] = False
    elif case == "wrong_evidence_ref":
        descriptor["evidence_ref"] = "other/bundle.json"
    elif case == "descriptor_stale":
        descriptor["observed_at"] = "2000-01-01T00:00:00+00:00"
    elif case == "static_preview":
        bundle["preview_identity"].update(kind="static", runtime_handle=None)
        bundle["preview_identity"]["source"]["scheme"] = "preview-static-tree-v1"
    if case in {"path_role_alias", "root_escape", "uniform_png", "wrong_dimensions", "transparent_png",
                "broken_png", "json_budget", "duplicate_json", "nonfinite_json", "bundle_legacy", "static_preview"}:
        descriptor["bundle_file"] = _rewrite_file(tmp_path, descriptor["bundle_file"], bundle)
    validator = getattr(_module(), "validate_case_browser_bundle", None)
    assert callable(validator), "full file-backed validator missing"
    with pytest.raises(ValueError):
        validator(descriptor, evidence_root=tmp_path, expected_scope=expected_scope,
                  validated_manifest=manifest, device=device)


@pytest.mark.parametrize("device", ["desktop", "mobile"])
def test_framed_bundle_decodes_frame_separately_from_viewport(tmp_path: Path, device: str) -> None:
    from tests.unit.harness.browser_evidence_fixture import build_device_bundle

    descriptor: Any = build_device_bundle(tmp_path, scope=_scope(), validated_manifest=_manifest(), device=device)
    bundle = _json_file(tmp_path, descriptor["bundle_file"])
    reference = {"path": f"case-{device}/frame.png"}
    with Image.new("RGB", (300, 400), "white") as image:
        ImageDraw.Draw(image).rectangle((0, 0, 149, 399), fill="green")
        output = BytesIO()
        image.save(output, format="PNG")
    bundle["files"]["frame_png"] = _rewrite_file(tmp_path, reference, output.getvalue())
    descriptor["bundle_file"] = _rewrite_file(tmp_path, descriptor["bundle_file"], bundle)
    _rewrite_role(tmp_path, descriptor, "render", lambda doc: doc.update(frame={
        "bounds": {"x": 20, "y": 20, "width": 299.5, "height": 400}, "width": 300, "height": 400,
    }))
    assert _validate(tmp_path, descriptor, device=device) == descriptor


@pytest.mark.parametrize("case", ["wrong_dimensions", "blank", "unframed", "overflow", "tamper"])
def test_frame_requires_its_own_matching_nonblank_file(tmp_path: Path, case: str) -> None:
    from tests.unit.harness.browser_evidence_fixture import build_device_bundle

    descriptor: Any = build_device_bundle(tmp_path, scope=_scope(), validated_manifest=_manifest(), device="desktop")
    bundle = _json_file(tmp_path, descriptor["bundle_file"])
    reference = {"path": "case-desktop/frame.png"}
    with Image.new("RGB", (300, 400), "white") as image:
        if case != "blank":
            ImageDraw.Draw(image).rectangle((0, 0, 149, 399), fill="green")
        output = BytesIO()
        image.save(output, format="PNG")
    bundle["files"]["frame_png"] = _rewrite_file(tmp_path, reference, output.getvalue())
    descriptor["bundle_file"] = _rewrite_file(tmp_path, descriptor["bundle_file"], bundle)
    if case != "unframed":
        _rewrite_role(tmp_path, descriptor, "render", lambda doc: doc.update(frame={
            "bounds": {"x": 1400 if case == "overflow" else 20, "y": 20, "width": 300, "height": 400},
            "width": 301 if case == "wrong_dimensions" else 300, "height": 400,
        }))
    if case == "tamper":
        (tmp_path / reference["path"]).write_bytes(b"tampered")
    with pytest.raises(ValueError):
        _validate(tmp_path, descriptor)


@pytest.mark.parametrize("case", ["root_before_array", "nested_selectors", "asset_304", "manifest_exclusions"])
def test_bundle_accepts_supported_wire_variants(tmp_path: Path, case: str) -> None:
    from tests.unit.harness.browser_evidence_fixture import build_device_bundle

    manifest = _manifest()
    if case == "manifest_exclusions":
        manifest["nested/node_modules/ignored.js"] = (8, "a" * 64)
        manifest["nested/.env.local"] = (4, "b" * 64)
    descriptor: Any = build_device_bundle(tmp_path, scope=_scope(), validated_manifest=manifest, device="desktop")
    if case == "root_before_array":
        def root_array(doc: Any) -> None:
            doc["selectors"]["before_records"] = []
            doc["selectors"]["before_success"] = None
            doc["before"]["body"] = []
        _rewrite_role(tmp_path, descriptor, "business", root_array)
    elif case == "nested_selectors":
        def nested(doc: Any) -> None:
            for trace in ("before", "mutation", "readback"):
                doc[trace]["body"] = {"data": doc[trace]["body"]}
            for key in ("before_records", "before_success", "mutation_record", "readback_record", "success"):
                doc["selectors"][key].insert(0, "data")
        _rewrite_role(tmp_path, descriptor, "business", nested)
    elif case == "asset_304":
        _rewrite_role(tmp_path, descriptor, "render", lambda doc: doc.update(assets=[{
            "path": "assets/app.js", "resource_type": "script", "status": 304, "loaded": True,
        }]))
    assert _validate(tmp_path, descriptor) == descriptor


@pytest.mark.parametrize("case", ["deep_body", "wide_body", "large_body", "large_png", "selector_depth", "error_type", "file_role_null"])
def test_bundle_resource_limits_reject_explicitly(tmp_path: Path, case: str) -> None:
    from tests.unit.harness.browser_evidence_fixture import build_device_bundle

    descriptor: Any = build_device_bundle(tmp_path, scope=_scope(), validated_manifest=_manifest(), device="desktop")
    if case in {"deep_body", "wide_body", "large_body"}:
        value: Any = {"key": "leaf"}
        if case == "deep_body":
            for _ in range(33):
                value = {"nested": value}
        elif case == "wide_body":
            value = {f"field-{index}": index for index in range(4097)}
        else:
            value = ["x" * 1000] * 300
        _rewrite_role(tmp_path, descriptor, "business", lambda doc: doc["readback"]["body"].update(unused=value))
    elif case == "large_png":
        bundle = _json_file(tmp_path, descriptor["bundle_file"])
        bundle["files"]["viewport_png"] = _rewrite_file(tmp_path, bundle["files"]["viewport_png"], b"x" * (8 * 1024 * 1024 + 1))
        descriptor["bundle_file"] = _rewrite_file(tmp_path, descriptor["bundle_file"], bundle)
    elif case == "selector_depth":
        _rewrite_role(tmp_path, descriptor, "business", lambda doc: doc["selectors"].update(success=["data"] * 17))
    elif case == "error_type":
        _rewrite_role(tmp_path, descriptor, "render", lambda doc: doc.update(console_errors={}))
    else:
        bundle = _json_file(tmp_path, descriptor["bundle_file"])
        bundle["files"]["provenance"] = None
        descriptor["bundle_file"] = _rewrite_file(tmp_path, descriptor["bundle_file"], bundle)
    with pytest.raises(ValueError):
        _validate(tmp_path, descriptor)


@pytest.mark.parametrize("record_id", [17, 2**53 - 1, "bounded-record"])
def test_business_accepts_exact_safe_numeric_or_string_record_id(tmp_path: Path, record_id: object) -> None:
    from tests.unit.harness.browser_evidence_fixture import build_device_bundle

    descriptor: Any = build_device_bundle(tmp_path, scope=_scope(), validated_manifest=_manifest(), device="desktop")
    def change(doc: Any) -> None:
        for trace in ("mutation", "readback"):
            doc[trace]["body"]["record"]["id"] = record_id
    _rewrite_role(tmp_path, descriptor, "business", change)
    assert _validate(tmp_path, descriptor) == descriptor


@pytest.mark.parametrize("mutation_id,readback_id", [
    (True, True), (False, False), (0, 0), (-1, -1), (2**53, 2**53),
    (17, "17"), (17, 17.0), (17.0, 17.0), ("", ""), ("x" * 513, "x" * 513),
])
def test_business_rejects_unsafe_or_type_drifting_record_ids(
    tmp_path: Path, mutation_id: object, readback_id: object,
) -> None:
    from tests.unit.harness.browser_evidence_fixture import build_device_bundle

    descriptor: Any = build_device_bundle(tmp_path, scope=_scope(), validated_manifest=_manifest(), device="desktop")
    def change(doc: Any) -> None:
        doc["mutation"]["body"]["record"]["id"] = mutation_id
        doc["readback"]["body"]["record"]["id"] = readback_id
    _rewrite_role(tmp_path, descriptor, "business", change)
    with pytest.raises(ValueError):
        _validate(tmp_path, descriptor)


@pytest.mark.parametrize("case", ["outside_frame", "duplicate_selector"])
def test_parent_controls_and_ambiguous_selectors_cannot_prove_app_interaction(tmp_path: Path, case: str) -> None:
    from tests.unit.harness.browser_evidence_fixture import build_device_bundle

    descriptor: Any = build_device_bundle(tmp_path, scope=_scope(), validated_manifest=_manifest(), device="desktop")
    if case == "outside_frame":
        bundle = _json_file(tmp_path, descriptor["bundle_file"])
        with Image.new("RGB", (300, 400), "white") as image:
            ImageDraw.Draw(image).rectangle((0, 0, 149, 399), fill="green")
            output = BytesIO()
            image.save(output, format="PNG")
        bundle["files"]["frame_png"] = _rewrite_file(tmp_path, {"path": "case-desktop/frame.png"}, output.getvalue())
        descriptor["bundle_file"] = _rewrite_file(tmp_path, descriptor["bundle_file"], bundle)
        _rewrite_role(tmp_path, descriptor, "render", lambda doc: doc.update(frame={
            "bounds": {"x": 600, "y": 20, "width": 300, "height": 400}, "width": 300, "height": 400,
        }))
    else:
        _rewrite_role(tmp_path, descriptor, "render", lambda doc: doc.update(visible_controls=doc["visible_controls"] * 2))
    with pytest.raises(ValueError):
        _validate(tmp_path, descriptor)


@pytest.mark.parametrize("role", ["bundle", "render", "business", "provenance", "cleanup", "viewport_png"])
def test_saved_validated_descriptor_reopens_every_file(tmp_path: Path, role: str) -> None:
    from tests.unit.harness.browser_evidence_fixture import build_device_bundle

    descriptor: Any = build_device_bundle(tmp_path, scope=_scope(), validated_manifest=_manifest(), device="desktop")
    saved: Any = _validate(tmp_path, descriptor)
    bundle = _json_file(tmp_path, saved["bundle_file"])
    reference = saved["bundle_file"] if role == "bundle" else bundle["files"][role]
    target = tmp_path / reference["path"]
    data = bytearray(target.read_bytes())
    data[-1] ^= 1
    target.write_bytes(data)
    with pytest.raises(ValueError):
        _validate(tmp_path, saved)


def test_bundle_rejects_actual_directory_alias(tmp_path: Path) -> None:
    import subprocess
    import sys

    from tests.unit.harness.browser_evidence_fixture import build_device_bundle

    descriptor: Any = build_device_bundle(tmp_path, scope=_scope(), validated_manifest=_manifest(), device="desktop")
    alias, directory = tmp_path / "alias", tmp_path / "case-desktop"
    if sys.platform == "win32":
        result = subprocess.run(["cmd.exe", "/d", "/c", "mklink", "/J", str(alias), str(directory)],
                                capture_output=True, text=True, check=False)
        assert result.returncode == 0, result.stderr
    else:
        alias.symlink_to(directory, target_is_directory=True)
    bundle = _json_file(tmp_path, descriptor["bundle_file"])
    bundle["files"]["render"]["path"] = "alias/render.json"
    descriptor["bundle_file"] = _rewrite_file(tmp_path, descriptor["bundle_file"], bundle)
    with pytest.raises(ValueError):
        _validate(tmp_path, descriptor)


@pytest.mark.parametrize("status,result", [("pending", "present"), ("unknown", "unknown")])
def test_consistent_pending_or_unknown_cleanup_is_still_not_proof(tmp_path: Path, status: str, result: str) -> None:
    from tests.unit.harness.browser_evidence_fixture import build_device_bundle

    descriptor: Any = build_device_bundle(tmp_path, scope=_scope(), validated_manifest=_manifest(), device="desktop")
    def change(doc: Any) -> None:
        receipt = doc["cleanup_record"]["cleanup_receipt"]
        receipt["status"] = status
        receipt["observations"][0].update(result=result, reason_code="resource_present" if status == "pending" else "observation_failed")
    _rewrite_role(tmp_path, descriptor, "cleanup", change)
    with pytest.raises(ValueError, match="pending or unknown"):
        _validate(tmp_path, descriptor)


def test_resource_observations_must_be_after_last_readback(tmp_path: Path) -> None:
    from tests.unit.harness.browser_evidence_fixture import build_device_bundle

    descriptor: Any = build_device_bundle(tmp_path, scope=_scope(), validated_manifest=_manifest(), device="desktop")
    bundle = _json_file(tmp_path, descriptor["bundle_file"])
    readback_at = _json_file(tmp_path, bundle["files"]["business"])["readback"]["observed_at"]
    def change(doc: Any) -> None:
        receipt = doc["cleanup_record"]["cleanup_receipt"]
        receipt["requested_at"] = readback_at
        receipt["observations"][0]["observed_at"] = readback_at
    _rewrite_role(tmp_path, descriptor, "cleanup", change)
    with pytest.raises(ValueError):
        _validate(tmp_path, descriptor)


def test_entire_json_node_budget_is_enforced_without_truncation(tmp_path: Path) -> None:
    from tests.unit.harness.browser_evidence_fixture import build_device_bundle

    descriptor: Any = build_device_bundle(tmp_path, scope=_scope(), validated_manifest=_manifest(), device="desktop")
    _rewrite_role(tmp_path, descriptor, "business", lambda doc: doc["readback"]["body"].update(
        unused=[[None] * 500 for _ in range(200)],
    ))
    with pytest.raises(ValueError, match="JSON resource budget"):
        _validate(tmp_path, descriptor)


@pytest.mark.parametrize("case", [
    "uuid_only", "uuid_old_prefix", "unique_key", "nested_uuid_key",
    "nested_unique_key", "nested_uuid_string", "uuid_substring", "second_uuid",
])
def test_fresh_marker_rejects_existing_uuid_or_marker_in_all_json_strings(
    tmp_path: Path, case: str,
) -> None:
    from tests.unit.harness.browser_evidence_fixture import build_device_bundle

    descriptor: Any = build_device_bundle(
        tmp_path, scope=_scope(), validated_manifest=_manifest(), device="desktop",
    )
    assert _validate(tmp_path, descriptor) == descriptor

    def change(doc: Any) -> None:
        token = doc["unique_value"][-36:]
        if case == "second_uuid":
            token = "40000000-0000-0000-0000-000000000001"
            doc["unique_value"] += f"-also-{token}"
            doc["mutation"]["input_value"] = doc["unique_value"]
            for trace in ("mutation", "readback"):
                doc[trace]["body"]["record"]["value"] = doc["unique_value"]
        row: dict[str, object] = {"id": "old", "value": "unrelated"}
        if case in {"uuid_only", "second_uuid"}:
            row["value"] = token
        elif case == "uuid_old_prefix":
            row["value"] = f"older-prefix-{token}"
        elif case == "unique_key":
            row[doc["unique_value"]] = "old"
        elif case == "nested_uuid_key":
            row["metadata"] = [{"nested": {token: "old"}}]
        elif case == "nested_unique_key":
            row["metadata"] = {"nested": [{doc["unique_value"]: "old"}]}
        elif case == "nested_uuid_string":
            row["metadata"] = {"nested": [None, {"text": f"contains-{token}-already"}]}
        else:
            row["value"] = f"f{token}a"
        doc["before"]["body"]["records"] = [row]

    _rewrite_role(tmp_path, descriptor, "business", change)
    with pytest.raises(ValueError, match="preexisting"):
        _validate(tmp_path, descriptor)


def test_before_records_can_contain_other_uuid_values_and_nested_keys(tmp_path: Path) -> None:
    from tests.unit.harness.browser_evidence_fixture import build_device_bundle

    descriptor: Any = build_device_bundle(
        tmp_path, scope=_scope(), validated_manifest=_manifest(), device="desktop",
    )
    _rewrite_role(tmp_path, descriptor, "business", lambda doc: doc["before"]["body"].update(records=[{
        "id": 17, "value": "older-unrelated-40000000-0000-0000-0000-000000000001",
        "metadata": [{"40000000-0000-0000-0000-000000000002": "old"}],
    }]))
    assert _validate(tmp_path, descriptor) == descriptor


def _frame_descriptor(
    root: Path, *, bounds: dict[str, float], png_size: tuple[int, int],
) -> dict[str, object]:
    from tests.unit.harness.browser_evidence_fixture import build_device_bundle

    descriptor: Any = build_device_bundle(
        root, scope=_scope(), validated_manifest=_manifest(), device="desktop",
    )
    bundle = _json_file(root, descriptor["bundle_file"])
    with Image.new("RGB", png_size, "white") as image:
        ImageDraw.Draw(image).rectangle((0, 0, png_size[0] // 2 - 1, png_size[1] - 1), fill="black")
        output = BytesIO()
        image.save(output, format="PNG")
    bundle["files"]["frame_png"] = _rewrite_file(root, {"path": "case-desktop/frame.png"}, output.getvalue())
    descriptor["bundle_file"] = _rewrite_file(root, descriptor["bundle_file"], bundle)
    _rewrite_role(root, descriptor, "render", lambda doc: doc.update(frame={
        "bounds": bounds, "width": png_size[0], "height": png_size[1],
    }))
    return cast(dict[str, object], descriptor)


@pytest.mark.parametrize("bounds,png_size", [
    ({"x": 0, "y": 0, "width": 1000.5, "height": 900.5}, (2, 2)),
    ({"x": 0, "y": 0, "width": 300, "height": 400}, (2, 2)),
    ({"x": 0, "y": 0, "width": 300, "height": 400}, (302, 400)),
    ({"x": 0, "y": 0, "width": 300, "height": 400}, (300, 402)),
    ({"x": 0, "y": 0, "width": 300, "height": 400}, (299, 400)),
    ({"x": 0, "y": 0, "width": 300, "height": 400}, (300, 399)),
    ({"x": 10.75, "y": 10.75, "width": 300.5, "height": 400.5}, (300, 400)),
], ids=["large-css-tiny-png", "tiny-png", "wide-png", "tall-png", "short-width", "short-height", "fractional-origin"])
def test_frame_png_geometry_cannot_detach_from_dpr1_css_bounds(
    tmp_path: Path, bounds: dict[str, float], png_size: tuple[int, int],
) -> None:
    descriptor = _frame_descriptor(tmp_path, bounds=bounds, png_size=png_size)
    with pytest.raises(ValueError, match="frame.*dimensions|frame.*geometry"):
        _validate(tmp_path, descriptor)


@pytest.mark.parametrize("bounds,png_size", [
    ({"x": 0, "y": 0, "width": 300, "height": 400}, (300, 400)),
    ({"x": 20, "y": 20, "width": 299.5, "height": 400}, (300, 400)),
    ({"x": 10.25, "y": 10.25, "width": 300.5, "height": 400.5}, (301, 401)),
    ({"x": 10.75, "y": 10.75, "width": 300.5, "height": 400.5}, (302, 402)),
], ids=["integer", "fractional-size", "fractional-origin-down", "fractional-origin-up"])
def test_frame_png_uses_enclosing_dpr1_capture_pixels(
    tmp_path: Path, bounds: dict[str, float], png_size: tuple[int, int],
) -> None:
    descriptor = _frame_descriptor(tmp_path, bounds=bounds, png_size=png_size)
    assert _validate(tmp_path, descriptor) == descriptor


@pytest.mark.parametrize("case", ["false", "unknown", "missing", "integer_true", "error_envelope"])
def test_before_application_failure_or_unknown_cannot_prove_absence(tmp_path: Path, case: str) -> None:
    from tests.unit.harness.browser_evidence_fixture import build_device_bundle

    descriptor: Any = build_device_bundle(
        tmp_path, scope=_scope(), validated_manifest=_manifest(), device="desktop",
    )
    assert _validate(tmp_path, descriptor) == descriptor

    def change(doc: Any) -> None:
        body = doc["before"]["body"]
        if case == "false":
            body.update(success=False, error="database unavailable")
        elif case == "unknown":
            body["success"] = None
        elif case == "integer_true":
            body["success"] = 1
        elif case == "error_envelope":
            doc["before"]["body"] = {"records": [], "error": "query did not complete"}
        else:
            del body["success"]

    _rewrite_role(tmp_path, descriptor, "business", change)
    with pytest.raises(ValueError):
        _validate(tmp_path, descriptor)


@pytest.mark.parametrize("row", [
    {}, {"error": "query failed"}, {"id": "old"}, {"value": "old"},
    {"id": True, "value": "old"}, {"id": 0, "value": "old"},
    {"id": 2**53, "value": "old"}, {"id": 17.0, "value": "old"},
    {"id": "", "value": "old"}, {"id": 17, "value": None},
    {"id": 17, "value": False}, {"id": 17, "value": {}}, {"id": 17, "value": ""},
], ids=["empty", "error", "missing-value", "missing-id", "bool-id", "zero-id", "large-id",
        "float-id", "empty-id", "null-value", "bool-value", "object-value", "empty-value"])
def test_before_root_array_rows_must_be_actual_configured_records(tmp_path: Path, row: object) -> None:
    from tests.unit.harness.browser_evidence_fixture import build_device_bundle

    descriptor: Any = build_device_bundle(
        tmp_path, scope=_scope(), validated_manifest=_manifest(), device="desktop",
    )

    def change(doc: Any) -> None:
        doc["selectors"]["before_records"] = []
        doc["selectors"]["before_success"] = None
        doc["before"]["body"] = [row]

    _rewrite_role(tmp_path, descriptor, "business", change)
    with pytest.raises(ValueError):
        _validate(tmp_path, descriptor)


@pytest.mark.parametrize("case", ["root_array_records", "configured_array_fields", "separate_before_success_path"])
def test_before_success_has_its_own_path_or_valid_root_record_array(tmp_path: Path, case: str) -> None:
    from tests.unit.harness.browser_evidence_fixture import build_device_bundle

    descriptor: Any = build_device_bundle(
        tmp_path, scope=_scope(), validated_manifest=_manifest(), device="desktop",
    )

    def change(doc: Any) -> None:
        if case == "separate_before_success_path":
            doc["selectors"]["before_records"] = ["data", "records"]
            doc["selectors"]["before_success"] = ["status", "loaded"]
            doc["before"]["body"] = {"status": {"loaded": True}, "data": {"records": []}}
        else:
            doc["selectors"]["before_records"] = []
            doc["selectors"]["before_success"] = None
            if case == "configured_array_fields":
                doc["selectors"].update(id_field="record_number", value_field="label")
                for trace in ("mutation", "readback"):
                    old = doc[trace]["body"]["record"]
                    doc[trace]["body"]["record"] = {"record_number": old["id"], "label": old["value"]}
                doc["before"]["body"] = [{"record_number": 17, "label": "old"}]
            else:
                doc["before"]["body"] = [{"id": 17, "value": "old"}, {"id": "old-second", "value": "other"}]

    _rewrite_role(tmp_path, descriptor, "business", change)
    assert _validate(tmp_path, descriptor) == descriptor


@pytest.mark.parametrize("case", [
    "null_object", "empty_selector", "missing_selector", "wrong_path", "non_string_key",
    "too_deep", "array_nonnull", "array_nonroot_selector", "nested_array_null", "scalar_null",
])
def test_before_success_contract_rejects_missing_or_ambiguous_absence_proofs(tmp_path: Path, case: str) -> None:
    from tests.unit.harness.browser_evidence_fixture import build_device_bundle

    descriptor: Any = build_device_bundle(
        tmp_path, scope=_scope(), validated_manifest=_manifest(), device="desktop",
    )
    assert _validate(tmp_path, descriptor) == descriptor

    def change(doc: Any) -> None:
        selectors = doc["selectors"]
        if case == "null_object":
            selectors["before_success"] = None
        elif case == "empty_selector":
            selectors["before_success"] = []
        elif case == "missing_selector":
            del selectors["before_success"]
        elif case == "wrong_path":
            selectors["before_success"] = ["unknown"]
        elif case == "non_string_key":
            selectors["before_success"] = [0]
        elif case == "too_deep":
            selectors["before_success"] = ["data"] * 17
        elif case == "array_nonnull":
            selectors["before_records"] = []
            doc["before"]["body"] = []
        elif case == "array_nonroot_selector":
            selectors["before_success"] = None
            doc["before"]["body"] = []
        elif case == "nested_array_null":
            selectors["before_success"] = None
        else:
            selectors["before_records"] = []
            selectors["before_success"] = None
            doc["before"]["body"] = None

    _rewrite_role(tmp_path, descriptor, "business", change)
    with pytest.raises(ValueError):
        _validate(tmp_path, descriptor)
