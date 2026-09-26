# Active Run Queue Control Implementation Plan

> **Required sub-skill:** Use `superpowers:test-driven-development` for every implementation task and `superpowers:verification-before-completion` before deployment or completion claims.

**Goal:** Allow users to queue a message while a conversation run is active, then edit it, cancel it, or use it to change direction without creating concurrent runs or losing attachments and references.

**Architecture:** Persist one ordered queue per tenant and conversation. Each queue item owns a blocked successor run so the existing run execution pipeline remains the source of truth. PostgreSQL transaction-scoped advisory locks serialize queue mutations and releases. Terminal run transitions release exactly one successor; change-direction requests cooperatively cancel the predecessor before releasing the selected successor. The React conversation composer reads queue state from the conversation detail API and exposes compact, responsive controls.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy async, PostgreSQL/Alembic, React 19, TypeScript, TanStack Query, Zod, Vitest, Testing Library, Playwright.

**Spec:** `docs/superpowers/specs/2026-09-26-active-run-queue-control-design.md`

**Global Constraints:** Preserve tenant isolation, idempotency, attachment/reference metadata, and existing direct-send behavior when no run is active. Do not introduce a second execution path. Do not allow two runs in the same conversation to execute concurrently. Keep all user-visible queue text in Chinese. Mobile controls must have at least 44 px targets and desktop layouts must not stretch the queue card into an oversized panel.

**Review Focus:**

- A run can become active between clicking send and the request reaching the server; the draft must remain recoverable and no duplicate run may be created.
- Two browser tabs can enqueue concurrently; positions must remain deterministic and idempotency keys must prevent duplicates.
- Editing can race with release; stale versions must return a conflict while preserving the proposed text in the UI.
- Change direction can be requested during an irreversible tool call; the successor must remain blocked until the predecessor reaches a terminal state.
- Worker restart or predecessor failure must release the next item once, and only once.

---

## Task 1: Add durable queue and blocked-run schema

**Files:**
- Create: `alembic/versions/0028_conversation_run_queue.py`
- Modify: `src/agent_hub/db/models.py`
- Modify: `src/agent_hub/domain/runs.py`
- Create: `tests/unit/runs/test_conversation_queue_contract.py`
- Modify: `tests/unit/domain/test_runs.py`

**Interfaces:**

```python
class ConversationQueueStatus(StrEnum):
    QUEUED = "queued"
    REDIRECTING = "redirecting"
    RELEASED = "released"
    CANCELLED = "cancelled"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"

class ConversationQueueItemRow(Base):
    __tablename__ = "agent_hub_conversation_queue_items"
```

Add nullable `RunRow.blocked_by_run_id`, indexed queue ordering, unique tenant-scoped idempotency, optimistic `version`, timestamps, and redacted failure metadata. Queue rows reference predecessor and successor runs without cascade deletion.

**TDD steps:**

1. Add failing contract tests for every status, required column, unique/index constraint, and self-referencing blocked-run relationship.
2. Run `python -m pytest tests/unit/runs/test_conversation_queue_contract.py tests/unit/domain/test_runs.py -q` and confirm the failures describe missing schema/contracts.
3. Implement the enum, ORM row, run column, and Alembic upgrade/downgrade.
4. Re-run the focused tests and migration smoke test until green.
5. Commit: `feat: add durable conversation queue schema`.

## Task 2: Implement atomic queue lifecycle operations

**Files:**
- Create: `src/agent_hub/runs/conversation_queue.py`
- Modify: `src/agent_hub/runs/repository.py`
- Create: `tests/integration/runs/test_conversation_queue_repository.py`

**Interfaces:**

```python
class ConversationQueueRepository:
    async def enqueue(self, command: EnqueueConversationMessage) -> ConversationQueueItem: ...
    async def list_for_conversation(self, tenant_id: UUID, conversation_id: str) -> list[ConversationQueueItem]: ...
    async def edit(self, tenant_id: UUID, item_id: UUID, expected_version: int, message: str) -> ConversationQueueItem: ...
    async def cancel(self, tenant_id: UUID, item_id: UUID, expected_version: int) -> ConversationQueueItem: ...
```

Acquire `pg_advisory_xact_lock` using a stable tenant/conversation key before calculating position or changing mutable state. Enqueue creates the successor through `RunRepository.create_run(..., blocked_by_run_id=predecessor_id)` in the same transaction. Editing mutates the same queue item and successor request snapshot; cancelling marks both queue item and blocked successor cancelled.

**TDD steps:**

1. Write failing PostgreSQL integration tests for ordered enqueue, duplicate idempotency, tenant isolation, edit-in-place, attachment/reference preservation, cancellation, stale version conflict, and two concurrent enqueues.
2. Run `python -m pytest tests/integration/runs/test_conversation_queue_repository.py -q` and retain the first meaningful failures.
3. Implement immutable command/result dataclasses and repository operations with one transaction and advisory lock.
4. Re-run until all lifecycle and race tests pass.
5. Commit: `feat: persist ordered conversation queue items`.

