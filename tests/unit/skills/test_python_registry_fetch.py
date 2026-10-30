"""AF_UNIX-only Root simulation; only the existing DynamicUser unit may read PyPI."""

from __future__ import annotations

import asyncio
import importlib
import json
from contextvars import ContextVar

import pytest

from agent_hub.skills.sandbox.base import SkillResult
from agent_hub.skills.sandbox.broker import BrokerPolicy
from tests.unit.skills import test_python_registry_read as registry
from tests.unit.skills import test_python_resolution_store as state_tests

root_store = state_tests.root_store
worker_case_fixture = state_tests.worker_case
offline_network = registry.offline_network
child = ContextVar("g1_registry_child", default=False)
QUIET = "LoadState=not-found\nActiveState=inactive\nSubState=dead\nMainPID=0\nControlPID=0\nControlGroup=\nTasksCurrent=0\n"


@pytest.fixture
async def case(root_store, monkeypatch):
    worker = importlib.import_module("agent_hub.skills.sandbox.python_resolution_worker")
    real_factory = worker.RegistryReader
    c = worker_case_fixture.__wrapped__(root_store, monkeypatch)
    monkeypatch.setattr(worker, "RegistryReader", real_factory)
    supply_worker = importlib.import_module("agent_hub.skills.sandbox.python_input_supply_worker")
    c.policy = BrokerPolicy()
    c.processes = []
    c.child_reads = []
    c.root_dns = []
    c.fail = None

    async def root_dns(*args, **kwargs):
        c.root_dns.append(args)
        raise PermissionError("AF_UNIX-only Root cannot resolve AF_INET")

    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", root_dns)

    class DNS:
        async def resolve(self, host):
            assert child.get(), "Root performed DNS"
            c.child_reads.append(("dns", host))
            return ["93.184.216.34"]

    class Wire:
        async def get(self, url, *, resolved_addresses):
            assert child.get(), "Root performed HTTP"
            c.child_reads.append(("http", url, resolved_addresses))
            return await c.wire.get(url, resolved_addresses=resolved_addresses)

    async def run(command, limits, **kwargs):
        c.processes.append((command, limits, kwargs))
        if command[0].endswith("systemd-run"):
            assert "DynamicUser=yes" in command
            assert "RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6 AF_NETLINK" in command
            assert command[-2:] == (
                "agent_hub.skills.sandbox.python_registry_fetch",
                "--metadata-worker",
            )
            module = importlib.import_module("agent_hub.skills.sandbox.python_registry_fetch")
            query = module.MetadataReadRequest.model_validate_json(
                kwargs["stdin_payload"], strict=True
            )
            assert query.deadline <= c.request.claim.monotonic_deadline
            token = child.set(True)
            try:
                response = await module.read_metadata_snapshot(
                    query, resolver=DNS(), transport=Wire()
                )
            finally:
                child.reset(token)
            if c.fail == "spoof":
                response["approved"] = True
            if c.fail == "url":
                response["url"] = "https://evil.invalid/"
            return SkillResult(exit_code=0, stdout=json.dumps(response), stderr="", timed_out=False)
        return SkillResult(exit_code=0, stdout=QUIET, stderr="", timed_out=False)

    monkeypatch.setattr(supply_worker, "run_subprocess_with_limits", run)
    return c


async def test_af_unix_root_resolves_only_via_nonroot_fixed_metadata_unit(case):
    c = case
    result = await c.worker.execute_resolution(
        c.request, policy=c.policy, root=c.root, supply=c.supply
    )
    assert result.state == "resolved"
    assert c.root_dns == []
    assert len([r for r in c.child_reads if r[0] == "http"]) == 4
    assert len([r for r in c.processes if r[0][0].endswith("systemd-run")]) == 4
    assert c.processes[-1][0][1] == "show"
    assert {w["candidate"]["download"]["name"] for w in result.descriptor["wheels"]} == {
        "demo",
        "helper",
    }


@pytest.mark.parametrize("fault", ["spoof", "url"])
async def test_child_stdout_is_bounded_data_not_a_grant(case, fault):
    c = case
    c.fail = fault
    with pytest.raises(ValueError):
        await c.worker.execute_resolution(c.request, policy=c.policy, root=c.root, supply=c.supply)
    assert "publish" not in c.events and c.root_dns == []
    assert c.store.inspect(c.request.key, c.request.claim).state == "unknown"


async def test_original_budget_is_shared_across_child_reads(case):
    module = importlib.import_module("agent_hub.skills.sandbox.python_registry_fetch")
    p = registry.api()[0]
    c = case
    reader = module.BrokerRegistryReader(c.request, c.policy, c.root, c.supply)
    budget = p.ResolutionBudget(
        p.ResolutionLimits(max_requests=1), c.request.claim.monotonic_deadline
    )
    document, proof = await reader.read("demo", budget=budget)
    assert document["name"] == "demo"
    assert proof.url == "https://pypi.org/simple/demo/"
    usage = dict(budget.usage)
    assert usage["requests"] == 1 and usage["metadata_bytes"] > 0
    count = len(c.processes)
    with pytest.raises(ValueError, match="requests_limit"):
        await reader.read("demo", "1.0", budget=budget)
    assert len(c.processes) == count and not c.root_dns


