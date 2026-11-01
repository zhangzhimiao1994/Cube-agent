"""Absolute task deadlines with portable FD simulation, not native Linux IO."""

from __future__ import annotations

import sys
from collections.abc import Iterator
from types import ModuleType, SimpleNamespace
from typing import Any, Literal, cast

import pytest

from agent_hub.skills.sandbox import python_dependency_index_store as store
from agent_hub.skills.sandbox import python_dependency_inspection as inspection
from agent_hub.skills.sandbox import python_dependency_metadata as metadata
from agent_hub.skills.sandbox import python_input_fetch as fetch
from agent_hub.skills.sandbox import python_input_supply_catalog as catalog
from agent_hub.skills.sandbox import python_resolution_overlay as overlay
from agent_hub.skills.sandbox.project import ProjectInvocation
from agent_hub.skills.sandbox.python_dependency_metadata import PythonDependencyMetadataIndex
from agent_hub.skills.sandbox.python_input_supply_catalog import RootSupplyConfig
from agent_hub.skills.sandbox.python_resolution_overlay import effective_supply
from tests.unit.skills import test_python_dependency_inspection as inspection_tests
from tests.unit.skills.test_python_dependency_index_store import (
    STATE,
    FileSystem,
    final_path,
    install_index,
    install_wheel,
    metadata_index,
    root_configuration,
    supply_configuration,
)

api = inspection_tests.api
configured = inspection_tests.configured

_Action = Literal["read", "publish"]
_OMITTED = object()


