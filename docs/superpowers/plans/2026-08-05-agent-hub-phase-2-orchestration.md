# Agent Hub Phase 2 Models and Orchestration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Execute durable tasks through capability-aware, concurrency-limited model pools and Direct, CrewAI, Discussion Runtime, or Hybrid execution runtimes, including image understanding and low-confidence user clarification.

**Architecture:** Model deployments are hidden behind `ModelGateway`, which obtains Redis-backed capacity leases before calling the LiteLLM Proxy. `ModeRouter` combines explicit commands, deterministic rules, a classifier, and a verifier. Runtime adapters exchange only typed Task, Event, Artifact, and Checkpoint contracts.

**Tech Stack:** LiteLLM Proxy, OpenAI Python client, Redis Lua, Celery, CrewAI, Discussion Runtime backends (MAF default, AG2 optional, AutoGen AgentChat legacy explicit), Pillow, python-magic, optional Tesseract/PaddleOCR adapter, pytest.

---

## File map

- `src/agent_hub/models/`: logical model registry, LiteLLM client, capacity leases, health and fallback.
- `src/agent_hub/multimodal/`: secure image validation and structured vision analysis.
- `src/agent_hub/routing/`: explicit/rule/model routing and clarification decisions.
- `src/agent_hub/runtime/`: common contracts plus Direct, CrewAI, Discussion Runtime backend selection, legacy AutoGen compatibility, and Hybrid adapters.
- `src/agent_hub/runs/`: run repository, application service, Celery tasks, checkpoints, and events.
- `tests/contracts/`: fake model/runtime implementations used to enforce boundaries.

### Task 1: Build the logical model registry and LiteLLM client

**Files:**
- Create: `src/agent_hub/models/types.py`
- Create: `src/agent_hub/models/registry.py`
- Create: `src/agent_hub/models/litellm_client.py`
- Create: `tests/unit/models/test_registry.py`
- Create: `tests/contracts/test_litellm_client.py`

- [ ] **Step 1: Write capability and request-normalization tests**

```python
import pytest
from agent_hub.models.registry import ModelRegistry, NoCapableDeployment
from agent_hub.models.types import Deployment, ModelCapability


def test_registry_excludes_text_only_deployment_for_images() -> None:
    registry = ModelRegistry([Deployment(id="text", logical_model="general", capabilities={ModelCapability.TEXT})])
    with pytest.raises(NoCapableDeployment):
        registry.candidates("general", {ModelCapability.VISION})


def test_same_deployment_can_serve_different_agent_roles() -> None:
    deployment = Deployment(id="shared", logical_model="general", capabilities={ModelCapability.TEXT})
    registry = ModelRegistry([deployment])
    assert registry.candidates("general", {ModelCapability.TEXT}) == [deployment]
```

- [ ] **Step 2: Verify tests fail**

Run: `uv run pytest tests/unit/models/test_registry.py tests/contracts/test_litellm_client.py -q`

Expected: FAIL because model types are missing.

- [ ] **Step 3: Implement stable model types**

```python
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class ModelCapability(StrEnum):
    TEXT = "text"
    VISION = "vision"
    TOOL_CALLING = "tool_calling"
    STRUCTURED_OUTPUT = "structured_output"


@dataclass(frozen=True)
class Deployment:
    id: str
    logical_model: str
    provider_model: str = "openai/gpt-4o-mini"
    api_base: str = "http://litellm:4000/v1"
    secret_ref: str = "litellm-internal-key"
    quota_scope_id: str = "default-account"
    max_concurrency: int = 1
    target_utilization: float = 0.8
    reserved_slots: int = 0
    rpm: int | None = None
    tpm: int | None = None
    weight: int = 100
    capabilities: set[ModelCapability] = field(default_factory=lambda: {ModelCapability.TEXT})


@dataclass(frozen=True)
class ModelMessage:
    role: str
    content: str | list[dict[str, Any]]


@dataclass(frozen=True)
class ModelRequest:
    logical_model: str
    messages: list[ModelMessage]
    required_capabilities: set[ModelCapability]
    timeout_seconds: float = 60.0
    allow_fallback: bool = True
```

- [ ] **Step 4: Implement registry filtering and an `AsyncOpenAI` LiteLLM transport**