## Task 3: Gate worker claims and release one successor

**Files:**
- Modify: `src/agent_hub/runs/repository.py`
- Modify: `src/agent_hub/runs/service.py`
- Modify: `src/agent_hub/runtime/worker.py`
- Create: `tests/integration/runs/test_conversation_queue_release.py`
- Modify: `tests/resilience/test_worker_crash.py`

**Interfaces:**

```python
async def release_next_for_terminal_run(
    self,
    tenant_id: UUID,
    predecessor_run_id: UUID,
) -> ConversationQueueItem | None: ...
```

`claim_for_execution` must refuse any run with `blocked_by_run_id` still set. Every terminal transition (`completed`, `failed`, `cancelled`) invokes the same idempotent release hook. The hook locks the conversation, finalizes the previous queue item when applicable, clears exactly one successor block, marks its item `released`, and enqueues the run once.

**TDD steps:**

1. Add failing tests proving blocked runs cannot be claimed, terminal states release only the first queued item, repeated callbacks do not double enqueue, predecessor failure still advances the queue, and worker restart recovers correctly.
2. Run `python -m pytest tests/integration/runs/test_conversation_queue_release.py tests/resilience/test_worker_crash.py -q` and confirm red.
3. Add the claim guard and one shared terminal-release hook; wire all repository/service terminal paths through it.
4. Re-run focused tests and existing run recovery tests.
5. Commit: `feat: release queued conversation runs in order`.

## Task 4: Add safe change-direction behavior

**Files:**
- Modify: `src/agent_hub/runs/conversation_queue.py`
- Modify: `src/agent_hub/runs/service.py`
- Modify: `src/agent_hub/runtime/worker.py`
- Create: `tests/integration/runs/test_conversation_queue_redirect.py`
- Modify: `tests/integration/runs/test_dispatch_artifact_cancellation.py`

**Interfaces:**

```python
async def redirect(
    self,
    tenant_id: UUID,
    item_id: UUID,
    expected_version: int,
) -> ConversationQueueItem: ...
```

Redirect marks the selected item `redirecting`, records `superseded_by_user` on the predecessor cancellation request, and signals the existing cooperative cancellation path. It does not clear the successor block itself. The normal terminal hook releases the selected item after the predecessor is safely terminal; earlier queue items are cancelled with an explicit superseded reason.

**TDD steps:**

1. Write failing tests for redirect ordering, irreversible/in-flight tool protection, repeated redirect idempotency, redirect/cancel races, and timeout behavior that leaves the item visibly `redirecting`.
2. Run `python -m pytest tests/integration/runs/test_conversation_queue_redirect.py tests/integration/runs/test_dispatch_artifact_cancellation.py -q` and confirm red.
3. Implement redirect state transition and cancellation signalling using the existing runtime cancellation boundary.
4. Re-run focused tests and cancellation regressions.
5. Commit: `feat: support safe queued direction changes`.

## Task 5: Expose queue APIs and conversation projection

**Files:**
- Modify: `src/agent_hub/api/routers/runs.py`
- Modify: `src/agent_hub/runs/service.py`
- Modify: `src/agent_hub/runs/conversations.py`
- Modify: `tests/api/test_runs_api.py`

**Interfaces:**

```text
POST   /api/v1/admin/conversations/{conversation_id}/queue
PATCH  /api/v1/admin/conversation-queue/{queue_item_id}
POST   /api/v1/admin/conversation-queue/{queue_item_id}/redirect
DELETE /api/v1/admin/conversation-queue/{queue_item_id}
```

The enqueue endpoint is authoritative: when the conversation has no active/blocked run it may reject with `conversation_not_active`, allowing the client to retry as a normal send without losing its draft. Queue responses include `version`, position, status, predecessor/successor ids, attachment count, timestamps, and safe error details. Conversation detail embeds non-expired queue items.

**TDD steps:**

1. Extend API fakes and add failing tests for auth, tenant scoping, validation, archived conversations, idempotency, optimistic conflicts, active-run race behavior, all four actions, audit events, and queue projection.
2. Run `python -m pytest tests/api/test_runs_api.py -q` and confirm the new cases fail.
3. Add Pydantic request/response models, protocol methods, service orchestration, HTTP error mapping, and audit records.
4. Re-run API tests plus `python -m pytest tests/unit/runs tests/integration/runs -q`.
5. Commit: `feat: expose conversation queue controls`.

## Task 6: Add typed web client support

**Files:**
- Modify: `web/src/api/client.ts`
- Modify: `web/src/api/client.test.ts`

**Interfaces:**

