"""Offline Root metadata preparation; wheel networking belongs to the drained fetch unit."""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import re
import shutil
import sys
import time
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

from agent_hub.skills.sandbox import python_dependency_index_store as store
from agent_hub.skills.sandbox.project_contract_broker import (
    _encoded,
    _object,
    _Observed,
    _open_directory,
    _read_control,
    _root,
)
from agent_hub.skills.sandbox.python_dependency_index_store import (
    publish_dependency_metadata_index,
    read_dependency_metadata_index,
)
from agent_hub.skills.sandbox.python_input_publish import (
    publish_verified_wheel as publish_catalog_wheel,
)
from agent_hub.skills.sandbox.python_input_supply_catalog import (
    RootSupplyConfig,
    read_supply_configuration,
)
from agent_hub.skills.sandbox.python_input_supply_worker import (
    _once,
    fetch_catalog_wheels,
    validate_configuration,
)
from agent_hub.skills.sandbox.python_metadata_plan import (
    _digest,
    _identities,
    build_python_metadata_plan,
)
from agent_hub.skills.sandbox.python_metadata_plan_protocol import PythonMetadataPlan
from agent_hub.skills.sandbox.python_metadata_planning import _cached, _controls, _observe, _source
from agent_hub.skills.sandbox.python_metadata_preparation_protocol import (
    MetadataPreparationError,
    PythonMetadataPreparationReceipt,
    PythonMetadataPreparationRequest,
    PythonMetadataPreparationResponse,
    PythonMetadataPreparationStatus,
)
from agent_hub.skills.sandbox.python_project_backend import (
    RootConfig,
    _admission,
    _roots,
    read_root_configuration,
)
from agent_hub.skills.sandbox.python_project_profile import PythonProjectProfileScope
from agent_hub.subprocess_cleanup import ProcessCleanupIncomplete

if TYPE_CHECKING:
    from agent_hub.skills.sandbox.broker import BrokerPolicy
    from agent_hub.skills.sandbox.python_dependency_metadata import PythonDependencyMetadataIndex

_CONTROL_RESERVE = 4_000_000
_MAX_REQUEST = 64 * 1024


class _PreparationError(ValueError):
    def __init__(self, code: MetadataPreparationError) -> None:
        self.code = code
        super().__init__(code)


def _need(value: object, code: MetadataPreparationError) -> None:
    if not value:
        raise _PreparationError(code)


def error_code(error: Exception) -> MetadataPreparationError:
    if isinstance(error, _PreparationError):
        return error.code
    codes: dict[str, MetadataPreparationError] = {
        "metadata_plan_request_invalid": "metadata_prepare_request_invalid",
        "metadata_plan_draft_changed": "metadata_prepare_draft_changed",
        "metadata_plan_catalog_changed": "metadata_prepare_catalog_changed",
        "metadata_plan_cache_untrusted": "metadata_prepare_artifact_integrity",
        "metadata_plan_metadata_untrusted": "metadata_prepare_metadata_untrusted",
        "metadata_plan_quota": "metadata_prepare_quota",
        "metadata_plan_timeout": "metadata_prepare_timeout",
        "input_supply_quota": "metadata_prepare_quota",
        "input_supply_timeout": "metadata_prepare_timeout",
        "input_supply_artifact_integrity": "metadata_prepare_artifact_integrity",
        "python_input_publish_conflict": "metadata_prepare_artifact_integrity",
        "metadata_untrusted": "metadata_prepare_metadata_untrusted",
        "catalog_changed": "metadata_prepare_catalog_changed",
        "timeout": "metadata_prepare_timeout",
    }
    return codes.get(getattr(error, "code", ""), "metadata_prepare_unavailable")


def _failure(request: PythonMetadataPreparationRequest, code: MetadataPreparationError) -> PythonMetadataPreparationResponse:
    return PythonMetadataPreparationResponse(action=request.action,
        scope=PythonProjectProfileScope.for_invocation(request.invocation), ok=False, error=code)


def _native() -> None:
    _need(sys.platform == "linux" and os.name == "posix"
        and os.geteuid() == os.getegid() == 0, "metadata_prepare_unavailable")


def _check(deadline: float) -> None:
    _need(type(deadline) in {int, float} and math.isfinite(deadline)
        and time.monotonic() < deadline, "metadata_prepare_timeout")


