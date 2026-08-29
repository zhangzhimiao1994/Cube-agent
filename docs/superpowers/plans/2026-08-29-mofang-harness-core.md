# Mofang Harness Core Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add the first production-safe Mofang harness layer that owns capability-aware execution decisions without rewriting the existing runtime loop.

**Architecture:** Keep `agent_hub.runtime` as the low-level execution contract and add `agent_hub.harness` as the policy/protocol layer above it. The first slice introduces provider capability profiles, task requirements, scheduler decision envelopes, Hermes+ context hints, and replay-safe tool envelopes, then lets `RunService` stamp queued runs with a harness decision that downstream runtimes and UIs can observe.

**Tech Stack:** Python 3.12, dataclasses, existing `agent_hub.models`, `agent_hub.runtime.contracts`, pytest, ruff, mypy.

---

### Task 1: Harness Provider Profiles and Scheduler

**Files:**
- Create: `src/agent_hub/harness/__init__.py`
- Create: `src/agent_hub/harness/types.py`
- Create: `src/agent_hub/harness/scheduler.py`
- Test: `tests/unit/harness/test_scheduler.py`

- [x] **Step 1: Write failing scheduler tests**

```python
from uuid import UUID

from agent_hub.domain.runs import TaskMode
from agent_hub.harness.scheduler import CapabilityAwareHarnessScheduler
from agent_hub.harness.types import (
    HarnessPolicy,
    HarnessTaskRequirements,
    HermesContextHint,
    ProviderCapabilityProfile,
)

TENANT_ID = UUID("22222222-2222-4222-8222-222222222222")


def test_scheduler_prefers_deepseek_for_reasoning_tool_tasks_with_prefix_cache() -> None:
    scheduler = CapabilityAwareHarnessScheduler(
        profiles=(
            ProviderCapabilityProfile.deepseek("deepseek-chat", logical_model="main"),
            ProviderCapabilityProfile.openai_codex("gpt-5", logical_model="main"),
        )
    )

    decision = scheduler.select(
        tenant_id=TENANT_ID,
        mode=TaskMode.DISPATCH,
        requirements=HarnessTaskRequirements(
            required_capabilities=frozenset({"text", "tool_calling"}),
            needs_reasoning=True,
            needs_streamed_tool_calls=True,
            estimated_input_tokens=96_000,
            prefers_prefix_cache=True,
        ),
        policy=HarnessPolicy(allowed_providers=frozenset({"deepseek", "openai"})),
        hermes_hint=HermesContextHint(project_id="cube-agent", confidence=0.82),
    )

    assert decision.selected_provider == "deepseek"
    assert decision.selected_model == "deepseek-chat"
    assert "supports_prefix_cache" in decision.capability_reasons
    assert "hermes_context_match" in decision.context_reasons
    assert decision.policy_reasons == ("provider_allowed:deepseek",)


def test_scheduler_never_lets_hermes_override_policy() -> None:
    scheduler = CapabilityAwareHarnessScheduler(
        profiles=(
            ProviderCapabilityProfile.deepseek("deepseek-chat", logical_model="main"),
            ProviderCapabilityProfile.openai_codex("gpt-5", logical_model="main"),
        )
    )

    decision = scheduler.select(
        tenant_id=TENANT_ID,
        mode=TaskMode.DIRECT,
        requirements=HarnessTaskRequirements(required_capabilities=frozenset({"text"})),
        policy=HarnessPolicy(allowed_providers=frozenset({"openai"})),
        hermes_hint=HermesContextHint(
            project_id="cube-agent",
            preferred_provider="deepseek",
            confidence=0.99,
        ),
    )

    assert decision.selected_provider == "openai"
    assert "provider_blocked:deepseek" in decision.fallbacks_considered
```

- [x] **Step 2: Run tests and confirm the module is missing**

Run: `python -m pytest tests\unit\harness\test_scheduler.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'agent_hub.harness'`.

- [x] **Step 3: Implement the minimal typed scheduler**

Create immutable dataclasses in `types.py` for policy, requirements, provider profiles, Hermes hints, and decision envelopes. Create `CapabilityAwareHarnessScheduler.select(...)` in `scheduler.py` that filters by policy and hard capabilities, scores capability/profile/runtime/Hermes hints, and returns transparent reasons without letting Hermes override allowed providers.

