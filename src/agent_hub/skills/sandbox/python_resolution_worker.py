"""Narrow Root resolution/preparation handler using existing fetch/cache/index workers."""

from __future__ import annotations

import asyncio
import shutil
import sys
import time
from collections.abc import Awaitable, Callable
from contextlib import ExitStack
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, cast

from agent_hub.capabilities.project_baselines import ProjectBaselineReference, StoredProjectBaseline
from agent_hub.capabilities.python_dependency_resolution import resolve_dependencies
from agent_hub.capabilities.python_original_private_recipe import original_dependency_needs
from agent_hub.capabilities.scoped_read import _read_no_links
from agent_hub.skills.sandbox import python_dependency_index_store as index_store
from agent_hub.skills.sandbox.project import _uninterrupted, project_source_fingerprint
from agent_hub.skills.sandbox.project_contract_broker import (
    _Observed,
    _root,
    stage_original_contract,
)
from agent_hub.skills.sandbox.python_dependency_metadata import extract_python_wheel_metadata
from agent_hub.skills.sandbox.python_input_fetch import WheelDownload
from agent_hub.skills.sandbox.python_input_publish import publish_verified_wheel
from agent_hub.skills.sandbox.python_input_supply_catalog import (
    RootSupplyConfig,
    read_supply_configuration,
)
from agent_hub.skills.sandbox.python_input_supply_worker import (
    fetch_catalog_wheels,
    validate_configuration,
)
from agent_hub.skills.sandbox.python_metadata_planning import _controls, _source
from agent_hub.skills.sandbox.python_metadata_preparation_worker import _claim
from agent_hub.skills.sandbox.python_original_root_recipe import standard_original_intent
from agent_hub.skills.sandbox.python_project_backend import (
    RootConfig,
    _admission,
    read_root_configuration,
)
from agent_hub.skills.sandbox.python_registry_fetch import BrokerRegistryReader as RegistryReader
from agent_hub.skills.sandbox.python_registry_protocol import (
    ResolutionBudget,
    ResolutionLimits,
    digest,
    need,
)
from agent_hub.skills.sandbox.python_resolution_protocol import (
    MAX_CONTROL,
    ProtectedOriginalResolutionPlanning,
    ResolutionClaim,
    ResolutionFailurePhase,
    ResolutionRequest,
    ResolutionStatus,
    decode,
    decode_descriptor,
)
from agent_hub.skills.sandbox.python_resolution_store import (
    ResolutionLeaseWitness,
    ResolutionStore,
    overlay_supply,
)
from agent_hub.subprocess_cleanup import ProcessCleanupIncomplete

if TYPE_CHECKING:
    from agent_hub.skills.sandbox.broker import BrokerPolicy
    from agent_hub.skills.sandbox.project import ProjectExecutionBroker


async def _context(
    request: ResolutionRequest,
    policy: BrokerPolicy,
    root: RootConfig,
    supply: RootSupplyConfig,
    deadline: float,
    *,
    current_claim: Callable[[], ResolutionClaim] | None = None,
    original: StoredProjectBaseline | None = None,
) -> None:
    def check_lease() -> None:
        live = request.claim if current_claim is None else current_claim()
        live.require_continuity(request.claim)
        need(live.planning == request.claim.planning, "resolution_scope_changed")
        need(time.monotonic() < min(deadline, live.deadline), "resolution_deadline")

    check_lease()
    need(
        digest(read_root_configuration().to_payload())
        == digest(root.to_payload())
        == request.claim.planning.expected_root_config_sha256
        and read_supply_configuration().digest
        == supply.digest
        == request.claim.planning.expected_catalog_sha256,
        "resolution_configuration_changed",
    )
    validate_configuration(policy, root, supply)
    planning = request.claim.planning
    if isinstance(planning, ProtectedOriginalResolutionPlanning):
        need(await project_source_fingerprint(policy.project_workspace_root, request.invocation)
             == planning.draft.source_sha256, "resolution_source_changed")
    else:
        await _source(planning, policy, deadline)
    _resolution_controls(request, policy, root, deadline, original)
    env = request.environment
    need(
        env["implementation_name"] == root.interpreter.implementation
        and env["python_full_version"] == root.interpreter.version
        and "linux_" + env["platform_machine"] == root.interpreter.platform,
        "resolution_environment_changed",
    )
    check_lease()
    need(
        digest(read_root_configuration().to_payload())
        == request.claim.planning.expected_root_config_sha256
        and read_supply_configuration().digest == supply.digest,
        "resolution_configuration_changed",
    )