`LiteLLMClient.complete(deployment, request, api_key)` must pass the deployment's provider model, normalized messages, timeout, and optional structured response schema to the proxy. It returns text, parsed tool calls, token usage, and provider metadata without leaking the key.

Run: `uv run pytest tests/unit/models/test_registry.py tests/contracts/test_litellm_client.py -q`

Expected: registry tests and mocked HTTP contract tests pass.

- [ ] **Step 5: Commit model registry**

```bash
git add src/agent_hub/models tests/unit/models tests/contracts pyproject.toml uv.lock
git commit -m "feat: add capability aware model registry"
```

### Task 2: Implement API credential pools and capacity leases

**Files:**
- Create: `src/agent_hub/models/capacity.py`
- Create: `src/agent_hub/models/gateway.py`
- Create: `src/agent_hub/models/redis/acquire_lease.lua`
- Create: `src/agent_hub/models/redis/release_lease.lua`
- Create: `tests/integration/models/test_capacity.py`
- Create: `tests/unit/models/test_gateway.py`

- [ ] **Step 1: Write serial, concurrent, queue, and fallback tests**

```python
import asyncio
import pytest


@pytest.mark.integration
async def test_serial_deployment_never_exceeds_one(capacity_pool, serial_deployment) -> None:
    first = await capacity_pool.acquire([serial_deployment], wait_timeout=0.1)
    with pytest.raises(asyncio.TimeoutError):
        await capacity_pool.acquire([serial_deployment], wait_timeout=0.05)
    await capacity_pool.release(first)


@pytest.mark.integration
async def test_two_distinct_serial_keys_provide_two_slots(capacity_pool, serial_a, serial_b) -> None:
    first, second = await asyncio.gather(
        capacity_pool.acquire([serial_a, serial_b], wait_timeout=0.1),
        capacity_pool.acquire([serial_a, serial_b], wait_timeout=0.1),
    )
    assert first.deployment_id != second.deployment_id
```

- [ ] **Step 2: Run tests and confirm failure**

Run: `uv run pytest tests/integration/models/test_capacity.py -q`

Expected: FAIL because no lease pool exists.

- [ ] **Step 3: Implement atomic Redis leases**

Use a sorted set per `quota_scope_id` where score is lease expiry epoch and member is a UUID. The acquire Lua script removes expired leases, calculates a safe operational limit from configured concurrency, target utilization, and reserved slots, compares `ZCARD` to that limit, inserts a new lease when a slot is free, and returns success atomically. Multiple keys in the same quota scope share this signal. Release removes only the exact lease ID. RPM/TPM token buckets are quota-scoped Redis keys with TTL.

```python
@dataclass(frozen=True)
class CapacityLease:
    id: str
    deployment_id: str
    expires_at: datetime


class CapacityPool:
    async def acquire(self, candidates: list[Deployment], wait_timeout: float) -> CapacityLease:
        deadline = asyncio.get_running_loop().time() + wait_timeout
        while True:
            for deployment in self._least_loaded(candidates):
                lease = await self._try_acquire(deployment)
                if lease is not None:
                    return lease
            if asyncio.get_running_loop().time() >= deadline:
                raise asyncio.TimeoutError("model capacity queue timeout")
            await asyncio.sleep(0.05)
```

- [ ] **Step 4: Add gateway fallback and lease recovery**

`ModelGateway.complete()` must acquire from the primary logical model, resolve the secret only after lease acquisition, invoke the transport, and release in `finally` only after the response/stream completes. A capacity wait timeout may traverse the configured fallback chain only when `allow_fallback=True`. A worker heartbeat renews long calls; expired leases are naturally reclaimed. `429`, `503`, or elevated P95 latency reduce quota-scope concurrency and start cooldown; successful probes restore one slot at a time only up to the safe utilization limit.

- [ ] **Step 5: Verify concurrency behavior**

Run: `uv run pytest tests/unit/models/test_gateway.py tests/integration/models/test_capacity.py -q`

Expected: one-key concurrency, independent-quota pooling, same-account keys sharing one limit, safety headroom, RPM/TPM token buckets, bounded queue, timeout fallback, cancellation release, expired lease reclamation, and duplicate fingerprint tests pass.

