"""Real asyncpg UUID scan boundaries; no database, native RPC or cache deletion."""

from __future__ import annotations

import copy
import hashlib
import json
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pytest
from asyncpg.pgproto.pgproto import UUID as DatabaseUUID  # type: ignore[import-untyped]

from agent_hub.capabilities.project_contracts import build_project_check_contract
from agent_hub.capabilities.python_project_lifecycle import PythonProjectTerminalHook
from tests.unit.capabilities.test_python_expired_capacity_oct29 import Session as CapacitySession
from tests.unit.capabilities.test_python_project_lifecycle import Rows, Session
from tests.unit.skills.sandbox.test_python_project_client import invocation, reference


def rebuilt_contract(tenant: UUID, run: UUID) -> None:
    """Exercise the exact strict contract constructor reached by baseline.load."""
    files = {"requirements.lock": "b" * 64, "src/main.py": "a" * 64}
    source_sha = hashlib.sha256(json.dumps(
        files, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
    ).encode("ascii")).hexdigest()
    contract = build_project_check_contract(
        tenant_id=tenant, run_id=run, project_id="demo", session_id="one",
        input_source_sha256=source_sha, editable_paths=("src",), source_files=files,
    )
    assert type(contract.tenant_id) is UUID and type(contract.run_id) is UUID
    assert (contract.tenant_id, contract.run_id) == (UUID(int=1), UUID(int=2))


class RetrySession(Session):
    async def scalars(self, query: Any) -> Rows:
        if query.column_descriptions[0]["name"] == "RunRow":
            return Rows([getattr(self, "scan_row", self.row)])
        return await super().scalars(query)


def driver_row(session: Any) -> None:
    session.row.tenant_id = DatabaseUUID(str(UUID(int=1)))
    session.row.id = DatabaseUUID(str(UUID(int=2)))
    assert isinstance(session.row.id, UUID) and type(session.row.id) is not UUID


def hook_for(session: Any, calls: list[str]) -> PythonProjectTerminalHook:
    class Repository:
        async def run_transaction(self) -> Any:
            return session

    class Reader:
        async def python_original_context(self, tenant: UUID, run: UUID) -> Any:
            assert session.open
            rebuilt_contract(tenant, run)
            calls.append("context")
            return SimpleNamespace(
                invocation=invocation().model_copy(update={"tenant_id": tenant, "run_id": run}),
                environment_references=(reference(),),
            )

        async def python_expired_environment_invocation(self, tenant: UUID, run: UUID) -> Any:
            assert session.open
            rebuilt_contract(tenant, run)
            calls.append("expired-context")
            return invocation().model_copy(update={
                "tenant_id": tenant, "run_id": run, "source_kind": "original", "install_allowed": False,
            })

    class Executor:
        async def release_completed(self, item: Any, *, expected_environment: Any) -> Any:
            assert session.open and item.install_allowed is False
            assert type(item.tenant_id) is UUID and type(item.run_id) is UUID
            assert (item.tenant_id, item.run_id) == (session.row.tenant_id, session.row.id)
            calls.append("release")
            return expected_environment

        async def release_expired(self, item: Any) -> bool:
            assert session.open and item.install_allowed is False
            assert type(item.tenant_id) is UUID and type(item.run_id) is UUID
            assert (item.tenant_id, item.run_id) == (session.row.tenant_id, session.row.id)
            calls.append("expired-release")
            return True

    return PythonProjectTerminalHook(Repository(), Reader(), Executor())


@pytest.mark.parametrize("status", ["completed", "cancelled", "failed"])
async def test_retry_scan_real_database_uuid_reaches_strict_contract_and_typed_release(
    status: str,
) -> None:
    session = RetrySession()
    driver_row(session)
    session.row.status = status
    if status != "completed":
        policy = ("terminal_failed_python_environment_cleanup_version" if status == "failed"
                  else "python_project_cancel_cleanup_version")
        session.row.routing_decision[policy] = 1
    original_ids = session.row.tenant_id, session.row.id
    history = copy.deepcopy(session.checkpoints)
    calls: list[str] = []
    hook = hook_for(session, calls)

    assert await hook.retry_completed() == 1

    assert calls == ["context", "release"]
    assert type(hook._retry_cursor) is UUID and hook._retry_cursor == UUID(int=2)
    assert (session.row.tenant_id, session.row.id) == original_ids
    assert type(session.row.tenant_id) is type(session.row.id) is DatabaseUUID
    assert session.checkpoints == history and session.row.status == status
    assert session.row.version == 2
    assert session.row.routing_decision["python_project_cache_release"] == {
        "schema_version": 1, "environment_reference": reference().model_dump(mode="json"),
    }
    assert session.queries[0]._for_update_arg is not None


async def test_capacity_scan_real_database_uuid_reaches_readonly_selector_without_history_change() -> None:
    session = CapacitySession()
    driver_row(session)
    before = copy.deepcopy(vars(session.row))
    calls: list[str] = []
    hook = hook_for(session, calls)

    assert await hook.maintain_capacity() == 1

    assert calls == ["expired-context", "expired-release"]
    assert type(hook._capacity_cursor) is UUID and hook._capacity_cursor == UUID(int=2)
    assert vars(session.row) == before
    assert type(session.row.tenant_id) is type(session.row.id) is DatabaseUUID


@pytest.mark.parametrize("defect", ["unknown", "lease", "legacy_failed", "foreign_row"])
async def test_real_database_uuid_normalization_does_not_authorize_unsafe_rows(defect: str) -> None:
    session = RetrySession()
    driver_row(session)
    if defect == "unknown":
        session.row.routing_decision["retained"] = "UNKNOWN"
    elif defect == "lease":
        session.row.worker_lease_token = UUID(int=8)
    elif defect == "legacy_failed":
        session.row.status = "failed"
    else:
        session.scan_row = SimpleNamespace(  # type: ignore[attr-defined]
            tenant_id=session.row.tenant_id, id=session.row.id, status=session.row.status,
        )
        session.row.id = DatabaseUUID(str(UUID(int=9)))
    before = copy.deepcopy(vars(session.row))
    calls: list[str] = []
    hook = hook_for(session, calls)

    assert await hook.retry_completed() == 1

    assert calls == [] and vars(session.row) == before
    assert type(hook._retry_cursor) is UUID and hook._retry_cursor == UUID(int=2)


@pytest.mark.parametrize("method", ["retry_completed", "maintain_capacity"])
@pytest.mark.parametrize("field", ["tenant_id", "id"])
async def test_database_identity_boundary_rejects_text_instead_of_granting_scope(
    method: str, field: str,
) -> None:
    session: Any = RetrySession() if method == "retry_completed" else CapacitySession()
    driver_row(session)
    setattr(session.row, field, str(getattr(session.row, field)))
    before = copy.deepcopy(vars(session.row))
    calls: list[str] = []
    hook = hook_for(session, calls)

    with pytest.raises(TypeError, match="database identity must be a UUID"):
        await getattr(hook, method)()

    assert calls == [] and vars(session.row) == before
    assert hook._retry_cursor is None and hook._capacity_cursor is None
