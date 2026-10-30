"""Approval behavior identity is distinct from the fresh private worker envelope."""

import importlib
from datetime import timedelta
from uuid import UUID

import pytest

from tests.unit.skills import test_python_registry_read as registry
from tests.unit.skills import test_python_resolution_store as fixtures

root_store = fixtures.root_store
offline_network = registry.offline_network
from tests.unit.capabilities import test_python_resolution_preparation as authority_tests

live = authority_tests.live


def api():
    return importlib.import_module("agent_hub.capabilities.python_resolution_preparation")


def test_same_action_does_not_reask_just_because_approval_resumed_on_new_lease():
    first = fixtures.claim()
    second = first.model_copy(
        update={
            "worker_id": "worker_after_approval",
            "lease_token": UUID(int=999),
            "lease_expires_at": first.lease_expires_at + timedelta(seconds=1),
        }
    )
    needs = (registry.api()[0].DependencyNeed("demo>=1", "a" * 64),)
    a = api().resolution_action(first, needs, registry.environment())
    b = api().resolution_action(second, needs, registry.environment())
    assert a == b
    assert str(first.lease_token) not in a.model_dump_json()
    assert str(second.lease_token) not in b.model_dump_json()


async def test_verified_data_rebinds_to_fresh_worker_without_extending_original_budget(root_store):
    protocol, _ = fixtures.api()
    descriptor, _ = await fixtures.data()
    original = fixtures.claim()
    from dataclasses import replace

    original = original.model_copy(
        update={
            "planning": original.planning.model_copy(
                update={"expected_catalog_sha256": root_store.supply.digest}
            )
        }
    )
    descriptor = replace(descriptor, scope=original.resolution_scope())
    key = protocol.resolution_key(original, descriptor.needs, dict(descriptor.environment))
    root_store.store.begin(key, original)
    root_store.store.complete(key, original, descriptor)
    first = root_store.store.freeze(key, original)
    fresh = original.model_copy(
        update={"worker_id": "new_bound_worker", "lease_token": UUID(int=999)}
    )
    second = root_store.store.freeze(key, fresh)
    assert first.sha256 == second.sha256
    assert root_store.store.require_plan(key, fresh, first.sha256).sha256 == first.sha256
    before = {p: n.data for p, n in root_store.fs.nodes.items()}
    with pytest.raises(ValueError):
        root_store.store.freeze(
            key,
            fresh.model_copy(
                update={"original_deadline": original.original_deadline + timedelta(seconds=60)}
            ),
        )
    assert before == {p: n.data for p, n in root_store.fs.nodes.items()}


async def test_inspect_can_forward_exact_publication_plan_without_authorizing_it(monkeypatch):
    from agent_hub.skills.sandbox.python_resolution_protocol import ResolutionStatus

    preparation = api().PythonResolutionPreparation(current=lambda: None, approved=None)
    original = fixtures.claim()
    descriptor, _ = await fixtures.data()
    plan = fixtures.api()[1].freeze_plan(
        descriptor, original=original, current=original, overlay_sha256="c" * 64
    )
    captured = []

    async def send(action, **kwargs):
        captured.append((action, kwargs))
        return ResolutionStatus(state="unknown", key=plan.resolution_key)

    monkeypatch.setattr(preparation, "_send", send)
    result = await preparation.inspect(
        needs=descriptor.needs,
        environment=dict(descriptor.environment),
        planning=original.planning,
        plan=plan,
    )
    assert result.state == "unknown" and captured[0][0] == "inspect_python_resolution"
    assert captured[0][1]["plan"] == plan


async def test_legacy_unknown_publication_cannot_be_reissued_under_new_semantic_hash(root_store):
    from dataclasses import replace

    protocol, _ = fixtures.api()
    descriptor, _ = await fixtures.data()
    original = fixtures.claim()
    original = original.model_copy(
        update={
            "planning": original.planning.model_copy(
                update={"expected_catalog_sha256": root_store.supply.digest}
            )
        }
    )
    descriptor = replace(descriptor, scope=original.resolution_scope())
    key = protocol.resolution_key(original, descriptor.needs, dict(descriptor.environment))
    store = root_store.store
    store.begin(key, original)
    store.complete(key, original, descriptor)
    plan = store.freeze(key, original)
    legacy_sha = protocol.digest(
        {**plan.model_dump(mode="json"), "claim": plan.claim.effect_payload()}
    )
    assert legacy_sha != plan.sha256
    store._create(
        store._name(key, "publish-" + legacy_sha),
        {"state": "unknown", "plan_sha256": legacy_sha},
        original.deadline,
    )
    before = {p: n.data for p, n in root_store.fs.nodes.items()}
    with pytest.raises(ValueError, match="inspect_only"):
        store.publication_begin(key, original, plan)
    assert before == {p: n.data for p, n in root_store.fs.nodes.items()}


