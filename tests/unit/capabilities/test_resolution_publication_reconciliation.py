"""A lost publication response permits one fresh, read-only reconciliation."""

import asyncio
import time
from collections.abc import AsyncIterator
from contextlib import ExitStack, asynccontextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from agent_hub.capabilities.execution_authority import (
    ExecutionAuthorityError,
    bind_execution_authority,
)
from agent_hub.capabilities.python_resolution_authorization import InvalidResolutionAuthorization
from agent_hub.capabilities.python_resolution_preparation import (
    PythonResolutionPreparation,
    ResolutionApprovalRequired,
    resolution_action,
)
from agent_hub.capabilities.types import CapabilityRequest
from agent_hub.models.physical_admission import _PhysicalAdmissionStopped
from agent_hub.runs.repository import RunRecord
from agent_hub.skills.sandbox.python_registry_protocol import DependencyNeed
from agent_hub.skills.sandbox.python_resolution_protocol import (
    ResolutionPlan,
    ResolutionRequest,
    ResolutionStatus,
    resolution_key,
)
from agent_hub.subprocess_cleanup import ProcessCleanupIncomplete
from tests.unit.capabilities import test_python_resolution_preparation as fixtures
from tests.unit.skills import test_python_registry_read as registry

live = fixtures.live
offline_network = registry.offline_network


@pytest.mark.parametrize("opaque", [False, True])
async def test_lost_published_response_is_inspected_once_under_original_plan(
    live: fixtures.Live, opaque: bool,
) -> None:
    calls: list[ResolutionRequest] = []
    needs = (DependencyNeed("demo", "a" * 64),)
    environment = registry.environment()

    async def current() -> RunRecord:
        return live.row

    async def approved(request: CapabilityRequest) -> bool:
        assert request.arguments["purpose"] == "python_dependency_publication"
        assert request.arguments["plan_sha256"] == plan.sha256
        return True

    async def transport(request: ResolutionRequest) -> ResolutionStatus:
        calls.append(request)
        assert request.expected_plan_sha256 == plan.sha256
        assert request.needs == needs and request.environment == environment
        if request.action == "publish_python_resolution":
            # Root committed a real-shaped result; its response was lost.
            published = ResolutionStatus(state="published", key=request.key, plan=plan)
            assert published.plan is not None
            raise ConnectionResetError("response lost after commit")
        assert request.action == "inspect_python_resolution"
        return ResolutionStatus(state="published", key=request.key, plan=None if opaque else plan)

    client = PythonResolutionPreparation(current=current, approved=approved, transport=transport)
    with bind_execution_authority(live.context, worker_id="worker", actor="implementer"):
        original = await client._claim(live.planning)
        plan = ResolutionPlan(
            claim=original, resolution_key=resolution_key(original, needs, environment),
            descriptor_sha256="b" * 64, overlay_sha256="c" * 64,
            artifacts=(("demo-1-py3-none-any.whl", "d" * 64, 100),),
        )
        result = await client.publish(plan, needs=needs, environment=environment)
    assert result.state == "published" and result.key == plan.resolution_key
    assert result.plan == plan
    assert [request.action for request in calls] == [
        "publish_python_resolution", "inspect_python_resolution",
    ]
    assert calls[1].claim.original_deadline == original.original_deadline
    assert calls[1].claim.monotonic_deadline <= calls[0].claim.monotonic_deadline
    calls[1].claim.require_continuity(calls[0].claim)


@dataclass
class PublicationCase:
    client: PythonResolutionPreparation
    plan: ResolutionPlan
    live: fixtures.Live
    calls: list[ResolutionRequest]
    failure: BaseException | None
    inspected: ResolutionStatus
    inspect_failure: BaseException | None = None
    approval_failure: BaseException | None = None
    revoke: bool = False
    change: str = "none"
    post_response: bool = False
    change_after_inspect: bool = False
    revoke_after_inspect: bool = False
    tighten_deadline: bool = False

    async def publish(self) -> ResolutionStatus:
        return await self.client.publish(
            self.plan, needs=(DependencyNeed("demo", "a" * 64),),
            environment=registry.environment(),
        )


