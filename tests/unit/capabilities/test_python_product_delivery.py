"""Real streamed files and service checks; Root execution remains an offline double."""
from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import tempfile
import threading
import zipfile
from collections.abc import Mapping
from pathlib import Path
from typing import BinaryIO, Never
from uuid import UUID, uuid4

import pytest

from agent_hub.capabilities import python_generated_service
from agent_hub.capabilities import python_product_delivery as delivery_api
from agent_hub.capabilities.approvals import capability_approval_scope
from agent_hub.capabilities.generated_project_service import GeneratedProjectContext
from agent_hub.capabilities.project_verification import (
    ProjectVerificationError,
    ProjectVerificationService,
)
from agent_hub.capabilities.python_generated_service import PythonGeneratedProjectService
from agent_hub.capabilities.python_project_service import (
    PythonProjectExecutor,
    PythonProjectService,
)
from agent_hub.capabilities.python_verification_recovery import restore_python_verification
from agent_hub.files.generated import WHEEL_MIME_TYPE, GeneratedFileMetadata, GeneratedFileStore
from agent_hub.runtime.contracts import JsonValue
from agent_hub.skills.sandbox.base import SkillResult
from agent_hub.skills.sandbox.project import ProjectInvocation
from agent_hub.skills.sandbox.python_project_protocol import (
    PythonProductDescriptor,
    PythonProjectBrokerResponse,
    PythonProjectInvocation,
)
from tests.unit.capabilities import test_python_generated_service as generated
from tests.unit.capabilities import test_python_project_service as original
from tests.unit.capabilities.catalog_product_delivery_typed_fixture import ProductLive, RestoreArgs
from tests.unit.capabilities.durable_capture_typed_fixture import Case
from tests.unit.capabilities.durable_capture_typed_fixture import (
    case as durable_case,  # noqa: F401
)
from tests.unit.runs.project_auto_repair_typed_fixture import (
    json_map,
    mutable,
    present,
    sequence,
    string,
)
from tests.unit.runs.reviewed_report_revision_typed_fixture import (
    integer_value,
)


def wheel_bytes() -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("billing/__init__.py", "def total(): return 12\n")
        archive.writestr("billing/data.txt", b"x" * 150_000)
        archive.writestr("billing-1.0.0.dist-info/METADATA", "Metadata-Version: 2.1\nName: billing\nVersion: 1.0.0\n")
        archive.writestr("billing-1.0.0.dist-info/WHEEL", "Wheel-Version: 1.0\nTag: py3-none-any\n")
        archive.writestr("billing-1.0.0.dist-info/RECORD", "")
    return output.getvalue()


WHEEL = wheel_bytes()


class Spool(io.BufferedReader):
    def __init__(self, data: bytes) -> None:
        self.handle = tempfile.TemporaryFile("w+b")  # noqa: SIM115 - ownership transfers to the service.
        self.handle.write(data)
        self.handle.seek(0)
        super().__init__(self.handle)
        self.read_sizes: list[int] = []

    def read(self, size: int | None = -1) -> bytes:
        assert size is not None
        assert 0 < size <= 65536, "product must be read in bounded chunks"
        self.read_sizes.append(size)
        return self.handle.read(size)

    def close(self) -> None:
        self.handle.close()

    @property
    def closed(self) -> bool:
        return self.handle.closed