async def test_cold_inspect_accepts_only_opaque_plan_hash(monkeypatch):
    from agent_hub.skills.sandbox.python_resolution_protocol import ResolutionStatus

    preparation = api().PythonResolutionPreparation(current=lambda: None, approved=None)
    claim = fixtures.claim()
    seen = []

    async def send(action, **kwargs):
        seen.append(kwargs)
        return ResolutionStatus(state="unknown", key="a" * 64)

    monkeypatch.setattr(preparation, "_send", send)
    await preparation.inspect(
        needs=(registry.api()[0].DependencyNeed("demo", "a" * 64),),
        environment=registry.environment(),
        planning=claim.planning,
        expected_plan_sha256="b" * 64,
    )
    assert seen[0]["expected_plan_sha256"] == "b" * 64
    assert seen[0].get("plan") is None


async def test_root_opaque_inspect_reloads_original_unknown_without_old_private_lease(root_store):
    from dataclasses import replace

    protocol, _ = fixtures.api()
    original = fixtures.claim()
    original = original.model_copy(
        update={
            "planning": original.planning.model_copy(
                update={"expected_catalog_sha256": root_store.supply.digest}
            )
        }
    )
    descriptor, _ = await fixtures.data()
    descriptor = replace(descriptor, scope=original.resolution_scope())
    key = protocol.resolution_key(original, descriptor.needs, dict(descriptor.environment))
    store = root_store.store
    store.begin(key, original)
    store.complete(key, original, descriptor)
    plan = store.freeze(key, original)
    store.publication_begin(key, original, plan)
    current = original.model_copy(update={"worker_id": "cold_worker", "lease_token": UUID(int=999)})
    before = {p: n.data for p, n in root_store.fs.nodes.items()}
    status = store.inspect_publication(key, current, plan.sha256)
    assert status.state == "unknown" and status.plan is None and status.descriptor is None
    assert before == {p: n.data for p, n in root_store.fs.nodes.items()}


@pytest.mark.parametrize("change", ["actor", "tenant", "run", "source"])
async def test_opaque_root_lookup_rejects_scope_or_source_splice(root_store, change):
    from dataclasses import replace

    protocol, _ = fixtures.api()
    original = fixtures.claim()
    original = original.model_copy(
        update={
            "planning": original.planning.model_copy(
                update={"expected_catalog_sha256": root_store.supply.digest}
            )
        }
    )
    descriptor, _ = await fixtures.data()
    descriptor = replace(descriptor, scope=original.resolution_scope())
    key = protocol.resolution_key(original, descriptor.needs, dict(descriptor.environment))
    store = root_store.store
    store.begin(key, original)
    store.complete(key, original, descriptor)
    plan = store.freeze(key, original)
    store.publication_begin(key, original, plan)
    planning = original.planning
    if change == "source":
        planning = planning.model_copy(
            update={"draft": planning.draft.model_copy(update={"source_sha256": "f" * 64})}
        )
    else:
        field = {"actor": "actor", "tenant": "tenant_id", "run": "run_id"}[change]
        value = "other_actor" if change == "actor" else UUID(int=999)
        planning = planning.model_copy(
            update={"scope": planning.scope.model_copy(update={field: value})}
        )
    current = original.model_copy(
        update={"planning": planning, "worker_id": "cold_worker", "lease_token": UUID(int=998)}
    )
    before = {p: n.data for p, n in root_store.fs.nodes.items()}
    with pytest.raises(ValueError):
        store.inspect_publication(key, current, plan.sha256)
    assert before == {p: n.data for p, n in root_store.fs.nodes.items()}


