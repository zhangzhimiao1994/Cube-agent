"""Offline snapshot metadata contracts, not browser or native acceptance."""
from __future__ import annotations

import importlib
import json
import subprocess
import sys
from dataclasses import replace
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any

import pytest

from tests.unit.previews.test_cleanup import identity


def contract() -> Any:
    assert importlib.util.find_spec("agent_hub.previews.provenance"), "provenance contract missing"
    return importlib.import_module("agent_hub.previews.provenance")


def test_exact_wire_roundtrip_and_frozen() -> None:
    c = contract()
    manifest = {"index.html": (3, sha256(b"web").hexdigest())}
    snapshot = c.SnapshotManifestV1.from_manifest(
        manifest, selection_policy="static-display-root-v1"
    )
    expected_hash = sha256(json.dumps(manifest, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False).encode()).hexdigest()
    assert snapshot.to_wire() == {
        "schema_version": 1, "algorithm": "workspace-manifest-sha256-v1",
        "selection_policy": "static-display-root-v1", "manifest_sha256": expected_hash,
        "file_count": 1, "total_bytes": 3,
    }
    value = c.PreviewProvenanceV1(1, identity(), snapshot, datetime.now(UTC))
    assert set(value.to_wire()) == {"schema_version", "identity", "snapshot_manifest", "captured_at"}
    assert value.to_wire()["identity"] == identity().to_wire()
    assert c.PreviewProvenanceV1.from_wire(value.to_wire()) == value
    with pytest.raises(AttributeError):
        snapshot.total_bytes = 4
    with pytest.raises(AttributeError):
        value.identity = identity("dynamic")


@pytest.mark.parametrize("field,value", [
    ("schema_version", True), ("schema_version", 1.0), ("schema_version", 2),
    ("algorithm", "tree"), ("selection_policy", "unknown"), ("selection_policy", []),
    ("manifest_sha256", "A" * 64), ("manifest_sha256", "a" * 63),
    ("file_count", True), ("file_count", 0), ("file_count", 10001),
    ("total_bytes", False), ("total_bytes", -1), ("total_bytes", 128 * 1024 * 1024 + 1),
    ("extra", "file contents"),
])
def test_snapshot_strict_wire(field: str, value: object) -> None:
    c = contract()
    wire = c.SnapshotManifestV1.from_manifest(
        {"index.html": (0, "a" * 64)}, selection_policy="static-display-root-v1"
    ).to_wire()
    wire[field] = value
    with pytest.raises(ValueError):
        c.SnapshotManifestV1.from_wire(wire)


@pytest.mark.parametrize("field,value", [
    ("schema_version", True), ("schema_version", 1.0), ("schema_version", 2),
    ("identity", {}), ("snapshot_manifest", {}), ("captured_at", "2026-10-05T00:00:00"),
    ("captured_at", "nonsense"), ("captured_at", 42), ("extra", "secret"),
])
def test_provenance_strict_wire(field: str, value: object) -> None:
    c = contract()
    snapshot = c.SnapshotManifestV1.from_manifest(
        {"index.html": (0, "a" * 64)}, selection_policy="static-display-root-v1"
    )
    wire = c.PreviewProvenanceV1(1, identity(), snapshot, datetime.now(UTC)).to_wire()
    wire[field] = value
    with pytest.raises(ValueError):
        c.PreviewProvenanceV1.from_wire(wire)


def test_policy_kind_pairing_and_producer_budgets() -> None:
    c = contract()
    snapshot = c.SnapshotManifestV1.from_manifest(
        {"index.html": (0, "a" * 64)}, selection_policy="static-display-root-v1"
    )
    with pytest.raises(ValueError):
        c.PreviewProvenanceV1(1, identity("dynamic"), snapshot, datetime.now(UTC))
    for limits in ({"max_bytes": 1}, {"max_files": 1}):
        with pytest.raises(ValueError):
            c.SnapshotManifestV1.from_manifest(
                {"a": (1, "a" * 64), "b": (1, "b" * 64)},
                selection_policy="dynamic-staged-session-v1", **limits,
            )
    wire = snapshot.to_wire()
    wire.update(selection_policy="dynamic-staged-session-v1", file_count=4097)
    with pytest.raises(ValueError):
        c.SnapshotManifestV1.from_wire(wire)
    wire.update(file_count=1, total_bytes=32 * 1024 * 1024 + 1)
    with pytest.raises(ValueError):
        c.SnapshotManifestV1.from_wire(wire)