@pytest.mark.parametrize(
    "values",
    [
        {"name": "../demo"},
        {"version": "https://evil.invalid"},
        {"maximum": True},
        {"maximum": 2 * 1024**2 + 1},
        {"deadline": float("inf")},
        {"approved": True},
    ],
)
def test_metadata_worker_request_rejects_arbitrary_target_or_grant(values):
    import time

    module = importlib.import_module("agent_hub.skills.sandbox.python_registry_fetch")
    with pytest.raises(ValueError):
        module.MetadataReadRequest.model_validate(
            {"name": "demo", "version": None, "maximum": 1024, "deadline": time.monotonic() + 1}
            | values,
            strict=True,
        )


async def test_metadata_cancel_drains_its_existing_fetch_unit(case, monkeypatch):
    from agent_hub.skills.sandbox import python_input_supply_worker as supply_worker

    module = importlib.import_module("agent_hub.skills.sandbox.python_registry_fetch")
    c = case
    entered = asyncio.Event()
    commands = []

    async def run(command, limits, **kwargs):
        commands.append(command)
        if command[0].endswith("systemd-run"):
            entered.set()
            await asyncio.Future()
        return SkillResult(exit_code=0, stdout=QUIET, stderr="", timed_out=False)

    monkeypatch.setattr(supply_worker, "run_subprocess_with_limits", run)
    reader = module.BrokerRegistryReader(c.request, c.policy, c.root, c.supply)
    budget = registry.api()[0].ResolutionBudget(
        registry.api()[0].ResolutionLimits(), c.request.claim.monotonic_deadline
    )
    before = set(asyncio.all_tasks())
    task = asyncio.create_task(reader.read("demo", budget=budget))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert commands[-2][1] == "kill" and commands[-1][1] == "show"
    assert all(
        c.request.claim.planning.invocation.execution_id in " ".join(cmd) for cmd in commands
    )
    assert set(asyncio.all_tasks()) == before


async def test_expired_original_budget_never_starts_metadata_unit(case):
    import time

    module = importlib.import_module("agent_hub.skills.sandbox.python_registry_fetch")
    c = case
    reader = module.BrokerRegistryReader(c.request, c.policy, c.root, c.supply)
    budget = registry.api()[0].ResolutionBudget(
        registry.api()[0].ResolutionLimits(), time.monotonic() - 0.01
    )
    with pytest.raises(ValueError, match="deadline"):
        await reader.read("demo", budget=budget)
    assert c.processes == [] and c.root_dns == []


async def test_legacy_wheel_fetch_keeps_original_batch_command_and_payload(case, monkeypatch):
    import base64

    from agent_hub.skills.sandbox import python_input_supply_worker as supply_worker
    from agent_hub.skills.sandbox.python_input_fetch import WheelDownload

    c = case
    row, raw = registry.wheel("legacy")
    entry = WheelDownload(
        row["filename"], "legacy", "1.0", row["digests"]["sha256"], len(raw), row["url"]
    )
    launched = []

    async def run(command, limits, **kwargs):
        if command[0].endswith("systemd-run"):
            launched.append((command, json.loads(kwargs["stdin_payload"])))
            return SkillResult(
                exit_code=0,
                stdout=json.dumps(
                    {
                        "schema_version": 1,
                        "artifacts": [
                            {
                                "filename": entry.filename,
                                "body_base64": base64.b64encode(raw).decode(),
                            }
                        ],
                    }
                ),
                stderr="",
                timed_out=False,
            )
        return SkillResult(exit_code=0, stdout=QUIET, stderr="", timed_out=False)

    monkeypatch.setattr(supply_worker, "run_subprocess_with_limits", run)
    result = await supply_worker.fetch_catalog_wheels(
        (entry,),
        c.request.claim.planning.invocation,
        c.policy,
        c.root,
        c.supply,
        c.request.claim.monotonic_deadline,
    )
    assert result == {entry.filename: raw}
    command, payload = launched[0]
    assert command[-2:] == ("agent_hub.skills.sandbox.python_input_fetch", "--worker-batch")
    assert (
        set(payload) == {"artifacts", "timeout_seconds"}
        and payload["artifacts"][0]["sha256"] == entry.sha256
    )


async def test_child_metadata_uses_exact_original_deadline_and_rejects_private_dns():
    import time

    module = importlib.import_module("agent_hub.skills.sandbox.python_registry_fetch")
    deadline = time.monotonic() + 1
    query = module.MetadataReadRequest(name="demo", maximum=1024, deadline=deadline)

    class PrivateDNS:
        async def resolve(self, host):
            return ["127.0.0.1"]

    class Wire:
        async def get(self, *args, **kwargs):
            raise AssertionError("private DNS reached HTTP")

    with pytest.raises(ValueError, match="registry_dns"):
        await module.read_metadata_snapshot(query, resolver=PrivateDNS(), transport=Wire())
    assert query.deadline == deadline