- [x] **Step 4: Run scheduler tests**

Run: `python -m pytest tests\unit\harness\test_scheduler.py -q`
Expected: PASS.

### Task 2: Replay-Safe Harness Tool Envelope

**Files:**
- Modify: `src/agent_hub/harness/types.py`
- Test: `tests/unit/harness/test_tool_envelope.py`

- [x] **Step 1: Write failing tool envelope tests**

```python
from uuid import UUID

import pytest

from agent_hub.harness.types import HarnessToolCallRequest, HarnessToolCallResult

RUN_ID = UUID("11111111-1111-4111-8111-111111111111")


def test_tool_envelope_requires_idempotency_and_redacts_sensitive_arguments() -> None:
    with pytest.raises(ValueError, match="sensitive"):
        HarnessToolCallRequest(
            run_id=RUN_ID,
            actor="main_agent",
            tool_name="workspace_read",
            arguments={"api_key": "sk-secret"},
            approval_required=False,
            sandbox="read_only",
            idempotency_key="tool:abc",
        )


def test_tool_result_truncates_large_payloads_for_replay() -> None:
    result = HarnessToolCallResult(
        call_id="call-1",
        tool_name="workspace_read",
        status="succeeded",
        payload={"text": "x" * 70_000},
    )

    assert result.truncated is True
    assert len(str(result.payload["text"]).encode("utf-8")) <= 65_536
```

- [x] **Step 2: Run tests and confirm missing contracts**

Run: `python -m pytest tests\unit\harness\test_tool_envelope.py -q`
Expected: FAIL because `HarnessToolCallRequest` and `HarnessToolCallResult` do not exist.

- [x] **Step 3: Implement the contracts**

Add request/result dataclasses with safe identifiers, bounded JSON, sensitive-key rejection for requests, deterministic call IDs, explicit approval/sandbox fields, and result truncation metadata.

- [x] **Step 4: Run tool envelope tests**

Run: `python -m pytest tests\unit\harness\test_tool_envelope.py -q`
Expected: PASS.

### Task 3: RunService Harness Decision Stamping

**Files:**
- Modify: `src/agent_hub/runs/service.py`
- Test: `tests/unit/runs/test_harness_decision.py`

- [x] **Step 1: Write failing RunService tests**

```python
from agent_hub.domain.runs import TaskMode
from agent_hub.runs.service import RunService


async def test_submit_stamps_harness_decision_in_routing_payload(...) -> None:
    ...
```

- [x] **Step 2: Run focused test and confirm no harness decision is stamped**

Run: `python -m pytest tests\unit\runs\test_harness_decision.py -q`
Expected: FAIL because `routing_decision["harness_decision"]` is absent.

- [x] **Step 3: Inject a scheduler into RunService**

Accept an optional harness scheduler in `RunService.__init__`, build conservative requirements from mode/message/operator selection, call the scheduler before queued run creation, and write only the serializable decision envelope into `routing_decision["harness_decision"]`.

- [x] **Step 4: Run focused RunService tests**

Run: `python -m pytest tests\unit\runs\test_harness_decision.py -q`
Expected: PASS.

### Task 4: Quality Gates and Handoff

**Files:**
- Modify: local handoff/progress notes

- [x] **Step 1: Run focused harness tests**

Run: `python -m pytest tests\unit\harness tests\unit\runs\test_harness_decision.py -q`
Expected: PASS.

- [x] **Step 2: Run scoped backend gates**

Run: `python -m ruff check src\agent_hub\harness src\agent_hub\runs\service.py src\agent_hub\app.py tests\unit\harness tests\unit\runs\test_harness_decision.py tests\unit\test_app_wiring.py`
Expected: PASS.

Run: `python -m mypy --strict src\agent_hub\harness src\agent_hub\runs\service.py src\agent_hub\app.py tests\unit\harness tests\unit\runs\test_harness_decision.py tests\unit\test_app_wiring.py`
Expected: PASS.

- [x] **Step 3: Update local handoff**

Append a local-only handoff entry noting the new workspace, configured production validation target, implemented harness slice, tests run, and remaining harness phases.

### Task 5: Provider Stream Adapter Foundation

**Files:**
- Create: `src/agent_hub/harness/provider.py`
- Test: `tests/unit/harness/test_provider_adapter.py`

