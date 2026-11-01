"""Product cleanup follows owned publication outcome, never a caller exception alone."""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Never, Unpack
from uuid import UUID

import pytest

from agent_hub.capabilities.project_verification import (
    ProjectVerificationError,
    ProjectVerificationService,
)
from agent_hub.files.generated import GeneratedFileStore
from agent_hub.runs.project_verification_receipts import VerificationAuthority, VerificationRecord
from agent_hub.runtime.artifacts import ArtifactReference
from agent_hub.runtime.contracts import Artifact
from tests.unit.capabilities.durable_capture_typed_fixture import Case, CompleteArgs, WriteArgs
from tests.unit.capabilities.durable_capture_typed_fixture import (
    case as durable_case,  # noqa: F401
)
from tests.unit.capabilities.test_python_product_delivery import WHEEL, attach_product
from tests.unit.runs.project_auto_repair_typed_fixture import present
from tests.unit.runs.reviewed_report_revision_typed_fixture import read_map, string_value
from tests.unit.runtime.product_stack_diagnostic_typed_fixture import ProductCase


@pytest.fixture
def product_case(durable_case: Case, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ProductCase:  # noqa: F811
    case = durable_case
    streams = attach_product(case.executor, monkeypatch)
    store = GeneratedFileStore(tmp_path / "product-files")
    owner = case.owner()
    owner.generated_file_store = store
    return case, owner, store, streams


def products(store: GeneratedFileStore) -> list[Path]:
    return list(store._root.rglob("*.whl"))


async def assert_warm_delivery_blocked(case: Case, owner: ProjectVerificationService) -> None:
    case.assert_no_receipts(owner)
    with case.bind(), pytest.raises(ProjectVerificationError):
        await owner.require_current_python_product(case.db.row.tenant_id, case.db.row.id)


@pytest.mark.parametrize("outcome", ["committed_error", "committed_cancel", "unknown_error", "unknown_cancel"])
async def test_cas_boundary_retains_body_and_wheel_without_claiming_success(product_case: ProductCase, monkeypatch: pytest.MonkeyPatch, outcome: str) -> None:
    case, owner, store, streams = product_case
    original_complete = present(owner._durable).receipts.complete
    calls: list[CompleteArgs] = []

    async def uncertain(authority: VerificationAuthority, **kwargs: Unpack[CompleteArgs]) -> VerificationRecord:
        calls.append(kwargs)
        if outcome.startswith("committed"):
            await original_complete(authority, **kwargs)
        if outcome.endswith("cancel"):
            raise asyncio.CancelledError
        raise OSError("private CAS result unavailable")

    monkeypatch.setattr(present(owner._durable).receipts, "complete", uncertain)
    expected = asyncio.CancelledError if outcome.endswith("cancel") else ProjectVerificationError
    with case.bind(), pytest.raises(expected):
        await owner.verify(**case.arguments())
    assert len(calls) == 1 and len(case.executor.calls) == 1
    assert case.index().status == ("passed" if outcome.startswith("committed") else "running")
    body = next(iter(case.artifacts._artifacts.values()))
    ref = read_map(read_map(body.content["result"])["python_product"])
    assert products(store) == [store.resolve(string_value(read_map(ref["file"])["storage_key"]))], "possibly committed wheel deleted"
    assert products(store)[0].read_bytes() == WHEEL
    assert all(stream.closed for stream in streams)
    await assert_warm_delivery_blocked(case, owner)
    retained = present(owner._durable)._retained_product_publications
    assert any(item.product == ref and item.disposition == "unknown" for item in retained.values())
    if outcome.startswith("committed"):
        # Cold recovery reads actual committed evidence under a fresh lease;
        # this never reruns the Python executor or turns an UNKNOWN into PASS.
        case.db.new_lease()
        cold = case.owner()
        cold.generated_file_store = store
        with case.bind():
            restored = await cold.restore(**case.arguments())
            assert await cold.require_current_python_product(case.db.row.tenant_id, case.db.row.id) == ref
        assert restored["python_product"] == ref and len(case.executor.calls) == 1
    else:
        with case.bind(), pytest.raises(ProjectVerificationError):
            await owner.verify(**case.arguments(action="cannot-replay-unknown-product"))
        with case.bind(), pytest.raises(ProjectVerificationError):
            await case.owner().restore(**case.arguments())
        assert len(case.executor.calls) == 1 and products(store)[0].read_bytes() == WHEEL


@pytest.mark.parametrize("write", ["before_put", "after_put"])
@pytest.mark.parametrize("abort", ["true", "false", "error", "cancel", "succeeded_then_error"])
async def test_product_cleanup_requires_positive_abort_proof(product_case: ProductCase, monkeypatch: pytest.MonkeyPatch, write: str, abort: str) -> None:
    case, owner, store, streams = product_case
    original_put, original_abort = case.artifacts.put, case.artifacts.abort_write
    bodies: list[Artifact] = []
    abort_calls: list[dict[str, UUID]] = []

    async def put(tenant: UUID, run: UUID, artifact: Artifact, **kwargs: Unpack[WriteArgs]) -> None:
        bodies.append(artifact)
        if write == "after_put":
            await original_put(tenant, run, artifact, **kwargs)
        raise OSError("private put outcome unavailable")

    async def abort_write(tenant: UUID, run: UUID, reference: ArtifactReference, *, write_id: UUID) -> bool:
        abort_calls.append({'write_id': write_id})
        if abort == "true":
            return await original_abort(tenant, run, reference, write_id=write_id)
        if abort == "false":
            return False
        if abort == "cancel":
            raise asyncio.CancelledError
        if abort == "succeeded_then_error":
            await original_abort(tenant, run, reference, write_id=write_id)
        raise OSError("private abort result unavailable")

    monkeypatch.setattr(case.artifacts, "put", put)
    monkeypatch.setattr(case.artifacts, "abort_write", abort_write)
    with case.bind(), pytest.raises(ProjectVerificationError):
        await owner.verify(**case.arguments())
    assert len(bodies) == 1 and len(abort_calls) == 1 and len(case.executor.calls) == 1
    ref = read_map(read_map(bodies[0].content["result"])["python_product"])
    assert len(products(store)) == (0 if abort == "true" else 1)
    if abort != "true":
        assert store.resolve(string_value(read_map(ref["file"])["storage_key"])).read_bytes() == WHEEL
        retained = present(owner._durable)._retained_product_publications
        assert any(item.product == ref and item.disposition == "unknown" for item in retained.values())
    assert case.index().status == "running" and not case.index().artifact_references
    assert all(stream.closed for stream in streams)
    await assert_warm_delivery_blocked(case, owner)


async def test_failure_before_artifact_publication_deletes_only_new_private_product(product_case: ProductCase, monkeypatch: pytest.MonkeyPatch) -> None:
    case, owner, store, streams = product_case
    original_current = owner.require_current_receipt
    calls = 0

    async def revoked(*args: object, **kwargs: object) -> Never:
        nonlocal calls
        calls += 1
        raise ProjectVerificationError("project verification required before delivery")

    monkeypatch.setattr(owner, "require_current_receipt", revoked)
    with case.bind(), pytest.raises(ProjectVerificationError):
        await owner.verify(**case.arguments())
    assert calls == 1 and not products(store) and not case.artifacts._artifacts
    assert all(stream.closed for stream in streams)
    monkeypatch.setattr(owner, "require_current_receipt", original_current)
    await assert_warm_delivery_blocked(case, owner)


async def test_repeated_cancellation_joins_abort_before_deleting_product(product_case: ProductCase, monkeypatch: pytest.MonkeyPatch) -> None:
    case, owner, store, streams = product_case
    original_put, original_abort = case.artifacts.put, case.artifacts.abort_write
    put_entered, abort_entered, finish_abort = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def interrupted_put(tenant: UUID, run: UUID, artifact: Artifact, **kwargs: Unpack[WriteArgs]) -> None:
        await original_put(tenant, run, artifact, **kwargs)
        put_entered.set()
        await asyncio.Event().wait()

    async def delayed_abort(tenant: UUID, run: UUID, reference: ArtifactReference, *, write_id: UUID) -> bool:
        abort_entered.set()
        await finish_abort.wait()
        return await original_abort(tenant, run, reference, write_id=write_id)

    monkeypatch.setattr(case.artifacts, "put", interrupted_put)
    monkeypatch.setattr(case.artifacts, "abort_write", delayed_abort)
    with case.bind():
        task = asyncio.create_task(owner.verify(**case.arguments()))
        try:
            await asyncio.wait_for(put_entered.wait(), 10)
            task.cancel()
            await asyncio.wait_for(abort_entered.wait(), 5)
            task.cancel()
            await asyncio.sleep(0)
            assert len(products(store)) == 1 and not task.done()
        finally:
            finish_abort.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert not products(store) and not case.artifacts._artifacts
    assert all(stream.closed for stream in streams)
    await assert_warm_delivery_blocked(case, owner)
