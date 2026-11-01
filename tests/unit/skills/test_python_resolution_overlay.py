"""Root immutable data selection; no reference or cached bytes authorize installation."""

from collections.abc import Iterator
from dataclasses import replace
from types import ModuleType
from typing import Never
from uuid import UUID

import pytest

from agent_hub.skills.sandbox import python_registry_protocol as registry_protocol
from agent_hub.skills.sandbox import python_resolution_overlay as overlay_module
from agent_hub.skills.sandbox import python_resolution_protocol as resolution_protocol
from agent_hub.skills.sandbox import python_resolution_store as resolution_store
from agent_hub.skills.sandbox.broker import BrokerPolicy
from agent_hub.skills.sandbox.python_input_supply_catalog import RootSupplyConfig
from agent_hub.skills.sandbox.python_project_protocol import PythonProjectProbeInvocation
from agent_hub.skills.sandbox.python_registry_protocol import ResolutionDescriptor
from agent_hub.skills.sandbox.python_resolution_protocol import (
    ResolutionClaim,
    ResolutionPlan,
    ResolutionRequest,
    ResolutionStatus,
)
from tests.unit.skills import test_python_registry_fetch as fetch_tests
from tests.unit.skills import test_python_registry_read as registry
from tests.unit.skills import test_python_resolution_store as state_tests
from tests.unit.skills.resolution_overlay_process_typed_fixture import (
    ConsumerRoots,
    NetworkCase,
    RootStoreCase,
    StagedReference,
    present,
)

root_store = state_tests.root_store
network_case = fetch_tests.case
offline_network = state_tests.offline_network


async def configured(
    root_store: RootStoreCase, monkeypatch: pytest.MonkeyPatch | None
) -> tuple[ModuleType, ResolutionClaim, ResolutionDescriptor, ResolutionPlan]:
    from tests.unit.skills.test_python_dependency_index_store import root_configuration

    protocol = resolution_protocol
    original = state_tests.claim()
    original = original.model_copy(
        update={
            "planning": original.planning.model_copy(
                update={
                    "expected_catalog_sha256": root_store.supply.digest,
                    "expected_root_config_sha256": registry_protocol.digest(
                        root_configuration().to_payload()
                    ),
                }
            )
        }
    )
    descriptor, _ = await state_tests.data()
    descriptor = replace(descriptor, scope=original.resolution_scope())
    key = protocol.resolution_key(original, descriptor.needs, dict(descriptor.environment))
    root_store.store.begin(key, original)
    root_store.store.complete(key, original, descriptor)
    plan = root_store.store.freeze(key, original)
    return protocol, original, descriptor, plan


async def test_no_root_binding_preserves_base_but_known_unknown_never_falls_back(
    root_store: RootStoreCase,
) -> None:
    overlay = overlay_module
    _protocol, original, _descriptor, plan = await configured(root_store, None)
    invocation = original.planning.invocation
    assert (
        overlay.effective_supply(
            root_store.supply, invocation, source_sha256="2" * 64, deadline=original.deadline
        )
        == root_store.supply
    )
    overlay.record_attempt(
        root_store.supply,
        invocation,
        source_sha256="2" * 64,
        resolution_key=plan.resolution_key,
        deadline=original.deadline,
    )
    with pytest.raises(ValueError, match="pending"):
        overlay.effective_supply(
            root_store.supply, invocation, source_sha256="2" * 64, deadline=original.deadline
        )