def _resolution_controls(
    request: ResolutionRequest, policy: BrokerPolicy, root: RootConfig, deadline: float,
    original: StoredProjectBaseline | None,
) -> None:
    planning = request.claim.planning
    if not isinstance(planning, ProtectedOriginalResolutionPlanning):
        _controls(planning, policy, deadline)
        return
    need(original is not None, "resolution_source_changed")
    assert original is not None
    expected = planning.draft
    need(original.reference.to_payload() == expected.original_reference.model_dump()
         and original.contract.protected_sha256 == expected.protected_sha256,
         "resolution_source_changed")
    raw = _read_no_links(original.snapshot, ("pyproject.toml",),
                         max_bytes=min(request.invocation.source_limit_bytes, 256 * 1024))
    intent = standard_original_intent(original, raw)
    need(intent is not None and intent.manifest_sha256 == expected.manifest_sha256,
         "resolution_source_changed")
    assert intent is not None
    from agent_hub.skills.sandbox.python_dependency_inspection import _target_environment

    target = _target_environment(root)
    need(request.environment == target, "resolution_environment_changed")
    need(request.needs == original_dependency_needs(intent, target), "resolution_requirements_changed")
    need(time.monotonic() < deadline, "resolution_deadline")


async def _fetch(
    entry: WheelDownload,
    request: ResolutionRequest,
    policy: BrokerPolicy,
    root: RootConfig,
    supply: RootSupplyConfig,
    deadline: float,
) -> bytes:
    """Existing DynamicUser fetch unit owns all wheel IO; cancel and drain that exact task."""
    task = asyncio.create_task(
        fetch_catalog_wheels(
            (entry,), request.claim.planning.invocation, policy, root, supply, deadline
        )
    )
    try:
        result = await asyncio.shield(task)
    except BaseException:
        task.cancel()

        async def drain() -> None:
            results = await asyncio.gather(task, return_exceptions=True)
            if isinstance(results[0], ProcessCleanupIncomplete):
                raise results[0]

        await _uninterrupted(drain())
        raise
    need(set(result) == {entry.filename}, "resolution_artifact_invalid")
    return result[entry.filename]


async def execute_resolution(
    request: ResolutionRequest, *, policy: BrokerPolicy, root: RootConfig, supply: RootSupplyConfig,
    lease_witness: ResolutionLeaseWitness | None = None, stage: Path | None = None,
) -> ResolutionStatus:
    if lease_witness is None:
        # Non-renewing callers retain the original short lease bound.
        return await _execute_resolution(request, policy=policy, root=root, supply=supply, stage=stage)
    return await _await_resolution_with_root_lease(request,
        operation=_execute_resolution(request, policy=policy, root=root, supply=supply,
            current_claim=lease_witness.current, stage=stage), current=lease_witness.current)