def _stage_identity(stage: Path, root: RootConfig, request: PythonMetadataPreparationRequest) -> None:
    prefix = request.invocation.execution_id + "-"
    _need(stage.parent == root.stage_root and stage.name.startswith(prefix)
        and re.fullmatch(r"[a-z0-9_]{8}", stage.name[len(prefix):]) is not None,
        "metadata_prepare_request_invalid")


def _pins(pins: list[_Observed], deadline: float) -> None:
    _check(deadline)
    for pin in pins:
        pin.check()
    _check(deadline)


def _configuration(
    request: PythonMetadataPreparationRequest, root: RootConfig, supply: RootSupplyConfig, deadline: float,
) -> None:
    _check(deadline)
    current_root, current_supply = read_root_configuration(), read_supply_configuration()
    _need(current_root.to_payload() == root.to_payload()
        and _digest(current_root.to_payload()) == request.planning.expected_root_config_sha256
        and current_supply.digest == supply.digest == request.planning.expected_catalog_sha256,
        "metadata_prepare_catalog_changed")
    _check(deadline)


async def _context(
    request: PythonMetadataPreparationRequest, policy: BrokerPolicy, root: RootConfig,
    supply: RootSupplyConfig, deadline: float,
) -> None:
    _configuration(request, root, supply, deadline)
    await _source(request.planning, policy, deadline)
    _controls(request.planning, policy, deadline)
    _configuration(request, root, supply, deadline)


def _free_bytes(path: Path) -> int:
    return shutil.disk_usage(path).free


def _budget(root: RootConfig, supply: RootSupplyConfig, deadline: float) -> None:
    _check(deadline)
    total = sum(entry.size_bytes for entry in supply.catalog)
    reserve = 2 * total + 2 * store._MAX_INDEX_BYTES + _CONTROL_RESERVE
    _need(total <= min(root.wheel_max_bytes, supply.max_total_bytes)
        and _free_bytes(supply.state_root) >= supply.min_free_bytes + reserve,
        "metadata_prepare_quota")
    _check(deadline)


@contextmanager
def _claim(supply: RootSupplyConfig, digest: str, *, create: bool) -> Iterator[tuple[int | None, list[_Observed]]]:
    with ExitStack() as stack:
        pins: list[_Observed] = []
        parent = _root(supply.state_root, 0, stack, pins, stage=True)
        _need(os.fstat(parent).st_gid == 0, "metadata_prepare_metadata_untrusted")
        name = "metadata-" + digest
        if create:
            try:
                os.mkdir(name, 0o700, dir_fd=parent)
            except FileExistsError:
                raise _PreparationError("metadata_prepare_consumed") from None
            try:
                os.fsync(parent)
                target = _open_directory(parent, name, 0, stack, pins, mode=0o700)
                _need(os.fstat(target).st_gid == 0
                    and os.fstat(target).st_dev == os.fstat(parent).st_dev,
                    "metadata_prepare_metadata_untrusted")
            except BaseException:  # noqa: BLE001 - retain the newly created claim on every failure.
                raise ProcessCleanupIncomplete("metadata_prepare_cleanup_incomplete") from None
        else:
            try:
                target = _open_directory(parent, name, 0, stack, pins, mode=0o700)
            except FileNotFoundError:
                target = None
            if target is not None:
                _need(os.fstat(target).st_gid == 0
                    and os.fstat(target).st_dev == os.fstat(parent).st_dev,
                    "metadata_prepare_metadata_untrusted")
        yield target, pins


def _intent(parent: int, pins: list[_Observed], request: PythonMetadataPreparationRequest,
    root: RootConfig, supply: RootSupplyConfig) -> PythonMetadataPlan:
    raw = _read_control(parent, "intent.json", 0, pins)
    _need(len(raw) <= 2_000_000, "metadata_prepare_metadata_untrusted")
    item = json.loads(raw, object_pairs_hook=_object)
    _need(type(item) is dict and set(item) == {"schema_version", "purpose", "plan", "preparation_execution_id"}
        and type(item["schema_version"]) is int and item["schema_version"] == 1
        and item["purpose"] == "python_dependency_metadata"
        and item["preparation_execution_id"] == request.preparation_execution_id,
        "metadata_prepare_scope_changed")
    plan = PythonMetadataPlan.model_validate_json(_encoded(item["plan"]), strict=True)
    _need(plan.sha256 == request.expected_plan_sha256 and plan.mode != "ready",
        "metadata_prepare_plan_changed")
    _need(plan.scope == request.planning.scope and plan.draft == request.planning.draft,
        "metadata_prepare_scope_changed")
    _need(plan.root_config_sha256 == _digest(root.to_payload())
        and plan.catalog_sha256 == supply.digest
        and plan.catalog_identity_sha256 == _digest(_identities(supply.catalog))
        and plan.catalog_count == len(supply.catalog)
        and plan.catalog_bytes == sum(e.size_bytes for e in supply.catalog),
        "metadata_prepare_catalog_changed")
    return plan


