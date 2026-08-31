# Hermes Runtime Memory Injection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Hermes+ confirmed memories improve later runs by retrieving, gating, explaining, and injecting a small set of relevant structured lessons before execution.

**Architecture:** Extend the existing Hermes advisor path instead of adding RAG. `PersistentHermesRunAdvisor` returns scored injected/skipped memory decisions inside `HermesRunAdvice`; `RunService` stores them in `routing_decision.hermes`; runtime adapters format only approved top-3 lessons into bounded prompts. The frontend scheduling/process drawer shows a compact Hermes+ summary row and opens a drawer for pending/used/overridden memory details.

**Tech Stack:** Python 3.12, SQLAlchemy async, Pydantic contracts, pytest, React, TypeScript, Zod, Vitest/Testing Library.

---

## File Map

- Modify `src/agent_hub/runs/service.py`
  - Add immutable Hermes memory decision dataclasses.
  - Extend `HermesRunAdvice`.
  - Add advice timeout guard.
  - Serialize injected/skipped memory details into `routing_decision.hermes`.
- Modify `src/agent_hub/hermes/advisor.py`
  - Retrieve confirmed Hermes lessons.
  - Score quality, relevance, transferability, conflict risk.
  - Return top-3 injectable memories and skipped-memory explanations.
- Create `src/agent_hub/runtime/hermes_context.py`
  - Convert `routing_decision.hermes.injected_memories` into a bounded, safe prompt block.
- Modify `src/agent_hub/runtime/direct.py`
  - Add Hermes memory block to the direct runtime system/user context.
- Modify `src/agent_hub/runtime/defaults.py`
  - Add Hermes memory block to dispatch role steps and final synthesis step.
  - Add Hermes memory block to discussion task text through `routing_decision`.
- Modify `src/agent_hub/runtime/autogen/adapter.py`
  - Include Hermes memory block in discussion task text.
- Modify `src/agent_hub/runtime/crew/adapter.py`
  - Include Hermes memory block in Crew dispatch user payload.
- Modify `src/agent_hub/runtime/hybrid.py`
  - Preserve parent `routing_decision` when creating child `TaskContext`.
- Modify `web/src/api/client.ts`
  - Extend run/Hermes-related schemas for memory injection summary fields if current run detail schema needs typed access.
- Modify `web/src/pages/RunsPage.tsx`
  - Show compact Hermes+ memory summary in the process drawer.
  - Open a detail drawer from the Hermes+ summary row.
  - Confirm/ignore through existing Hermes endpoints where possible.
- Modify `web/src/styles.css`
  - Add compact row and drawer styles.
- Test `tests/unit/hermes/test_advisor.py`
  - Scoring, cross-mode transfer, same-mode noise filtering, conflict handling.
- Test `tests/unit/runs/test_conversation_mode.py`
  - Routing decision contains serialized Hermes injected/skipped details.
  - Advice timeout does not block submission.
- Test `tests/unit/runtime/test_direct_prompt.py`
  - Direct prompt includes bounded Hermes context.
- Test `tests/unit/runtime/test_configured_runtime.py`
  - Dispatch plan step text includes bounded Hermes context.
- Test `tests/unit/runtime/test_hybrid.py`
  - Hybrid child contexts keep routing decision.
- Test `web/src/pages/OperationalPages.test.tsx`
  - Compact Hermes+ row appears.
  - Detail drawer opens on row click.
  - Backdrop click closes drawer.
  - Esc is not required and no dedicated “查看详情” button appears.

---

### Task 1: Backend Hermes Advice Contract

**Files:**
- Modify: `src/agent_hub/runs/service.py`
- Test: `tests/unit/runs/test_conversation_mode.py`

- [ ] **Step 1: Write the failing routing payload test**

Add a test near `test_auto_submission_uses_hermes_before_local_direct_router_fallback`:

