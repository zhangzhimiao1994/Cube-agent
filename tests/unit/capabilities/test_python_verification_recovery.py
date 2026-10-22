"""Cold recovery uses real stores and fake Root transport, never native execution."""

from __future__ import annotations

import asyncio
import importlib
import json
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from agent_hub.capabilities.approvals import capability_approval_scope
from agent_hub.capabilities.project_verification import (
    ProjectVerificationError,
    ProjectVerificationService,
)
from agent_hub.subprocess_cleanup import ProcessCleanupIncomplete
from tests.unit.capabilities import test_python_generated_service as generated_helpers
from tests.unit.capabilities import test_python_project_service as original_helpers
from tests.unit.capabilities.test_scoped_read import project_path


def test_restore_entrypoint_exists() -> None:
    name = "agent_hub.capabilities.python_verification_recovery"
    assert importlib.util.find_spec(name) is not None
    assert callable(importlib.import_module(name).restore_python_verification)


@pytest.fixture
def recovery() -> Any:
    return importlib.import_module("agent_hub.capabilities.python_verification_recovery")


@pytest.fixture(params=["python_original", "python_generated"])
async def cold(request: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    kind = request.param
    if kind == "python_original":
        warm, repo, executor, base, source = await original_helpers.configured(tmp_path)
        approval = await warm.authorization_request(original_helpers.request(repo, base))
        repo.approvals.add(capability_approval_scope(approval))
        await original_helpers.observed(warm, repo, base)
        executor.failure_index = None
        payload = await warm.verify(tenant_id=base.tenant_id, run_id=base.run_id, actor="engineer")
        row = repo.record
    else:
        api = importlib.import_module("agent_hub.capabilities.python_generated_service")
        service, _, repo, executor, supply, context = await generated_helpers.configured(api, tmp_path)
        generated_helpers.approve(repo, await service.authorization_request(
            generated_helpers.request(), context))
        payload = await service.verify(context, "engineer")
        warm, base, row = service.owner, context.invocation, repo.row
        source = warm.root / project_path()
        assert not supply.supplies
    payload = json.loads(json.dumps(payload, default=dict))
    restarted_executor = type(executor)(warm.root)
    owner = ProjectVerificationService(root=warm.root, repository=repo, executor=None,
                                       python_executor=restarted_executor)

    async def forbidden(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("cold recovery attempted execution, preparation, supply or route mutation")

    for target, names in [
        (owner, ("verify", "prepare_python_original")),
        (owner._python, ("prepare", "verify", "_run", "_persist_reference")),
        (owner._python_generated, ("verify", "_prepare", "_plan", "_supply", "_persist_reference")),
        (owner.generated, ("prepare", "update")),
        (owner._python_generated.supply_client, ("plan", "supply")),
        (restarted_executor, ("run", "probe")),
    ]:
        for name in names:
            monkeypatch.setattr(target, name, forbidden)
    if kind == "python_generated":
        monkeypatch.setattr(repo, "compare_and_set_generated_preparation", forbidden)
    return {
        "owner": owner, "repo": repo, "row": row, "executor": restarted_executor,
        "kind": kind, "base": base, "source": source, "payload": payload,
        "kwargs": {"kind": kind, "tenant_id": base.tenant_id, "run_id": base.run_id,
                   "actor": "engineer", "payload": payload,
                   "source_sha256": payload["verification"]["source_sha256"],
                   "execution_id": payload["verification"]["execution_id"]},
    }


def caches(owner: Any) -> list[Any]:
    return [owner._receipts, owner._original_receipts, owner._generated_receipts,
            owner._python.receipts, owner._python.environments, owner._python_generated.receipts]


def assert_empty(cold: Any) -> None:
    key = (cold["base"].tenant_id, cold["base"].run_id)
    assert all(key not in cache for cache in caches(cold["owner"]))


async def test_cold_instance_restores_only_after_current_environment_inspection(
    recovery: Any, cold: Any,
) -> None:
    owner, row, executor = cold["owner"], cold["row"], cold["executor"]
    before = json.dumps(row.routing_decision, default=dict, sort_keys=True)
    assert_empty(cold)
    assert await recovery.restore_python_verification(owner, **cold["kwargs"]) is None
    assert len(executor.inspections) == 1
    assert await owner.require_current_receipt(cold["base"].tenant_id, cold["base"].run_id) == (
        cold["kwargs"]["source_sha256"])
    assert len(executor.inspections) == 2
    assert all(not invocation.install_allowed for invocation in executor.inspections)
    assert len({inv.execution_id for inv in executor.inspections}) == 2
    assert not executor.calls and not executor.probes
    assert json.dumps(row.routing_decision, default=dict, sort_keys=True) == before


@pytest.mark.parametrize("change", ["source", "contract", "recipe", "marker", "environment",
                                   "grant", "permissions", "session", "reference"])
async def test_current_changes_reject_recovery(recovery: Any, cold: Any, change: str) -> None:
    source, route = cold["source"], cold["row"].routing_decision
    if change == "source":
        (source / "src/billing/__init__.py").write_bytes(b"def total(): return 99\n")
    elif change == "contract":
        (source / "tests/test_billing.py").write_bytes(b"changed checks\n")
    elif change == "recipe":
        with (source / "pyproject.toml").open("ab") as stream:
            stream.write(b"\n# changed recipe\n")
    elif change == "marker":
        name = "project_input_preparation" if cold["kind"] == "python_original" else (
            "generated_project_preparation")
        route[name]["python_recipe"]["recipe_sha256"] = "0" * 64
    elif change == "environment":
        cold["executor"].changed_environment = True
    elif change == "grant":
        cold["repo"].approvals.clear()
    elif change == "permissions":
        route["requested_permissions"] = ["workspace.read"]
    elif change == "session":
        route["workspace_session_id"] = "other-session"
    else:
        prefix = "python-environment-" if cold["kind"] == "python_original" else (
            "generated-environment-")
        for member in cold["owner"].root.rglob(prefix + "*.json"):
            member.unlink()
        if cold["kind"] == "python_original":
            # The original observation may legitimately carry this reference.
            cold["payload"]["python_environment_reference"]["security_manifest_sha256"] = "0" * 64
    with pytest.raises(ProjectVerificationError):
        await recovery.restore_python_verification(cold["owner"], **cold["kwargs"])
    assert_empty(cold)
    assert not cold["executor"].calls and not cold["executor"].probes


@pytest.mark.parametrize("defect", ["source", "execution", "recipe", "runtime", "origin", "status",
                                   "reference_scope", "reference_digest", "reference_missing",
                                   "binding", "wrong_kind", "tenant", "run"])
async def test_unbound_payload_cannot_restore(recovery: Any, cold: Any, defect: str) -> None:
    payload, kwargs = cold["payload"], cold["kwargs"]
    report = payload["verification"]
    if defect == "source":
        report["source_sha256"] = "0" * 64
    elif defect == "execution":
        kwargs["execution_id"] = "other_execution"
    elif defect == "recipe":
        payload["python_recipe"]["environment_input_sha256"] = "0" * 64
    elif defect == "runtime":
        report["runtime"]["binary_sha256"] = "0" * 64
    elif defect == "origin":
        report["dependency_install"] = {"mode": "reused", "origin_execution_id": "other_origin"}
    elif defect == "status":
        report["status"] = "failed"
    elif defect == "reference_scope":
        payload["python_environment_reference"]["scope"]["run_id"] = str(UUID(int=99))
    elif defect == "reference_digest":
        payload["python_environment_reference"]["security_manifest_sha256"] = "0" * 64
    elif defect == "reference_missing":
        del payload["python_environment_reference"]
    elif defect == "binding":
        field = "original_check_contract" if cold["kind"] == "python_original" else (
            "generated_project_acceptance")
        payload[field]["schema_version"] = 99
    elif defect == "wrong_kind":
        kwargs["kind"] = "python_generated" if cold["kind"] == "python_original" else (
            "python_original")
    else:
        kwargs[defect + "_id"] = UUID(int=99)
    with pytest.raises(ProjectVerificationError):
        await recovery.restore_python_verification(cold["owner"], **kwargs)
    assert_empty(cold)
    assert not cold["executor"].calls and not cold["executor"].probes


@pytest.mark.parametrize("defect", ["acceptance", "names", "proof", "count", "request", "lease"])
async def test_generated_acceptance_and_current_authority_must_match(
    recovery: Any, cold: Any, defect: str,
) -> None:
    if cold["kind"] != "python_generated":
        pytest.skip("generated acceptance only")
    payload = cold["payload"]
    if defect == "acceptance":
        payload["verification"]["generated_acceptance_sha256"] = "0" * 64
    elif defect == "names":
        payload["generated_project_acceptance"]["test_execution"]["names"] = ["fake.test"]
    elif defect == "proof":
        payload["generated_project_acceptance"]["test_execution"]["test_evidence_sha256"] = "0" * 64
    elif defect == "count":
        payload["verification"]["test_count"] = 2
    elif defect == "request":
        cold["row"].request = "a different task"
    else:
        cold["row"].worker_lease_token = None
    with pytest.raises(ProjectVerificationError):
        await recovery.restore_python_verification(cold["owner"], **cold["kwargs"])
    assert_empty(cold)


@pytest.mark.parametrize("failure", [asyncio.CancelledError, ProcessCleanupIncomplete, OSError])
async def test_inspection_failure_clears_all_run_caches_and_preserves_cleanup(
    recovery: Any, cold: Any, monkeypatch: pytest.MonkeyPatch, failure: Any,
) -> None:
    owner, base = cold["owner"], cold["base"]
    key, other = (base.tenant_id, base.run_id), (UUID(int=77), UUID(int=88))
    for cache in caches(owner):
        cache[key] = object()
        cache[other] = "unrelated"
    error = failure("inspection interrupted")

    async def interrupted(*args: Any, **kwargs: Any) -> Any:
        raise error

    monkeypatch.setattr(cold["executor"], "inspect_environment", interrupted)
    expected = ProjectVerificationError if failure is OSError else failure
    with pytest.raises(expected) as caught:
        await recovery.restore_python_verification(owner, **cold["kwargs"])
    if failure is not OSError:
        assert caught.value is error
    assert_empty(cold)
    assert all(cache[other] == "unrelated" for cache in caches(owner))


async def test_original_observation_is_a_valid_historical_reference(
    recovery: Any, cold: Any,
) -> None:
    if cold["kind"] != "python_original":
        pytest.skip("original observation only")
    for member in cold["owner"].root.rglob("python-environment-*.json"):
        member.unlink()
    await recovery.restore_python_verification(cold["owner"], **cold["kwargs"])
    assert len(cold["executor"].inspections) == 1


async def test_original_observation_change_rejects_binding(recovery: Any, cold: Any) -> None:
    if cold["kind"] != "python_original":
        pytest.skip("original observation only")
    route = cold["row"].routing_decision
    route["project_input_preparation"]["observation_sha256"] = "0" * 64
    with pytest.raises(ProjectVerificationError):
        await recovery.restore_python_verification(cold["owner"], **cold["kwargs"])
    assert_empty(cold)