async def _execute_resolution(
    request: ResolutionRequest, *, policy: BrokerPolicy, root: RootConfig, supply: RootSupplyConfig,
    current_claim: Callable[[], ResolutionClaim] | None = None,
    stage: Path | None = None,
) -> ResolutionStatus:
    """Root peer validation is done by handle_resolution; all persisted data is re-read.

    No descriptor is accepted in the IPC request. Registry reads use fixed pinned
    transport; downloaded quarantine bytes are validated by the actual resolver.
    Cache publication requires a separately approved exact frozen plan in service.
    """
    index_store._native()
    request = ResolutionRequest.model_validate(request, strict=True)
    root, supply = replace(root), replace(supply)
    deadline = request.claim.deadline if current_claim is None else request.claim.task_deadline
    original = None
    if isinstance(request.claim.planning, ProtectedOriginalResolutionPlanning):
        need(stage is not None and stage.parent == root.stage_root, "resolution_stage_invalid")
        assert stage is not None
        original = stage_original_contract(
            policy.project_workspace_root, request.invocation,
            ProjectBaselineReference.from_payload(request.claim.planning.draft.original_reference.model_dump()),
            stage, policy.allowed_uid,
        )

    def active() -> ResolutionRequest:
        nonlocal request
        if current_claim is not None:
            current = current_claim()
            current.require_continuity(request.claim)
            need(current.planning == request.claim.planning, "resolution_scope_changed")
            request = request.model_copy(update={"claim": current})
        return request

    live_claim = None if current_claim is None else lambda: active().claim
    async def context() -> None:
        if original is None:
            await _context(active(), policy, root, supply, deadline, current_claim=live_claim)
        else:
            await _context(active(), policy, root, supply, deadline, current_claim=live_claim, original=original)

    await context()
    store = ResolutionStore(supply)
    status = store.inspect(request.key, request.claim)
    if request.action == "inspect_python_resolution":
        if request.expected_plan_sha256 is not None:
            return store.inspect_publication(request.key,request.claim,request.expected_plan_sha256)
        return status
    if request.action == "resolve_python_inputs":
        need(status.state == "absent", "resolution_consumed")
        maximum = min(
            supply.max_total_bytes, root.wheel_max_bytes, request.limits["max_wheel_bytes"]
        )
        need(
            shutil.disk_usage(supply.state_root).free
            >= supply.min_free_bytes + 2 * maximum + 2 * MAX_CONTROL,
            "resolution_quota",
        )
        limits = ResolutionLimits(**(request.limits | {"max_wheel_bytes": maximum}))
        from agent_hub.skills.sandbox.python_resolution_overlay import record_attempt

        record_attempt(supply,request.invocation,source_sha256=request.claim.planning.draft.source_sha256,
            resolution_key=request.key,deadline=deadline)
        store.begin(request.key, request.claim)

        # A durable intent exists before even the first registry read. Every error
        # leaves UNKNOWN; a new execution id or changed source cannot replay it.
        async def content(entry: WheelDownload) -> bytes:
            await context()
            raw = await _fetch(entry, request, policy, root, supply, deadline)
            await context()
            extract_python_wheel_metadata(entry, raw, deadline=deadline)
            store.put_wheel(request.key, entry.sha256, raw, deadline=deadline)
            return raw

        descriptor = await resolve_dependencies(
            request.needs,
            scope=request.claim.resolution_scope(),
            environment=request.environment,
            reader=RegistryReader(request, policy, root, supply),
            wheel_reader=content,
            budget=ResolutionBudget(limits, deadline),
        )
        await context()
        store.complete(request.key, active().claim, descriptor)
        return store.inspect(request.key, active().claim)
    need(status.state == "resolved" and status.descriptor is not None, "resolution_inspect_only")
    descriptor = decode_descriptor(status.descriptor)
    if request.action == "freeze_python_resolution":
        # Only data is rebound. The original receipt remains unchanged.
        plan = store.freeze(request.key, request.claim)
        from agent_hub.skills.sandbox.python_resolution_overlay import record_frozen_plan

        record_frozen_plan(supply,plan,deadline=min(deadline,plan.claim.monotonic_deadline))
        await context()
        return ResolutionStatus(
            state="frozen", key=request.key, descriptor=descriptor.to_payload(), plan=plan
        )
    assert request.expected_plan_sha256 is not None
    plan = store.require_plan(request.key, request.claim, request.expected_plan_sha256)
    deadline = min(deadline,plan.claim.monotonic_deadline)
    effective = overlay_supply(supply, descriptor)
    need(
        effective.digest == plan.overlay_sha256 and plan.descriptor_sha256 == descriptor.sha256,
        "resolution_plan_changed",
    )
    await context()
    store.publication_begin(request.key, request.claim, plan)
    # Reuse metadata's Root700 claim scratch and the shared stage admission. No
    # temporary wheel/cache pathname is model-controlled.
    with _claim(supply, plan.sha256, create=True) as (parent, pins), ExitStack() as stack:
        need(parent is not None)
        stage_pins: list[_Observed] = []
        stage_fd = _root(root.stage_root, 0, stack, stage_pins, stage=True)
        with _admission(stage_fd, deadline):
            _resolution_controls(request, policy, root, deadline, original)
            for wheel in descriptor.wheels:
                entry = wheel.candidate.download
                raw = store.wheel(request.key, entry.sha256, entry.size_bytes, deadline=deadline)
                need(
                    extract_python_wheel_metadata(entry, raw, deadline=deadline) == wheel.metadata,
                    "resolution_artifact_invalid",
                )
                for pin in pins + stage_pins:
                    pin.check()
                publish_verified_wheel(
                    root.wheel_root,
                    supply.state_root / ("metadata-" + plan.sha256),
                    entry,
                    raw,
                    deadline=deadline,
                )
        await context()
        if original is not None and effective.digest != supply.digest:
            index = index_store.publish_dependency_metadata_index(
                root, effective, deadline=deadline, reuse_unchanged_from=supply,
            )
        else:
            index = index_store.publish_dependency_metadata_index(root, effective, deadline=deadline)
        await context()
        for pin in pins + stage_pins:
            pin.check()
        store.publication_finish(request.key, request.claim, plan, index.sha256)
    return ResolutionStatus(state="published", key=request.key, plan=plan)