async def test_published_binding_is_root_reloaded_and_same_effective_index(
    root_store: RootStoreCase, monkeypatch: pytest.MonkeyPatch
) -> None:
    import stat

    from agent_hub.skills.sandbox import python_dependency_index_store as indexes

    overlay = overlay_module
    _protocol, original, descriptor, plan = await configured(root_store, monkeypatch)
    from tests.unit.skills.test_python_dependency_index_store import root_configuration

    root = root_configuration()
    key = plan.resolution_key
    overlay.record_frozen_plan(root_store.supply, plan, deadline=original.deadline)
    base = root_store.supply.catalog[0]
    root_store.fs.add(
        root.wheel_root.as_posix() + "/" + base.filename, stat.S_IFREG | 0o444, root_store.base_raw
    )
    for wheel in descriptor.wheels:
        entry = wheel.candidate.download
        _, raw = registry.wheel(entry.name)
        root_store.fs.add(
            root.wheel_root.as_posix() + "/" + entry.filename, stat.S_IFREG | 0o444, raw
        )
    effective = resolution_store.overlay_supply(root_store.supply, descriptor)
    index = indexes.publish_dependency_metadata_index(root, effective, deadline=original.deadline)
    root_store.store.publication_begin(key, original, plan)
    root_store.store.publication_finish(key, original, plan, index.sha256)
    monkeypatch.setattr(overlay, "read_root_configuration", lambda: root)
    monkeypatch.setattr(overlay, "read_supply_configuration", lambda: root_store.supply)
    before = {p: n.data for p, n in root_store.fs.nodes.items()}
    actual = overlay.effective_supply(
        root_store.supply,
        original.planning.invocation,
        source_sha256="2" * 64,
        deadline=original.deadline,
    )
    assert actual == effective and actual.digest != root_store.supply.digest
    assert before == {p: n.data for p, n in root_store.fs.nodes.items()}
    assert (
        indexes.read_dependency_metadata_index(actual, deadline=original.deadline).sha256
        == index.sha256
    )


async def test_frozen_marker_without_publication_is_not_catalog_authority(
    root_store: RootStoreCase,
) -> None:
    overlay = overlay_module
    _protocol, original, _descriptor, plan = await configured(root_store, None)
    overlay.record_frozen_plan(root_store.supply, plan, deadline=original.deadline)
    with pytest.raises(ValueError):
        overlay.effective_supply(
            root_store.supply,
            original.planning.invocation,
            source_sha256="2" * 64,
            deadline=original.deadline,
        )
    assert not hasattr(plan, "install_allowed")


@pytest.mark.parametrize(
    "action", ["plan_python_project_inputs", "inspect_python_project_input_supply"]
)
async def test_real_supply_consumer_selects_only_after_verified_original_contract(
    root_store: RootStoreCase, monkeypatch: pytest.MonkeyPatch, action: str
) -> None:
    from contextlib import contextmanager

    from agent_hub.capabilities.project_baselines import ProjectBaselineReference
    from agent_hub.skills.sandbox import python_input_supply_catalog as catalog
    from agent_hub.skills.sandbox import python_input_supply_worker as worker
    from agent_hub.skills.sandbox import python_project_backend as backend
    from agent_hub.skills.sandbox.python_input_supply_protocol import PythonInputSupplyRequest
    from tests.unit.skills.test_python_dependency_index_store import root_configuration

    overlay = overlay_module
    _protocol, original, _descriptor, plan = await configured(root_store, monkeypatch)
    overlay.record_frozen_plan(root_store.supply, plan, deadline=original.deadline)
    reference = {
        "schema_version": 1,
        "contract_sha256": "a" * 64,
        "input_source_sha256": "2" * 64,
        "file_count": 3,
    }
    invocation = PythonProjectProbeInvocation.model_validate(
        {**original.planning.invocation.model_dump(), "original_reference": reference}, strict=True
    )
    args = {"action": action, "invocation": invocation}
    if action == "inspect_python_project_input_supply":
        args.update(expected_plan_sha256="c" * 64, supply_execution_id="input_supply_" + "d" * 32)
    request = PythonInputSupplyRequest.model_validate(args, strict=True)
    verified = False
    selected: list[tuple[str, UUID, str]] = []

    @contextmanager
    def roots(*args: object) -> Iterator[ConsumerRoots]:
        yield ConsumerRoots(stage_root_fd=90, pins=[])

    @contextmanager
    def admission(*args: object) -> Iterator[None]:
        yield

    def stage(*args: object, **kwargs: object) -> StagedReference:
        nonlocal verified
        verified = True
        return StagedReference(reference=ProjectBaselineReference.from_payload(reference))

    class ReachedConsumer(Exception):
        pass

    def effective(
        base: RootSupplyConfig,
        actual: PythonProjectProbeInvocation,
        *,
        source_sha256: str,
        deadline: float,
    ) -> RootSupplyConfig:
        assert verified and source_sha256 == "2" * 64
        selected.append((base.digest, actual.run_id, source_sha256))
        return base

    def reached(*args: object, **kwargs: object) -> Never:
        raise ReachedConsumer

    monkeypatch.setattr(worker, "validate_configuration", lambda *a: None)
    monkeypatch.setattr(worker, "stage_original_contract", stage)
    monkeypatch.setattr(backend, "_roots", roots)
    monkeypatch.setattr(backend, "_admission", admission)
    monkeypatch.setattr(overlay, "effective_supply", effective)
    monkeypatch.setattr(catalog, "plan_inputs", reached)
    monkeypatch.setattr(worker, "_status", reached)
    with pytest.raises(ReachedConsumer):
        worker.execute(
            request,
            BrokerPolicy(project_workspace_root=root_configuration().stage_root, allowed_uid=10001),
            root_configuration(),
            root_store.supply,
            root_configuration().stage_root / "dummy",
        )
    assert selected and verified


