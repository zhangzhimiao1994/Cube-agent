"""Private failed metadata only; real receipt DAO/ArtifactRepository, no native execution."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from dataclasses import replace
from typing import Unpack
from uuid import UUID, uuid4

import pytest

from agent_hub.capabilities.execution_diagnostics import ExecutionDiagnostic
from agent_hub.capabilities.project_verification import (
    ProjectVerificationError,
    ProjectVerificationService,
)
from agent_hub.capabilities.python_project_reports import PythonProjectFailure
from agent_hub.runs.project_verification_receipts import (
    RECEIPT_KEY,
    VerificationAuthority,
    VerificationRecord,
)
from agent_hub.runtime.artifacts import ArtifactReference
from agent_hub.runtime.contracts import Artifact, JsonValue
from agent_hub.skills.sandbox.base import SkillResult
from agent_hub.skills.sandbox.python_project_protocol import (
    PythonProjectBrokerResponse,
    PythonProjectInvocation,
)
from tests.unit.capabilities.durable_capture_typed_fixture import (
    Case,
    CompleteArgs,
    PrivateVerifyArgs,
    WriteArgs,
    assign_attribute,
    case,
    mapping,
    present,
    string,
)

__all__ = ["case"]
SUMMARY = "FAIL: subtest_one\nAssertionError: expected first\nFAIL: subtest_two\nAssertionError: expected second\nRan 4 tests in 0.001s\n\nFAILED (failures=2)\n"


def configure(
    case: Case, monkeypatch: pytest.MonkeyPatch, text: str = SUMMARY
) -> ProjectVerificationService:
    case.executor.failure_index = 2
    original = case.executor.run

    async def run(invocation: PythonProjectInvocation) -> PythonProjectBrokerResponse:
        response = await original(invocation)
        assert response.result is not None
        report = json.loads(response.result.stdout)
        check = report["checks"][2]
        check.update(stderr=text, stderr_observed_bytes=len(text.encode()), stderr_truncated=False)
        return response.model_copy(
            update={
                "result": SkillResult(
                    exit_code=0, timed_out=False, stdout=json.dumps(report), stderr=""
                )
            }
        )

    monkeypatch.setattr(case.executor, "run", run)
    return case.owner()


async def fail(case: Case, owner: ProjectVerificationService) -> ProjectVerificationError:
    with case.bind(), pytest.raises(ProjectVerificationError) as caught:
        await owner.verify(**case.arguments())
    return caught.value


async def body(case: Case) -> Artifact:
    index = case.index()
    assert index.status == "failed" and len(index.artifact_references) == 1
    found = await case.artifacts.get_many(
        case.db.row.tenant_id, case.db.row.id, index.artifact_references
    )
    assert len(found) == 1
    return found[0]


async def test_real_python_failure_persists_safe_bound_private_report_only(
    case: Case, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = configure(case, monkeypatch)
    error = await fail(case, owner)
    artifact = await body(case)
    assert artifact.type == "project_verification_failure"
    assert artifact.producer == "trusted_project_failure_verifier"
    assert artifact.provenance is None and artifact.source_ids == ()
    index, value = (case.index(), artifact.content)
    assert value["kind"] == case.kind and value["status"] == "failed"
    assert value["scope"] == index.authority.stable_scope()
    assert value["generation"] == index.generation and value["action_key"] == index.action_key
    assert (
        value["execution_id"] == index.execution_id
        and value["source_sha256"] == index.source_sha256
    )
    assert type(error.python_project_failure) is PythonProjectFailure
    result = mapping(value["result"])
    assert result["report"] == error.python_project_failure.to_payload()
    assert result["diagnosis"] == {
        "category": "functional_assertion",
        "reported_test_count": 4,
        "reported_failures": 2,
        "reported_errors": 0,
        "preview_truncated": False,
        "observed_summary_only": False,
    }
    encoded = json.dumps(value, default=dict)
    assert "expected first" not in encoded and SUMMARY not in encoded
    assert 'stdout"' not in encoded and 'stderr"' not in encoded
    assert str(index.authority.execution_token) not in encoded
    case.assert_no_receipts(owner)
    assert not present(owner._durable)._issued


@pytest.mark.parametrize(
    ("text", "category", "counts"),
    [
        (
            "ImportError: failed to import module\nRan 1 test in 0.001s\n\nFAILED (errors=1)\n",
            "import_error",
            (1, 0, 1),
        ),
        (
            "SyntaxError: invalid syntax\nRan 1 test in 0.001s\n\nFAILED (errors=1)\n",
            "syntax_error",
            (1, 0, 1),
        ),
        (
            "ERROR collecting tests/test_sample.py\ninterrupted during collection\n",
            "collection_error",
            (None, None, None),
        ),
        ("AssertionError: no trustworthy test summary\n", "unknown", (None, None, None)),
        ("Ran 4 tests in 0.001s\n\nFAILED (failures=7)\n", "unknown", (4, 7, 0)),
    ],
)
async def test_finite_classification_counts_do_not_invent_execution_results(
    case: Case,
    monkeypatch: pytest.MonkeyPatch,
    text: str,
    category: str,
    counts: tuple[int | None, int | None, int | None],
) -> None:
    await fail(case, configure(case, monkeypatch, text))
    diagnosis = mapping(mapping((await body(case)).content["result"])["diagnosis"])
    assert diagnosis["category"] == category
    assert (
        tuple(diagnosis[k] for k in ("reported_test_count", "reported_failures", "reported_errors"))
        == counts
    )


async def test_no_preview_keeps_safe_metadata_but_never_restores_logs(
    case: Case, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = configure(case, monkeypatch)
    original = owner._verify

    async def metadata_only(**kwargs: Unpack[PrivateVerifyArgs]) -> Mapping[str, JsonValue]:
        try:
            return await original(**kwargs)
        except ProjectVerificationError as error:
            failure = error.python_project_failure
            assert type(failure) is PythonProjectFailure
            safe = PythonProjectFailure(failure.report, failure.environment_reference)
            raise ProjectVerificationError(
                "python_project_checks_failed", python_project_failure=safe
            ) from None

    monkeypatch.setattr(owner, "_verify", metadata_only)
    await fail(case, owner)
    diagnosis = mapping(mapping((await body(case)).content["result"])["diagnosis"])
    assert diagnosis["category"] == "unknown" and diagnosis["reported_test_count"] is None


async def test_redacted_or_clipped_preview_cannot_supply_counts_or_persist_text(
    case: Case, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw = "password=PRIVATE_CREDENTIAL_SENTINEL\n" + SUMMARY
    await fail(case, configure(case, monkeypatch, raw))
    artifact = await body(case)
    assert mapping(mapping(artifact.content["result"])["diagnosis"])["category"] == "unknown"
    assert mapping(mapping(artifact.content["result"])["diagnosis"])["reported_test_count"] is None
    assert "PRIVATE_CREDENTIAL_SENTINEL" not in json.dumps(artifact.to_payload(), default=dict)


@pytest.mark.parametrize("fault", ["source", "execution", "untyped", "diagnostic"])
async def test_false_or_unbound_failure_does_not_issue_native_evidence(
    case: Case, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    owner = configure(case, monkeypatch)
    original = owner._verify

    async def malformed(**kwargs: Unpack[PrivateVerifyArgs]) -> Mapping[str, JsonValue]:
        try:
            return await original(**kwargs)
        except ProjectVerificationError as error:
            observed = error.python_project_failure
            assert type(observed) is PythonProjectFailure
            if fault in ("source", "execution"):
                report = dict(observed.report)
                if fault == "source":
                    report["source_sha256"] = "0" * 64
                else:
                    report["execution_id"] = "foreign_execution"
                # Keep reference consistent for the DTO; durable still must bind issuance.
                if fault == "execution":
                    report["dependency_install"] = {
                        "mode": "reused",
                        "origin_execution_id": string(
                            mapping(report["dependency_install"])["origin_execution_id"]
                        ),
                    }
                try:
                    forged = PythonProjectFailure(
                        report, observed.environment_reference, observed.diagnostic
                    )
                except ValueError:
                    forged = object.__new__(PythonProjectFailure)
                    object.__setattr__(forged, "report", report)
                    object.__setattr__(
                        forged, "environment_reference", observed.environment_reference
                    )
                    object.__setattr__(forged, "diagnostic", observed.diagnostic)
                error.python_project_failure = forged
            elif fault == "untyped":
                assign_attribute(error, "python_project_failure", observed.to_payload())
            else:
                error.diagnostic = ExecutionDiagnostic(
                    "systemd",
                    SkillResult(
                        exit_code=1, timed_out=False, stdout="", stderr="RAW_MISMATCH_SENTINEL"
                    ),
                )
            raise

    monkeypatch.setattr(owner, "_verify", malformed)
    error = await fail(case, owner)
    assert str(error) == "project verification receipt is unavailable or changed"
    assert case.index().status == "running" and (not case.index().artifact_references)
    assert not case.artifacts._artifacts


@pytest.mark.parametrize("when", ["before", "after", "cancel"])
async def test_failed_cas_unknown_retains_body_and_never_aborts_or_authorizes(
    case: Case, monkeypatch: pytest.MonkeyPatch, when: str
) -> None:
    owner = configure(case, monkeypatch)
    complete = present(owner._durable).receipts.complete
    aborted: list[tuple[object, ...]] = []

    async def uncertain(
        authority: VerificationAuthority, **kwargs: Unpack[CompleteArgs]
    ) -> VerificationRecord:
        assert kwargs["status"] == "failed" and len(kwargs["artifact_references"]) == 1
        if when == "after":
            await complete(authority, **kwargs)
        if when == "cancel":
            raise asyncio.CancelledError
        raise OSError("PRIVATE_STORAGE_SENTINEL")

    async def abort(*args: object, **kwargs: object) -> bool:
        aborted.append(args)
        pytest.fail("CAS UNKNOWN may not clean possibly committed private evidence")

    monkeypatch.setattr(present(owner._durable).receipts, "complete", uncertain)
    monkeypatch.setattr(case.artifacts, "abort_write", abort)
    if when == "cancel":
        with case.bind(), pytest.raises(asyncio.CancelledError):
            await owner.verify(**case.arguments())
    else:
        error = await fail(case, owner)
        assert str(error) == "project verification receipt is unavailable or changed"
    assert not aborted and len(case.artifacts._artifacts) == 1
    assert case.index().status == ("failed" if when == "after" else "running")
    case.assert_no_receipts(owner)
    calls = len(case.executor.calls)
    with case.bind(), pytest.raises(ProjectVerificationError):
        await case.owner().restore(**case.arguments())
    if when != "after":
        with case.bind(), pytest.raises(ProjectVerificationError):
            await case.owner().verify(**case.arguments(action="no-unknown-replay"))
    assert len(case.executor.calls) == calls


@pytest.mark.parametrize("fault", ["lease", "source"])
async def test_pre_cas_failure_aborts_only_its_own_uncommitted_write(
    case: Case, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    owner = configure(case, monkeypatch)
    other_run = uuid4()
    keep = Artifact(id=uuid4(), type="text", producer="fixture", content={"text": "unrelated"})
    await case.artifacts.put(case.db.row.tenant_id, other_run, keep)
    original_put = case.artifacts.put

    async def changed(
        tenant_id: UUID, run_id: UUID, artifact: Artifact, **kwargs: Unpack[WriteArgs]
    ) -> None:
        await original_put(tenant_id, run_id, artifact, **kwargs)
        if fault == "lease":
            case.db.new_lease()
        elif fault == "source":
            current = case.root / str(case.db.row.tenant_id) / "projects"
            target = next(current.rglob("README.md"), None)
            if target is None:
                target = next(current.rglob("*.py"))
            target.write_bytes(b"changed synthetic input")
        else:
            raise OSError("RAW_PUT_SENTINEL")

    monkeypatch.setattr(case.artifacts, "put", changed)
    error = await fail(case, owner)
    assert "RAW_PUT_SENTINEL" not in str(error)
    assert case.index().status == "running" and (not case.index().artifact_references)
    ref = ArtifactReference(keep.id, keep.content_sha256)
    assert await case.artifacts.get_many(case.db.row.tenant_id, other_run, (ref,)) == (keep,)
    assert len(case.artifacts._artifacts) == 1


async def test_failed_body_can_never_be_recovered_as_passed_even_with_relabelled_index(
    case: Case, monkeypatch: pytest.MonkeyPatch
) -> None:
    await fail(case, configure(case, monkeypatch))
    await body(case)
    index = case.index()
    case.db.new_lease()
    owner = case.owner()
    with case.bind(), pytest.raises(ProjectVerificationError):
        await owner.restore(**case.arguments())
    present(case.db.row.routing_decision)[RECEIPT_KEY] = replace(
        index, status="passed"
    ).to_payload()
    with case.bind(), pytest.raises(ProjectVerificationError):
        await owner.restore(**case.arguments())
    assert len(case.executor.calls) == 1
    case.assert_no_receipts(owner)


async def test_success_identity_and_cold_restore_remain_unchanged(case: Case) -> None:
    case.executor.failure_index = None
    owner = case.owner()
    with case.bind():
        result = await owner.verify(**case.arguments())
    index = case.index()
    artifacts = await case.artifacts.get_many(
        case.db.row.tenant_id, case.db.row.id, index.artifact_references
    )
    assert len(artifacts) == 1 and artifacts[0].type == "project_verification_receipt"
    assert artifacts[0].content["result"] == result and "diagnosis" not in artifacts[0].content
    case.db.new_lease()
    with case.bind():
        assert await case.owner().restore(**case.arguments()) == result
    assert len(case.executor.calls) == 1


@pytest.mark.parametrize(
    ("tail", "redacted", "expected"),
    [
        ("Ran 4 tests in 0.001s\n\nFAILED (failures=7)\n", False, (4, 7, 0)),
        ("Ran 4 tests in 0.001s\n\nFAILED (failures=", False, (None, None, None)),
        ("Ran 4 tests in 0.001s\n\nFAILED (failures=7)\n", True, (None, None, None)),
    ],
)
async def test_truncated_unittest_tail_retains_only_complete_observed_summary(
    case: Case,
    monkeypatch: pytest.MonkeyPatch,
    tail: str,
    redacted: bool,
    expected: tuple[int | None, int | None, int | None],
) -> None:
    # Above the diagnostic 4096-byte limit, below native phase capture; real decoder retains tail.
    text = (
        "native preamble\n" * 350 + ("password=PRIVATE_TAIL_SENTINEL\n" if redacted else "") + tail
    )
    owner = configure(case, monkeypatch, text)
    error = await fail(case, owner)
    assert (
        error.diagnostic is not None
        and mapping(error.diagnostic.to_payload()["stderr_metadata"])["truncated"] is True
    )
    diagnosis = mapping(mapping((await body(case)).content["result"])["diagnosis"])
    assert diagnosis["category"] == "unknown" and diagnosis["preview_truncated"] is True
    assert (
        tuple(diagnosis[k] for k in ("reported_test_count", "reported_failures", "reported_errors"))
        == expected
    )
    assert diagnosis["observed_summary_only"] is (expected[0] is not None)
    assert tail not in json.dumps((await body(case)).to_payload(), default=dict)


async def test_untyped_backend_error_never_becomes_private_failure_evidence(
    case: Case, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = configure(case, monkeypatch)
    original = owner._verify

    async def untyped(**kwargs: Unpack[PrivateVerifyArgs]) -> Mapping[str, JsonValue]:
        try:
            return await original(**kwargs)
        except ProjectVerificationError:
            raise OSError("PRIVATE_BACKEND_ERROR_SENTINEL") from None

    monkeypatch.setattr(owner, "_verify", untyped)
    error = await fail(case, owner)
    assert str(error) == "project verification receipt is unavailable or changed"
    assert error.diagnostic is None and error.python_project_failure is None
    assert case.index().status == "running" and (not case.artifacts._artifacts)


@pytest.mark.parametrize("fault", ["put", "quota"])
async def test_confirmed_aborted_storage_failure_keeps_native_error_and_allows_repair(
    case: Case, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    from agent_hub.files.workspace import ProjectWorkspaceStore
    from agent_hub.runtime.artifacts import InMemoryArtifactRepository

    if fault == "quota":
        case.artifacts = InMemoryArtifactRepository(max_artifact_bytes=1)
    owner = configure(case, monkeypatch)
    native_errors: list[ProjectVerificationError] = []
    original_verify = owner._verify

    async def observed(**kwargs: Unpack[PrivateVerifyArgs]) -> Mapping[str, JsonValue]:
        try:
            return await original_verify(**kwargs)
        except ProjectVerificationError as error:
            native_errors.append(error)
            raise

    monkeypatch.setattr(owner, "_verify", observed)
    if fault == "put":
        original_put = case.artifacts.put

        async def failed_put(
            tenant_id: UUID, run_id: UUID, artifact: Artifact, **kwargs: Unpack[WriteArgs]
        ) -> None:
            await original_put(tenant_id, run_id, artifact, **kwargs)
            raise OSError("PRIVATE_DIAGNOSTIC_STORAGE_SENTINEL")

        monkeypatch.setattr(case.artifacts, "put", failed_put)
    error = await fail(case, owner)
    assert error is native_errors[0] and type(error.python_project_failure) is PythonProjectFailure
    assert error.diagnostic is error.python_project_failure.diagnostic
    assert error.diagnostic is not None
    assert case.index().status == "failed" and (not case.index().artifact_references)
    assert not case.artifacts._artifacts and (not present(owner._durable)._issued)
    with case.bind(), pytest.raises(ProjectVerificationError):
        await case.owner().verify(**case.arguments())
    assert len(case.executor.calls) == 1
    row = case.db.row
    current = ProjectWorkspaceStore(case.root).session_root(
        row.tenant_id,
        string(present(row.routing_decision)["project_id"]),
        string(present(row.routing_decision)["workspace_session_id"]),
    )
    (current / "src/diagnostic_repair.py").write_text("REPAIRED = True\n")
    source = case.index().source_sha256
    # Capacity/service is available again; the next ordinary native attempt is independent.
    case.artifacts = InMemoryArtifactRepository()
    case.executor.failure_index = None
    with case.bind():
        await case.owner().verify(**case.arguments(action="python-check-repaired"))
    assert case.index().status == "passed" and case.index().generation == 2
    assert case.index().source_sha256 != source and len(case.executor.calls) == 2


async def test_unconfirmed_abort_cannot_downgrade_to_metadata_only_failed(
    case: Case, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = configure(case, monkeypatch)
    original_put = case.artifacts.put

    async def failed_put(
        tenant_id: UUID, run_id: UUID, artifact: Artifact, **kwargs: Unpack[WriteArgs]
    ) -> None:
        await original_put(tenant_id, run_id, artifact, **kwargs)
        raise OSError("PRIVATE_PUT_WITHOUT_ABORT_ACK")

    async def no_ack(*args: object, **kwargs: object) -> bool:
        return False

    monkeypatch.setattr(case.artifacts, "put", failed_put)
    monkeypatch.setattr(case.artifacts, "abort_write", no_ack)
    error = await fail(case, owner)
    assert error.python_project_failure is None and case.index().status == "running"
    assert len(case.artifacts._artifacts) == 1


@pytest.mark.parametrize("when", ["before", "after", "cancel"])
async def test_fallback_cas_unknown_is_never_retried_or_cleaned(
    case: Case, monkeypatch: pytest.MonkeyPatch, when: str
) -> None:
    from agent_hub.runtime.artifacts import InMemoryArtifactRepository

    case.artifacts = InMemoryArtifactRepository(max_artifact_bytes=1)
    owner = configure(case, monkeypatch)
    original_complete = present(owner._durable).receipts.complete
    attempts: list[CompleteArgs] = []

    async def uncertain(
        authority: VerificationAuthority, **kwargs: Unpack[CompleteArgs]
    ) -> VerificationRecord:
        attempts.append(kwargs)
        assert kwargs["status"] == "failed" and (not kwargs.get("artifact_references"))
        if when == "after":
            await original_complete(authority, **kwargs)
        if when == "cancel":
            raise asyncio.CancelledError
        raise OSError("PRIVATE_FALLBACK_CAS_SENTINEL")

    monkeypatch.setattr(present(owner._durable).receipts, "complete", uncertain)
    if when == "cancel":
        with case.bind(), pytest.raises(asyncio.CancelledError):
            await owner.verify(**case.arguments())
    else:
        error = await fail(case, owner)
        assert error.python_project_failure is None and "PRIVATE_FALLBACK" not in str(error)
    assert len(attempts) == 1 and (not case.artifacts._artifacts)
    assert case.index().status == ("failed" if when == "after" else "running")
    assert not present(owner._durable)._issued