def _index(supply: RootSupplyConfig, deadline: float) -> PythonDependencyMetadataIndex:
    from agent_hub.skills.sandbox.python_dependency_metadata import PythonDependencyMetadataIndex

    value = read_dependency_metadata_index(supply, deadline=deadline)
    _need(type(value) is PythonDependencyMetadataIndex, "metadata_prepare_metadata_untrusted")
    value = PythonDependencyMetadataIndex.from_bytes(value.to_bytes())
    value.require_catalog(supply.catalog, supply.digest)
    _check(deadline)
    return value


async def _inspect(request: PythonMetadataPreparationRequest, policy: BrokerPolicy,
    root: RootConfig, supply: RootSupplyConfig, deadline: float) -> PythonMetadataPreparationResponse:
    await _context(request, policy, root, supply, deadline)
    receipt = None
    state: str = "absent"
    with _claim(supply, request.expected_plan_sha256, create=False) as (parent, pins):
        if parent is not None:
            state = "unknown"
            try:
                plan = _intent(parent, pins, request, root, supply)
            except FileNotFoundError:
                pass
            else:
                try:
                    raw = _read_control(parent, "receipt.json", 0, pins)
                except FileNotFoundError:
                    pass
                else:
                    _need(len(raw) <= 2_000_000, "metadata_prepare_metadata_untrusted")
                    receipt = PythonMetadataPreparationReceipt.model_validate_json(
                        _encoded(json.loads(raw, object_pairs_hook=_object)), strict=True)
                    _need(receipt.plan == plan
                        and receipt.preparation_execution_id == request.preparation_execution_id,
                        "metadata_prepare_scope_changed")
                    index = _index(supply, deadline)
                    _need(index.sha256 == receipt.index_sha256
                        and len(index.records) == receipt.index_record_count,
                        "metadata_prepare_metadata_untrusted")
                    state = "prepared"
        await _context(request, policy, root, supply, deadline)
        _pins(pins, deadline)
    assert request.preparation_execution_id is not None
    status = PythonMetadataPreparationStatus.model_validate({"state": state,
        "plan_sha256": request.expected_plan_sha256,
        "preparation_execution_id": request.preparation_execution_id, "receipt": receipt}, strict=True)
    result = PythonMetadataPreparationResponse(action=request.action,
        scope=PythonProjectProfileScope.for_invocation(request.invocation), ok=True, status=status)
    result.require_for(request)
    return result