```python
async def test_auto_submission_records_hermes_injected_memory_payload() -> None:
    repository = ConversationModeRepository(None)
    advisor = RecordingHermesAdvisor(
        HermesRunAdvice(
            recommended_mode=TaskMode.DISPATCH,
            confidence=0.86,
            reasons=("matched previous execution pattern",),
            recommended_skills=("script-review",),
            requires_approval=False,
            injected_memories=(
                HermesMemoryInjection(
                    id="hermes_confirmed_review",
                    summary="reviewer 超时时先压缩上下文再分块审查。",
                    memory_type="error_handling",
                    target="reviewer",
                    score=0.91,
                    reason="命中 reviewer 超时处理经验",
                ),
            ),
            skipped_memories=(
                HermesSkippedMemory(
                    id="hermes_old_direct",
                    summary="旧 direct 模式观察。",
                    reason="当前任务相关性不足",
                    score=0.42,
                ),
            ),
        )
    )
    service = RunService(
        repository,  # type: ignore[arg-type]
        runtime_registry=RuntimeRegistry((UnavailableRuntime(TaskMode.DISPATCH),)),
        router=None,
        task_queue=RecordingQueue(),
        hermes_advisor=advisor,
    )

    submitted = await service.submit(
        tenant_id=uuid4(),
        actor_id=uuid4(),
        message="review the generated script",
        mode=TaskMode.AUTO,
        conversation_id="conv-1",
        idempotency_key="idem-hermes-memory-payload",
    )

    assert submitted.status is RunStatus.QUEUED
    routing = repository.created[0]["routing_decision"]
    hermes = routing["hermes"]
    assert hermes["injected_memories"] == [
        {
            "id": "hermes_confirmed_review",
            "summary": "reviewer 超时时先压缩上下文再分块审查。",
            "memory_type": "error_handling",
            "target": "reviewer",
            "score": 0.91,
            "reason": "命中 reviewer 超时处理经验",
        }
    ]
    assert hermes["skipped_memories"][0]["reason"] == "当前任务相关性不足"
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\unit\runs\test_conversation_mode.py::test_auto_submission_records_hermes_injected_memory_payload -q
```

Expected: FAIL because `HermesMemoryInjection` and `HermesSkippedMemory` are not defined and `HermesRunAdvice` lacks these fields.

- [ ] **Step 3: Add dataclasses and serialization**

In `src/agent_hub/runs/service.py`, add near `HermesRunAdvice`:

```python
@dataclass(frozen=True, slots=True)
class HermesMemoryInjection:
    id: str
    summary: str
    memory_type: str
    target: str
    score: float
    reason: str


@dataclass(frozen=True, slots=True)
class HermesSkippedMemory:
    id: str
    summary: str
    reason: str
    score: float
```

Extend `HermesRunAdvice`:

```python
@dataclass(frozen=True, slots=True)
class HermesRunAdvice:
    recommended_mode: TaskMode
    confidence: float
    reasons: tuple[str, ...]
    recommended_skills: tuple[str, ...] = ()
    requires_approval: bool = True
    injected_memories: tuple[HermesMemoryInjection, ...] = ()
    skipped_memories: tuple[HermesSkippedMemory, ...] = ()
```

Update `_hermes_advice_payload()`:

```python
def _hermes_advice_payload(advice: HermesRunAdvice) -> dict[str, object]:
    return {
        "recommended_mode": advice.recommended_mode.value,
        "confidence": advice.confidence,
        "reasons": list(advice.reasons),
        "recommended_skills": list(advice.recommended_skills),
        "requires_approval": advice.requires_approval,
        "injected_memories": [
            {
                "id": item.id,
                "summary": item.summary,
                "memory_type": item.memory_type,
                "target": item.target,
                "score": item.score,
                "reason": item.reason,
            }
            for item in advice.injected_memories[:3]
        ],
        "skipped_memories": [
            {
                "id": item.id,
                "summary": item.summary,
                "reason": item.reason,
                "score": item.score,
            }
            for item in advice.skipped_memories[:5]
        ],
    }
```

Add both dataclass names to the module `__all__` near the bottom.

- [ ] **Step 4: Run test to verify it passes**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\unit\runs\test_conversation_mode.py::test_auto_submission_records_hermes_injected_memory_payload -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add src/agent_hub/runs/service.py tests/unit/runs/test_conversation_mode.py
git -c user.name=zhangzhimiao -c user.email=41898282+zhangzhimiao1994@users.noreply.github.com commit -m "feat: serialize Hermes memory injection advice"
```

---

### Task 2: Hermes Structured Retrieval, Scoring, and Gating

**Files:**
- Modify: `src/agent_hub/hermes/advisor.py`
- Test: `tests/unit/hermes/test_advisor.py`

- [ ] **Step 1: Write failing advisor tests**

Add tests:

```python
@pytest.mark.asyncio
async def test_runtime_advice_injects_cross_mode_project_rule_when_relevant() -> None:
    lesson = {
        "id": "hermes_ui_drawer_rule",
        "category": "conversation",
        "outcome": "success",
        "lesson": "调度卡片应默认显示摘要，详情放抽屉，点击遮罩关闭。",
        "user_summary": "调度卡片默认只显示摘要，详情放抽屉。",
        "tags": ["调度卡片", "抽屉", "ui"],
        "weight": 9,
        "source_mode": "discuss",
        "applies_to_modes": ["dispatch", "direct", "hybrid"],
        "memory_type": "ui_rule",
        "target": "frontend",
        "confidence": 0.88,
        "noise_risk": 0.1,
        "created_at": datetime.now(UTC).isoformat(),
        "confirmed_at": datetime.now(UTC).isoformat(),
    }
    advisor = PersistentHermesRunAdvisor(FakeSessionFactory([[], [FakeRow({"hermes_policy": "suggest"})], [FakeRow(lesson)]]))  # type: ignore[arg-type]

    advice = await advisor.advise(
        tenant_id=uuid4(),
        actor_id=uuid4(),
        message="修改调度卡片 UI，详情用抽屉展示",
        mode=TaskMode.DISPATCH,
        agent_ids=("frontend",),
        workflow_id=None,
    )

    assert advice is not None
    assert advice.injected_memories[0].id == "hermes_ui_drawer_rule"
    assert advice.injected_memories[0].target == "frontend"


