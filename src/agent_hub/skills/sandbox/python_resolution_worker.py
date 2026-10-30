"""Narrow Root resolution/preparation handler using existing fetch/cache/index workers."""

from __future__ import annotations

import asyncio
import shutil
import time
from contextlib import ExitStack
from dataclasses import replace
from typing import TYPE_CHECKING

from agent_hub.capabilities.python_dependency_resolution import resolve_dependencies
from agent_hub.skills.sandbox import python_dependency_index_store as index_store
from agent_hub.skills.sandbox.project import _uninterrupted
from agent_hub.skills.sandbox.project_contract_broker import _Observed, _root
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
    ResolutionRequest,
    ResolutionStatus,
    decode,
    decode_descriptor,
)
from agent_hub.skills.sandbox.python_resolution_store import ResolutionStore, overlay_supply
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
) -> None:
    need(time.monotonic() < min(deadline, request.claim.deadline), "resolution_deadline")
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
    await _source(request.claim.planning, policy, deadline)
    _controls(request.claim.planning, policy, deadline)
    env = request.environment
    need(
        env["implementation_name"] == root.interpreter.implementation
        and env["python_full_version"] == root.interpreter.version
        and "linux_" + env["platform_machine"] == root.interpreter.platform,
        "resolution_environment_changed",
    )
    need(time.monotonic() < min(deadline, request.claim.deadline), "resolution_deadline")
    need(
        digest(read_root_configuration().to_payload())
        == request.claim.planning.expected_root_config_sha256
        and read_supply_configuration().digest == supply.digest,
        "resolution_configuration_changed",
    )


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
    request: ResolutionRequest, *, policy: BrokerPolicy, root: RootConfig, supply: RootSupplyConfig
) -> ResolutionStatus:
    """Root peer validation is done by handle_resolution; all persisted data is re-read.

    No descriptor is accepted in the IPC request. Registry reads use fixed pinned
    transport; downloaded quarantine bytes are validated by the actual resolver.
    Cache publication requires a separately approved exact frozen plan in service.
    """
    index_store._native()
    request = ResolutionRequest.model_validate(request, strict=True)
    root, supply = replace(root), replace(supply)
    deadline = request.claim.deadline
    await _context(request, policy, root, supply, deadline)
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
            await _context(request, policy, root, supply, deadline)
            raw = await _fetch(entry, request, policy, root, supply, deadline)
            await _context(request, policy, root, supply, deadline)
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
        await _context(request, policy, root, supply, deadline)
        store.complete(request.key, request.claim, descriptor)
        return store.inspect(request.key, request.claim)
    need(status.state == "resolved" and status.descriptor is not None, "resolution_inspect_only")
    descriptor = decode_descriptor(status.descriptor)
    if request.action == "freeze_python_resolution":
        # Only data is rebound. The original receipt remains unchanged.
        plan = store.freeze(request.key, request.claim)
        from agent_hub.skills.sandbox.python_resolution_overlay import record_frozen_plan

        record_frozen_plan(supply,plan,deadline=min(deadline,plan.claim.monotonic_deadline))
        await _context(request, policy, root, supply, deadline)
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
    await _context(request, policy, root, supply, deadline)
    store.publication_begin(request.key, request.claim, plan)
    # Reuse metadata's Root700 claim scratch and the shared stage admission. No
    # temporary wheel/cache pathname is model-controlled.
    with _claim(supply, plan.sha256, create=True) as (parent, pins), ExitStack() as stack:
        need(parent is not None)
        stage_pins: list[_Observed] = []
        stage_fd = _root(root.stage_root, 0, stack, stage_pins, stage=True)
        with _admission(stage_fd, deadline):
            _controls(request.claim.planning, policy, deadline)
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
        await _context(request, policy, root, supply, deadline)
        index = index_store.publish_dependency_metadata_index(root, effective, deadline=deadline)
        await _context(request, policy, root, supply, deadline)
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


async def request_resolution(request: ResolutionRequest) -> ResolutionStatus:
    """SDK authenticates the Root peer. A response is data, never an install receipt."""
    from agent_hub.skills.sandbox.broker import BROKER_SOCKET_PATH, _read_frame, _write_frame
    from agent_hub.skills.sandbox.project import _systemd_supported
    from agent_hub.skills.sandbox.python_metadata_preparation_client import _close, _root_peer

    need(_systemd_supported(), "resolution_unavailable")
    request = ResolutionRequest.model_validate(request, strict=True)
    raw = request.model_dump_json(warnings="error").encode()
    decode(raw)
    writer = None
    try:
        async with asyncio.timeout_at(request.claim.deadline):
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

    try:
        _native()
        need(
            sys.flags.isolated and sys.dont_write_bytecode and not sys.flags.optimize,
            "resolution_unavailable",
        )
        parser = argparse.ArgumentParser(allow_abbrev=False)
        from pathlib import Path

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
            execute_resolution(request, policy=load_broker_policy(), root=root, supply=supply)
        )
        need(status.key == request.key, "resolution_response_invalid")
        print(status.model_dump_json(), flush=True)
        return 0
    except ProcessCleanupIncomplete:
        return 3
    except Exception:  # noqa: BLE001 - no private scope, metadata or source reflected.
        print("resolution_unavailable", file=sys.stderr, flush=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