async def handle_resolution(
    broker: ProjectExecutionBroker, request: ResolutionRequest, *, peer_uid: int
) -> ResolutionStatus:
    from agent_hub.skills.sandbox.python_metadata_preparation_broker import (
        handle_resolution_preparation,
    )

    return await handle_resolution_preparation(broker, request, peer_uid=peer_uid)


async def _await_resolution_with_root_lease(
    request: ResolutionRequest,
    *,
    operation: Awaitable[ResolutionStatus],
    current: Callable[[], ResolutionClaim],
) -> ResolutionStatus:
    """Root independently drains an operation when its private witness expires."""
    seen = request.claim

    def checked() -> ResolutionClaim:
        nonlocal seen
        claim = current()
        claim.require_continuity(seen)
        need(claim.planning == request.claim.planning
             and claim.lease_expires_at >= seen.lease_expires_at, "resolution_scope_changed")
        seen = claim
        return claim

    async def watch() -> None:
        while True:
            claim = checked()
            remaining = claim.deadline - time.monotonic()
            await asyncio.sleep(min(0.25, max(0.001, remaining / 3)))

    work = asyncio.ensure_future(operation)
    monitor: asyncio.Task[None] | None = None
    try:
        checked()  # Read latest Root-owned evidence before executing the operation.
        monitor = asyncio.create_task(watch())
        async with asyncio.timeout_at(request.claim.task_deadline):
            await asyncio.wait((work, monitor), return_when=asyncio.FIRST_COMPLETED)
            if monitor.done():
                # A failure already observed by Root remains terminal even if
                # work completes in the same event-loop turn or the file heals.
                await monitor
            if work.done():
                result = await work
                checked()
                need(type(result) is ResolutionStatus and result.key == request.key,
                     "resolution_response_invalid")
                return result
            await monitor
            raise ValueError("resolution_authority_unavailable")
    finally:
        work.cancel()
        if monitor is not None:
            monitor.cancel()

        async def drain() -> None:
            tasks = (work,) if monitor is None else (work, monitor)
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for result in results:
                if isinstance(result, ProcessCleanupIncomplete):
                    raise result

        await _uninterrupted(drain())


