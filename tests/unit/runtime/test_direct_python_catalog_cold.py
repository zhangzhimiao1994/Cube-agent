"""Real action/Harness/private verifier recovery; model, SQL and native reports are synthetic.

No provider, broker process, install, build or test subprocess is executed here.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Unpack
from uuid import UUID

import pytest

from agent_hub.capabilities.runtime import RuntimeCapabilityGateway
from agent_hub.harness.tool_gateway import HarnessToolGateway
from agent_hub.runs.approval_checkpoint import checkpoint_waits_for_approval
from agent_hub.runs.project_verification_receipts import RECEIPT_KEY
from agent_hub.runtime.action_direct import ActionApprovedDirectRuntime
from agent_hub.runtime.continuation_artifacts import load_continuation
from agent_hub.runtime.contracts import Artifact, EventKind, JsonValue, RunEvent, RuntimeCheckpoint
from agent_hub.runtime.direct import RuntimeExecutionError
from agent_hub.runtime.worker_execution import worker_runtime_events
from agent_hub.skills.sandbox.project import ProjectInvocation
from agent_hub.skills.sandbox.python_project_profile import (
    PythonProjectProfileResponse,
    PythonProjectProfileSandbox,
    PythonProjectProfileScope,
)
from tests.unit.capabilities.test_scoped_read import project_path
from tests.unit.capabilities.test_verification_cold_recovery import Fixture
from tests.unit.runs.project_auto_repair_typed_fixture import (
    json_map,
    mapping,
    present,
    sequence,
    string,
)
from tests.unit.runtime.direct_catalog_diagnostic_typed_fixture import (
    CallTrace,
    ExecuteArgs,
    Policy,
)
from tests.unit.runtime.test_direct_project_verification import Model, batch
from tests.unit.runtime.test_direct_python_catalog import (
    DIGEST,
    catalog_batch,
    profile,
    profile_transport,
)


class ColdCatalog:
    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.tmp_path, self.monkeypatch = tmp_path, monkeypatch
        self.fixture = Fixture(tmp_path)
        self.fixture.db.row.routing_decision.update({
            "project_scale": "small", "project_delivery": "workspace",
            "artifact_strategy": "workspace_bundle", "permission_mode": "default",
            "engineering_catalog_reads_version": 1,
        })
        self.policy = Policy()
        self.policy.allowed.update({"write", "run", "create"})
        self.calls: list[CallTrace] = []
        self.gateways: list[RuntimeCapabilityGateway] = []
        self.published: dict[UUID, Artifact] = {}
        profile_transport(monkeypatch)

    async def step(self, model: Model, checkpoint: RuntimeCheckpoint | None = None) -> list[RunEvent]:
        fixture = self.fixture
        backend = RuntimeCapabilityGateway(skill_store_dir=self.tmp_path / "skills",
            project_workspace_dir=fixture.root, generated_artifact_dir=self.tmp_path / "generated",
            run_repository=fixture.runs, project_executor=fixture.executor,
            receipt_repository=fixture.db.repository(), artifact_repository=fixture.artifacts)
        self.gateways.append(backend)
        execute = backend.execute

        async def trace(**kwargs: Unpack[ExecuteArgs]) -> Mapping[str, JsonValue]:
            self.calls.append({**kwargs, "lease": fixture.db.row.worker_lease_token})
            return await execute(**kwargs)

        self.monkeypatch.setattr(backend, "execute", trace)
        runtime = ActionApprovedDirectRuntime(model, logical_model="main", capability_gateway=backend,
            artifact_repository=fixture.artifacts, harness_tool_gateway=HarnessToolGateway(
                backend, policy_gateway=self.policy, require_actor_identity=True))
        context = fixture.context().model_copy(update={
            "checkpoint": checkpoint, "token_budget": 50_000, "artifacts": tuple(self.published.values()),
        })
        if checkpoint is not None:
            await runtime.restore_checkpoint(checkpoint)
        async with worker_runtime_events(runtime, context, worker_id=present(fixture.db.row.worker_id)) as stream:
            events = [event async for event in stream]
        for event in events:
            if event.artifact is not None:
                self.published[event.artifact.id] = event.artifact
        return events

    async def pause(self) -> tuple[Model, RuntimeCheckpoint]:
        model = Model(catalog_batch())
        events = await self.step(model)
        checkpoint = events[-1].checkpoint
        assert checkpoint is not None and checkpoint.runtime_type == "direct_action"
        assert checkpoint.state["tool_name"] == "project.inspect"
        assert checkpoint_waits_for_approval(checkpoint, "approval_read")
        assert len(model.requests) == 1 and not model.texts
        assert not self.calls and not self.fixture.executor.calls
        assert not (self.fixture.root / project_path() / "README.md").exists()
        assert not list((self.tmp_path / "generated").rglob("*.zip"))
        assert not any(event.kind is EventKind.RUNTIME_COMPLETED for event in events)
        return model, checkpoint


async def test_catalog_read_approval_cold_restores_only_pending_query_then_verifies_and_bundles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = ColdCatalog(tmp_path, monkeypatch)
    original, checkpoint = await case.pause()
    fixture = case.fixture
    saved = await load_continuation(fixture.artifacts,
        tenant_id=fixture.db.row.tenant_id, run_id=fixture.db.row.id,
        reference=json_map(checkpoint.state["reference"]))
    assert isinstance(saved, Mapping)
    assert len(sequence(saved["models"])) == 1 and saved["actions"] == ()
    pending = mapping(mapping(saved["pending"])["request"])
    assert pending["tool_name"] == "project.inspect" and pending["actor"] == "main_agent"
    assert mapping(pending["arguments"])["python_catalog"] == {"catalog_sha256": DIGEST, "names": ("pkg0500",)}
    assert mapping(mapping(saved["context"])["routing_decision"])[ "project_id"] == "project-a"
    assert mapping(mapping(saved["context"])["routing_decision"])[ "workspace_session_id"] == "session-a"

    prior_lease = fixture.db.row.worker_lease_token
    fixture.db.new_lease()
    case.policy.allowed.add("read")
    files = {"README.md": "Synthetic catalog-cold fixture, not a real-user acceptance result.\n"}
    resumed_model = Model(batch(files))
    events = await case.step(resumed_model, checkpoint)
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert len(original.requests) == len(resumed_model.requests) == 1
    assert not resumed_model.texts and len(case.gateways) == 2
    assert case.gateways[0] is not case.gateways[1]
    assert [call["name"] for call in case.calls] == [
        "project.inspect", "workspace.write_text", "workspace.write_text", "project.verify", "workspace.bundle",
    ]
    assert [mapping(call["arguments"])["path"] for call in case.calls if call["name"] == "workspace.write_text"] == [
        "README.md", "DELIVERY_MANIFEST.json",
    ]
    query = case.calls[0]
    assert query["arguments"] == pending["arguments"]
    assert query["idempotency_key"] == pending["idempotency_key"]
    assert query["tenant_id"] == fixture.db.row.tenant_id and query["run_id"] == fixture.db.row.id
    assert query["actor"] == "main_agent"
    assert fixture.db.row.worker_lease_token != prior_lease
    assert all(call["lease"] == fixture.db.row.worker_lease_token for call in case.calls)
    prompt = "\n".join(string(message.content) for message in resumed_model.requests[0].messages)
    assert "UNTRUSTED_PYTHON_CATALOG_JSON" in prompt and profile().catalog[500].sha256 in prompt
    assert (fixture.root / project_path() / "README.md").read_text() == files["README.md"]
    # The durable verifier binds the synthetic native report; it is not a real subprocess proof.
    assert len(fixture.executor.calls) == 1
    receipt = mapping(fixture.db.row.routing_decision[RECEIPT_KEY])
    assert receipt["status"] == "passed" and receipt["artifact_references"]
    assert len(list((tmp_path / "generated").rglob("*.zip"))) == 1


@pytest.mark.parametrize("selector", ["absent", None, True, "1", 0, 2, 1.0])
async def test_catalog_cold_selector_change_rejects_before_model_or_pending_query(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, selector: JsonValue,
) -> None:
    case = ColdCatalog(tmp_path, monkeypatch)
    original, checkpoint = await case.pause()
    route = case.fixture.db.row.routing_decision
    if selector == "absent":
        route.pop("engineering_catalog_reads_version")
    else:
        route["engineering_catalog_reads_version"] = selector
    case.fixture.db.new_lease()
    case.policy.allowed.add("read")
    cold_model = Model()
    with pytest.raises(RuntimeExecutionError, match="action continuation context changed"):
        await case.step(cold_model, checkpoint)
    assert len(original.requests) == 1 and not cold_model.requests
    assert not case.calls and not case.fixture.executor.calls
    assert not list((tmp_path / "generated").rglob("*.zip"))


async def test_catalog_digest_change_while_awaiting_read_cannot_refresh_or_deliver(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = ColdCatalog(tmp_path, monkeypatch)
    original, checkpoint = await case.pause()

    async def changed_profile(_self: PythonProjectProfileSandbox, invocation: ProjectInvocation) -> PythonProjectProfileResponse:
        return PythonProjectProfileResponse(ok=True,
            scope=PythonProjectProfileScope.for_invocation(invocation),
            profile=profile().model_copy(update={"catalog_sha256": "d" * 64}))

    monkeypatch.setattr(PythonProjectProfileSandbox, "inspect", changed_profile)
    case.fixture.db.new_lease()
    case.policy.allowed.add("read")
    cold_model = Model()
    with pytest.raises(RuntimeExecutionError, match="generated project inspection failed"):
        await case.step(cold_model, checkpoint)
    assert len(original.requests) == 1 and not cold_model.requests
    assert [call["name"] for call in case.calls] == ["project.inspect"]
    assert mapping(mapping(case.calls[0]["arguments"])["python_catalog"])["catalog_sha256"] == DIGEST
    assert not case.fixture.executor.calls
    assert not (case.fixture.root / project_path() / "README.md").exists()
    assert not list((tmp_path / "generated").rglob("*.zip"))