async def execute(
    request: PythonMetadataPreparationRequest, policy: BrokerPolicy, configuration: RootConfig,
    supply: RootSupplyConfig, stage: Path, *, deadline: float,
) -> PythonMetadataPreparationResponse:
    _native()
    _need(type(request) is PythonMetadataPreparationRequest, "metadata_prepare_request_invalid")
    request = PythonMetadataPreparationRequest.model_validate(request, strict=True)
    _check(deadline)
    configuration, supply = replace(configuration), replace(supply)
    _stage_identity(stage, configuration, request)
    validate_configuration(policy, configuration, supply)
    with _roots(policy, configuration, stage, False) as roots:
        if request.action == "inspect_python_dependency_metadata_preparation":
            return await _inspect(request, policy, configuration, supply, deadline)
        observed = await _observe(request.planning, policy, deadline)
        observed.require_for(request.planning)
        plan = observed.plan
        _need(observed.ok and plan is not None, "metadata_prepare_unavailable")
        assert plan is not None
        _need(plan.mode != "ready" and plan.sha256 == request.expected_plan_sha256,
            "metadata_prepare_plan_changed")
        with ExitStack() as stack:
            cached, check_cache = _cached(configuration, supply, stack, deadline)
            measured = build_python_metadata_plan(request.planning, root=configuration,
                supply=supply, cached=cached)
            _need(measured == plan, "metadata_prepare_plan_changed")
            check_cache()
        await _context(request, policy, configuration, supply, deadline)
        _budget(configuration, supply, deadline)
        missing = tuple(e for e in supply.catalog if e.filename not in {w.filename for w in cached})
        with _claim(supply, plan.sha256, create=True) as (parent, pins):
            assert parent is not None
            try:
                _once(parent, "intent.json", {"schema_version": 1,
                    "purpose": "python_dependency_metadata", "plan": plan.model_dump(mode="json"),
                    "preparation_execution_id": request.invocation.execution_id})
                _pins(pins, deadline)
            except BaseException:  # noqa: BLE001 - incomplete durable intent is uncertain.
                raise ProcessCleanupIncomplete("metadata_prepare_cleanup_incomplete") from None
            downloaded = await fetch_catalog_wheels(missing, request.invocation,
                policy, configuration, supply, deadline)
            await _context(request, policy, configuration, supply, deadline)
            _pins(roots.pins + pins, deadline)
            _budget(configuration, supply, deadline)
            # Only publication takes stage admission. Fetching holds no flock.
            try:
                with _admission(roots.stage_root_fd, deadline):
                    _configuration(request, configuration, supply, deadline)
                    for entry in missing:
                        _pins(roots.pins + pins, deadline)
                        data = downloaded.get(entry.filename)
                        _need(type(data) is bytes, "metadata_prepare_artifact_integrity")
                        assert data is not None
                        publish_catalog_wheel(configuration.wheel_root,
                            supply.state_root / ("metadata-" + plan.sha256), entry, data, deadline=deadline)
                await _context(request, policy, configuration, supply, deadline)
                published = publish_dependency_metadata_index(configuration, supply, deadline=deadline)
                index = _index(supply, deadline)
                _need(index.sha256 == published.sha256, "metadata_prepare_metadata_untrusted")
                await _context(request, policy, configuration, supply, deadline)
                _pins(roots.pins + pins, deadline)
                receipt = PythonMetadataPreparationReceipt(plan=plan,
                    preparation_execution_id=request.invocation.execution_id,
                    index_sha256=index.sha256, index_record_count=len(index.records))
                _once(parent, "receipt.json", receipt.model_dump(mode="json"))
                _pins(roots.pins + pins, deadline)
            except BaseException:  # noqa: BLE001 - publication without durable receipt is uncertain.
                raise ProcessCleanupIncomplete("metadata_prepare_cleanup_incomplete") from None
    result = PythonMetadataPreparationResponse(action=request.action,
        scope=PythonProjectProfileScope.for_invocation(request.invocation), ok=True, receipt=receipt)
    result.require_for(request)
    return result


def main() -> int:
    request = None
    try:
        _native()
        _need(sys.flags.isolated and sys.dont_write_bytecode and not sys.flags.optimize,
            "metadata_prepare_unavailable")
        os.umask(0o077)
        parser = argparse.ArgumentParser(allow_abbrev=False)
        parser.add_argument("--stage", type=Path, required=True)
        args = parser.parse_args()
        from agent_hub.skills.sandbox.broker import load_broker_policy

        root, supply = read_root_configuration(), read_supply_configuration()
        _need(args.stage.parent == root.stage_root, "metadata_prepare_request_invalid")
        with ExitStack() as stack:
            pins: list[_Observed] = []
            parent = _root(args.stage, 0, stack, pins, stage=True)
            raw = _read_control(parent, "request.json", 0, pins)
            _need(0 < len(raw) <= _MAX_REQUEST, "metadata_prepare_request_invalid")
            request = PythonMetadataPreparationRequest.model_validate_json(
                _encoded(json.loads(raw, object_pairs_hook=_object)), strict=True)
            _stage_identity(args.stage, root, request)
            _pins(pins, time.monotonic() + request.invocation.timeout_seconds)
        deadline = time.monotonic() + request.invocation.timeout_seconds
        response = asyncio.run(execute(request, load_broker_policy(), root, supply,
            args.stage, deadline=deadline))
        response.require_for(request)
        print(response.model_dump_json(), flush=True)
        return 0
    except ProcessCleanupIncomplete:
        return 3
    except Exception as error:  # noqa: BLE001 - privileged failures expose only finite codes.
        if request is not None:
            print(_failure(request, error_code(error)).model_dump_json(), flush=True)
            return 0
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