@asynccontextmanager
async def publication_case(live: fixtures.Live) -> AsyncIterator[PublicationCase]:
    calls: list[ResolutionRequest] = []
    case: PublicationCase

    async def current() -> RunRecord:
        row = live.row
        if calls and (not case.change_after_inspect or len(calls) > 1):
            if case.change == "worker":
                bindings.enter_context(bind_execution_authority(
                    live.context, worker_id="other-worker", actor="implementer",
                ))
                return replace(row, worker_id="other-worker")
            if case.change == "token":
                return replace(row, worker_lease_token=UUID(int=999))
            if case.change == "scope":
                return replace(row, request="different project request")
            if case.change == "lease":
                return replace(row, worker_lease_expires_at=datetime.now(UTC)-timedelta(seconds=1))
            if case.change == "deadline":
                return replace(row, created_at=datetime.now(UTC)-timedelta(seconds=60))
        return row

    async def approved(request: CapabilityRequest) -> bool:
        if calls and case.tighten_deadline:
            await asyncio.sleep(0.3)
        if calls and case.approval_failure is not None:
            raise case.approval_failure
        if calls and case.post_response and len(calls) == 1 and case.failure is not None:
            failure, case.failure = case.failure, None
            raise failure
        return not (calls and case.revoke) and not (len(calls) > 1 and case.revoke_after_inspect)

    async def transport(request: ResolutionRequest) -> ResolutionStatus:
        calls.append(request)
        if request.action == "publish_python_resolution":
            if case.tighten_deadline:
                assert client._original is not None
                client._original = client._original.model_copy(
                    update={"monotonic_deadline": time.monotonic() + 0.05},
                )
            if case.failure is not None and not case.post_response:
                raise case.failure
            return ResolutionStatus(state="published", key=request.key, plan=case.plan)
        assert request.action == "inspect_python_resolution"
        assert request.expected_plan_sha256 == case.plan.sha256
        if case.inspect_failure is not None:
            raise case.inspect_failure
        return case.inspected

    client = PythonResolutionPreparation(current=current, approved=approved, transport=transport)
    with (
        bind_execution_authority(live.context, worker_id="worker", actor="implementer"),
        ExitStack() as bindings,
    ):
        original = await client._claim(live.planning)
        plan = ResolutionPlan(
            claim=original,
            resolution_key=resolution_key(
                original, (DependencyNeed("demo", "a" * 64),), registry.environment(),
            ),
            descriptor_sha256="b" * 64, overlay_sha256="c" * 64,
            artifacts=(("demo-1-py3-none-any.whl", "d" * 64, 100),),
        )
        case = PublicationCase(
            client, plan, live, calls, ConnectionResetError("lost response"),
            ResolutionStatus(state="published", key=plan.resolution_key),
        )
        yield case


async def test_successful_publish_does_not_inspect(live: fixtures.Live) -> None:
    async with publication_case(live) as case:
        case.failure = None
        result = await case.publish()
        assert result.state == "published" and result.plan == case.plan
        assert [r.action for r in case.calls] == ["publish_python_resolution"]


@pytest.mark.parametrize("failure", [
    ConnectionError(), ConnectionAbortedError(), ConnectionRefusedError(),
    BrokenPipeError(), EOFError(), asyncio.IncompleteReadError(b"", 10),
])
async def test_only_finite_transport_losses_are_reconciled(
    live: fixtures.Live, failure: BaseException,
) -> None:
    async with publication_case(live) as case:
        case.failure = failure
        assert (await case.publish()).plan == case.plan
        assert [r.action for r in case.calls] == [
            "publish_python_resolution", "inspect_python_resolution",
        ]


async def test_transport_error_after_published_response_is_also_read_only(
    live: fixtures.Live,
) -> None:
    async with publication_case(live) as case:
        case.post_response = True
        result = await case.publish()
        assert result.state == "published" and result.plan == case.plan
        assert [r.action for r in case.calls] == [
            "publish_python_resolution", "inspect_python_resolution",
        ]


@pytest.mark.parametrize("state", ["unknown", "frozen", "absent", "resolved"])
async def test_nonterminal_inspection_keeps_original_failure(
    live: fixtures.Live, state: str,
) -> None:
    async with publication_case(live) as case:
        case.inspected = ResolutionStatus.model_validate(
            {"state": state, "key": case.plan.resolution_key}, strict=True,
        )
        with pytest.raises(ConnectionResetError) as caught:
            await case.publish()
        assert caught.value is case.failure
        assert len(case.calls) == 2


@pytest.mark.parametrize("change", ["key", "plan", "invalid_status"])
async def test_forged_inspection_never_recovers(live: fixtures.Live, change: str) -> None:
    async with publication_case(live) as case:
        if change == "key":
            case.inspected = ResolutionStatus(state="published", key="f" * 64)
        elif change == "plan":
            case.inspected = ResolutionStatus(
                state="published", key=case.plan.resolution_key,
                plan=case.plan.model_copy(update={"overlay_sha256": "e" * 64}),
            )
        else:
            case.inspected = case.inspected.model_copy(update={"state": "invented"})
        with pytest.raises((ConnectionResetError, ValueError)):
            await case.publish()
        assert len(case.calls) == 2


@pytest.mark.parametrize("failure", [
    ExecutionAuthorityError(), InvalidResolutionAuthorization(),
    _PhysicalAdmissionStopped(), ProcessCleanupIncomplete(), asyncio.CancelledError(),
    TimeoutError(), PermissionError(), RuntimeError("unknown"), ValueError("unknown"),
    KeyError("unknown"), AssertionError("unknown"),
])
async def test_cancellation_authority_cleanup_budget_and_unknown_errors_do_not_inspect(
    live: fixtures.Live, failure: BaseException,
) -> None:
    async with publication_case(live) as case:
        case.failure = failure
        with pytest.raises(type(failure)) as caught:
            await case.publish()
        assert caught.value is failure
        assert [r.action for r in case.calls] == ["publish_python_resolution"]


