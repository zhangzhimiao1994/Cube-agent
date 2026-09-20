from __future__ import annotations

import json
from collections.abc import Mapping
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

from agent_hub.db.session import build_database
from agent_hub.domain.runs import RunStatus, TaskMode
from agent_hub.models.gateway import GatewayCompletion, GatewayRejectedOutput
from agent_hub.models.types import ModelRequest, ModelResponse, RejectedOutputEvidence, TokenUsage
from agent_hub.runs.repository import RunRepository
from agent_hub.runs.service import RunService
from agent_hub.runtime.crew.adapter import CrewAIObjectFactory, CrewDispatchRuntime
from agent_hub.runtime.crew.plan import AgentSpec, DispatchPlan, DispatchStep
from agent_hub.runtime.registry import RuntimeRegistry

SCENARIOS = ("corrected", "native_rejected", "invalid_correction", "review_failure", "shared_slot")


class ProbeQueue:
    async def enqueue_run(self, run_id: UUID, *, idempotency_key: str) -> None:
        pass


class RepairGateway:
    def __init__(self, scenario: str) -> None:
        self.scenario = scenario
        self.requests: list[ModelRequest] = []
        self.invalid_text = "private-invalid-handoff-" + uuid4().hex

    async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
        self.requests.append(request)
        count = sum(item.logical_model == request.logical_model for item in self.requests)
        if request.logical_model == "worker":
            if self.scenario == "review_failure":
                text, usage = '{"result":"candidate"}', TokenUsage(80, 49, 129)
            elif count == 1:
                text, usage = self.invalid_text, TokenUsage(80, 49, 129)
                if self.scenario == "native_rejected":
                    raise GatewayRejectedOutput(
                        evidence=RejectedOutputEvidence(
                            final_text=text, usage=usage, usage_status="known",
                            status="completed", reason="invalid_json",
                        ),
                        deployment_id="worker", logical_model="worker",
                        provider_id="probe", provider_model="probe/worker",
                        cost_usd=Decimal("0.000001"),
                    )
            else:
                assert count == 2, "Only one format correction is permitted"
                assert not request.tools
                assert request.response_schema is not None
                text = (
                    self.invalid_text + "-still-invalid"
                    if self.scenario == "invalid_correction"
                    else '{"result":"corrected candidate"}'
                )
                usage = TokenUsage(10, 7, 17)
        elif request.logical_model == "reviewer":
            assert count == 1, "The worker consumed the shared correction slot"
            if self.scenario == "review_failure":
                raise RuntimeError("reviewer unavailable")
            text = (
                "invalid-review"
                if self.scenario == "shared_slot"
                else '{"verdict":"approve"}'
            )
            usage = TokenUsage(5, 3, 8)
        else:
            assert request.logical_model == "final" and count == 1
            text, usage = "Final independently reviewed result.", TokenUsage(7, 4, 11)
        return GatewayCompletion(
            response=ModelResponse(text=text, usage=usage),
            deployment_id=request.logical_model,
            logical_model=request.logical_model,
            provider_id="probe",
            provider_model="probe/" + request.logical_model,
            cost_usd=Decimal("0.000001"),
        )


def repair_plan() -> DispatchPlan:
    return DispatchPlan(
        agents=(
            AgentSpec(id="worker", role="Worker", goal="Produce the candidate", logical_model="worker",
                      output_schema={"result": "string"}, max_output_tokens=512),
            AgentSpec(id="reviewer", role="Reviewer", goal="Review the exact candidate",
                      logical_model="reviewer", max_output_tokens=512),
            AgentSpec(id="final", role="Final", goal="Report reviewed result", logical_model="final",
                      max_output_tokens=512),
        ),
        steps=(
            DispatchStep(id="draft", agent="worker", reviewer="reviewer", reviewer_retries=0,
                         task="Produce a short candidate result", timeout_seconds=45, token_budget=8192,
                         cost_budget_usd=Decimal(1)),
            DispatchStep(id="final", agent="final", depends_on=("draft",),
                         task="Report the reviewed candidate", final_synthesizer=True,
                         timeout_seconds=45, token_budget=8192, cost_budget_usd=Decimal(1)),
        ),
        max_parallelism=1, total_timeout_seconds=240, total_token_budget=16384,
        total_cost_usd=Decimal(3),
    )