async def test_new_bound_worker_keeps_behavior_approval_but_unbound_row_cannot_adopt_it(live):
    from dataclasses import replace

    from agent_hub.capabilities.execution_authority import bind_execution_authority
    from agent_hub.skills.sandbox.python_resolution_protocol import ResolutionStatus

    c = live
    approved_actions = []
    dispatched = []

    async def current():
        return c.row

    async def approved(request):
        approved_actions.append(request)
        return True

    async def transport(request):
        dispatched.append(request)
        return ResolutionStatus(state="unknown", key=request.key)

    preparation = api().PythonResolutionPreparation(
        current=current, approved=approved, transport=transport
    )
    args = {
        "needs": (registry.api()[0].DependencyNeed("demo", "a" * 64),),
        "environment": registry.environment(),
        "planning": c.planning,
    }
    with bind_execution_authority(c.context, worker_id="worker", actor="implementer"):
        await preparation.resolve(**args)
    original = preparation._original
    c.row = replace(c.row, worker_id="fresh_worker", worker_lease_token=UUID(int=999))
    with (
        bind_execution_authority(c.context, worker_id="worker", actor="implementer"),
        pytest.raises((ValueError, RuntimeError)),
    ):
        await preparation.resolve(**args)
    fresh_context = c.context.model_copy(update={"execution_token": c.row.worker_lease_token})
    with bind_execution_authority(fresh_context, worker_id="fresh_worker", actor="implementer"):
        await preparation.resolve(**args)
    assert len(dispatched) == 2 and dispatched[-1].claim.lease_token == UUID(int=999)
    assert all(action == approved_actions[0] for action in approved_actions)
    assert preparation._original.original_deadline == original.original_deadline
    assert preparation._original.monotonic_deadline <= original.monotonic_deadline


async def test_unknown_publication_cannot_escape_through_fresh_source_plan(root_store):
    from dataclasses import replace

    protocol, _ = fixtures.api()
    original = fixtures.claim()
    original = original.model_copy(
        update={
            "planning": original.planning.model_copy(
                update={"expected_catalog_sha256": root_store.supply.digest}
            )
        }
    )
    descriptor, _ = await fixtures.data()
    descriptor = replace(descriptor, scope=original.resolution_scope())
    key = protocol.resolution_key(original, descriptor.needs, dict(descriptor.environment))
    store = root_store.store
    store.begin(key, original)
    store.complete(key, original, descriptor)
    first = store.freeze(key, original)
    store.publication_begin(key, original, first)
    fresh = original.model_copy(
        update={
            "worker_id": "fresh_worker",
            "lease_token": UUID(int=999),
            "planning": original.planning.model_copy(
                update={
                    "draft": original.planning.draft.model_copy(update={"source_sha256": "f" * 64})
                }
            ),
        }
    )
    second = store.freeze(key, fresh)
    assert second.sha256 != first.sha256
    before = {p: n.data for p, n in root_store.fs.nodes.items()}
    with pytest.raises(ValueError, match="inspect_only"):
        store.publication_begin(key, fresh, second)
    assert before == {p: n.data for p, n in root_store.fs.nodes.items()}
    store.publication_finish(key, original, first, "c" * 64)
    store.publication_begin(key, fresh, second)
    assert store.publication_status(key, fresh, second) == "unknown"


async def test_publication_keeps_original_monotonic_ceiling_on_fresh_lease(root_store):
    import time
    from dataclasses import replace

    protocol, _ = fixtures.api()
    original = fixtures.claim()
    original = original.model_copy(
        update={
            "planning": original.planning.model_copy(
                update={"expected_catalog_sha256": root_store.supply.digest}
            )
        }
    )
    descriptor, _ = await fixtures.data()
    descriptor = replace(descriptor, scope=original.resolution_scope())
    key = protocol.resolution_key(original, descriptor.needs, dict(descriptor.environment))
    store = root_store.store
    store.begin(key, original)
    store.complete(key, original, descriptor)
    plan = store.freeze(key, original)
    expired = plan.model_copy(
        update={
            "claim": plan.claim.model_copy(update={"monotonic_deadline": time.monotonic() - 0.01})
        }
    )
    assert expired.sha256 == plan.sha256
    before = {p: n.data for p, n in root_store.fs.nodes.items()}
    with pytest.raises(ValueError):
        store.publication_begin(key, original, expired)
    assert before == {p: n.data for p, n in root_store.fs.nodes.items()}