- [ ] **Step 6: Commit the capacity pool**

```bash
git add src/agent_hub/models tests/unit/models tests/integration/models
git commit -m "feat: add model credential capacity pools"
```

### Task 3: Add secure image intake and vision analysis

**Files:**
- Create: `src/agent_hub/multimodal/types.py`
- Create: `src/agent_hub/multimodal/images.py`
- Create: `src/agent_hub/multimodal/service.py`
- Create: `tests/unit/multimodal/test_images.py`
- Create: `tests/integration/multimodal/test_vision.py`

- [ ] **Step 1: Write image validation and routing tests**

```python
import pytest
from agent_hub.multimodal.images import InvalidImage


def test_rejects_mime_spoof(image_intake, fake_executable_named_png) -> None:
    with pytest.raises(InvalidImage):
        image_intake.validate(fake_executable_named_png, declared_mime="image/png")


async def test_vision_request_never_uses_text_only_model(vision_service, model_gateway) -> None:
    await vision_service.analyze(sample_png(), tenant_id="tenant-1")
    assert model_gateway.last_request.required_capabilities == {"vision", "structured_output"}
```

- [ ] **Step 2: Run tests and confirm failure**

Run: `uv run pytest tests/unit/multimodal tests/integration/multimodal -q`

Expected: FAIL because image services are absent.

- [ ] **Step 3: Implement strict image validation and Artifact storage**

Accept JPEG, PNG, and WebP by decoded format, not extension. Reject files exceeding configured bytes, pixels, dimensions, or decompression ratio. Decode with Pillow, transpose orientation, remove metadata, re-encode to a safe canonical format, hash bytes with SHA-256, and save under a tenant-scoped random object key.

```python
class ImageAnalysisArtifact(BaseModel):
    source_sha256: str
    summary: str
    extracted_text: str | None
    objects: list[str]
    confidence: float = Field(ge=0, le=1)
    logical_model: str
    deployment_id: str
```

- [ ] **Step 4: Implement structured vision calls and OCR fallback**

The vision request sends the canonical image as a short-lived signed URL or bounded data URL, requests the exact `ImageAnalysisArtifact` schema, and records supplier/audit metadata. OCR fallback runs only for text-oriented images and must label its result `ocr_only=True`; it cannot fabricate general visual descriptions.

- [ ] **Step 5: Run tests and commit**

Run: `uv run pytest tests/unit/multimodal tests/integration/multimodal -q`

Expected: MIME spoof, oversized image, EXIF removal, text-only exclusion, low-confidence response, and OCR fallback tests pass.

```bash
git add src/agent_hub/multimodal tests/unit/multimodal tests/integration/multimodal
git commit -m "feat: add secure multimodal image analysis"
```

### Task 4: Implement reliable mode routing and clarification

**Files:**
- Create: `src/agent_hub/routing/types.py`
- Create: `src/agent_hub/routing/rules.py`
- Create: `src/agent_hub/routing/classifier.py`
- Create: `src/agent_hub/routing/service.py`
- Create: `tests/unit/routing/test_router.py`

- [ ] **Step 1: Write routing precedence tests**

```python
async def test_explicit_command_cannot_be_overridden(router) -> None:
    result = await router.route("/discuss compare these proposals")
    assert result.mode.value == "discuss"
    assert result.needs_user_choice is False


async def test_classifier_disagreement_asks_user(router, classifier, verifier) -> None:
    classifier.result(mode="dispatch", confidence=0.91)
    verifier.result(mode="discuss", confidence=0.89)
    result = await router.route("an ambiguous complex request")
    assert result.needs_user_choice is True
    assert result.status == "waiting_user_mode"
```

- [ ] **Step 2: Verify failure**

Run: `uv run pytest tests/unit/routing/test_router.py -q`

Expected: FAIL because `ModeRouter` is absent.

- [ ] **Step 3: Define structured routing results**

```python
class RouteAssessment(BaseModel):
    mode: TaskMode
    confidence: float = Field(ge=0, le=1)
    reason: str
    roles: list[str]
    estimated_seconds: int = Field(ge=0)
    estimated_cost_usd: Decimal = Field(ge=0)
    risk: Literal["low", "medium", "high"]


class RouteDecision(BaseModel):
    mode: TaskMode | None
    needs_user_choice: bool
    status: Literal["ready", "waiting_user_mode"]
    assessments: list[RouteAssessment]
```