@pytest.mark.asyncio
async def test_runtime_advice_skips_same_mode_low_quality_noise() -> None:
    lesson = {
        "id": "hermes_noise",
        "category": "conversation",
        "outcome": "neutral",
        "lesson": "这个任务成功了。",
        "user_summary": "这个任务成功了。",
        "tags": ["direct"],
        "weight": 10,
        "source_mode": "direct",
        "memory_type": "temporary_state",
        "target": "main_agent",
        "confidence": 0.3,
        "noise_risk": 0.9,
        "created_at": datetime.now(UTC).isoformat(),
        "confirmed_at": datetime.now(UTC).isoformat(),
    }
    advisor = PersistentHermesRunAdvisor(FakeSessionFactory([[], [FakeRow({"hermes_policy": "suggest"})], [FakeRow(lesson)]]))  # type: ignore[arg-type]

    advice = await advisor.advise(
        tenant_id=uuid4(),
        actor_id=uuid4(),
        message="direct 模式继续处理这个任务",
        mode=TaskMode.DIRECT,
        agent_ids=(),
        workflow_id=None,
    )

    assert advice is None


@pytest.mark.asyncio
async def test_runtime_advice_records_conflicting_memory_as_skipped() -> None:
    lesson = {
        "id": "hermes_hybrid_preference",
        "category": "conversation",
        "outcome": "success",
        "lesson": "大任务优先使用混合模式。",
        "user_summary": "大任务优先使用混合模式。",
        "tags": ["大任务", "hybrid"],
        "weight": 8,
        "source_mode": "hybrid",
        "memory_type": "scheduling_rule",
        "target": "scheduler",
        "confidence": 0.8,
        "noise_risk": 0.1,
        "created_at": datetime.now(UTC).isoformat(),
        "confirmed_at": datetime.now(UTC).isoformat(),
    }
    advisor = PersistentHermesRunAdvisor(FakeSessionFactory([[], [FakeRow({"hermes_policy": "suggest"})], [FakeRow(lesson)]]))  # type: ignore[arg-type]

    advice = await advisor.advise(
        tenant_id=uuid4(),
        actor_id=uuid4(),
        message="先跑直连模式，不要混合",
        mode=TaskMode.DIRECT,
        agent_ids=(),
        workflow_id=None,
    )

    assert advice is None or not advice.injected_memories
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\unit\hermes\test_advisor.py -q
```

Expected: FAIL because advisor currently returns only one best lesson and no memory injection payload.

- [ ] **Step 3: Implement structured scoring helpers**

In `src/agent_hub/hermes/advisor.py`:

- Import `HermesMemoryInjection` and `HermesSkippedMemory`.
- Stop deleting `mode` and `agent_ids`.
- Add helpers with deterministic rules:

```python
_INJECTABLE_TYPES = {
    "user_preference",
    "project_fact",
    "ui_rule",
    "error_handling",
    "scheduling_rule",
}
_LOW_QUALITY_PHRASES = ("这个任务成功了", "任务成功了", "出错了", "失败了")


def _lesson_user_summary(lesson: dict[str, object]) -> str:
    value = lesson.get("user_summary") or lesson.get("summary") or lesson.get("lesson")
    return str(value).strip()[:220] if isinstance(value, str) and value.strip() else "Hermes+ 记忆"


def _lesson_memory_type(lesson: dict[str, object]) -> str:
    value = lesson.get("memory_type")
    return value if isinstance(value, str) and value else "conversation_advice"


def _lesson_target(lesson: dict[str, object]) -> str:
    value = lesson.get("target")
    return value if isinstance(value, str) and value else "main_agent"
```

Implement:

```python
def _lesson_noise_reason(lesson: dict[str, object]) -> str | None:
    confidence = _float_or_default(lesson.get("confidence"), 0.7)
    noise = _float_or_default(lesson.get("noise_risk"), 0.0)
    text = f"{lesson.get('lesson', '')} {lesson.get('user_summary', '')}"
    if confidence < 0.45:
        return "置信度不足"
    if noise >= 0.7:
        return "噪音风险过高"
    if any(phrase in text for phrase in _LOW_QUALITY_PHRASES):
        return "记忆过于泛化"
    if _lesson_memory_type(lesson) in {"temporary_state", "single_run_state"}:
        return "临时运行状态不参与注入"
    return None