@pytest.fixture
def simulated(monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    filesystem = FileSystem()
    monkeypatch.setattr(store, "os", filesystem)
    monkeypatch.setattr(catalog, "os", filesystem)
    monkeypatch.setattr(store, "sys", SimpleNamespace(platform="linux"))
    clock = SimpleNamespace(monotonic=lambda: filesystem.clock, sleep=filesystem.sleep)
    monkeypatch.setattr(store, "time", clock)
    # Every deadline consumer uses the same simulated monotonic epoch. Keep
    # wheel validation real, including its absolute deadline checks.
    monkeypatch.setattr(metadata, "time", clock)
    monkeypatch.setattr(fetch, "time", clock)
    locks = ModuleType("fcntl")
    for name, value in (("LOCK_SH", 1), ("LOCK_EX", 2), ("LOCK_NB", 4),
                        ("flock", filesystem.flock)):
        monkeypatch.setattr(locks, name, value, raising=False)
    monkeypatch.setitem(sys.modules, "fcntl", locks)
    supply = supply_configuration()
    yield SimpleNamespace(fs=filesystem, supply=supply, root=root_configuration(),
                          value=metadata_index(supply))
    assert filesystem.fds == {}


def _call(
    simulated: Any, action: _Action, *, deadline: Any = _OMITTED,
) -> PythonDependencyMetadataIndex:
    kwargs = {} if deadline is _OMITTED else {"deadline": deadline}
    if action == "read":
        return store.read_dependency_metadata_index(simulated.supply, **kwargs)
    return store.publish_dependency_metadata_index(simulated.root, simulated.supply, **kwargs)


@pytest.mark.parametrize("action", ["read", "publish"])
@pytest.mark.parametrize("deadline", [
    pytest.param(float("nan"), id="nan"),
    pytest.param(float("inf"), id="infinity"),
    pytest.param(float("-inf"), id="negative-infinity"),
    pytest.param(True, id="bool"),
    pytest.param("101.0", id="string"),
    pytest.param(object(), id="object"),
    pytest.param(10**400, id="overflowing-int"),
    pytest.param(0.0, id="zero"),
    pytest.param(-1.0, id="negative"),
    pytest.param(99.0, id="expired"),
    pytest.param(100.0, id="expires-now"),
])
def test_invalid_or_expired_deadline_rejects_before_io(
    simulated: Any, action: _Action, deadline: Any,
) -> None:
    with pytest.raises(store.PythonDependencyIndexError, match="^timeout$"):
        _call(simulated, action, deadline=deadline)
    assert simulated.fs.events == []


@pytest.mark.parametrize("action", ["read", "publish"])
@pytest.mark.parametrize("deadline", [_OMITTED, None, 100.05, 10000.0])
def test_lock_wait_uses_earlier_deadline_without_restarting_operation(
    simulated: Any, action: _Action, deadline: Any,
) -> None:
    simulated.fs.block_lock = True
    with pytest.raises(store.PythonDependencyIndexError, match="^timeout$"):
        _call(simulated, action, deadline=deadline)
    expected = 100.05 if deadline == 100.05 else 110.0 if action == "read" else 160.0
    assert simulated.fs.clock == pytest.approx(expected, abs=1e-9)
    assert simulated.fs.events.count(("open", STATE)) == 1
    assert not any(event in {"read", "create", "link"} for event, _ in simulated.fs.events)


@pytest.mark.parametrize("action", ["read", "publish"])
def test_configuration_time_does_not_refresh_operation_budget(
    simulated: Any, action: _Action, monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = store._supply

    def slow_supply(supply: RootSupplyConfig) -> RootSupplyConfig:
        simulated.fs.clock += 3.0
        return original(supply)

    monkeypatch.setattr(store, "_supply", slow_supply)
    simulated.fs.block_lock = True
    with pytest.raises(store.PythonDependencyIndexError, match="^timeout$"):
        _call(simulated, action, deadline=10000.0)
    assert simulated.fs.clock == pytest.approx(110.0 if action == "read" else 160.0, abs=1e-9)


@pytest.mark.parametrize("action", ["read", "publish"])
@pytest.mark.parametrize("deadline", [None, 101.0])
def test_valid_deadline_keeps_existing_index_reuse_read_only(
    simulated: Any, action: _Action, deadline: float | None,
) -> None:
    install_index(simulated)
    assert _call(simulated, action, deadline=deadline) == simulated.value
    assert not any(event in {"create", "write", "link", "unlink"}
                   for event, _ in simulated.fs.events)


def test_publish_with_task_deadline_builds_index(simulated: Any) -> None:
    install_wheel(simulated)
    assert _call(simulated, "publish", deadline=101.0) == simulated.value
    assert simulated.fs.nodes[final_path(simulated.supply)].data == simulated.value.to_bytes()


@pytest.mark.parametrize("action", ["read", "publish"])
def test_expiry_during_index_read_does_not_replay_or_publish(
    simulated: Any, action: _Action,
) -> None:
    install_index(simulated)
    simulated.fs.after_read = lambda node, path: setattr(simulated.fs, "clock", 100.05)
    with pytest.raises(store.PythonDependencyIndexError, match="^timeout$"):
        _call(simulated, action, deadline=100.05)
    assert simulated.fs.events.count(("read", final_path(simulated.supply))) == 1
    assert not any(event in {"create", "link"} for event, _ in simulated.fs.events)


def test_expiry_after_link_retains_final_without_republishing(simulated: Any) -> None:
    install_wheel(simulated)
    simulated.fs.after_link = lambda source, target: setattr(simulated.fs, "clock", 100.05)
    with pytest.raises(store.PythonDependencyIndexError, match="^timeout$"):
        _call(simulated, "publish", deadline=100.05)
    assert simulated.fs.nodes[final_path(simulated.supply)].data == simulated.value.to_bytes()
    assert sum(event == "create" for event, _ in simulated.fs.events) == 1
    assert sum(event == "link" for event, _ in simulated.fs.events) == 1
    assert not any("/.dependency-metadata-" in path for path in simulated.fs.nodes)


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["success", "timeout", "type_error"])
async def test_inspection_passes_original_deadline_once(
    configured: Any, monkeypatch: pytest.MonkeyPatch, outcome: str,
) -> None:
    request = await inspection_tests.request_for(configured)
    clock = SimpleNamespace(now=100.0)
    monotonic = SimpleNamespace(monotonic=lambda: clock.now)
    monkeypatch.setattr(inspection, "time", monotonic)
    monkeypatch.setattr(store, "time", monotonic)
    monkeypatch.setattr(inspection_tests, "time", monotonic)
    original = configured.api.read_root_configuration

    def read_root() -> Any:
        clock.now += 0.01
        return original()

    monkeypatch.setattr(inspection, "read_root_configuration", read_root)
    seen: list[float | None] = []
    selected: list[RootSupplyConfig] = []

    def select_supply(base: RootSupplyConfig, invocation: ProjectInvocation, *,
                      source_sha256: str, deadline: float) -> RootSupplyConfig:
        assert base is configured.supply and deadline == 100.05
        value = effective_supply(base, invocation, source_sha256=source_sha256, deadline=deadline)
        selected.append(value)
        return value

    monkeypatch.setattr(overlay, "effective_supply", select_supply)

    def read_index(
        supply: RootSupplyConfig, *, deadline: float | None = None,
    ) -> PythonDependencyMetadataIndex:
        # Overlay selection owns an immutable copy. Require the exact object
        # returned by the real selector, with the complete unchanged base value.
        assert len(selected) == 1 and supply is selected[0]
        assert supply == configured.supply
        seen.append(deadline)
        if outcome == "timeout":
            raise store.PythonDependencyIndexError("timeout")
        if outcome == "type_error":
            raise TypeError("reader failed")
        return cast(PythonDependencyMetadataIndex, configured.index)

    monkeypatch.setattr(inspection, "read_dependency_metadata_index", read_index)
    if outcome == "success":
        response = await inspection._observe(request, configured.policy, 100.05)
        assert response.ok and response.version == 8
    else:
        error = store.PythonDependencyIndexError if outcome == "timeout" else TypeError
        with pytest.raises(error):
            await inspection._observe(request, configured.policy, 100.05)
    assert seen == [100.05]