- [ ] **Step 4: Implement explicit commands, rules, classifier, and verifier**

Order must be explicit command, deterministic rule, classifier, then verifier for complex/expensive/borderline results. Ask the user when confidence is below `0.85`, modes disagree, risk is high, or cost exceeds policy. A clarification decision creates no runtime job until the user selects a mode.

- [ ] **Step 5: Test and commit**

Run: `uv run pytest tests/unit/routing/test_router.py -q`

Expected: explicit precedence, simple direct answer, fixed workflow dispatch, discussion intent, disagreement, confidence threshold, high-cost confirmation, and user override tests pass.

```bash
git add src/agent_hub/routing tests/unit/routing
git commit -m "feat: add verified execution mode routing"
```

### Task 5: Define runtime contracts and Direct execution

**Files:**
- Create: `src/agent_hub/runtime/contracts.py`
- Create: `src/agent_hub/runtime/registry.py`
- Create: `src/agent_hub/runtime/direct.py`
- Create: `tests/contracts/test_runtime_contract.py`

- [ ] **Step 1: Write a runtime contract test**

```python
async def test_direct_runtime_emits_artifact_and_checkpoint(direct_runtime, task_context) -> None:
    events = [event async for event in direct_runtime.run(task_context)]
    assert [event.kind for event in events] == ["model.started", "artifact.created", "checkpoint.saved", "runtime.completed"]
    assert events[1].artifact.producer == "main"
```

- [ ] **Step 2: Run and verify failure**

Run: `uv run pytest tests/contracts/test_runtime_contract.py -q`

Expected: FAIL because runtime contracts are absent.

- [ ] **Step 3: Define framework-neutral contracts**

```python
JsonValue = None | bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"]


class Artifact(BaseModel):
    id: UUID
    type: str
    producer: str
    content: dict[str, JsonValue]
    source_ids: list[str] = Field(default_factory=list)


class RunEvent(BaseModel):
    kind: str
    artifact: Artifact | None = None
    reason: str | None = None
    inputs: list[Artifact] = Field(default_factory=list)


class RuntimeCheckpoint(BaseModel):
    runtime_type: str
    runtime_version: str
    state: dict[str, JsonValue]


class TaskContext(BaseModel):
    run_id: UUID
    tenant_id: UUID
    mode: TaskMode
    request: str
    artifacts: list[Artifact] = Field(default_factory=list)
    checkpoint: RuntimeCheckpoint | None = None


class ExecutionRuntime(Protocol):
    mode: TaskMode
    async def run(self, context: TaskContext) -> AsyncIterator[RunEvent]:
        raise NotImplementedError

    async def save_checkpoint(self) -> RuntimeCheckpoint:
        raise NotImplementedError

    async def restore_checkpoint(self, checkpoint: RuntimeCheckpoint) -> None:
        raise NotImplementedError

    async def cancel(self) -> None:
        raise NotImplementedError


class RuntimeRegistry:
    def __init__(self, runtimes: list[ExecutionRuntime]) -> None:
        self._runtimes = {runtime.mode: runtime for runtime in runtimes}

    def get(self, mode: TaskMode) -> ExecutionRuntime:
        return self._runtimes[mode]
```

- [ ] **Step 4: Implement DirectRuntime through ModelGateway**

DirectRuntime builds context, calls only `ModelGateway`, emits explicit events, creates one typed text Artifact, and checkpoints after any completed tool or model boundary.

- [ ] **Step 5: Test and commit**

Run: `uv run pytest tests/contracts/test_runtime_contract.py -q`

Expected: contract test passes without importing FastAPI, SQLAlchemy, CrewAI, or AutoGen from `contracts.py`.

```bash
git add src/agent_hub/runtime tests/contracts
git commit -m "feat: add execution runtime contracts"
```

### Task 6: Implement the CrewAI dispatch adapter

**Files:**
- Create: `src/agent_hub/runtime/crew/plan.py`
- Create: `src/agent_hub/runtime/crew/adapter.py`
- Create: `tests/unit/runtime/crew/test_plan.py`
- Create: `tests/integration/runtime/test_crew_adapter.py`