- [x] **Step 1: Write failing provider adapter tests**

Create tests that prove DeepSeek-style deltas preserve `reasoning_content`, aggregate streamed tool call argument fragments into one `ToolCall`, estimate prefix-cache usefulness from repeated prompt prefixes, and redact provider error messages containing credentials.

- [x] **Step 2: Run tests and confirm missing adapter**

Run: `python -m pytest tests\unit\harness\test_provider_adapter.py -q`
Expected: FAIL because `agent_hub.harness.provider` does not exist.

- [x] **Step 3: Implement provider adapter pure functions**

Create `ProviderStreamNormalizer`, `ProviderStreamDelta`, and `ProviderErrorEnvelope`. Keep the layer network-free and provider-neutral, with DeepSeek handled by explicit provider profile rather than UI/runtime branching.

- [x] **Step 4: Run provider adapter tests**

Run: `python -m pytest tests\unit\harness\test_provider_adapter.py -q`
Expected: PASS.

### Task 6: Harness Profiles From Real Model Config

**Files:**
- Create: `src/agent_hub/harness/config.py`
- Test: `tests/unit/harness/test_config_profiles.py`

- [x] **Step 1: Write failing config profile tests**

Create tests that build a `PlatformConfig` with DeepSeek, OpenAI/Codex, Anthropic, local OpenAI-compatible, and multimodal generation deployments. Prove the generated harness scheduler profiles preserve real logical model ids, declare provider strengths conservatively, keep generation providers separate from reasoning models, and do not invent unconfigured providers.

- [x] **Step 2: Run tests and confirm missing config module**

Run: `python -m pytest tests\unit\harness\test_config_profiles.py -q`
Expected: FAIL because `agent_hub.harness.config` does not exist.

- [x] **Step 3: Implement config-to-profile conversion**

Create `provider_profiles_from_config(config: PlatformConfig) -> tuple[ProviderCapabilityProfile, ...]` and `harness_scheduler_from_config(config: PlatformConfig) -> CapabilityAwareHarnessScheduler | None`. The factory returns `None` when no usable text model is configured.

- [x] **Step 4: Run config profile tests**

Run: `python -m pytest tests\unit\harness\test_config_profiles.py -q`
Expected: PASS.

### Task 7: Production App Wiring and AUTO Queue Coverage

**Files:**
- Modify: `src/agent_hub/app.py`
- Modify: `src/agent_hub/runs/service.py`
- Test: `tests/unit/test_app_wiring.py`
- Test: `tests/unit/runs/test_harness_decision.py`

- [x] **Step 1: Write failing app wiring test**

Create a lifespan test proving production `create_app(...)` reads the current published model config and injects a harness scheduler into the constructed `RunService`.

- [x] **Step 2: Wire config-backed scheduler into production app creation**

Add a startup-safe helper that reads the current config, validates it as `PlatformConfig`, builds a scheduler with `harness_scheduler_from_config(...)`, and degrades to `None` if current config is absent or invalid.

- [x] **Step 3: Write failing AUTO queued-run coverage**

Add a regression proving `TaskMode.AUTO` local resolution to an executable mode stamps `routing_decision.harness_decision` after mode selection.

- [x] **Step 4: Stamp selected-mode AUTO queue paths**

Apply the existing `_with_harness_decision(...)` helper to queued runs after explicit mode switch, conversation continuation, router-ready selection, local resolution, Hermes recommendation, unavailable-router local fallback, and auto-resolved routing.

- [x] **Step 5: Verify wiring and AUTO coverage**

Run: `python -m pytest tests\unit\test_app_wiring.py tests\unit\runs\test_harness_decision.py -q`
Expected: PASS.

### Task 8: Review Hardening Before Server Probe

**Files:**
- Modify: `src/agent_hub/harness/types.py`
- Modify: `src/agent_hub/harness/provider.py`
- Modify: `src/agent_hub/harness/scheduler.py`
- Modify: `src/agent_hub/runs/service.py`
- Test: `tests/unit/harness/test_tool_envelope.py`
- Test: `tests/unit/harness/test_provider_adapter.py`
- Test: `tests/unit/runs/test_harness_decision.py`

- [x] **Step 1: Request independent code review**