```

Implement score:

```python
def _lesson_relevance_score(
    lowered_message: str,
    lesson: dict[str, object],
    *,
    mode: TaskMode,
    agent_ids: tuple[str, ...],
    workflow_id: str | None,
) -> float:
    score = 0.0
    if _lesson_matches(lowered_message, lesson, workflow_id):
        score += 0.35
    tags = lesson.get("tags")
    if isinstance(tags, list):
        if any(isinstance(tag, str) and tag.lower() in lowered_message for tag in tags):
            score += 0.2
        if any(isinstance(tag, str) and tag in agent_ids for tag in tags):
            score += 0.1
    applies = lesson.get("applies_to_modes")
    if isinstance(applies, list) and mode.value in [item for item in applies if isinstance(item, str)]:
        score += 0.12
    elif _lesson_memory_type(lesson) in _INJECTABLE_TYPES:
        score += 0.08
    score += min(0.12, _lesson_weight(lesson) / 100)
    score += min(0.08, _float_or_default(lesson.get("confidence"), 0.7) / 10)
    score -= min(0.2, _float_or_default(lesson.get("noise_risk"), 0.0) / 2)
    return max(0.0, min(1.0, score))
```

Implement conflict:

```python
def _lesson_conflicts_with_request(lowered_message: str, lesson: dict[str, object]) -> bool:
    text = f"{lesson.get('lesson', '')} {' '.join(lesson.get('tags', [])) if isinstance(lesson.get('tags'), list) else ''}".lower()
    direct_requested = any(token in lowered_message for token in ("直连", "direct", "不要混合", "不混合"))
    hybrid_suggested = any(token in text for token in ("hybrid", "混合"))
    return direct_requested and hybrid_suggested
```

- [ ] **Step 4: Return injected and skipped memory decisions**

In `advise()`:

- Build candidate lessons from confirmed lessons only.
- For each lesson:
  - If `noise_reason`, append skipped memory.
  - If conflict, append skipped memory.
  - If score >= 0.65, append injected memory.
  - If 0.45 <= score < 0.65, append skipped memory.
- Sort injected by score descending and weight descending.
- Use top injected lesson to choose `recommended_mode`, but only if present.
- Return `None` if no injected memories and no previous mode recommendation is usable.

The returned `HermesRunAdvice` must include:

```python
injected_memories=tuple(injected[:3]),
skipped_memories=tuple(skipped[:5]),
```

- [ ] **Step 5: Run advisor tests**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\unit\hermes\test_advisor.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit**

```powershell
git add src/agent_hub/hermes/advisor.py tests/unit/hermes/test_advisor.py
git -c user.name=zhangzhimiao -c user.email=41898282+zhangzhimiao1994@users.noreply.github.com commit -m "feat: gate Hermes memory injection"
```

---

### Task 3: Bounded Runtime Prompt Injection

**Files:**
- Create: `src/agent_hub/runtime/hermes_context.py`
- Modify: `src/agent_hub/runtime/direct.py`
- Modify: `src/agent_hub/runtime/defaults.py`
- Modify: `src/agent_hub/runtime/autogen/adapter.py`
- Modify: `src/agent_hub/runtime/crew/adapter.py`
- Test: `tests/unit/runtime/test_direct_prompt.py`
- Test: `tests/unit/runtime/test_configured_runtime.py`

- [ ] **Step 1: Write failing direct prompt test**

Add to `tests/unit/runtime/test_direct_prompt.py`:

```python
def test_direct_prompt_includes_bounded_hermes_memory_context() -> None:
    context = TaskContext(
        run_id=uuid4(),
        tenant_id=uuid4(),
        mode=TaskMode.DIRECT,
        request="审查脚本",
        artifacts=(),
        timeout_seconds=60,
        token_budget=10_000,
        routing_decision={
            "hermes": {
                "injected_memories": [
                    {
                        "summary": "reviewer 超时时先压缩上下文再分块审查。",
                        "memory_type": "error_handling",
                        "target": "reviewer",
                        "reason": "命中 reviewer 超时经验",
                    }
                ]
            }
        },
    )
    runtime = DirectRuntime(FakeGateway(), logical_model="main")

    prompt = runtime._build_prompt(context)  # noqa: SLF001

    serialized = "\n".join(message.content for message in prompt.messages)
    assert "HERMES_MEMORY_CONTEXT" in serialized
    assert "reviewer 超时时先压缩上下文再分块审查" in serialized
```

- [ ] **Step 2: Write failing dispatch plan test**

Add to `tests/unit/runtime/test_configured_runtime.py` near dispatch plan tests:

```python
def test_dispatch_plan_includes_hermes_memory_context_in_steps() -> None:
    role = RoleAssignment(
        id="reviewer",
        role="Reviewer",
        purpose=RolePurpose.VERIFY,
        mission="Review output quality.",
        must_answer=("What risks remain?",),
        allowed_tools=(),
        forbidden_actions=(),
        skills=(),
        output_schema={"summary": "string"},
        model="main",
    )
    context = TaskContext(
        run_id=uuid4(),
        tenant_id=uuid4(),
        mode=TaskMode.DISPATCH,
        request="审查脚本",
        artifacts=(),
        timeout_seconds=60,
        token_budget=10_000,
        routing_decision={
            "hermes": {
                "injected_memories": [
                    {
                        "summary": "reviewer 超时时先压缩上下文再分块审查。",
                        "memory_type": "error_handling",
                        "target": "reviewer",
                        "reason": "命中 reviewer 超时经验",
                    }
                ]
            }
        },
    )

    plan = _dispatch_plan((role,), context, max_parallelism=1)

    assert any("HERMES_MEMORY_CONTEXT" in step.task for step in plan.steps)