- [ ] **Step 1: Write DAG validation and parallelism tests**

```python
def test_cyclic_dispatch_plan_is_rejected() -> None:
    plan = DispatchPlan(steps=[Step(id="a", agent="x", depends_on=["b"]), Step(id="b", agent="x", depends_on=["a"])])
    with pytest.raises(InvalidDispatchPlan, match="cycle"):
        plan.validate_graph()


async def test_ready_steps_execute_in_parallel(crew_runtime, two_key_model_pool) -> None:
    result = await collect(crew_runtime.run(parallel_plan_context()))
    assert two_key_model_pool.maximum_observed_concurrency == 2
```

- [ ] **Step 2: Verify tests fail**

Run: `uv run pytest tests/unit/runtime/crew tests/integration/runtime/test_crew_adapter.py -q`

Expected: FAIL because dispatch plan and adapter are absent.

- [ ] **Step 3: Implement typed plan validation**

Validate unique step IDs, known agents, existing dependencies, acyclic graph, maximum step count, per-step tool policy, total budget, reviewer retry count, and a final synthesizer.

- [ ] **Step 4: Map plan steps to CrewAI without leaking framework objects**

Build CrewAI Agents and Tasks from the immutable runtime generation. Model calls and tools must be wrappers over `ModelGateway` and `CapabilityGateway`. Emit normalized step, artifact, retry, review, and checkpoint events. Independent ready steps may run concurrently, constrained by model capacity leases.

- [ ] **Step 5: Test and commit**

Run: `uv run pytest tests/unit/runtime/crew tests/integration/runtime/test_crew_adapter.py -q`

Expected: fixed YAML, dynamic DAG, parallel fan-out, reviewer retry limit, model queueing, failure, cancellation, and checkpoint tests pass.

```bash
git add src/agent_hub/runtime/crew tests/unit/runtime/crew tests/integration/runtime
git commit -m "feat: add crewai dispatch runtime"
```

### Task 7: Implement Discussion Runtime backend selection and Hybrid runtimes

**Files:**
- Create: `src/agent_hub/runtime/discussion/registry.py`
- Create: `src/agent_hub/runtime/discussion/__init__.py`
- Create: `src/agent_hub/runtime/autogen/adapter.py`
- Create: `src/agent_hub/runtime/autogen/termination.py`
- Create: `src/agent_hub/runtime/hybrid.py`
- Create: `tests/integration/runtime/test_discussion_backend.py`
- Create: `tests/integration/runtime/test_autogen_adapter.py`
- Create: `tests/integration/runtime/test_hybrid_runtime.py`

- [ ] **Step 1: Write termination and Artifact handoff tests**

```python
async def test_discussion_stops_at_budget(discussion_runtime, low_budget_context) -> None:
    events = await collect(discussion_runtime.run(low_budget_context))
    assert events[-1].kind == "runtime.completed"
    assert events[-1].reason == "budget_exhausted"


def test_default_discussion_backend_prefers_maf_then_ag2(registry) -> None:
    runtime = registry.create(DiscussionBackendSelection())
    assert runtime.backend == "maf"


async def test_hybrid_passes_artifacts_not_framework_state(hybrid_runtime) -> None:
    events = await collect(hybrid_runtime.run(hybrid_context()))
    discussion_input = next(e for e in events if e.kind == "discussion.started")
    assert all(item.type == "artifact" for item in discussion_input.inputs)
```

- [ ] **Step 2: Confirm tests fail**

Run: `uv run pytest tests/integration/runtime/test_discussion_backend.py tests/integration/runtime/test_autogen_adapter.py tests/integration/runtime/test_hybrid_runtime.py -q`

Expected: FAIL because backend selection and adapters are absent.

- [ ] **Step 3: Implement Discussion Runtime backend selection**

Default `Discuss` backend selection must prefer Microsoft Agent Framework (MAF), then AG2. AutoGen AgentChat is retained only as a legacy/experimental compatibility backend and must require explicit selection. Backend selection must fail closed when the selected backend is unavailable or malformed; it must not silently fall back across frameworks and change discussion semantics.