@pytest.mark.parametrize("fault", ["none", "index", "scope", "result"])
async def test_actual_root_producer_rebinds_verified_data_then_reloads_published_evidence(
    network_case: NetworkCase, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    import json

    c = network_case
    overlay = overlay_module
    monkeypatch.setattr(overlay, "read_root_configuration", lambda: c.root)
    monkeypatch.setattr(overlay, "read_supply_configuration", lambda: c.supply)

    async def execute(request: ResolutionRequest) -> ResolutionStatus:
        return await c.worker.execute_resolution(
            request, policy=c.policy, root=c.root, supply=c.supply
        )

    resolved = await execute(c.request)
    assert resolved.state == "resolved" and len(c.child_reads) > 0
    reads = len(c.child_reads)
    old_receipt = c.store._read(c.store._name(c.request.key, "receipt"), c.request.claim.deadline)
    current = c.request.claim.model_copy(
        update={
            "worker_id": "fresh_worker",
            "lease_token": UUID(int=999),
            "planning": c.request.claim.planning.model_copy(
                update={
                    "draft": c.request.claim.planning.draft.model_copy(
                        update={"source_sha256": "f" * 64}
                    )
                }
            ),
        }
    )
    request = c.request.model_copy(update={"claim": current, "action": "freeze_python_resolution"})
    frozen = await execute(request)
    assert present(frozen.plan).claim.planning.draft.source_sha256 == "f" * 64
    with pytest.raises(ValueError, match="pending"):
        overlay.effective_supply(
            c.supply, current.planning.invocation, source_sha256="f" * 64, deadline=current.deadline
        )
    published = await execute(
        request.model_copy(
            update={
                "action": "publish_python_resolution",
                "expected_plan_sha256": present(frozen.plan).sha256,
            }
        )
    )
    assert published.state == "published"
    assert len(c.child_reads) == reads and c.root_dns == []
    assert c.store._read(c.store._name(c.request.key, "receipt"), current.deadline) == old_receipt
    if fault == "index":
        from agent_hub.skills.sandbox.python_resolution_store import overlay_supply

        effect = overlay_supply(
            c.supply, resolution_protocol.decode_descriptor(resolved.descriptor)
        )
        path = next(p for p in c.fs.nodes if effect.digest in p and p.endswith(".json"))
        c.fs.nodes[path].data = b"{}"
    elif fault == "scope":
        path = next(p for p in c.fs.nodes if "/resolution-binding-" in p)
        value = json.loads(c.fs.nodes[path].data)
        value["scope"]["source_sha256"] = "e" * 64
        c.fs.nodes[path].data = json.dumps(value).encode()
    elif fault == "result":
        path = (
            c.supply.state_root.as_posix()
            + "/"
            + c.store._name(c.request.key, "result-" + present(frozen.plan).sha256)
        )
        del c.fs.nodes[path]
    before = {p: n.data for p, n in c.fs.nodes.items()}
    if fault == "none":
        effective = overlay.effective_supply(
            c.supply, current.planning.invocation, source_sha256="f" * 64, deadline=current.deadline
        )
        assert {entry.name for entry in effective.catalog} == {"base", "demo", "helper"}
        status = await execute(
            request.model_copy(
                update={
                    "action": "inspect_python_resolution",
                    "expected_plan_sha256": present(frozen.plan).sha256,
                }
            )
        )
        assert status.state == "published" and status.plan is None and status.descriptor is None
    else:
        with pytest.raises(ValueError):
            overlay.effective_supply(
                c.supply,
                current.planning.invocation,
                source_sha256="f" * 64,
                deadline=current.deadline,
            )
    assert before == {p: n.data for p, n in c.fs.nodes.items()}