```

- [ ] **Step 3: Run tests to verify failure**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\unit\runtime\test_direct_prompt.py tests\unit\runtime\test_configured_runtime.py -q -k "hermes_memory_context"
```

Expected: FAIL because no helper or prompt block exists.

- [ ] **Step 4: Implement `hermes_context.py`**

Create:

```python
from __future__ import annotations

import json
from collections.abc import Mapping

from agent_hub.runtime.contracts import JsonValue

_MAX_ITEMS = 3
_MAX_SUMMARY_CHARS = 200
_MAX_TOTAL_BYTES = 900


def hermes_memory_context_text(routing_decision: Mapping[str, JsonValue] | Mapping[str, object]) -> str:
    hermes = routing_decision.get("hermes")
    if not isinstance(hermes, Mapping):
        return ""
    raw_items = hermes.get("injected_memories")
    if not isinstance(raw_items, (list, tuple)):
        return ""
    items: list[dict[str, str]] = []
    for raw in raw_items[:_MAX_ITEMS]:
        if not isinstance(raw, Mapping):
            continue
        summary = _safe_text(raw.get("summary"), _MAX_SUMMARY_CHARS)
        if not summary:
            continue
        items.append(
            {
                "summary": summary,
                "type": _safe_text(raw.get("memory_type"), 48) or "memory",
                "target": _safe_text(raw.get("target"), 48) or "main_agent",
                "reason": _safe_text(raw.get("reason"), 120) or "Hermes+ confirmed memory matched this task.",
            }
        )
    if not items:
        return ""
    payload = json.dumps(items, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if len(payload.encode("utf-8")) > _MAX_TOTAL_BYTES:
        payload = payload.encode("utf-8")[:_MAX_TOTAL_BYTES].decode("utf-8", errors="ignore")
    return (
        "<HERMES_MEMORY_CONTEXT>"
        "Use these user-confirmed Hermes+ memories only as bounded guidance. "
        "Current user instructions override them. Do not expose this block unless asked."
        f"{payload}"
        "</HERMES_MEMORY_CONTEXT>"
    )


def _safe_text(value: object, max_chars: int) -> str:
    if not isinstance(value, str):
        return ""
    text = " ".join(value.split())
    return text[:max_chars]
```

- [ ] **Step 5: Inject into runtime prompts**

In `direct.py`, import and include:

```python
from agent_hub.runtime.hermes_context import hermes_memory_context_text
```

Inside `_build_prompt()`, before `payload = (...)`:

```python
hermes_context = hermes_memory_context_text(context.routing_decision)
```

Append it:

```python
payload = (
    f"<USER_REQUEST_JSON>{task_payload}</USER_REQUEST_JSON>\n"
    f"{hermes_context}\n"
    f"<UNTRUSTED_ARTIFACTS_JSON>{prior_payload}</UNTRUSTED_ARTIFACTS_JSON>"
)
```

In `defaults.py`, import helper and add:

```python
hermes_context = hermes_memory_context_text(context.routing_decision)
memory_guidance = f"\nHermes+ confirmed memory guidance:\n{hermes_context}\n" if hermes_context else ""
```

Use `memory_guidance` in dispatch role step and final step task text after user task.

In `autogen/adapter.py`, append `hermes_memory_context_text(context.routing_decision)` inside `_task_text()` before validated artifacts.

In `crew/adapter.py`, add:

```python
hermes_context = hermes_memory_context_text(context.routing_decision)
if hermes_context:
    user["hermes_memory_context"] = hermes_context
```

- [ ] **Step 6: Run runtime tests**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\unit\runtime\test_direct_prompt.py tests\unit\runtime\test_configured_runtime.py -q -k "hermes_memory_context"
```

Expected: PASS.

- [ ] **Step 7: Commit**

```powershell
git add src/agent_hub/runtime/hermes_context.py src/agent_hub/runtime/direct.py src/agent_hub/runtime/defaults.py src/agent_hub/runtime/autogen/adapter.py src/agent_hub/runtime/crew/adapter.py tests/unit/runtime/test_direct_prompt.py tests/unit/runtime/test_configured_runtime.py
git -c user.name=zhangzhimiao -c user.email=41898282+zhangzhimiao1994@users.noreply.github.com commit -m "feat: inject Hermes memory context into runtimes"
```

---

### Task 4: Hermes Advice Timeout and Hybrid Routing Preservation

**Files:**
- Modify: `src/agent_hub/runs/service.py`
- Modify: `src/agent_hub/runtime/hybrid.py`
- Test: `tests/unit/runs/test_conversation_mode.py`
- Test: `tests/unit/runtime/test_hybrid.py`

- [ ] **Step 1: Write failing Hermes timeout test**

Add to `tests/unit/runs/test_conversation_mode.py`:

```python
class SlowHermesAdvisor(RecordingHermesAdvisor):
    async def advise(self, **kwargs: object) -> HermesRunAdvice | None:
        await asyncio.sleep(2)
        return None


