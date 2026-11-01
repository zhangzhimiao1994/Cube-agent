"""Real registry/wheel parsing in memory; production authorization remains unwired."""

import hashlib
import io
import time
import zipfile
from collections.abc import Mapping, Sequence
from dataclasses import replace
from typing import Never

import pytest

from agent_hub.skills.sandbox.python_input_fetch import WheelDownload
from agent_hub.skills.sandbox.python_registry_protocol import ResolutionBudget, ResolutionDescriptor
from tests.unit.skills import test_python_registry_read as registry_tests
from tests.unit.skills.test_python_registry_read import (
    Wire,
    api,
    budget,
    environment,
    reader,
    scope,
)

offline_network = registry_tests.offline_network


async def resolve(
    wire: Wire, bodies: Mapping[str, bytes], needs: Sequence[str], *,
    env: dict[str, str] | None = None, allowance: ResolutionBudget | None = None,
) -> ResolutionDescriptor:
    p, _ = api()
    from agent_hub.capabilities import python_dependency_resolution as r

    async def content(download: WheelDownload) -> bytes:
        return bodies[download.filename]

    return await r.resolve_dependencies(
        tuple(p.DependencyNeed(n, "a" * 64) for n in needs),
        scope=scope(),
        environment=env or environment(),
        reader=reader(wire),
        wheel_reader=content,
        budget=allowance or budget(),
    )


async def test_actual_wheel_metadata_not_json_builds_transitive_descriptor() -> None:
    wire, bodies = Wire(), {}
    for name, deps in [("demo", ("helper>=1",)), ("helper", ())]:
        row, body = wire.release(name, deps=deps)
        bodies[row["filename"]] = body
    result = await resolve(wire, bodies, ["demo>=1"])
    assert [w.candidate.download.name for w in result.wheels] == ["demo", "helper"]
    assert ("demo", "helper") in result.edges
    assert "json-only-fake" not in str(result)
    assert result.scope == scope()
    assert len(result.sha256) == 64
    assert not hasattr(result, "install_allowed")
    assert replace(result, scope=replace(scope(), source_sha256="4" * 64)).sha256 != result.sha256


async def test_markers_extras_and_cycles_use_exact_target_environment() -> None:
    wire, bodies = Wire(), {}
    for name, deps in [
        ("demo", ('helper; extra == "feature"', 'windows-only; sys_platform == "win32"')),
        ("helper", ("demo>=1",)),
    ]:
        row, body = wire.release(name, deps=deps)
        bodies[row["filename"]] = body
    result = await resolve(wire, bodies, ["demo[feature]"])
    assert [w.candidate.download.name for w in result.wheels] == ["demo", "helper"]
    assert set(result.edges) == {("demo", "helper"), ("helper", "demo")}
    assert len(wire.requests) == 4


async def test_conflicting_selected_version_is_a_precise_blocker() -> None:
    wire, bodies = Wire(), {}
    for name, deps in [("demo", ("helper>=2",)), ("helper", ())]:
        row, body = wire.release(name, deps=deps)
        bodies[row["filename"]] = body
    with pytest.raises(ValueError, match="conflict|candidate"):
        await resolve(wire, bodies, ["demo", "helper==1.0"])


async def test_hash_mismatched_wheel_cannot_freeze_descriptor() -> None:
    wire = Wire()
    row, body = wire.release("demo")
    with pytest.raises(ValueError):
        await resolve(wire, {row["filename"]: body + b"tampered"}, ["demo"])


async def test_seventeen_packages_do_not_require_a_new_grant_or_reset_deadline() -> None:
    wire, bodies = Wire(), {}
    for i in range(17):
        row, body = wire.release(f"dep-{i}")
        bodies[row["filename"]] = body
    allowance = budget()
    deadline = allowance.deadline
    result = await resolve(wire, bodies, [f"dep-{i}" for i in range(17)], allowance=allowance)
    assert len(result.wheels) == 17 and allowance.deadline == deadline
    with pytest.raises(ValueError):
        await resolve(
            wire, bodies, [f"dep-{i}" for i in range(17)], allowance=budget(max_packages=16)
        )


async def test_missing_environment_never_uses_host_defaults() -> None:
    wire = Wire()
    row, body = wire.release("demo")
    env = environment()
    del env["sys_platform"]
    with pytest.raises(ValueError):
        await resolve(wire, {row["filename"]: body}, ["demo"], env=env)
    assert wire.requests == []


@pytest.mark.parametrize(
    "field,value",
    [
        ("python_full_version", "9.0"),
        ("implementation_version", "3.13.0"),
        ("platform_python_implementation", "PyPy"),
    ],
)
async def test_inconsistent_target_identity_fails_before_registry(field: str, value: str) -> None:
    wire = Wire()
    row, body = wire.release("demo")
    env = environment()
    env[field] = value
    with pytest.raises(ValueError):
        await resolve(wire, {row["filename"]: body}, ["demo"], env=env)
    assert wire.requests == []