def test_static_selection_rebases_root_dot_and_requires_entrypoint() -> None:
    c = contract()
    manifest = {"dist/index.html": (3, "a" * 64), "dist/a.js": (4, "b" * 64),
                "server.js": (9, "c" * 64), "dist-other/index.html": (8, "d" * 64)}
    expected = c.SnapshotManifestV1.from_manifest(
        {"index.html": manifest["dist/index.html"], "a.js": manifest["dist/a.js"]},
        selection_policy="static-display-root-v1",
    )
    assert c.snapshot_manifest_for_identity(manifest, identity()) == expected
    rebased = {"index.html": (3, "a" * 64), "a.js": (4, "b" * 64)}
    assert c.snapshot_manifest_for_identity(rebased, replace(identity(), display_root=".")) == expected
    for missing in ({"server.js": (9, "c" * 64)}, {"dist/a.js": (4, "b" * 64)}):
        with pytest.raises(ValueError):
            c.snapshot_manifest_for_identity(missing, identity())
    with pytest.raises(ValueError):
        c.snapshot_manifest_for_identity({**manifest, "../secret": (0, "a" * 64)}, identity())


def test_dynamic_selection_uses_component_exclusions() -> None:
    c = contract()
    manifest = {"package.json": (1, "a" * 64), "server.js": (2, "b" * 64)}
    for name in ("node_modules", ".git", ".preview-staging", ".venv", ".npmrc",
                 ".ssh", ".aws", ".codex", ".env", ".env.local"):
        manifest[f"nested/{name}/secret"] = (10, "c" * 64)
    result = c.snapshot_manifest_for_identity(manifest, identity("dynamic"))
    assert result.file_count == 2 and result.total_bytes == 3
    assert result == c.SnapshotManifestV1.from_manifest(
        {"package.json": (1, "a" * 64), "server.js": (2, "b" * 64)},
        selection_policy="dynamic-staged-session-v1",
    )


@pytest.mark.parametrize("missing_windows_native", [False, True])
def test_broker_and_provenance_import_without_harness_or_third_party(
    missing_windows_native: bool,
) -> None:
    script = """
import importlib.abc, sys
sys.path.insert(0, sys.argv[1])
if sys.argv[2] == 'missing':
    sys.stdlib_module_names = sys.stdlib_module_names - {'_wmi'}
    sys.modules.pop('_wmi', None)
class Block(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.startswith('agent_hub.harness'):
            raise RuntimeError('heavy harness import: ' + fullname)
        if fullname == '_wmi' and fullname not in sys.stdlib_module_names:
            raise ModuleNotFoundError('optional Windows native module missing', name=fullname)
        if fullname.split('.')[0] not in sys.stdlib_module_names | {'agent_hub'}:
            raise RuntimeError('third party import: ' + fullname)
sys.meta_path.insert(0, Block())
if sys.argv[2] == 'missing':
    try:
        __import__('_wmi')
    except ModuleNotFoundError as error:
        assert error.name == '_wmi'
    else:
        raise AssertionError('missing native module was imported')
for name, message in [('requests', 'third party import:'),
                      ('agent_hub.harness', 'heavy harness import:')]:
    try:
        __import__(name)
    except RuntimeError as error:
        assert str(error).startswith(message)
    else:
        raise AssertionError('forbidden import was accepted')
from agent_hub.workspace_manifest import workspace_manifest_sha256
from agent_hub.previews import provenance, dynamic_broker
assert provenance.SnapshotManifestV1
assert dynamic_broker.PreviewBroker
assert workspace_manifest_sha256({'index.html': (0, 'a' * 64)})
assert not any(n.startswith('agent_hub.harness') for n in sys.modules)
"""
    source = Path(__file__).resolve().parents[3] / "src"
    result = subprocess.run([sys.executable, "-I", "-S", "-c", script, str(source),
                             "missing" if missing_windows_native else "native"],
                            capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("manifest", [
    {"index.html": (0, "a" * 64)}, {"nested/\u6587\u4ef6.txt": [1, "b" * 64], "a": (9, "a" * 64)},
    {}, {"../escape": (0, "a" * 64)}, {"./a": (0, "a" * 64)},
    {"POST/a": (0, "a" * 64)}, {"a\\b": (0, "a" * 64)}, {"a:b": (0, "a" * 64)},
    {"a": (True, "a" * 64)}, {"a": (-1, "a" * 64)}, {"a": (1, "A" * 64)},
])
def test_shared_canonicalizer_matches_standalone_harness(manifest: Any) -> None:
    import runpy

    from agent_hub.workspace_manifest import workspace_manifest_sha256
    source = Path(__file__).resolve().parents[3] / "src/agent_hub/harness/project_validation_result.py"
    original = runpy.run_path(str(source))["scale_validation_manifest_sha256"]
    try:
        expected = original(manifest)
    except ValueError:
        with pytest.raises(ValueError):
            workspace_manifest_sha256(manifest)
    else:
        assert workspace_manifest_sha256(manifest) == expected