```typescript
type ConversationQueueItem = {
  id: string;
  message: string;
  position: number;
  status: "queued" | "redirecting" | "released" | "cancelled" | "running" | "completed" | "failed";
  version: number;
  attachment_count: number;
};
```

Add `queueConversationMessage`, `editConversationQueueItem`, `redirectConversationQueueItem`, and `cancelConversationQueueItem`; parse queue items from conversation detail with Zod.

**TDD steps:**

1. Add failing client tests for request methods, encoded ids, version payloads, response parsing, and malformed queue responses.
2. Run `npm test -- --run src/api/client.test.ts` from `web` and confirm red.
3. Add schemas, exported types, and API methods.
4. Re-run client tests and `npm run lint`.
5. Commit: `feat: add web conversation queue client`.

## Task 7: Build compact queue controls in the composer

**Files:**
- Modify: `web/src/pages/RunsPage.tsx`
- Modify: `web/src/styles.css`
- Modify: `web/src/pages/OperationalPages.test.tsx`
- Create: `web/e2e/conversation-queue.spec.ts`

**Behavior:**

- No active run: primary action remains `发送`.
- Active or blocked run: primary action becomes `排队`, with a tooltip explaining that it runs after the current task.
- Render compact ordered queue rows above the composer with a two-line preview, attachment count, and status.
- Each mutable row exposes `改变方向`, an edit icon labelled `编辑排队信息`, and a cancel icon labelled `取消排队`.
- Editing is inline and preserves the unsaved text after server errors or version conflicts. Redirect and cancel require explicit confirmation.
- On `conversation_not_active`, refresh once and submit normally with the same draft/idempotency key; never silently duplicate.

**TDD steps:**

1. Add failing Testing Library cases for send/queue switching, queue rendering, edit/save/cancel, conflict draft preservation, confirmation dialogs, API errors, two-tab refresh simulation, keyboard focus, and no `插话` control.
2. Run `npm test -- --run src/pages/OperationalPages.test.tsx` and confirm red.
3. Implement mutations, optimistic query updates with rollback, compact markup, focus management, and responsive CSS.
4. Re-run component tests, `npm run lint`, and `npm run build`.
5. Add Playwright flows at 390x844, 768x1024, 1440x900, and 1920x1080. Assert no clipping/overlap, full keyboard access, stable composer height, and correct queue transitions.
6. Run `npx playwright test web/e2e/conversation-queue.spec.ts` from the repository root (or the project Playwright command documented by existing config).
7. Commit: `feat: add editable conversation queue controls`.

## Task 8: Full verification, production deployment, and real-device acceptance

**Files:**
- Modify: `HANDOFF.md` (local current-state index only; do not commit if ignored)
- Modify only if failures require fixes: queue/backend/frontend files above

**Verification:**

1. Run focused backend tests, then the full Python test suite and static checks used by CI.
2. Run full frontend Vitest, TypeScript lint, production build, and Playwright desktop/mobile suites.
3. Run migration upgrade on a disposable PostgreSQL database and verify downgrade/upgrade symmetry.
4. Review the diff for tenant leaks, message/attachment logging, stale-version handling, and irreversible-tool cancellation behavior.
5. Commit any fixes in focused commits, push the branch, inspect the triggered GitHub run, and repeat until green or a real external blocker is documented.
6. Build one release archive, upload it to production, migrate, restart services, and verify revision/health through Caddy.
7. With account `test`, perform production acceptance: start a long run, enqueue two messages, edit the second, cancel it, enqueue again, choose change direction, confirm predecessor termination, confirm exactly one successor executes, and verify queue state after page reload.
8. Repeat the rendered flow on a real mobile browser and desktop viewport; verify touch scrolling, no clipped sheets, readable contrast, and 44 px controls.
9. Remove production temp archives and every non-current release, check disk free space, and leave no rollback point on the server.
10. Update `HANDOFF.md` with the deployed revision, verification snapshot, remaining external blockers, and next mainline work.
11. Commit: `fix: close active run queue acceptance gaps` only if acceptance required code changes.

## Task 9: Resume the approved mainline after queue closure

**Files:**
- Use the existing Skill-source and Hermes planning documents; do not redefine their approved scope here.
- Update `HANDOFF.md` as each durable slice is completed.

**Sequence:**

1. Close the production Skill-source networking gap so trusted GitHub sources can actually sync on the server; verify install, enable, disable, upgrade, rollback, audit, and cleanup flows with a real non-placeholder package.
2. Run production desktop/mobile acceptance for plugin discovery and installation, including a second representative plugin type beyond Strix.
3. Execute the previously approved Hermes improvements in order: conversation checkpoints/search, reviewed learning loop, layered memory, execution backend configuration, team Skill Tap, journey visualization, and slash-command conversation controls.
4. For every Hermes slice, use TDD, responsive rendered QA, production deployment, real-device acceptance, GitHub check verification, server disk cleanup, and a concise current-state handoff update.