async def test_hermes_advice_timeout_does_not_block_auto_submission() -> None:
    repository = ConversationModeRepository(None)
    service = RunService(
        repository,  # type: ignore[arg-type]
        runtime_registry=RuntimeRegistry((UnavailableRuntime(TaskMode.DIRECT),)),
        router=None,
        task_queue=RecordingQueue(),
        hermes_advisor=SlowHermesAdvisor(None),
    )

    submitted = await service.submit(
        tenant_id=uuid4(),
        actor_id=uuid4(),
        message="hello",
        mode=TaskMode.AUTO,
        conversation_id="conv-1",
        idempotency_key="idem-hermes-timeout",
    )

    assert submitted.status is RunStatus.QUEUED
    assert submitted.mode is TaskMode.DIRECT
```

- [ ] **Step 2: Write failing hybrid preservation test**

Add to `tests/unit/runtime/test_hybrid.py`:

```python
async def test_hybrid_child_context_preserves_routing_decision() -> None:
    dispatch = RecordingRuntime(TaskMode.DISPATCH)
    discussion = RecordingRuntime(TaskMode.DISCUSS)
    runtime = HybridRuntime(dispatch, discussion)
    context = TaskContext(
        run_id=uuid4(),
        tenant_id=uuid4(),
        mode=TaskMode.HYBRID,
        request="审查脚本",
        artifacts=(),
        timeout_seconds=60,
        token_budget=10_000,
        routing_decision={"hermes": {"injected_memories": [{"summary": "保留记忆"}]}},
    )

    _ = [event async for event in runtime.run(context)]

    assert dispatch.contexts[0].routing_decision["hermes"]["injected_memories"][0]["summary"] == "保留记忆"
    assert discussion.contexts[0].routing_decision["hermes"]["injected_memories"][0]["summary"] == "保留记忆"
```

Use the existing fake/recording runtime helpers already present in `test_hybrid.py`; adapt only the class name if the file uses a different helper.

- [ ] **Step 3: Implement timeout and preservation**

In `RunService._safe_hermes_advice()`:

```python
try:
    async with asyncio.timeout(0.8):
        return await self._hermes_advisor.advise(...)
except TimeoutError:
    _LOGGER.warning("hermes_advice_timeout tenant_id=%s", tenant_id)
    return None
```

In `HybridRuntime._run_child()`, pass:

```python
routing_decision=parent.routing_decision,
```

- [ ] **Step 4: Run tests**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\unit\runs\test_conversation_mode.py::test_hermes_advice_timeout_does_not_block_auto_submission tests\unit\runtime\test_hybrid.py::test_hybrid_child_context_preserves_routing_decision -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add src/agent_hub/runs/service.py src/agent_hub/runtime/hybrid.py tests/unit/runs/test_conversation_mode.py tests/unit/runtime/test_hybrid.py
git -c user.name=zhangzhimiao -c user.email=41898282+zhangzhimiao1994@users.noreply.github.com commit -m "fix: bound Hermes advice and preserve hybrid routing"
```

---

### Task 5: Scheduling Card Hermes+ Summary and Confirmation Drawer

**Files:**
- Modify: `web/src/api/client.ts`
- Modify: `web/src/pages/RunsPage.tsx`
- Modify: `web/src/styles.css`
- Test: `web/src/pages/OperationalPages.test.tsx`

- [ ] **Step 1: Write failing frontend tests**

Add tests near process drawer tests in `OperationalPages.test.tsx`:

```tsx
it("shows compact Hermes memory summary in the process drawer and opens details from the row", async () => {
  const user = userEvent.setup();
  visibleRunDetail = {
    ...workforceRunDetail,
    explicit_details: {
      ...workforceRunDetail.explicit_details,
      hermes_injected_memories: "2",
      hermes_skipped_memories: "1",
    },
    routing_decision: {
      ...workforceRunDetail.routing_decision,
      hermes: {
        injected_memories: [
          {
            id: "hermes_review_timeout",
            summary: "reviewer 超时时先压缩上下文再分块审查。",
            memory_type: "error_handling",
            target: "reviewer",
            score: 0.91,
            reason: "命中 reviewer 超时处理经验",
          },
        ],
        skipped_memories: [
          {
            id: "hermes_hybrid",
            summary: "大任务优先混合模式。",
            reason: "当前用户要求直连，未注入。",
            score: 0.5,
          },
        ],
      },
    },
  };
  visibleConversationRuns = [visibleRunDetail];

  render(<TestApp initialPath="/runs" />);

  await user.click(await screen.findByRole("button", { name: /查看运行过程/ }));
  const drawer = await screen.findByRole("dialog", { name: "运行过程详情" });
  expect(within(drawer).getByRole("button", { name: /Hermes\\+ 记忆：已注入 1 条，未注入 1 条/ })).not.toBeNull();
  expect(within(drawer).queryByRole("button", { name: /查看详情/ })).toBeNull();

  await user.click(within(drawer).getByRole("button", { name: /Hermes\\+ 记忆/ }));
  const detail = await screen.findByRole("dialog", { name: "Hermes+ 记忆详情" });
  expect(within(detail).getByText("reviewer 超时时先压缩上下文再分块审查。")).not.toBeNull();
  expect(within(detail).getByText("当前用户要求直连，未注入。")).not.toBeNull();
});

it("closes Hermes memory drawer by clicking the backdrop", async () => {
  const user = userEvent.setup();
  visibleRunDetail = {
    ...workforceRunDetail,
    routing_decision: {
      ...workforceRunDetail.routing_decision,
      hermes: {
        injected_memories: [
          {
            id: "hermes_review_timeout",
            summary: "reviewer 超时时先压缩上下文再分块审查。",
            memory_type: "error_handling",
            target: "reviewer",
            score: 0.91,
            reason: "命中 reviewer 超时处理经验",
          },
        ],
        skipped_memories: [],
      },
    },
  };
  visibleConversationRuns = [visibleRunDetail];
  render(<TestApp initialPath="/runs" />);

  await user.click(await screen.findByRole("button", { name: /查看运行过程/ }));
  const drawer = await screen.findByRole("dialog", { name: "运行过程详情" });
  await user.click(within(drawer).getByRole("button", { name: /Hermes\\+ 记忆/ }));
  const detail = await screen.findByRole("dialog", { name: "Hermes+ 记忆详情" });
  await user.click(detail.parentElement as HTMLElement);
  await waitFor(() => expect(screen.queryByRole("dialog", { name: "Hermes+ 记忆详情" })).toBeNull());
});
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```powershell
npm --prefix web test -- OperationalPages.test.tsx -t "Hermes memory"
```

Expected: FAIL because the process drawer has no Hermes memory row or detail drawer.

- [ ] **Step 3: Implement Hermes memory extraction helpers**

In `RunsPage.tsx`, add types and helpers:

```tsx
type HermesMemoryItem = {
  id?: string;
  summary: string;
  memory_type?: string;
  target?: string;
  score?: number;
  reason?: string;
};

function hermesMemoryItems(detail: RunDetail | undefined, key: "injected_memories" | "skipped_memories"): HermesMemoryItem[] {
  const hermes = detail?.routing_decision?.hermes;
  if (!hermes || typeof hermes !== "object" || Array.isArray(hermes)) return [];
  const raw = hermes[key];
  if (!Array.isArray(raw)) return [];
  return raw
    .filter((item): item is Record<string, unknown> => Boolean(item) && typeof item === "object" && !Array.isArray(item))
    .map((item) => ({
      id: typeof item.id === "string" ? item.id : undefined,
      summary: typeof item.summary === "string" ? item.summary : "Hermes+ 记忆",
      memory_type: typeof item.memory_type === "string" ? item.memory_type : undefined,
      target: typeof item.target === "string" ? item.target : undefined,
      score: typeof item.score === "number" ? item.score : undefined,
      reason: typeof item.reason === "string" ? item.reason : undefined,
    }));
}
```

- [ ] **Step 4: Add compact row and detail drawer**

Inside `ProcessDrawer`, compute:

```tsx
const injectedHermesMemories = hermesMemoryItems(target.detail, "injected_memories");
const skippedHermesMemories = hermesMemoryItems(target.detail, "skipped_memories");
const [hermesDetailOpen, setHermesDetailOpen] = useState(false);
```

If `ProcessDrawerTarget` does not currently contain `detail`, add `detail?: RunDetail` and pass it from the existing process drawer open site.

Render a button row above `RunSummaryBuckets`:

```tsx
{injectedHermesMemories.length || skippedHermesMemories.length ? (
  <button type="button" className="hermes-memory-summary-row" onClick={() => setHermesDetailOpen(true)}>
    <span>Hermes+ 记忆：已注入 {injectedHermesMemories.length} 条，未注入 {skippedHermesMemories.length} 条</span>
  </button>
) : null}
```

Add drawer:

```tsx
function HermesMemoryDetailDrawer({ injected, skipped, onClose }: { injected: HermesMemoryItem[]; skipped: HermesMemoryItem[]; onClose: () => void }) {
  return (
    <div className="activity-detail-backdrop" role="presentation" onClick={(event) => event.target === event.currentTarget && onClose()}>
      <section className="activity-detail-drawer" role="dialog" aria-label="Hermes+ 记忆详情" aria-modal="true" onClick={(event) => event.stopPropagation()}>
        <div className="process-drawer-header">
          <div>
            <span className="eyebrow">Hermes+</span>
            <h3>记忆取回与注入</h3>
            <p className="agent-workforce-scope">只显示本次运行相关的已注入或被跳过记忆。</p>
          </div>
          <button type="button" className="secondary-action" onClick={onClose}>关闭</button>
        </div>
        <HermesMemoryGroup title="已注入" items={injected} empty="本次没有注入 Hermes+ 记忆。" />
        <HermesMemoryGroup title="未注入" items={skipped} empty="本次没有被门控跳过的 Hermes+ 记忆。" />
      </section>
    </div>
  );
}
```

Do not add `onKeyDown` or Escape handling.

- [ ] **Step 5: Add styles**

In `web/src/styles.css`, add compact styles:

```css
.hermes-memory-summary-row {
  width: 100%;
  border: 1px solid var(--border);
  border-radius: 16px;
  background: rgba(34, 211, 238, 0.08);
  color: inherit;
  display: flex;
  justify-content: space-between;
  padding: 0.75rem 0.9rem;
  text-align: left;
}