async def test_persisted_structured_repair_and_review_closure(
    database_url: str, tmp_path: Path,
    *, scenarios: tuple[str, ...] = ("corrected",),
) -> None:
    database = build_database(database_url)
    repository = RunRepository(database.session_factory)
    tenant, actor = uuid4(), uuid4()
    run_ids: list[UUID] = []
    try:
        for scenario in scenarios:
            assert scenario in SCENARIOS
            gateway = RepairGateway(scenario)
            runtime = CrewDispatchRuntime(
                gateway, repair_plan(),
                crew_factory=CrewAIObjectFactory(storage_dir=tmp_path / scenario / "crewai"),
            )
            service = RunService(
                repository, runtime_registry=RuntimeRegistry((runtime,)),
                router=None, task_queue=ProbeQueue(),
            )
            submitted = await service.submit(
                tenant_id=tenant, actor_id=actor, mode=TaskMode.DISPATCH,
                message="Produce, review and report a short result without any external actions.",
            )
            run_ids.append(submitted.id)
            result = await service.execute(submitted.id)
            events = await service.events(tenant, submitted.id)
            accepted = scenario in {"corrected", "native_rejected"}
            expected_status = RunStatus.COMPLETED if accepted else RunStatus.FAILED
            assert result.status is expected_status, (scenario, result.status, expected_status)
            expected_models = {
                "corrected": ["worker", "worker", "reviewer", "final"],
                "native_rejected": ["worker", "worker", "reviewer", "final"],
                "invalid_correction": ["worker", "worker"],
                "review_failure": ["worker", "reviewer"],
                "shared_slot": ["worker", "worker", "reviewer"],
            }
            assert [request.logical_model for request in gateway.requests] == expected_models[scenario]
            if scenario != "review_failure":
                first, correction = gateway.requests[:2]
                assert first.response_schema == correction.response_schema
                assert not correction.tools
                assert gateway.invalid_text in "\n".join(str(item.content) for item in correction.messages)
            assert gateway.invalid_text not in json.dumps(events, default=str)
            reviews = [event for event in events if event["kind"] == "review.completed"]
            assert all(
                not isinstance(payload, Mapping)
                or payload.get("review_status") not in {"skipped", "timeout_skipped"}
                for event in reviews for payload in (event.get("payload"),)
            )
            if not accepted:
                assert not any(event["kind"] == "runtime.completed" for event in events)
                assert not any(
                    event["kind"] == "step.completed" and event.get("step_id") == "draft"
                    for event in events
                )
                assert not any(
                    isinstance(payload, Mapping) and payload.get("verdict") == "approve"
                    for event in reviews for payload in (event.get("payload"),)
                )
            async with database.session_factory() as session:
                checkpoint = await repository.latest_checkpoint(
                    session, tenant_id=tenant, run_id=submitted.id,
                )
            assert checkpoint is not None
            usage = checkpoint.state["usage"]
            assert isinstance(usage, Mapping)
            assert usage["tokens"] == {
                "corrected": 165, "invalid_correction": 146,
                "native_rejected": 165, "review_failure": 129, "shared_slot": 154,
            }[scenario]
            if accepted:
                calls = len(gateway.requests)
                repeated = await service.execute(submitted.id)
                assert repeated.status is RunStatus.COMPLETED
                assert len(gateway.requests) == calls
                assert await service.events(tenant, submitted.id) == events
    finally:
        try:
            for run_id in run_ids:
                async with database.session_factory() as session, session.begin():
                    row = await repository.get_for_update(session, run_id)
                    row.status = RunStatus.CANCELLED.value
                await repository.delete_run(tenant, run_id)
        finally:
            await database.dispose()


async def test_persisted_native_rejection_correction(database_url: str, tmp_path: Path) -> None:
    await test_persisted_structured_repair_and_review_closure(
        database_url, tmp_path, scenarios=("native_rejected",),
    )


async def test_persisted_invalid_correction_stops_dependents(database_url: str, tmp_path: Path) -> None:
    await test_persisted_structured_repair_and_review_closure(
        database_url, tmp_path, scenarios=("invalid_correction",),
    )


async def test_persisted_reviewer_failure_never_approves(database_url: str, tmp_path: Path) -> None:
    await test_persisted_structured_repair_and_review_closure(
        database_url, tmp_path, scenarios=("review_failure",),
    )


async def test_persisted_worker_reviewer_share_correction_slot(database_url: str, tmp_path: Path) -> None:
    await test_persisted_structured_repair_and_review_closure(
        database_url, tmp_path, scenarios=("shared_slot",),
    )