@pytest.mark.parametrize(
    "history",
    [
        "unknown_original",
        "unknown_intermediate_with_new_head",
        "published",
        "partial_result",
        "unsafe_plan",
        "orphan_result",
    ],
)
async def test_legacy_publication_history_stays_once_across_source(root_store, history):
    from dataclasses import replace

    protocol, _ = fixtures.api()
    original = fixtures.claim()
    original = original.model_copy(
        update={
            "planning": original.planning.model_copy(
                update={"expected_catalog_sha256": root_store.supply.digest}
            )
        }
    )
    descriptor, _ = await fixtures.data()
    descriptor = replace(descriptor, scope=original.resolution_scope())
    key = protocol.resolution_key(original, descriptor.needs, dict(descriptor.environment))
    store = root_store.store
    store.begin(key, original)
    store.complete(key, original, descriptor)
    prior_claim = original
    if history == "unknown_intermediate_with_new_head":
        prior_claim = original.model_copy(
            update={
                "planning": original.planning.model_copy(
                    update={
                        "draft": original.planning.draft.model_copy(
                            update={"source_sha256": "e" * 64}
                        )
                    }
                )
            }
        )
    prior_plan = store.freeze(key, prior_claim)
    legacy_sha = protocol.digest(
        {**prior_plan.model_dump(mode="json"), "claim": prior_plan.claim.effect_payload()}
    )
    store._create(
        store._name(key, "plan-" + legacy_sha),
        prior_plan.model_dump(mode="json"),
        original.deadline,
    )
    if history != "orphan_result":
        store._create(
            store._name(key, "publish-" + legacy_sha),
            {"state": "unknown", "plan_sha256": legacy_sha},
            original.deadline,
        )
    if history in {"published", "orphan_result"}:
        store._create(
            store._name(key, "result-" + legacy_sha),
            {"plan_sha256": legacy_sha, "index_sha256": "c" * 64},
            original.deadline,
        )
    elif history == "partial_result":
        store._create(
            store._name(key, "result-" + legacy_sha), {"plan_sha256": legacy_sha}, original.deadline
        )
    elif history == "unsafe_plan":
        root_store.fs.nodes[
            root_store.supply.state_root.as_posix() + "/" + store._name(key, "plan-" + legacy_sha)
        ].uid = 10001
    fresh = original.model_copy(
        update={
            "worker_id": "fresh_bound_worker",
            "lease_token": UUID(int=999),
            "planning": original.planning.model_copy(
                update={
                    "draft": original.planning.draft.model_copy(update={"source_sha256": "f" * 64})
                }
            ),
        }
    )
    plan = store.freeze(key, fresh)
    assert plan.resolution_key == key and plan.sha256 != prior_plan.sha256
    if history == "unknown_intermediate_with_new_head":
        store._create(store._name(key, "published"), {"plan_sha256": plan.sha256}, fresh.deadline)
    before = {p: (n.data, n.uid, n.mode) for p, n in root_store.fs.nodes.items()}
    if history == "published":
        store.publication_begin(key, fresh, plan)
        assert store.publication_status(key, fresh, plan) == "unknown"
        assert store.inspect_publication(key, prior_claim, legacy_sha).state == "published"
        assert all(
            (root_store.fs.nodes[p].data, root_store.fs.nodes[p].uid, root_store.fs.nodes[p].mode)
            == value
            for p, value in before.items()
        )
    else:
        with pytest.raises(ValueError):
            store.publication_begin(key, fresh, plan)
        assert before == {p: (n.data, n.uid, n.mode) for p, n in root_store.fs.nodes.items()}
        if history.startswith("unknown"):
            assert store.inspect_publication(key, prior_claim, legacy_sha).state == "unknown"


@pytest.mark.parametrize("limit", ["deadline", "bytes"])
async def test_legacy_history_enumeration_is_bounded_before_any_new_effect(
    root_store, monkeypatch, limit
):
    import time
    from contextlib import contextmanager
    from dataclasses import replace
    from types import SimpleNamespace

    protocol, store_module = fixtures.api()
    original = fixtures.claim()
    original = original.model_copy(
        update={
            "planning": original.planning.model_copy(
                update={"expected_catalog_sha256": root_store.supply.digest}
            )
        }
    )
    descriptor, _ = await fixtures.data()
    descriptor = replace(descriptor, scope=original.resolution_scope())
    key = protocol.resolution_key(original, descriptor.needs, dict(descriptor.environment))
    store = root_store.store
    store.begin(key, original)
    store.complete(key, original, descriptor)
    plan = store.freeze(key, original)
    seen = []
    clock = [time.monotonic()]
    if limit == "deadline":
        monkeypatch.setattr(store_module.files, "time", SimpleNamespace(monotonic=lambda: clock[0]))

    @contextmanager
    def scandir(parent):
        assert root_store.fs.fds[parent][1] == root_store.supply.state_root.as_posix()

        def entries():
            for number in range(store_module.MAX_CONTROL):
                seen.append(number)
                if limit == "deadline" and number == 1:
                    clock[0] = original.monotonic_deadline + 1
                yield SimpleNamespace(name=f"{number:012d}" + "x" * 220)

        yield entries()

    monkeypatch.setattr(root_store.fs, "scandir", scandir)
    before = {p: n.data for p, n in root_store.fs.nodes.items()}
    with pytest.raises(ValueError, match="timeout" if limit == "deadline" else "history_limit"):
        store.publication_begin(key, original, plan)
    assert len(seen) <= (2 if limit == "deadline" else store_module.MAX_CONTROL // 232 + 1)
    assert before == {p: n.data for p, n in root_store.fs.nodes.items()}
