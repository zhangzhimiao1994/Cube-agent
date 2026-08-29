# Hermes+ Memory System Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build Hermes+ in the original repository by extending the existing memory system with heat, recall reinforcement, decay, lock/unlock, deterministic summaries, and project/conversation scope.

**Architecture:** Keep the current `MemoryService` and `InMemoryMemoryRepository` as the first implementation boundary, extend `MemoryRecord` with safe metadata, then surface the new behavior through ContextBuilder, admin APIs, and the Memory UI. Do not copy kiwi-mem AGPL code and do not start the future open harness / DeepSeek harness refactor in this repository.

**Tech Stack:** Python 3.12, Pydantic v2, pytest/pytest-asyncio, FastAPI/TestClient admin API tests, React/TypeScript/Zod, Vitest.

---

## File structure

- Modify `src/agent_hub/memory/types.py`: add Hermes+ fields and categories to `MemoryRecord`.
- Modify `src/agent_hub/memory/service.py`: add ranking, recall reinforcement, decay, lock/unlock, scope-aware search, and deterministic summaries.
- Modify `src/agent_hub/context/builder.py`: render Hermes+ memory labels and rely on ranked memories.
- Modify `src/agent_hub/api/routers/admin.py`: extend admin memory response/request schema and add lock/unlock/consolidate endpoints on the existing admin-resource persistence path.
- Modify `web/src/api/client.ts`: extend `MemoryRecordSchema` and add lock/unlock/consolidate client methods.
- Modify `web/src/pages/MemoryPage.tsx`: show heat, recall count, lock state, scope, and summary period.
- Modify tests:
  - `tests/integration/memory/test_memory.py`
  - `tests/unit/context/test_builder.py`
  - `tests/api/test_admin_resources.py`
  - `web/src/pages/OperationalPages.test.tsx`

## Task 1: Extend Hermes+ memory record types

**Files:**
- Modify: `src/agent_hub/memory/types.py`
- Test: `tests/integration/memory/test_memory.py`

- [x] **Step 1: Write failing record validation tests**

Add tests to `tests/integration/memory/test_memory.py`:

```python
async def test_hermes_plus_record_defaults_are_safe() -> None:
    service, _ = memory_service_with_clock()

    added = await service.add_candidate(
        tenant_id=TENANT_A,
        user_id=USER_1,
        text="prefers implementation plans before large changes",
        category=MemoryCategory.PREFERENCE,
    )

    assert added.record is not None
    assert added.record.heat == 0.5
    assert added.record.recall_count == 0
    assert added.record.last_recalled_at is None
    assert added.record.locked is False
    assert added.record.project_id is None
    assert added.record.conversation_id is None
    assert added.record.summary_period is MemorySummaryPeriod.NONE
    assert added.record.metadata == {}
```

Update imports:

```python
from agent_hub.memory.types import (
    MemoryAddStatus,
    MemoryCategory,
    MemoryLayer,
    MemorySummaryPeriod,
)
```

- [x] **Step 2: Run the failing test**

Run:

```powershell
$env:PYTHONPATH = (Resolve-Path src).Path
& 'E:\code_x\agent-hub\.venv\Scripts\python.exe' -m pytest tests\integration\memory\test_memory.py::test_hermes_plus_record_defaults_are_safe -q
```

Expected: FAIL because `MemorySummaryPeriod` and the new fields do not exist.

- [x] **Step 3: Implement type fields**

In `src/agent_hub/memory/types.py`, add:

```python
class MemorySummaryPeriod(StrEnum):
    NONE = "none"
    DAY = "day"
    WEEK = "week"
    MONTH = "month"
```

Extend `MemoryCategory`:

```python
    SUMMARY = "summary"
    DECISION = "decision"
    LESSON = "lesson"
```

Extend `MemoryRecord`:

```python
    heat: float = Field(default=0.5, ge=0, le=1)
    last_recalled_at: datetime | None = None
    recall_count: int = Field(default=0, ge=0)
    locked: bool = False
    project_id: str | None = Field(default=None, min_length=1, max_length=128)
    conversation_id: str | None = Field(default=None, min_length=1, max_length=128)
    summary_period: MemorySummaryPeriod = MemorySummaryPeriod.NONE
    metadata: dict[str, str] = Field(default_factory=dict, max_length=16)
```

Add validators:

```python
    @field_validator("project_id", "conversation_id")
    @classmethod
    def validate_scope_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if value != value.strip() or any(ch.isspace() for ch in value):
            raise ValueError("memory scope identifiers must be unpadded non-whitespace strings")
        return value

    @field_validator("metadata")
    @classmethod
    def validate_metadata(cls, value: dict[str, str]) -> dict[str, str]:
        for key, item in value.items():
            if (
                key != key.strip()
                or item != item.strip()
                or not key
                or len(key) > 64
                or len(item) > 256
                or any(ord(ch) < 32 for ch in key + item)
            ):
                raise ValueError("memory metadata must contain bounded printable strings")
        return value
```

- [x] **Step 4: Run the test to verify green**

Run the same command from Step 2.

Expected: PASS.

- [x] **Step 5: Run mypy target**

Run:

```powershell
$env:PYTHONPATH = (Resolve-Path src).Path
& 'E:\code_x\agent-hub\.venv\Scripts\python.exe' -m mypy --strict src\agent_hub\memory tests\integration\memory\test_memory.py
```

Expected: success.

## Task 2: Add scope-aware search and recall reinforcement

**Files:**
- Modify: `src/agent_hub/memory/service.py`
- Test: `tests/integration/memory/test_memory.py`

- [x] **Step 1: Write failing tests**

Add:

```python
async def test_scope_aware_search_prioritizes_conversation_project_and_core() -> None:
    service, _ = memory_service_with_clock()
    await service.add_candidate(
        tenant_id=TENANT_A,
        user_id=USER_1,
        text="prefers pytest for backend verification",
        project_id="cube-agent",
        conversation_id="thread-a",
    )
    await service.add_candidate(
        tenant_id=TENANT_A,
        user_id=USER_1,
        text="prefers pytest for older backend work",
        project_id="cube-agent",
        conversation_id="thread-b",
    )
    await service.add_candidate(
        tenant_id=TENANT_A,
        user_id=USER_1,
        text="prefers pytest for all projects",
        layer=MemoryLayer.CORE,
        user_confirmed=True,
    )

    results = await service.search(
        tenant_id=TENANT_A,
        user_id=USER_1,
        query="pytest backend",
        project_id="cube-agent",
        conversation_id="thread-a",
    )

    assert [record.text for record in results] == [
        "prefers pytest for backend verification",
        "prefers pytest for older backend work",
        "prefers pytest for all projects",
    ]


async def test_search_can_reinforce_returned_memories() -> None:
    service, now = memory_service_with_clock()
    added = await service.add_candidate(
        tenant_id=TENANT_A,
        user_id=USER_1,
        text="likes concise deployment reports",
    )
    assert added.record is not None
    now[0] = now[0] + timedelta(minutes=5)

    recalled = await service.search(
        tenant_id=TENANT_A,
        user_id=USER_1,
        query="deployment reports",
        reinforce=True,
    )

    assert recalled[0].id == added.record.id
    assert recalled[0].recall_count == 1
    assert recalled[0].last_recalled_at == now[0]
    assert recalled[0].heat > added.record.heat
    assert (await service.audit_events(tenant_id=TENANT_A))[-1].kind == "memory.recalled"
```

- [x] **Step 2: Run failing tests**

Run:

```powershell
$env:PYTHONPATH = (Resolve-Path src).Path
& 'E:\code_x\agent-hub\.venv\Scripts\python.exe' -m pytest tests\integration\memory\test_memory.py::test_scope_aware_search_prioritizes_conversation_project_and_core tests\integration\memory\test_memory.py::test_search_can_reinforce_returned_memories -q
```

Expected: FAIL because search does not accept `project_id`, `conversation_id`, or `reinforce`.

- [x] **Step 3: Implement add/search parameters and ranking**

In `MemoryService.add_candidate`, add keyword parameters:

```python
        project_id: str | None = None,
        conversation_id: str | None = None,
        metadata: dict[str, str] | None = None,
```

Pass them into `MemoryRecord`.

In `MemoryService.search`, add:

```python
        project_id: str | None = None,
        conversation_id: str | None = None,
        reinforce: bool = False,
```

Score records with:

```python
            scope_score = _scope_score(record, project_id, conversation_id)
            weighted = (
                score * 1000
                + scope_score
                + int(record.heat * 100)
                + int(record.confidence * 50)
                + (100 if record.layer is MemoryLayer.CORE else 0)
                + (75 if record.locked else 0)
            )
            scored.append((weighted, record))
```

Add helper:

```python
def _scope_score(
    record: MemoryRecord, project_id: str | None, conversation_id: str | None
) -> int:
    score = 0
    if project_id is not None and record.project_id == project_id:
        score += 400
    if conversation_id is not None and record.conversation_id == conversation_id:
        score += 600
    if record.project_id is None and record.conversation_id is None and record.layer is MemoryLayer.CORE:
        score += 150
    return score
```

If `reinforce` is true, update each returned record:

```python
        results = tuple(record for _, record in scored[:limit])
        if reinforce:
            reinforced: list[MemoryRecord] = []
            for record in results:
                updated = _validated_update(
                    record,
                    {
                        "heat": min(1.0, record.heat + 0.1),
                        "recall_count": record.recall_count + 1,
                        "last_recalled_at": self._now(),
                        "updated_at": self._now(),
                    },
                )
                await self._repository.upsert(updated)
                await self._audit("memory.recalled", tenant_id, user_id, record.id, "recalled")
                reinforced.append(updated)
            return tuple(reinforced)
        return results
```

- [x] **Step 4: Run targeted tests**

Run Step 2 command again.

Expected: PASS.

## Task 3: Add lock/unlock and heat decay

**Files:**
- Modify: `src/agent_hub/memory/service.py`
- Test: `tests/integration/memory/test_memory.py`

- [x] **Step 1: Write failing tests**

Add:

```python
async def test_locked_memory_does_not_decay() -> None:
    service, now = memory_service_with_clock()
    added = await service.add_candidate(
        tenant_id=TENANT_A,
        user_id=USER_1,
        text="never decay this important preference",
    )
    assert added.record is not None
    locked = await service.lock(added.record.id, tenant_id=TENANT_A, user_id=USER_1)
    assert locked.locked is True
    now[0] = now[0] + timedelta(days=30)

    assert await service.decay_due() == 0
    visible = await service.inspect(tenant_id=TENANT_A, user_id=USER_1)
    assert visible[0].locked is True
    assert visible[0].deleted_at is None


async def test_low_heat_unlocked_episodic_memory_decays_to_tombstone() -> None:
    service, now = memory_service_with_clock()
    added = await service.add_candidate(
        tenant_id=TENANT_A,
        user_id=USER_1,
        text="temporary old implementation detail",
        confidence=0.2,
    )
    assert added.record is not None
    now[0] = now[0] + timedelta(days=90)

    expired = await service.decay_due(days=30, heat_loss=0.6, tombstone_below=0.05)

    assert expired == 1
    tombstones = await service.inspect(tenant_id=TENANT_A, user_id=USER_1, include_deleted=True)
    assert tombstones[0].deleted_at is not None
    assert tombstones[0].tombstone_reason == "memory_decay_expired"
```

- [x] **Step 2: Run failing tests**

Run:

```powershell
$env:PYTHONPATH = (Resolve-Path src).Path
& 'E:\code_x\agent-hub\.venv\Scripts\python.exe' -m pytest tests\integration\memory\test_memory.py::test_locked_memory_does_not_decay tests\integration\memory\test_memory.py::test_low_heat_unlocked_episodic_memory_decays_to_tombstone -q
```

Expected: FAIL because `lock` and `decay_due` do not exist.

- [x] **Step 3: Implement lock/unlock/decay**

Add methods to `MemoryService`:

```python
    async def lock(self, memory_id: UUID, *, tenant_id: UUID, user_id: UUID) -> MemoryRecord:
        record = await self._owned_record(memory_id, tenant_id, user_id)
        updated = _validated_update(record, {"locked": True, "updated_at": self._now()})
        await self._repository.upsert(updated)
        await self._audit("memory.locked", tenant_id, user_id, memory_id, "locked")
        return updated

    async def unlock(self, memory_id: UUID, *, tenant_id: UUID, user_id: UUID) -> MemoryRecord:
        record = await self._owned_record(memory_id, tenant_id, user_id)
        updated = _validated_update(record, {"locked": False, "updated_at": self._now()})
        await self._repository.upsert(updated)
        await self._audit("memory.unlocked", tenant_id, user_id, memory_id, "unlocked")
        return updated

    async def decay_due(
        self, *, days: int = 30, heat_loss: float = 0.1, tombstone_below: float = 0.05
    ) -> int:
        if days < 1 or not 0 <= heat_loss <= 1 or not 0 <= tombstone_below <= 1:
            raise ValueError("memory decay settings are invalid")
        changed = 0
        now = self._now()
        for record in await self._repository.list_all():
            if record.deleted_at is not None or record.locked or record.layer is MemoryLayer.CORE:
                continue
            last_activity = record.last_recalled_at or record.updated_at
            if (now - last_activity).days < days:
                continue
            next_heat = max(0.0, record.heat - heat_loss)
            updates: dict[str, object] = {"heat": next_heat, "updated_at": now}
            if next_heat <= tombstone_below:
                updates["deleted_at"] = now
                updates["tombstone_reason"] = "memory_decay_expired"
            updated = _validated_update(record, updates)
            await self._repository.upsert(updated)
            await self._audit(
                "memory.decayed",
                record.tenant_id,
                record.user_id,
                record.id,
                "decayed",
                reason=updated.tombstone_reason,
            )
            changed += 1
        return changed
```

- [x] **Step 4: Run targeted tests**

Run Step 2 command again.

Expected: PASS.

## Task 4: Add deterministic daily/weekly/monthly consolidation

**Files:**
- Modify: `src/agent_hub/memory/service.py`
- Test: `tests/integration/memory/test_memory.py`

- [x] **Step 1: Write failing tests**

Add:

```python
async def test_consolidation_creates_summary_memory() -> None:
    service, _ = memory_service_with_clock()
    await service.add_candidate(
        tenant_id=TENANT_A,
        user_id=USER_1,
        text="deployments go to prod-web-01",
        category=MemoryCategory.FACT,
        project_id="cube-agent",
    )
    await service.add_candidate(
        tenant_id=TENANT_A,
        user_id=USER_1,
        text="Hermes+ must finish before harness refactor",
        category=MemoryCategory.DECISION,
        project_id="cube-agent",
    )

    summary = await service.consolidate(
        tenant_id=TENANT_A,
        user_id=USER_1,
        period=MemorySummaryPeriod.DAY,
        project_id="cube-agent",
    )

    assert summary.category is MemoryCategory.SUMMARY
    assert summary.summary_period is MemorySummaryPeriod.DAY
    assert summary.project_id == "cube-agent"
    assert "deployments go to prod-web-01" in summary.text
    assert "Hermes+ must finish before harness refactor" in summary.text
    assert summary.locked is True
    assert (await service.audit_events(tenant_id=TENANT_A))[-1].kind == "memory.consolidated"
```