async def _await_resolution_with_renewal(
    request: ResolutionRequest,
    *,
    operation: Awaitable[ResolutionStatus],
    refresh: Callable[[], Awaitable[ResolutionClaim]],
    renew: Callable[[ResolutionRequest], Awaitable[ResolutionStatus]],
) -> ResolutionStatus:
    """Continue one operation only while its original owner keeps proving authority.

    Renewals do not create an execution, extend the original task budget, replace
    the token or make an UNKNOWN operation retryable. A stalled authority read is
    bounded by the *previous* lease, even if it would eventually return a new one.
    """
    active = request.claim

    work = asyncio.ensure_future(operation)

    async def completed() -> ResolutionStatus:
        result = await work
        need(type(result) is ResolutionStatus and result.key == request.key,
             "resolution_response_invalid")
        return result

    try:
        _ = active.deadline
        async with asyncio.timeout_at(request.claim.task_deadline):
            while True:
                lease_deadline = active.deadline
                remaining = lease_deadline - time.monotonic()
                # Authority checks run in the original owner task. Only the
                # already-authorized immutable IPC operation runs in a child.
                await asyncio.wait((work,), timeout=min(5.0, max(0.001, remaining / 3)))
                if work.done():
                    return await completed()
                async with asyncio.timeout_at(lease_deadline):
                    current = await refresh()
                    current.require_continuity(active)
                    need(current.planning == active.planning, "resolution_source_changed")
                    renewal = request.model_copy(update={"action": "renew_python_resolution", "claim": current})
                    try:
                        response = await renew(renewal)
                    except (OSError, ValueError, TimeoutError):
                        if work.done():
                            return await completed()
                        raise
                    if work.done():
                        # The original operation, not the late renewal, owns
                        # the result. The preparer still revalidates authority.
                        return await completed()
                    need(type(response) is ResolutionStatus and response.key == request.key
                         and response.state == "lease_renewed" and response.descriptor is None
                         and response.plan is None, "resolution_renewal_unavailable")
                    active = current
    finally:
        work.cancel()

        async def drain() -> None:
            results = await asyncio.gather(work, return_exceptions=True)
            for result in results:
                if isinstance(result, ProcessCleanupIncomplete):
                    raise result

        await _uninterrupted(drain())


async def request_resolution(request: ResolutionRequest) -> ResolutionStatus:
    """SDK authenticates the Root peer. A response is data, never an install receipt."""
    from agent_hub.skills.sandbox.broker import BROKER_SOCKET_PATH, _read_frame, _write_frame
    from agent_hub.skills.sandbox.project import _systemd_supported
    from agent_hub.skills.sandbox.python_metadata_preparation_client import _close, _root_peer

    if sys.platform != "linux":
        raise ValueError("resolution_unavailable")
    need(_systemd_supported(), "resolution_unavailable")
    request = ResolutionRequest.model_validate(request, strict=True)
    raw = request.model_dump_json(warnings="error").encode()
    decode(raw)
    writer = None
    try:
        deadline = (request.claim.deadline if request.action == "renew_python_resolution"
                    else request.claim.task_deadline)
        _ = request.claim.deadline  # The first send still requires a currently live lease.
        async with asyncio.timeout_at(deadline):
            reader, writer = await asyncio.open_unix_connection(BROKER_SOCKET_PATH.as_posix())
            _root_peer(writer)
            await _write_frame(writer, raw)
            value = decode(await _read_frame(reader, MAX_CONTROL))
            from agent_hub.skills.sandbox.python_resolution_protocol import encoded

            response = ResolutionStatus.model_validate_json(encoded(value), strict=True)
            need(response.key == request.key, "resolution_response_invalid")
            if response.descriptor is not None:
                descriptor = decode_descriptor(response.descriptor)
                need(
                    resolution_descriptor_matches(descriptor, request),
                    "resolution_response_invalid",
                )
            return response
    except (asyncio.CancelledError, TimeoutError):
        from agent_hub.skills.sandbox.project import ProjectSystemdSandbox

        await _uninterrupted(
            ProjectSystemdSandbox().terminate(request.claim.planning.invocation.execution_id)
        )
        raise
    finally:
        if writer is not None:
            await _uninterrupted(_close(writer))