def attach_product(executor: PythonProjectExecutor, monkeypatch: pytest.MonkeyPatch, *, defect: str | None = None) -> list[Spool]:
    run = executor.run
    streams: list[Spool] = []

    async def response(invocation: PythonProjectInvocation) -> PythonProjectBrokerResponse:
        result = await run(invocation)
        assert result.result is not None
        raw = mutable(json.loads(result.result.stdout))
        if raw["status"] != "passed":
            return result
        metadata: dict[str, JsonValue] = {"filename": "billing-1.0.0-py3-none-any.whl",
                    "sha256": hashlib.sha256(WHEEL).hexdigest(), "size": len(WHEEL)}
        raw["wheel"] = metadata
        build = mutable(sequence(raw["checks"])[1])
        detail = mutable(json.loads(string(build["stdout"])))
        detail["wheel"] = metadata
        build["stdout"] = json.dumps(detail)
        build["stdout_observed_bytes"] = len(string(build["stdout"]).encode())
        descriptor = PythonProductDescriptor.model_validate({
            "schema_version": 1, "execution_id": invocation.execution_id,
            "tenant_id": str(invocation.tenant_id), "run_id": str(invocation.run_id),
            "project_id": invocation.project_id, "session_id": invocation.session_id,
            "source_sha256": raw["source_sha256"], "recipe_sha256": invocation.recipe_sha256,
            **metadata,
        }, strict=True)
        if defect == "scope":
            descriptor = descriptor.model_copy(update={"run_id": str(uuid4())})
        elif defect == "stdout":
            raw["wheel"] = {**metadata, "sha256": "0" * 64}
        result = result.model_copy(update={"product": descriptor,
            "result": SkillResult(exit_code=0, timed_out=False, stdout=json.dumps(raw), stderr="")})
        if defect != "missing_stream":
            stream = Spool(WHEEL[:-1] if defect == "truncated" else WHEEL)
            streams.append(stream)
            result._product_stream = stream
        return result

    monkeypatch.setattr(executor, "run", response)
    return streams