.hermes-memory-card {
  border: 1px solid var(--border);
  border-radius: 14px;
  padding: 0.75rem;
  background: var(--surface);
}
```

- [ ] **Step 6: Run frontend tests**

Run:

```powershell
npm --prefix web test -- OperationalPages.test.tsx -t "Hermes memory"
```

Expected: PASS.

- [ ] **Step 7: Commit**

```powershell
git add web/src/api/client.ts web/src/pages/RunsPage.tsx web/src/styles.css web/src/pages/OperationalPages.test.tsx
git -c user.name=zhangzhimiao -c user.email=41898282+zhangzhimiao1994@users.noreply.github.com commit -m "feat: show Hermes memory injection in run drawer"
```

---

### Task 6: Full Verification, Push, Deploy, and Handoff

**Files:**
- Modify: `HANDOFF.md` locally only; do not stage.

- [ ] **Step 1: Run backend verification**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\unit\hermes\test_advisor.py tests\unit\runs\test_conversation_mode.py tests\unit\runtime\test_direct_prompt.py tests\unit\runtime\test_configured_runtime.py tests\unit\runtime\test_hybrid.py -q
.\.venv\Scripts\python.exe -m ruff check src tests
```

Expected: all selected tests pass and ruff passes.

- [ ] **Step 2: Run frontend verification**

Run:

```powershell
npm --prefix web run lint
npm --prefix web test -- OperationalPages.test.tsx
npm --prefix web run build
```

Expected: lint passes, `OperationalPages.test.tsx` passes, build passes. Existing Vite large chunk warning is acceptable if unchanged.

- [ ] **Step 3: Check repository cleanliness**

Run:

```powershell
git diff --check
git status --short
```

Expected: no whitespace errors; only intentional tracked changes are committed; `HANDOFF.md` remains ignored/untracked from git perspective.

- [ ] **Step 4: Push**

Run:

```powershell
git push origin main
```

Expected: push succeeds to `ssh://git@ssh.github.com:443/zhangzhimiao1994/Cube-agent.git`.

- [ ] **Step 5: Check GitHub Actions**

Run:

```powershell
gh run list --repo zhangzhimiao1994/Cube-agent --branch main --limit 5
gh run watch --repo zhangzhimiao1994/Cube-agent --exit-status
```

Expected: latest quality/check run succeeds. If it fails, fetch logs, fix, verify locally, commit, push, and repeat.

- [ ] **Step 6: Deploy to `prod-web-01`**

Deploy the verified commit to:

```text
root@103.236.98.133
/opt/agent-hub/current
```

Use the existing release deployment pattern from the handoff: build `web/dist`, create source and web archives, upload to `/tmp`, create a timestamped release under `/opt/agent-hub/releases`, reuse existing production `.venv` and `.litellm-venv`, run Alembic upgrade, switch `/opt/agent-hub/current`, restart `agent-hub-api` and `agent-hub-worker`, reload Caddy, and remove old releases after health checks pass.

- [ ] **Step 7: Production smoke**

Run:

```bash
curl -fsS http://127.0.0.1:8000/health/live
curl -fsS http://127.0.0.1:8000/health/ready
```

Expected: both return `{"status":"ok"}`.

- [ ] **Step 8: Handoff**

Update local `HANDOFF.md` with:

- commit hash
- files changed
- tests run
- CI status and URL
- production release path
- health results
- remaining risks or follow-up items

Do not stage or push `HANDOFF.md`.