Reviewer found submit-time scheduler failure could break submissions, `direct_model` could be contradicted by harness stamping, aggregate tool result payloads could remain too large, and streamed provider tool arguments were not cumulatively bounded.

- [x] **Step 2: Add failing hardening regressions**

Add tests for scheduler fail-open behavior, direct-model logical-model constraint, aggregate result payload replacement, and oversized streamed tool argument rejection.

- [x] **Step 3: Implement hardening fixes**

Catch harness scheduling failures during submit and preserve queued run creation with `harness_unavailable=scheduling_failed`; add `required_logical_model` to requirements and scheduler filtering; replace oversized aggregate result payloads with a bounded summary; cumulatively bound streamed tool-call argument fragments and add JSON depth/node limits to provider event payloads.

- [x] **Step 4: Re-run local gates**

Run: `python -m pytest tests\unit\harness tests\unit\runs\test_harness_decision.py tests\unit\test_app_wiring.py -q`
Observed: 42 passed, 1 existing Starlette/httpx deprecation warning.

Run: scoped `ruff check`
Observed: passed.

Run: scoped `mypy --strict`
Observed: success, 13 source files.

Run: `git diff --check`
Observed: passed, with Git line-ending warnings only.

### Task 9: Deploy First Batch and Server Probe

**Files:**
- Modify: local handoff/progress notes

- [x] **Step 1: Deploy tracked harness commit to the configured validation server**

Observed: native installer deployment completed on the configured production validation server.

- [x] **Step 2: Verify server services and external forwarded HTTP entry**

Observed: `caddy`, `agent-hub-api`, `agent-hub-worker`, and `agent-hub-litellm` were active; `scripts/agent-hub doctor` passed; the external forwarded Web UI and API health endpoints returned successful responses from the local machine.

- [x] **Step 3: Run feature-specific deployed harness probe**

Observed: deployed venv probe passed with `server_harness_core_probe_ok selected_model=deepseek-chat fail_open_status=queued`.

- [x] **Step 4: Push GitHub and inspect checks**

Observed: branch `codex/mofang-harness-core` pushed to `zhangzhimiao1994/Cube-agent` with `git push`; GitHub `quality` run `33276623663` completed successfully for commit `2aba1544583218358d4401fd0aee7b4abd92461e`.

### Task 10: Provider Stream Adapter Runtime Wiring

**Files:**
- Modify: `src/agent_hub/models/gateway.py`
- Modify as needed: `src/agent_hub/models/transport.py` or existing transport protocols
- Test: `tests/unit/models/test_gateway.py` or a focused new gateway stream test

- [x] **Step 1: Map current gateway and transport streaming boundaries**

Identify the smallest production path that can expose harness-normalized OpenAI-compatible stream events without breaking existing non-streaming completion behavior.

- [x] **Step 2: Write failing runtime wiring test**

Add a test proving a gateway/harness integration can consume a transport's raw OpenAI-compatible stream chunks and produce normalized events preserving text, reasoning deltas, tool-call aggregation, selected deployment, and capacity release.

- [x] **Step 3: Implement minimal runtime wiring**

Wire the already-tested harness provider stream bridge into the actual model gateway path behind a typed method or adapter. Preserve existing `complete()` behavior and provider fallback semantics.

- [x] **Step 4: Verify locally**

Run focused gateway/harness tests first, then broaden to backend static gates before deployment.

Observed: added `ModelGateway.stream_openai_compatible_events(...)` with scoped capacity, secret resolution, heartbeat, provider fallback before the first emitted event only, deterministic stream cleanup, outcome recording, and lease release. Focused verification passed: `pytest tests\unit\models\test_gateway.py tests\unit\harness\test_streaming.py -q` -> 66 passed; scoped `ruff` passed; scoped `mypy --strict` passed.

### Task 11: Harness Tool Execution Boundary Recon

**Files:**
- Inspect: existing capability/runtime/tool execution modules
- Modify later only after a TDD plan is written

- [ ] **Step 1: Identify existing approval/sandbox/tool execution surfaces**

Map where `HarnessToolCallRequest` and `HarnessToolCallResult` should connect without bypassing existing capability approvals.

- [ ] **Step 2: Produce a test-first implementation plan**

Define the next minimal tool-boundary behavior that can be deployed and server-probed.