- [ ] **Step 4: Implement legacy AutoGen adapter and composite termination**

Use 2–8 unique participants, a ModelGateway-backed model client, and termination conditions for explicit completion, maximum turns, wall time, tokens, cost, cancellation, and consensus. Persist only explicit messages, citations, tool events, and summaries; do not request hidden reasoning.

- [ ] **Step 5: Implement HybridRuntime composition**

Run dispatch, collect validated Artifacts, build a new discussion context from those Artifacts, run discussion, then invoke the configured synthesizer. Support Direct→Dispatch, Dispatch→Hybrid, and Discuss→Dispatch→Discuss upgrades without repeating completed Artifact-producing work.

- [ ] **Step 6: Test and commit**

Run: `uv run pytest tests/integration/runtime/test_discussion_backend.py tests/integration/runtime/test_autogen_adapter.py tests/integration/runtime/test_hybrid_runtime.py -q`

Expected: backend default/explicit/legacy selection, participant selection, all termination paths, explicit transcript storage, artifact-only handoff, upgrade, resume, and cancel tests pass.

```bash
git add src/agent_hub/runtime/discussion src/agent_hub/runtime/autogen src/agent_hub/runtime/hybrid.py tests/integration/runtime
git commit -m "feat: add discussion and hybrid runtimes"
```

### Task 8: Persist runs, events, checkpoints, and Celery execution

**Files:**
- Modify: `src/agent_hub/db/models.py`
- Create: `alembic/versions/0003_runs.py`
- Create: `src/agent_hub/runs/repository.py`
- Create: `src/agent_hub/runs/service.py`
- Create: `src/agent_hub/runs/tasks.py`
- Create: `src/agent_hub/api/routers/runs.py`
- Create: `tests/integration/runs/test_recovery.py`
- Create: `tests/api/test_runs_api.py`

- [ ] **Step 1: Write crash recovery and clarification API tests**

```python
async def test_worker_resumes_from_last_safe_checkpoint(run_service, crash_after_checkpoint) -> None:
    run_id = await run_service.submit(task_request())
    await crash_after_checkpoint(run_id)
    await run_service.recover(run_id)
    run = await run_service.get(run_id)
    assert run.status.value == "completed"
    assert run.completed_step_ids.count("research") == 1


def test_low_confidence_submission_does_not_enqueue_runtime(client, operator_headers) -> None:
    response = client.post("/api/v1/runs", headers=operator_headers, json={"message": "ambiguous task", "mode": "auto"})
    assert response.status_code == 202
    assert response.json()["status"] == "waiting_user_mode"
```

- [ ] **Step 2: Run and verify failure**

Run: `uv run pytest tests/integration/runs/test_recovery.py tests/api/test_runs_api.py -q`

Expected: FAIL because run persistence is absent.

- [ ] **Step 3: Add run, step, event, artifact, checkpoint, approval, and usage tables**

Use tenant-scoped UUIDs, monotonic event sequence numbers, immutable event payloads, JSONB checkpoints with runtime type/version, Artifact hashes, and indexes on `(tenant_id, status)`, `(run_id, sequence)`, and idempotency keys.

- [ ] **Step 4: Implement transactional outbox and Celery task**

Submission writes Run plus outbox atomically. A publisher enqueues the Celery task and marks the outbox delivered. Worker acquires a run lock, restores the runtime generation and latest checkpoint, emits events transactionally, and acknowledges only after the final checkpoint/event commit.

- [ ] **Step 5: Expose run control APIs**

Add create, read, event stream, choose-mode, pause, resume, cancel, and details endpoints. Enforce tenant RBAC and never expose hidden provider credentials or model reasoning.

- [ ] **Step 6: Run phase verification and commit**

Run: `uv run ruff check . && uv run mypy src && uv run pytest tests/unit/models tests/unit/routing tests/contracts tests/integration/models tests/integration/multimodal tests/integration/runtime tests/integration/runs tests/api/test_runs_api.py -q`

Expected: all checks pass; four modes execute; full-capacity requests queue then fall back; image tasks use vision; low confidence waits for user; crash recovery does not duplicate completed work.

```bash
git add src tests alembic pyproject.toml uv.lock
git commit -m "feat: complete durable multi-agent orchestration"
```