- [x] **Step 2: Run failing test**

Run:

```powershell
$env:PYTHONPATH = (Resolve-Path src).Path
& 'E:\code_x\agent-hub\.venv\Scripts\python.exe' -m pytest tests\integration\memory\test_memory.py::test_consolidation_creates_summary_memory -q
```

Expected: FAIL because `consolidate` does not exist.

- [x] **Step 3: Implement consolidation**

Add method:

```python
    async def consolidate(
        self,
        *,
        tenant_id: UUID,
        user_id: UUID,
        period: MemorySummaryPeriod,
        project_id: str | None = None,
        conversation_id: str | None = None,
        limit: int = 12,
    ) -> MemoryRecord:
        if period is MemorySummaryPeriod.NONE:
            raise ValueError("summary period must be day, week, or month")
        records = [
            record
            for record in await self._active_records(tenant_id, user_id)
            if record.summary_period is MemorySummaryPeriod.NONE
            and (project_id is None or record.project_id == project_id)
            and (conversation_id is None or record.conversation_id == conversation_id)
        ]
        records.sort(key=lambda record: (-record.heat, -record.confidence, record.created_at))
        selected = records[:limit]
        if not selected:
            raise ValueError("no memories available for consolidation")
        bullet_text = "; ".join(record.text for record in selected)
        text = f"{period.value} summary: {bullet_text}"
        now = self._now()
        summary = MemoryRecord(
            id=uuid4(),
            tenant_id=tenant_id,
            user_id=user_id,
            layer=MemoryLayer.EPISODIC,
            category=MemoryCategory.SUMMARY,
            text=text[:4096],
            confidence=min(0.95, max(record.confidence for record in selected)),
            created_at=now,
            updated_at=now,
            heat=0.8,
            locked=True,
            project_id=project_id,
            conversation_id=conversation_id,
            summary_period=period,
            metadata={"source_count": str(len(selected))},
        )
        await self._repository.upsert(summary)
        await self._audit("memory.consolidated", tenant_id, user_id, summary.id, "consolidated")
        return summary
```

Update imports in `service.py`:

```python
from agent_hub.memory.types import MemorySummaryPeriod
```

- [x] **Step 4: Run targeted test**

Run Step 2 command again.

Expected: PASS.

## Task 5: Render Hermes+ memories in ContextBuilder

**Files:**
- Modify: `src/agent_hub/context/builder.py`
- Test: `tests/unit/context/test_builder.py`

- [x] **Step 1: Write failing test**

Add:

```python
def test_context_builder_renders_hermes_plus_memory_labels() -> None:
    hot = memory("deploy to prod-web-01", MemoryLayer.EPISODIC).model_copy(
        update={
            "heat": 0.9,
            "locked": True,
            "project_id": "cube-agent",
            "summary_period": "week",
        }
    )

    context = ContextBuilder().build(
        ContextBuildInput(system_policy="system", current_user_request="request", memories=(hot,)),
        token_budget=100,
    )

    rendered = "\n".join(section.content for section in context.sections)
    assert "[locked]" in rendered
    assert "[heat:0.90]" in rendered
    assert "[project:cube-agent]" in rendered
    assert "[summary:week]" in rendered
```

- [x] **Step 2: Run failing test**

Run:

```powershell
$env:PYTHONPATH = (Resolve-Path src).Path
& 'E:\code_x\agent-hub\.venv\Scripts\python.exe' -m pytest tests\unit\context\test_builder.py::test_context_builder_renders_hermes_plus_memory_labels -q
```

Expected: FAIL because memory labels are not rendered.

- [x] **Step 3: Implement memory rendering helper**

In `src/agent_hub/context/builder.py`, replace direct `memory.text` rendering with `_render_memory(memory)`.

Add:

```python
def _render_memory(memory: MemoryRecord) -> str:
    labels = [f"heat:{memory.heat:.2f}"]
    if memory.locked:
        labels.append("locked")
    if memory.project_id:
        labels.append(f"project:{escape(memory.project_id, quote=False)}")
    if memory.conversation_id:
        labels.append(f"conversation:{escape(memory.conversation_id, quote=False)}")
    if memory.summary_period.value != "none":
        labels.append(f"summary:{memory.summary_period.value}")
    return f"[{' '.join(f'[{label}]' for label in labels)}] {memory.text}"
```

Use:

```python
        _section("core_memory", _render_memory(memory), 60)
```

and:

```python
        _section("episodic_memory", _render_memory(memory), 70)
```

- [x] **Step 4: Run targeted test**

Run Step 2 command again.

Expected: PASS.

## Task 6: Extend Admin memory API

**Files:**
- Modify: `src/agent_hub/api/routers/admin.py`
- Test: `tests/api/test_admin_resources.py`

- [x] **Step 1: Write failing API test**

Add:

```python
def test_memory_api_exposes_hermes_plus_fields_and_lock_controls(api: TestClient) -> None:
    created = api.post(
        "/api/v1/admin/memory",
        headers=headers(),
        json={
            "id": "hermes-plus-policy",
            "scope": "cube-agent",
            "value": "Hermes+ must finish before harness refactor.",
            "heat": 0.7,
            "project_id": "cube-agent",
            "conversation_id": "handoff",
        },
    )
    assert created.status_code == 200
    body = created.json()
    assert body["heat"] == 0.7
    assert body["locked"] is False
    assert body["summary_period"] == "none"

    locked = api.post("/api/v1/admin/memory/hermes-plus-policy/lock", headers=headers())
    assert locked.status_code == 200
    assert locked.json()["locked"] is True

    unlocked = api.post("/api/v1/admin/memory/hermes-plus-policy/unlock", headers=headers())
    assert unlocked.status_code == 200
    assert unlocked.json()["locked"] is False
```

- [x] **Step 2: Run failing test**

Run:

```powershell
$env:PYTHONPATH = (Resolve-Path src).Path
& 'E:\code_x\agent-hub\.venv\Scripts\python.exe' -m pytest tests\api\test_admin_resources.py::test_memory_api_exposes_hermes_plus_fields_and_lock_controls -q
```

Expected: FAIL because response schema and endpoints do not exist.

- [x] **Step 3: Extend admin schemas**

In `MemoryRecordRequest`, add optional fields:

```python
    heat: float = Field(default=0.5, ge=0, le=1)
    locked: bool = False
    project_id: str | None = Field(default=None, min_length=1, max_length=128)
    conversation_id: str | None = Field(default=None, min_length=1, max_length=128)
    summary_period: str = Field(default="none", pattern=r"^(none|day|week|month)$")
    recall_count: int = Field(default=0, ge=0)
    last_recalled_at: str | None = None
```

Mirror the fields in `MemoryRecordResponse`.

- [x] **Step 4: Preserve fields in create/update and add endpoints**

Update create/update methods in `InMemoryAdminResourceService` and `PersistentAdminResourceService` to store all request fields.

Add routes:

```python
@router.post("/memory/{memory_id}/lock", response_model=MemoryRecordResponse, responses=error_responses(401, 403, 404))
async def lock_memory(memory_id: str, principal: Principal = Depends(require_principal), service: AdminResourceService = Depends(get_admin_service)) -> MemoryRecordResponse:
    _require(principal, "memory:write")
    try:
        current = next(item for item in await service.list_memory() if item.id == memory_id)
    except StopIteration:
        raise HTTPException(status_code=404, detail={"error": {"code": "memory_not_found", "message": "memory not found"}}) from None
    return await service.update_memory(memory_id, MemoryRecordRequest(**{**current.model_dump(), "locked": True}))


@router.post("/memory/{memory_id}/unlock", response_model=MemoryRecordResponse, responses=error_responses(401, 403, 404))
async def unlock_memory(memory_id: str, principal: Principal = Depends(require_principal), service: AdminResourceService = Depends(get_admin_service)) -> MemoryRecordResponse:
    _require(principal, "memory:write")
    try:
        current = next(item for item in await service.list_memory() if item.id == memory_id)
    except StopIteration:
        raise HTTPException(status_code=404, detail={"error": {"code": "memory_not_found", "message": "memory not found"}}) from None
    return await service.update_memory(memory_id, MemoryRecordRequest(**{**current.model_dump(), "locked": False}))
```

Keep route formatting consistent with nearby endpoints.

- [x] **Step 5: Run targeted API test**

Run Step 2 command again.

Expected: PASS.

## Task 7: Update web client and Memory page

**Files:**
- Modify: `web/src/api/client.ts`
- Modify: `web/src/pages/MemoryPage.tsx`
- Test: `web/src/pages/OperationalPages.test.tsx`

- [x] **Step 1: Write failing UI test**

In `web/src/pages/OperationalPages.test.tsx`, extend the `/api/v1/admin/memory` mock record with:

```ts
heat: 0.82,
locked: true,
project_id: "cube-agent",
conversation_id: "handoff",
summary_period: "week",
recall_count: 3,
last_recalled_at: "2026-08-29T09:00:00Z",
```

Add assertions to the memory page test:

```ts
expect(await screen.findByText("热度 0.82")).toBeInTheDocument();
expect(screen.getByText("已锁定")).toBeInTheDocument();
expect(screen.getByText("项目 cube-agent")).toBeInTheDocument();
expect(screen.getByText("摘要 week")).toBeInTheDocument();
expect(screen.getByText("召回 3 次")).toBeInTheDocument();
```

- [x] **Step 2: Run failing UI test**

Run:

```powershell
npm test -- --run web/src/pages/OperationalPages.test.tsx
```

from `E:\code_x\mofang agent\web`.

Expected: FAIL because the UI does not render Hermes+ fields.

- [x] **Step 3: Extend Zod schema and client methods**

In `web/src/api/client.ts`, extend `MemoryRecordSchema`:

```ts
  heat: z.number().default(0.5),
  locked: z.boolean().default(false),
  project_id: z.string().nullable().default(null),
  conversation_id: z.string().nullable().default(null),
  summary_period: z.enum(["none", "day", "week", "month"]).default("none"),
  recall_count: z.number().default(0),
  last_recalled_at: z.string().nullable().default(null),
```

Add API methods:

```ts
  lockMemory(id: string): Promise<MemoryRecord> {
    return request(`/api/v1/admin/memory/${id}/lock`, { method: "POST" }, MemoryRecordSchema);
  },
  unlockMemory(id: string): Promise<MemoryRecord> {
    return request(`/api/v1/admin/memory/${id}/unlock`, { method: "POST" }, MemoryRecordSchema);
  },
```

- [x] **Step 4: Render fields in MemoryPage**

In `MemoryPage.tsx`, render badges in each record card:

```tsx
<div className="inline-status-list">
  <span>热度 {record.heat.toFixed(2)}</span>
  <span>{record.locked ? "已锁定" : "未锁定"}</span>
  {record.project_id ? <span>项目 {record.project_id}</span> : null}
  {record.summary_period !== "none" ? <span>摘要 {record.summary_period}</span> : null}
  <span>召回 {record.recall_count} 次</span>
</div>
```

Add lock/unlock button:

```tsx
<button type="button" onClick={() => (record.locked ? unlock.mutate(record.id) : lock.mutate(record.id))}>
  {record.locked ? "解除锁定" : "锁定记忆"}
</button>
```

- [x] **Step 5: Run targeted UI test**

Run Step 2 command again.