@pytest.fixture(params=["original", "generated"])
async def live(request: pytest.FixtureRequest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ProductLive:
    service: PythonProjectService | PythonGeneratedProjectService
    context: GeneratedProjectContext | None
    kind = request.param
    assert kind in ("original", "generated")
    store = GeneratedFileStore(tmp_path / "artifacts")
    if request.param == "original":
        owner, repo, executor, base, source = await original.configured(tmp_path)
        assert isinstance(owner, ProjectVerificationService)
        assert isinstance(repo, original.Repository)
        assert isinstance(executor, original.PythonExecutor)
        assert isinstance(base, ProjectInvocation)
        assert isinstance(source, Path)
        approval = await owner.authorization_request(original.request(repo, base))
        repo.approvals.add(present(capability_approval_scope(present(approval))))
        await original.observed(owner, repo, base)
        executor.failure_index = None
        owner = ProjectVerificationService(root=owner.root, repository=repo, executor=None,
            python_executor=executor, generated_file_store=store)
        service, context = owner._python, None
    else:
        api = python_generated_service
        service, _, repo, executor, _, context = await generated.configured(api, tmp_path)
        assert isinstance(service, PythonGeneratedProjectService)
        assert isinstance(repo, generated.Approvals)
        assert isinstance(executor, generated.Executor)
        assert isinstance(context, GeneratedProjectContext)
        generated.approve(repo, await service.authorization_request(generated.request(), context))
        owner = service.owner
        owner.generated_file_store = store
        owner._python_generated = service
        base, source = context.invocation, owner.root
    return ProductLive("original" if kind == "original" else "generated", owner, service, context,
                       repo, executor, base, source, store, monkeypatch, tmp_path)


async def verify(case: ProductLive) -> Mapping[str, JsonValue]:
    if case.kind == "original":
        base = case.base
        return await case.owner.verify(tenant_id=base.tenant_id, run_id=base.run_id, actor="engineer")
    assert isinstance(case.service, PythonGeneratedProjectService) and case.context is not None
    return await case.service.verify(case.context, "engineer")


async def test_streamed_product_survives_reparse_and_is_current_readable_file(live: ProductLive) -> None:
    streams = attach_product(live.executor, live.monkeypatch)
    payload = await verify(live)
    ref = json_map(payload["python_product"])
    assert set(ref) == {"schema_version", "artifact_id", "descriptor", "file", "report_sha256"}
    assert len(json.dumps(ref, default=dict)) < 2500
    assert all(stream.closed and len(stream.read_sizes) >= 3 for stream in streams)
    base = live.base
    current = await live.owner.require_current_python_product(base.tenant_id, base.run_id)
    assert current == ref
    api = delivery_api
    with api.open_product(live.store, ref, base.tenant_id, base.run_id,
                          string(json_map(payload["verification"])["source_sha256"])) as (fd, descriptor):
        assert isinstance(fd, int) and descriptor.filename.endswith(".whl")
        with os.fdopen(os.dup(fd), "rb") as source:
            assert source.read() == WHEEL
    assert not list(live.owner.root.rglob("*.whl"))


@pytest.mark.parametrize("defect", ["scope", "stdout", "missing_stream", "truncated"])
async def test_invalid_product_never_creates_delivery_receipt_or_leaks_spool(live: ProductLive, defect: str) -> None:
    streams = attach_product(live.executor, live.monkeypatch, defect=defect)
    with pytest.raises(ProjectVerificationError):
        await verify(live)
    assert all(stream.closed for stream in streams)
    assert not list((live.root / "artifacts").rglob("*.whl"))
    key = (live.base.tenant_id, live.base.run_id)
    assert key not in live.service.receipts


async def test_real_store_rejects_metadata_only_legacy_response(live: ProductLive) -> None:
    with pytest.raises(ProjectVerificationError):
        await verify(live)
    assert not list((live.root / "artifacts").rglob("*.whl"))


@pytest.mark.parametrize("defect", ["bytes", "hardlink", "scope", "source", "grant"])
async def test_current_product_rechecks_bytes_and_current_authority(live: ProductLive, defect: str) -> None:
    attach_product(live.executor, live.monkeypatch)
    payload = await verify(live)
    ref, base = json_map(payload["python_product"]), live.base
    path = live.store.resolve(string(json_map(ref["file"])["storage_key"]))
    if defect == "bytes":
        path.write_bytes(b"not the verified wheel")
    elif defect == "hardlink":
        os.link(path, path.with_name("alias.whl"))
    elif defect == "scope":
        ref = {**ref, "descriptor": {**json_map(ref["descriptor"]), "run_id": str(UUID(int=8))}}
    elif defect == "source":
        api = delivery_api
        with pytest.raises(ProjectVerificationError), api.open_product(
            live.store, ref, base.tenant_id, base.run_id, "0" * 64,
        ):
            pytest.fail("wrong source exposed fd")
        return
    else:
        live.repo.approvals.clear()
    api = delivery_api
    if defect in {"bytes", "hardlink", "scope"}:
        with pytest.raises(ProjectVerificationError), api.open_product(
            live.store, ref, base.tenant_id, base.run_id, string(json_map(payload["verification"])["source_sha256"]),
        ):
            pytest.fail("invalid product exposed fd")
    if defect != "scope":
        with pytest.raises(ProjectVerificationError):
            await live.owner.require_current_python_product(base.tenant_id, base.run_id)


async def test_after_persistence_failure_removes_only_this_attempt_product(live: ProductLive) -> None:
    attach_product(live.executor, live.monkeypatch)
    base = live.base
    previous = live.store.store_bytes(base.tenant_id, base.run_id, uuid4(),
        "previous-1.0-py3-none-any.whl", WHEEL_MIME_TYPE, WHEEL)
    store_stream = live.store.store_stream

    def store_then_revoke(tenant_id: UUID, run_id: UUID, artifact_id: UUID,
                          filename: str, mime_type: str, stream: BinaryIO, *,
                          expected_sha256: str, expected_size: int) -> GeneratedFileMetadata:
        metadata = store_stream(tenant_id, run_id, artifact_id, filename, mime_type, stream,
                                expected_sha256=expected_sha256, expected_size=expected_size)
        live.repo.approvals.clear()
        return metadata

    live.monkeypatch.setattr(live.store, "store_stream", store_then_revoke)
    with pytest.raises(ProjectVerificationError):
        await verify(live)
    assert list((live.root / "artifacts").rglob("*.whl")) == [
        live.store.resolve(previous.storage_key)]


@pytest.mark.parametrize("defect", [None, "missing", "report", "execution", "storage", "grant"])
async def test_cold_product_restore_requires_real_file_and_exact_trusted_report(live: ProductLive, defect: str | None) -> None:
    attach_product(live.executor, live.monkeypatch)
    payload = mutable(json.loads(json.dumps(await verify(live), default=dict)))
    owner = ProjectVerificationService(root=live.owner.root, repository=live.repo,
        executor=None, python_executor=live.executor, generated_file_store=live.store)
    base = live.base
    if defect == "missing":
        payload.pop("python_product")
    elif defect == "report":
        mutable(payload["python_product"])["report_sha256"] = "0" * 64
    elif defect == "execution":
        mutable(mutable(payload["python_product"])["descriptor"])["execution_id"] = "foreign_execution"
    elif defect == "storage":
        mutable(mutable(payload["python_product"])["file"])["storage_key"] = "../../foreign.whl"
    elif defect == "grant":
        live.repo.approvals.clear()

    async def forbidden(*args: object, **kwargs: object) -> Never:
        pytest.fail("cold product restore executed a new run or probe")

    live.monkeypatch.setattr(live.executor, "run", forbidden)
    live.monkeypatch.setattr(live.executor, "probe", forbidden)
    kwargs: RestoreArgs = {"kind": "python_original" if live.kind == "original" else "python_generated", "tenant_id": base.tenant_id, "run_id": base.run_id,
        "actor": "engineer", "payload": json_map(payload), "source_sha256": string(json_map(payload["verification"])["source_sha256"]),
        "execution_id": string(json_map(payload["verification"])["execution_id"])}
    if defect:
        with pytest.raises(ProjectVerificationError):
            await restore_python_verification(owner, **kwargs)
        assert (base.tenant_id, base.run_id) not in owner._python.receipts
        assert (base.tenant_id, base.run_id) not in owner._python_generated.receipts
    else:
        await restore_python_verification(owner, **kwargs)
        assert await owner.require_current_python_product(base.tenant_id, base.run_id) == payload["python_product"]


@pytest.mark.parametrize("writer_failure", [False, True])
async def test_cancelled_persistence_joins_writer_closes_spool_and_rolls_back(live: ProductLive, writer_failure: bool) -> None:
    streams = attach_product(live.executor, live.monkeypatch)
    started, release = threading.Event(), threading.Event()
    original_store = live.store.store_stream

    def blocked(tenant_id: UUID, run_id: UUID, artifact_id: UUID,
                          filename: str, mime_type: str, stream: BinaryIO, *,
                          expected_sha256: str, expected_size: int) -> GeneratedFileMetadata:
        started.set()
        assert release.wait(5)
        if writer_failure:
            raise OSError("private write failure after cancellation")
        return original_store(tenant_id, run_id, artifact_id, filename, mime_type, stream,
                                expected_sha256=expected_sha256, expected_size=expected_size)

    live.monkeypatch.setattr(live.store, "store_stream", blocked)
    task = asyncio.create_task(verify(live))
    try:
        assert await asyncio.to_thread(started.wait, 5)
        task.cancel()
        await asyncio.sleep(0)
        assert streams and not streams[-1].closed
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert streams[-1].closed
    assert not list((live.root / "artifacts").rglob("*.whl"))


async def test_product_ref_requires_integer_stored_size_not_equal_float(live: ProductLive) -> None:
    attach_product(live.executor, live.monkeypatch)
    payload = mutable(json.loads(json.dumps(await verify(live), default=dict)))
    ref = mutable(payload["python_product"])
    file = mutable(ref["file"])
    file["size_bytes"] = float(integer_value(file["size_bytes"]))
    api = delivery_api
    base = live.base
    with pytest.raises(ProjectVerificationError), api.open_product(
        live.store, json_map(ref), base.tenant_id, base.run_id, string(json_map(payload["verification"])["source_sha256"]),
    ):
        pytest.fail("noncanonical stored metadata exposed a product")


@pytest.mark.parametrize("publish_failure", [False, True])
async def test_product_through_durable_private_artifact_and_cold_owner(
    durable_case: Case, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, publish_failure: bool,  # noqa: F811
) -> None:
    case = durable_case
    streams = attach_product(case.executor, monkeypatch)
    store = GeneratedFileStore(tmp_path / "durable-products")
    owner = case.owner()
    owner.generated_file_store = store
    if publish_failure:
        async def unavailable(*args: object, **kwargs: object) -> Never:
            raise OSError("private artifact unavailable")
        monkeypatch.setattr(case.artifacts, "put", unavailable)
        with case.bind(), pytest.raises(ProjectVerificationError):
            await owner.verify(**case.arguments())
        assert not list((tmp_path / "durable-products").rglob("*.whl"))
    else:
        with case.bind():
            payload = await owner.verify(**case.arguments())
        cold = case.owner()
        cold.generated_file_store = store
        with case.bind():
            restored = await cold.restore(**case.arguments())
            ref = await cold.require_current_python_product(case.db.row.tenant_id, case.db.row.id)
        assert restored == payload and ref == payload["python_product"]
        assert len(list((tmp_path / "durable-products").rglob("*.whl"))) == 1
    assert streams and all(stream.closed for stream in streams)