def resolution_descriptor_matches(descriptor: object, request: ResolutionRequest) -> bool:
    from agent_hub.skills.sandbox.python_registry_protocol import ResolutionDescriptor

    return (
        type(descriptor) is ResolutionDescriptor
        and descriptor.needs == request.needs
        and dict(descriptor.environment) == request.environment
    )


def main() -> int:
    """Invoked only by the existing isolated Root write-unit command."""
    import argparse
    import sys

    from agent_hub.skills.sandbox.broker import load_broker_policy
    from agent_hub.skills.sandbox.project_contract_broker import _read_control
    from agent_hub.skills.sandbox.python_metadata_preparation_worker import _native, _stage_identity
    from agent_hub.skills.sandbox.python_resolution_protocol import encoded

    request: ResolutionRequest | None = None
    try:
        _native()
        need(
            sys.flags.isolated and sys.dont_write_bytecode and not sys.flags.optimize,
            "resolution_unavailable",
        )
        parser = argparse.ArgumentParser(allow_abbrev=False)
        parser.add_argument("--stage", type=Path, required=True)
        args = parser.parse_args()
        root, supply = read_root_configuration(), read_supply_configuration()
        need(args.stage.parent == root.stage_root, "resolution_stage_invalid")
        with ExitStack() as stack:
            pins: list[_Observed] = []
            parent = _root(args.stage, 0, stack, pins, stage=True)
            raw = _read_control(parent, "request.json", 0, pins)
            need(0 < len(raw) <= 64 * 1024, "resolution_request_invalid")
            request = ResolutionRequest.model_validate_json(encoded(decode(raw)), strict=True)
            _stage_identity(args.stage, root, request.invocation)
            for pin in pins:
                pin.check()
        status = asyncio.run(
            execute_resolution(request, policy=load_broker_policy(), root=root, supply=supply,
                lease_witness=ResolutionLeaseWitness(args.stage, request), stage=args.stage)
        )
        need(status.key == request.key, "resolution_response_invalid")
        print(status.model_dump_json(), flush=True)
        return 0
    except ProcessCleanupIncomplete:
        if request is not None:
            from agent_hub.skills.sandbox.python_resolution_protocol import (
                ResolutionFailureDiagnostic,
            )

            print(ResolutionStatus(state="unknown", key=request.key,
                diagnostic=ResolutionFailureDiagnostic(phase="cleanup",
                    code="metadata_prepare_cleanup_incomplete", exit_code=3, timed_out=None)
                ).model_dump_json(), flush=True)
        return 3
    except Exception as error:  # noqa: BLE001 - no private scope, metadata or source reflected.
        if request is not None:
            from agent_hub.skills.sandbox.python_resolution_protocol import (
                resolution_failure_diagnostic,
            )

            phase = cast(ResolutionFailurePhase,
                {"freeze_python_resolution": "freeze", "publish_python_resolution": "publication"}.get(
                    request.action, "resolution"))
            diagnostic = resolution_failure_diagnostic(error, phase=phase, exit_code=2)
            print(ResolutionStatus(state="unknown", key=request.key, diagnostic=diagnostic).model_dump_json(), flush=True)
        print("resolution_unavailable", file=sys.stderr, flush=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