Expected: PASS.

## Task 8: Run local gates, deploy, push, and hand off

**Files:**
- Modify: `HANDOFF.md`
- Modify: `task_plan.md`
- Modify: `findings.md`
- Modify: `progress.md`

- [x] **Step 1: Run backend gates**

Run:

```powershell
$env:PYTHONPATH = (Resolve-Path src).Path
& 'E:\code_x\agent-hub\.venv\Scripts\python.exe' -m ruff check .
& 'E:\code_x\agent-hub\.venv\Scripts\python.exe' -m mypy --strict src tests
& 'E:\code_x\agent-hub\.venv\Scripts\python.exe' -m pytest tests\integration\memory tests\unit\context tests\api\test_admin_resources.py -q
```

Expected: all pass.

- [x] **Step 2: Run frontend gates**

Run from `E:\code_x\mofang agent\web`:

```powershell
npm run lint
npm test -- --run
npm run build
```

Expected: all pass.

- [ ] **Step 3: Commit Hermes+**

Run:

```powershell
git add src\agent_hub\memory src\agent_hub\context src\agent_hub\api\routers\admin.py tests\integration\memory tests\unit\context tests\api\test_admin_resources.py web\src\api\client.ts web\src\pages\MemoryPage.tsx web\src\pages\OperationalPages.test.tsx docs\superpowers\plans\2026-08-29-hermes-memory-plus.md docs\superpowers\specs\2026-08-29-mofang-agent-open-harness-design.md
git commit -m "Add Hermes+ memory controls"
```

Expected: commit succeeds.

- [ ] **Step 4: Deploy to prod-web-01**

Use the existing fallback release-copy deployment pattern from this handoff. New release name:

```text
20260829-hermes-memory-plus
```

Run a deployed probe that verifies:

- `/health` returns ok.
- admin memory list still works.
- a Hermes+ memory can be created with heat/project/summary fields.
- lock/unlock route works.
- no copied server temp files remain.

- [ ] **Step 5: Push original repository and check Actions**

Push to `https://github.com/zhangzhimiao1994/CubeAgent.git` main with a recovery tag, then watch GitHub Actions until green.

Expected: `quality` conclusion is `success`.

- [ ] **Step 6: Update HANDOFF and pause**

Append to `HANDOFF.md`:

- Hermes+ final state.
- Commit SHA.
- Local verification commands and results.
- Production release path and real probe result.
- GitHub Actions run id and result.
- Docker/WSL cleanup status.
- Future repository: `https://github.com/zhangzhimiao1994/Cube-agent`.
- Remaining follow-up tasks for the new conversation:
  1. Verify/push final completed source to `Cube-agent`.
  2. Start open harness / DeepSeek harness refactor in `Cube-agent`.
  3. Implement Mofang Harness Core.
  4. Add capability-aware scheduler.
  5. Add provider capability profiles.
  6. Harden DeepSeek provider adapter.
  7. Add tool gateway unification.
  8. Connect Hermes+ as harness context provider.
  9. Add CLI/MCP/channel entrypoints.
  10. Deploy and verify from the new repository.

After `HANDOFF.md` is updated, stop work and wait for the user's new conversation. Do not implement harness refactor in this conversation.

## Self-review

- Spec coverage: This plan covers type fields, recall, decay, lock/unlock, deterministic summaries, context rendering, API/UI exposure, verification, deployment, GitHub Actions, and final handoff pause.
- Scope control: The plan keeps Hermes+ in the original repository and leaves harness implementation for `Cube-agent`.
- Type consistency: New names are `MemorySummaryPeriod`, `heat`, `last_recalled_at`, `recall_count`, `locked`, `project_id`, `conversation_id`, `summary_period`, and `metadata`.
- Safety: The plan preserves sensitive-content rejection, prompt-like external content rejection, tenant/user isolation, explicit locking, audit events, and no AGPL source copying.