async def test_edge_and_total_wheel_budgets_fail_without_unbounded_recovery() -> None:
    wire, bodies = Wire(), {}
    for name, deps in [("demo", ("helper",)), ("helper", ())]:
        row, body = wire.release(name, deps=deps)
        bodies[row["filename"]] = body
    with pytest.raises(ValueError):
        await resolve(wire, bodies, ["demo"], allowance=budget(max_edges=1))
    with pytest.raises(ValueError):
        await resolve(wire, bodies, ["demo"], allowance=budget(max_wheel_bytes=32))


async def test_cancellation_propagates_from_controlled_wheel_input() -> None:
    import asyncio

    p, _ = api()
    from agent_hub.capabilities import python_dependency_resolution as r
    wire = Wire()
    wire.release("demo")

    async def cancelled(download: WheelDownload) -> Never:
        raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await r.resolve_dependencies(
            (p.DependencyNeed("demo", "a" * 64),),
            scope=scope(),
            environment=environment(),
            reader=reader(wire),
            wheel_reader=cancelled,
            budget=budget(),
        )


async def test_real_compressed_archive_stops_at_original_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    """A small valid archive must stop mid-inflate, not finish 192 MiB after expiry."""
    import json

    p, _ = api()
    from agent_hub.capabilities import python_dependency_resolution as r
    packed = io.BytesIO()
    with zipfile.ZipFile(packed, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "demo-1.0.dist-info/METADATA",
            "Metadata-Version: 2.1\nName: demo\nVersion: 1.0\nRequires-Python: >=3.12\n\n",
        )
        archive.writestr(
            "demo-1.0.dist-info/WHEEL",
            "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n\n",
        )
        with archive.open("demo/payload.bin", "w") as stream:
            for _ in range(3072):
                stream.write(b"\0" * 65536)
    raw = packed.getvalue()
    assert len(raw) < 256 * 1024
    wire = Wire()
    row, _ = wire.release("demo")
    row["size"] = len(raw)
    row["digests"]["sha256"] = hashlib.sha256(raw).hexdigest()
    read = zipfile.ZipExtFile.read
    inflated = 0

    def counted(stream: zipfile.ZipExtFile, n: int | None = -1) -> bytes:
        nonlocal inflated
        result = read(stream, n)
        if stream.name == "demo/payload.bin":
            inflated += len(result)
        return result

    monkeypatch.setattr(zipfile.ZipExtFile, "read", counted)
    entered_remaining: float | None = None
    allowance = p.ResolutionBudget(p.ResolutionLimits(), time.monotonic() + 0.03)

    async def content(download: WheelDownload) -> bytes:
        nonlocal entered_remaining
        entered_remaining = allowance.deadline - time.monotonic()
        return raw

    started = time.monotonic()
    with pytest.raises(ValueError):
        await r.resolve_dependencies(
            (p.DependencyNeed("demo", "a" * 64),),
            scope=scope(), environment=environment(), reader=reader(wire),
            wheel_reader=content, budget=allowance,
        )
    elapsed = time.monotonic() - started
    print(json.dumps({
        "elapsed_seconds": elapsed, "compressed_bytes": len(raw),
        "inflated_bytes": inflated, "original_budget_seconds": 0.03,
        "entered_remaining_seconds": entered_remaining,
    }, sort_keys=True))
    assert entered_remaining is not None and entered_remaining > 0
    assert inflated < 192 * 1024**2, "archive was fully inflated after original deadline"
    assert elapsed < 0.18, "archive parser retained CPU well beyond original deadline"


def test_optional_deadlines_preserve_ordinary_bytes_and_legacy_calls() -> None:
    from agent_hub.skills.sandbox.python_dependency_metadata import extract_python_wheel_metadata
    from agent_hub.skills.sandbox.python_input_fetch import WheelDownload, validate_wheel_bytes
    from tests.unit.skills.test_python_registry_read import wheel

    row, raw = wheel("demo")
    entry = WheelDownload(
        row["filename"], "demo", "1.0", row["digests"]["sha256"], row["size"], row["url"],
    )
    original = extract_python_wheel_metadata(entry, raw)
    validate_wheel_bytes(entry, raw)
    deadline = time.monotonic() + 1
    validate_wheel_bytes(entry, raw, deadline=deadline)
    assert extract_python_wheel_metadata(entry, raw, deadline=deadline) == original
    with pytest.raises(ValueError):
        validate_wheel_bytes(entry, raw, deadline=time.monotonic() - 1)
    with pytest.raises(ValueError):
        extract_python_wheel_metadata(entry, raw, deadline=time.monotonic() - 1)