async def test_approval_required_does_not_inspect(live: fixtures.Live) -> None:
    async with publication_case(live) as case:
        failure = ResolutionApprovalRequired(resolution_action(
            case.plan.claim, (DependencyNeed("demo", "a" * 64),), registry.environment(),
            plan=case.plan,
        ))
        case.failure = failure
        with pytest.raises(ResolutionApprovalRequired) as caught:
            await case.publish()
        assert caught.value is failure and len(case.calls) == 1


@pytest.mark.parametrize("failure", [
    ExecutionAuthorityError(), InvalidResolutionAuthorization(),
    _PhysicalAdmissionStopped(), ProcessCleanupIncomplete(), asyncio.CancelledError(),
    KeyError("unclassified callback failure"),
])
async def test_post_response_callback_failures_are_not_mistaken_for_transport_loss(
    live: fixtures.Live, failure: BaseException,
) -> None:
    async with publication_case(live) as case:
        case.failure, case.approval_failure = None, failure
        with pytest.raises(type(failure)) as caught:
            await case.publish()
        assert caught.value is failure
        assert [r.action for r in case.calls] == ["publish_python_resolution"]


@pytest.mark.parametrize("change", ["worker", "token", "scope", "lease", "deadline"])
async def test_fresh_authority_changes_cannot_be_hidden_by_publication_receipt(
    live: fixtures.Live, change: str,
) -> None:
    async with publication_case(live) as case:
        case.change = change
        with pytest.raises((ValueError, RuntimeError)):
            await case.publish()
        assert [r.action for r in case.calls] == ["publish_python_resolution"]


async def test_revoked_publication_approval_prevents_reconciliation(live: fixtures.Live) -> None:
    async with publication_case(live) as case:
        case.revoke = True
        with pytest.raises(ResolutionApprovalRequired):
            await case.publish()
        assert len(case.calls) == 1


async def test_inspection_loss_is_not_retried(live: fixtures.Live) -> None:
    async with publication_case(live) as case:
        case.inspect_failure = ConnectionResetError("inspection also lost")
        with pytest.raises(ConnectionResetError) as caught:
            await case.publish()
        assert caught.value is case.failure
        assert len(case.calls) == 2


@pytest.mark.parametrize("change", ["worker", "token", "scope", "lease", "deadline"])
async def test_authority_changes_during_inspection_still_deny_success(
    live: fixtures.Live, change: str,
) -> None:
    async with publication_case(live) as case:
        case.change, case.change_after_inspect = change, True
        with pytest.raises((ValueError, RuntimeError)):
            await case.publish()
        assert len(case.calls) == 2


async def test_approval_revoked_during_inspection_still_denies_success(live: fixtures.Live) -> None:
    async with publication_case(live) as case:
        case.revoke_after_inspect = True
        with pytest.raises(ResolutionApprovalRequired):
            await case.publish()
        assert len(case.calls) == 2


async def test_inspect_checks_actual_source_before_accepting_a_receipt(live: fixtures.Live) -> None:
    async with publication_case(live) as case:
        case.inspect_failure = ValueError("resolution_source_changed")
        with pytest.raises(ValueError, match="resolution_source_changed"):
            await case.publish()
        assert len(case.calls) == 2


async def test_unknown_transport_subclass_is_not_silently_reclassified(live: fixtures.Live) -> None:
    class UnknownConnectionFailure(ConnectionError):
        pass

    async with publication_case(live) as case:
        case.failure = UnknownConnectionFailure()
        with pytest.raises(UnknownConnectionFailure):
            await case.publish()
        assert len(case.calls) == 1


async def test_unknown_inspection_error_keeps_its_diagnostic_type(live: fixtures.Live) -> None:
    class UnknownInspectionFailure(ConnectionError):
        pass

    async with publication_case(live) as case:
        failure = UnknownInspectionFailure()
        case.inspect_failure = failure
        with pytest.raises(UnknownInspectionFailure) as caught:
            await case.publish()
        assert caught.value is failure and len(case.calls) == 2


async def test_other_needs_cannot_reuse_the_published_plan(live: fixtures.Live) -> None:
    async with publication_case(live) as case:
        with pytest.raises(ValueError, match="resolution_scope_changed"):
            await case.client.publish(
                case.plan, needs=(DependencyNeed("other", "a" * 64),),
                environment=registry.environment(),
            )
        assert case.calls == []


async def test_reconciliation_approval_cannot_outlive_the_tightened_budget(
    live: fixtures.Live,
) -> None:
    async with publication_case(live) as case:
        case.tighten_deadline = True
        with pytest.raises(TimeoutError):
            await case.publish()
        assert len(case.calls) == 1


