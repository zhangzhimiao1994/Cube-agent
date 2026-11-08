"""DB/Root boundary state machine only; no live database, cache or model."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import selectors
from collections.abc import Coroutine
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pytest

from agent_hub.capabilities.python_project_lifecycle import PythonProjectTerminalHook
from agent_hub.db.models import RunEventRow, RunRow, RuntimeArtifactRow
from agent_hub.runs.artifacts import PostgresArtifactRepository
from agent_hub.runtime.artifacts import ArtifactReference, InMemoryArtifactRepository
from agent_hub.runtime.contracts import Artifact
from agent_hub.skills.sandbox.python_premodel_retirement import PythonStoppedPremodelSnapshot
from agent_hub.skills.sandbox.python_project_cache import python_project_cache_key
from agent_hub.skills.sandbox.python_project_protocol import (
    PythonFailedMaintenanceInvocation,
    PythonProjectInvocation,
)
from agent_hub.subprocess_cleanup import ProcessCleanupIncomplete
from tests.unit.capabilities.test_python_expired_capacity_oct29 import Session as OldSession
from tests.unit.skills.sandbox.test_python_project_client import invocation
from tests.unit.skills.sandbox.test_python_stopped_premodel_retirement import snapshot_payload


class MemorySelector(selectors.SelectSelector):
    def select(self, timeout: float | None = None) -> list[tuple[selectors.SelectorKey, int]]:
        return []


class MemoryLoop(asyncio.SelectorEventLoop):
    def __init__(self) -> None:
        self.memory_selector = MemorySelector()
        super().__init__(selector=self.memory_selector)

    def _make_self_pipe(self) -> None:
        self._ssock = self._csock = None

    def _close_self_pipe(self) -> None:
        pass

    def _write_to_self(self) -> None:
        pass


def run[T](coro: Coroutine[object, object, T]) -> T:
    loop = MemoryLoop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def encoded(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def snapshot_for(item: Any) -> PythonStoppedPremodelSnapshot:
    p = snapshot_payload()
    scope = {
        key: str(getattr(item, key)) for key in ("tenant_id", "run_id", "project_id", "session_id")
    }
    reservation = json.loads(p["reservation"])
    reservation.update(
        scope=scope | {"original_reference": item.original_reference.model_dump(mode="json")},
        recipe_sha256=item.recipe_sha256,
        environment_input_sha256=item.environment_input_sha256,
        origin_execution_id="original_failed",
    )
    reference = json.loads(p["reference"])
    reference.update(
        scope=reservation["scope"],
        recipe_sha256=item.recipe_sha256,
        environment_input_sha256=item.environment_input_sha256,
        origin_execution_id="original_failed",
    )
    p.update(
        scope=reservation["scope"],
        cache_key=python_project_cache_key(item),
        reservation=encoded(reservation).decode(),
        reference=encoded(reference).decode(),
    )
    p["reservation_sha256"] = hashlib.sha256(p["reservation"].encode()).hexdigest()
    p["reference_sha256"] = hashlib.sha256(p["reference"].encode()).hexdigest()
    identities = list(p["identities"])
    for index, key in ((1, "reservation"), (2, "reference")):
        values = list(identities[index])
        values[6] = len(p[key])
        identities[index] = tuple(values)
    p["identities"] = tuple(identities)
    stage = p["stages"][0]
    control = json.loads(stage["record"])
    control.update(
        scope, execution_id="original_failed", unit="agent-hub-project-original_failed.service"
    )
    stage["name"] = "original_failed-abcdefgh"
    stage["record"] = json.dumps(control, sort_keys=True) + "\n"
    stage["record_sha256"] = hashlib.sha256(stage["record"].encode()).hexdigest()
    identities = list(stage["identities"])
    values = list(identities[1])
    values[6] = len(stage["record"])
    identities[1] = tuple(values)
    stage["identities"] = tuple(identities)
    return PythonStoppedPremodelSnapshot.model_validate_json(encoded(p), strict=True)


class Session(OldSession):
    item: PythonProjectInvocation
    inspected: PythonFailedMaintenanceInvocation
    def __init__(self) -> None:
        super().__init__()
        self.row.blocked_by_run_id = None
        self.row.actor_id = UUID(int=3)
        self.row.actor_role = "operator"
        self.row.request = "immutable original request"
        self.row.mode = "dispatch"
        self.archive: RuntimeArtifactRow | None = None
        self.artifacts: dict[UUID, RuntimeArtifactRow] = {}
        self.child: object = None
        self.events = [
            SimpleNamespace(
                id=UUID(int=10),
                tenant_id=self.row.tenant_id,
                run_id=self.row.id,
                sequence=1,
                kind="runtime.failed",
                payload={"reason": "pre-model preparation failed"},
            )
        ]
        self.trace: list[str] = []
        self.lock_count = 0

    @asynccontextmanager
    async def begin(self) -> Any:
        assert not self.open
        self.open = True
        self.lock_count += 1
        self.trace.append("lock")
        try:
            yield
        finally:
            self.trace.append("unlock")
            self.open = False

    async def scalar(self, query: Any) -> Any:
        assert self.open
        self.queries.append(query)
        entity = query.column_descriptions[0]["entity"]
        if entity is RunRow:
            return self.row if query._for_update_arg is not None else self.child
        if entity is RuntimeArtifactRow:
            selected = query.compile().params.get("id_1")
            return self.artifacts.get(selected)
        if entity.__tablename__ == self.block_table:
            return UUID(int=90)
        return None

    async def scalars(self, query: Any) -> Any:
        self.queries.append(query)
        entity = query.column_descriptions[0]["entity"]
        if entity is RunRow:
            return SimpleNamespace(all=lambda: [self.row])
        assert self.open and entity is RunEventRow
        return SimpleNamespace(all=lambda: self.events)


def setup(defect: str | None = None, *, archive_repo: bool = True) -> tuple[Any, Any, list[str]]:
    s = Session()
    calls = []
    item = invocation().model_copy(update={"source_kind": "original", "install_allowed": False})
    s.item = item

    class Repository:
        async def run_transaction(self) -> Any:
            return s

    class Reader:
        async def python_expired_environment_invocation(self, tenant: UUID, run: UUID) -> Any:
            assert s.open and (tenant, run) == (s.row.tenant_id, s.row.id)
            calls.append("metadata")
            return s.item

        async def python_original_context(self, *args: Any) -> Any:
            raise AssertionError("must not load live workspace")

    class Executor:
        async def inspect_stopped_premodel(self, inv: Any) -> Any:
            assert s.open and inv.install_allowed is False
            calls.append("inspect")
            s.trace.append("inspect")
            s.inspected = inv
            if defect == "inspect-unknown":
                raise ProcessCleanupIncomplete("python_project_cleanup_incomplete")
            if defect == "inspect-invalid":
                return {"status": "unknown"}
            if defect == "inspect-none-race":
                s.row.version += 1
                return None
            if defect == "inspect-none":
                return None
            selected = snapshot_for(item)
            if defect == "commit-stage-collision":
                p = selected.model_dump(mode="json")
                stage = copy.deepcopy(p["stages"][0])
                execution = "python_release_expired_" + UUID(int=2).hex
                marker = json.loads(stage["record"])
                marker.update(execution_id=execution, unit=f"agent-hub-project-{execution}.service")
                stage["name"] = execution + "-abcdefgh"
                stage["record"] = json.dumps(marker, sort_keys=True) + "\n"
                stage["record_sha256"] = hashlib.sha256(stage["record"].encode()).hexdigest()
                stage["identities"][1][6] = len(stage["record"])
                p["stages"].append(stage)
                return PythonStoppedPremodelSnapshot.model_validate_json(encoded(p), strict=True)
            if defect == "snapshot-foreign":
                return snapshot_for(item.model_copy(update={"run_id": UUID(int=9)}))
            if defect == "snapshot-reference":
                return snapshot_for(
                    item.model_copy(
                        update={
                            "original_reference": item.original_reference.model_copy(
                                update={"file_count": 7}
                            )
                        }
                    )
                )
            if defect == "snapshot-recipe":
                return snapshot_for(item.model_copy(update={"recipe_sha256": "2" * 64}))
            if defect == "snapshot-input":
                return snapshot_for(item.model_copy(update={"environment_input_sha256": "2" * 64}))
            return selected

        async def commit_stopped_premodel(self, inv: Any, snapshot: Any) -> Any:
            assert s.open and s.archive is not None and s.archive.permanent is True
            assert inv.execution_id != s.inspected.execution_id
            assert inv.execution_id not in {st.execution_id for st in snapshot.stages}
            calls.append("commit")
            s.trace.append("commit")
            if defect == "commit-unknown":
                raise ProcessCleanupIncomplete("python_project_cleanup_incomplete")
            if defect == "commit-invalid":
                return 1
            return defect != "commit-false"

        async def release_expired(self, inv: Any) -> bool:
            assert s.open
            calls.append("normal-release")
            s.trace.append("normal-release")
            return True

        async def inspect_failed(self, *args: Any) -> Any:
            calls.append("empty-failed")
            raise AssertionError("stopped path may not fall through to another Root protocol")

        async def commit_failed(self, *args: Any) -> Any:
            raise AssertionError("unexpected empty commit")

        async def release_completed(self, *args: Any, **kwargs: Any) -> Any:
            raise AssertionError("unexpected completed cleanup")

    class Archive(InMemoryArtifactRepository):
        async def put(
            self, tenant_id: UUID, run_id: UUID, artifact: Artifact, *, write_id: UUID | None = None,
        ) -> None:
            assert not s.open, "recursive RunRow archive lock"
            intent = artifact.type == "python_stopped_premodel_commit_intent"
            calls.append("intent" if intent else "archive")
            s.trace.append("intent" if intent else "archive")
            if defect == "archive-failure":
                raise ValueError("storage refused")
            if intent and defect == "intent-failure":
                raise ValueError("intent storage refused")
            if intent and defect == "intent-other-owner-attempt":
                foreign = Artifact(id=artifact.id, type=artifact.type, producer=artifact.producer,
                    content={"different_inspector": True})
                await super().put(tenant_id, run_id, foreign)
            await super().put(tenant_id, run_id, artifact, write_id=write_id)
            raw = PostgresArtifactRepository._encode_payload(artifact.to_payload())
            row = RuntimeArtifactRow(
                tenant_id=tenant_id,
                run_id=run_id,
                id=artifact.id,
                payload=raw,
                content_sha256=artifact.content_sha256,
                byte_size=len(raw.encode()),
                permanent=defect != "not-permanent" and not(intent and defect == "intent-not-permanent"),
            )
            s.artifacts[row.id] = row
            if intent and defect == "intent-stored-corrupt":
                row.payload = "{}"
            if not intent:
                s.archive = row

        async def get_many(self, tenant_id: UUID, run_id: UUID, references: tuple[ArtifactReference, ...]) -> tuple[Artifact, ...]:
            assert not s.open
            refs = references
            intent = bool(refs and s.artifacts.get(refs[0].id) is not s.archive)
            calls.append("intent-readback" if intent else "readback")
            s.trace.append("intent-readback" if intent else "readback")
            if intent and defect == "intent-readback-empty":
                return ()
            if defect == "readback-empty":
                return ()
            if defect == "readback-foreign":
                return (Artifact(id=UUID(int=99), type="foreign", producer="fixture", content={}),)
            if defect == "version-race":
                s.row.version += 1
            if defect == "route-race":
                s.row.routing_decision["unreviewed"] = "changed"
            if defect == "request-race":
                s.row.request = "changed"
            if defect == "actor-race":
                s.row.actor_id = UUID(int=8)
            if defect == "lease-race":
                s.row.worker_id = "live"
            if defect == "child-race":
                s.child = UUID(int=99)
            if defect == "provider-race":
                s.block_table = "agent_hub_provider_continuations"
            if defect == "reserved-race":
                s.block_table = "agent_hub_runtime_artifact_writes"
            if defect == "event-race":
                s.events.append(
                    SimpleNamespace(
                        id=UUID(int=11),
                        tenant_id=s.row.tenant_id,
                        run_id=s.row.id,
                        sequence=2,
                        kind="terminal.notified",
                        payload={},
                    )
                )
            if defect == "reference-race":
                s.item = s.item.model_copy(update={"environment_input_sha256": "3" * 64})
            if defect == "stored-corrupt":
                assert s.archive is not None
                s.archive.payload = "{}"
            if defect == "stored-foreign":
                assert s.archive is not None
                s.archive.tenant_id = UUID(int=99)
            return await super().get_many(tenant_id, run_id, references)

    h = PythonProjectTerminalHook(Repository(), Reader(), Executor())
    if archive_repo:
        h._failure_archive_repository = Archive()
    return h, s, calls


def test_archive_protocol_holds_lock_only_for_inspect_and_commit_and_never_mutates_run() -> None:
    h, s, calls = setup()
    before = copy.deepcopy(vars(s.row))
    assert run(h.maintain_capacity()) == 1
    assert [c for c in calls if c != "metadata"] == [
        "inspect",
        "archive",
        "readback",
        "intent",
        "intent-readback",
        "commit",
    ]
    assert s.trace == [
        "lock",
        "inspect",
        "unlock",
        "archive",
        "readback",
        "intent",
        "intent-readback",
        "lock",
        "commit",
        "unlock",
    ]
    assert vars(s.row) == before


@pytest.mark.parametrize(
    "defect",
    [
        "snapshot-foreign",
        "snapshot-reference",
        "snapshot-recipe",
        "snapshot-input",
        "inspect-unknown",
        "archive-failure",
        "readback-empty",
        "readback-foreign",
        "not-permanent",
        "stored-corrupt",
        "stored-foreign",
        "version-race",
        "route-race",
        "request-race",
        "actor-race",
        "lease-race",
        "child-race",
        "provider-race",
        "reserved-race",
        "event-race",
        "reference-race",
        "inspect-invalid",
        "intent-failure",
        "intent-readback-empty",
        "intent-not-permanent",
        "intent-stored-corrupt",
        "intent-other-owner-attempt",
    ],
)
def test_unproven_archive_snapshot_or_owner_drift_never_commits_or_falls_back(defect: str) -> None:
    h, _s, calls = setup(defect)
    assert run(h.maintain_capacity()) == 0
    assert "commit" not in calls and "normal-release" not in calls and "empty-failed" not in calls


@pytest.mark.parametrize("defect", ["commit-unknown", "commit-invalid", "commit-false"])
def test_commit_unknown_or_false_never_replays_or_falls_back(defect: str) -> None:
    h, _s, calls = setup(defect)
    assert run(h.maintain_capacity()) == 0
    assert (
        calls.count("commit") == 1 and "normal-release" not in calls and "empty-failed" not in calls
    )
    # Reset pagination only: this cannot authorize another attempt after UNKNOWN.
    if defect != "commit-false":
        h._capacity_cursor = None
        before = list(calls)
        assert run(h.maintain_capacity()) == 0
        assert calls == before


def test_known_inspect_none_revalidates_under_same_lock_then_uses_old_release() -> None:
    h, s, calls = setup("inspect-none")
    assert run(h.maintain_capacity()) == 1
    assert calls == ["metadata", "inspect", "metadata", "normal-release"]
    assert s.trace == ["lock", "inspect", "normal-release", "unlock"]


def test_no_archive_repository_keeps_old_expiry_behavior() -> None:
    h, _s, calls = setup(archive_repo=False)
    assert run(h.maintain_capacity()) == 1
    assert calls == ["metadata", "normal-release"]


def test_commit_unknown_is_not_retried_by_a_fresh_hook() -> None:
    h, _s, calls = setup("commit-unknown")
    assert run(h.maintain_capacity()) == 0
    assert calls.count("commit") == 1
    fresh = PythonProjectTerminalHook(h.repository, h.verifier, h.executor)
    fresh._failure_archive_repository = h._failure_archive_repository
    before = list(calls)
    assert run(fresh.maintain_capacity()) == 0
    assert "inspect" not in calls[len(before) :] and "commit" not in calls[len(before) :]


def test_known_none_with_db_drift_cannot_trigger_normal_fallback() -> None:
    h, _s, calls = setup("inspect-none-race")
    assert run(h.maintain_capacity()) == 0
    assert "inspect" in calls and "normal-release" not in calls and "commit" not in calls


def test_commit_id_cannot_reuse_any_archived_stage(monkeypatch: Any) -> None:
    from agent_hub.capabilities import python_project_lifecycle as lifecycle

    ids = iter((UUID(int=1), UUID(int=2)))
    monkeypatch.setattr(lifecycle, "uuid4", lambda: next(ids))
    h, _s, calls = setup("commit-stage-collision")
    assert run(h.maintain_capacity()) == 0
    assert "inspect" in calls and "commit" not in calls and "normal-release" not in calls


@pytest.mark.parametrize(
    "defect",
    [
        "blocked",
        "child",
        "approval",
        "provider",
        "outbox",
        "reserved",
        "checkpoint",
        "usage",
        "model",
        "tool",
        "foreign-history",
        "generated",
        "observed",
        "lease",
        "accepted-selfrepair",
        "pending-kind",
        "pending-token",
    ],
)
def test_only_exact_original_failed_premodel_owner_reaches_root(defect: str) -> None:
    h, s, calls = setup()
    row = s.row
    if defect == "blocked":
        row.blocked_by_run_id = UUID(int=9)
    elif defect == "child":
        s.child = UUID(int=9)
    elif defect == "generated":
        row.routing_decision["generated_project_contract_version"] = 1
    elif defect == "observed":
        row.routing_decision["project_input_preparation"]["phase"] = "observed"
    elif defect == "lease":
        row.worker_id = "live"
    elif defect == "accepted-selfrepair":
        row.routing_decision["self_repair_accepted"] = True
    elif defect == "pending-kind":
        row.routing_decision["approval_kind"] = "self_repair"
    elif defect == "pending-token":
        row.routing_decision["decision_token"] = "decision-" + "1" * 64
    elif defect == "foreign-history":
        s.events[0].tenant_id = UUID(int=9)
    elif defect in ("model", "tool"):
        s.events[0].kind = "model.started" if defect == "model" else "tool.started"
    else:
        s.block_table = {
            "approval": "agent_hub_run_approvals",
            "provider": "agent_hub_provider_continuations",
            "outbox": "agent_hub_run_outbox",
            "reserved": "agent_hub_runtime_artifact_writes",
            "checkpoint": "agent_hub_run_checkpoints",
            "usage": "agent_hub_run_usage",
        }[defect]
    before = copy.deepcopy(vars(row))
    assert run(h.maintain_capacity()) == 0
    assert "inspect" not in calls and "commit" not in calls and "normal-release" not in calls
    assert vars(row) == before
