# Hermes+ Memory System Design

Date: 2026-08-29

## Repository and priority

Hermes+ is the first-priority implementation slice in the current/original repository. The OpenAI open harness and DeepSeek harness refactors are intentionally deferred to the future `https://github.com/zhangzhimiao1994/Cube-agent` repository after this source is completed and pushed there.

## Goal

Upgrade the existing CubeAgent memory/Hermes system into Hermes+ by borrowing kiwi-mem's strongest product ideas without copying AGPL implementation code:

- memory heat / reinforcement
- decay and archival
- Dream-like consolidation
- day/week/month summaries
- project/conversation isolation
- safe memory tools

The result should improve long-term usefulness while preserving the current system's stronger safety properties: tenant/user isolation, explicit core-memory confirmation, sensitive-content rejection, prompt-injection resistance, auditability, and forget/edit controls.

## Current baseline

The current repository already has:

- `src/agent_hub/memory/types.py`: `MemoryRecord`, `MemoryLayer`, `MemoryCategory`, audit/result types.
- `src/agent_hub/memory/service.py`: add/search/inspect/edit/forget/expire and safety filters.
- `src/agent_hub/context/builder.py`: core and episodic memory sections in context construction.
- `src/agent_hub/hermes/advisor.py`: persistent Hermes run advice and bounded outcome learning.
- `src/agent_hub/api/routers/admin.py`: admin memory resources and Hermes resources through `AdminResourceRow`.
- `web/src/pages/MemoryPage.tsx`: existing memory management UI.

This means Hermes+ should be an incremental extension, not a replacement.

## Recommended approach

Use a native Hermes+ implementation inside the current repository:

1. Extend memory domain types with metadata fields.
2. Add service methods for recall reinforcement, decay, locking, summaries, and consolidation.
3. Update ContextBuilder scoring to prefer hot, recent, confirmed, project-relevant memories.
4. Add admin/API/UI visibility for heat, lock state, and summaries.
5. Keep kiwi-mem as an inspiration/reference only, with optional external adapter later if needed.

## Domain model

Extend memory records with:

- `heat`: float from 0 to 1, default 0.5.
- `last_recalled_at`: timezone-aware datetime or null.
- `recall_count`: nonnegative integer.
- `locked`: boolean; locked memories do not decay or auto-expire.
- `project_id`: bounded string or null.
- `conversation_id`: bounded string or null.
- `summary_period`: one of `none`, `day`, `week`, `month`.
- `metadata`: small JSON object for safe non-secret labels.

Add memory categories as needed:

- `SUMMARY`
- `DECISION`
- `LESSON`

Keep existing `WORKING`, `EPISODIC`, and `CORE` layers. Do not add vector search in the first slice; keyword scoring plus heat/recency is enough and avoids introducing pgvector migration risk before the design is stable.

## Service behavior

### Recall reinforcement

When `MemoryService.search()` returns records, optionally mark recalled records:

- increment `recall_count`
- update `last_recalled_at`
- increase `heat` by a bounded amount
- audit `memory.recalled`

Search should remain bounded and deterministic.

### Decay

Add a scheduled/service method that reduces heat for unlocked non-core memories over time. Records below a threshold can be tombstoned with reason `memory_decay_expired`, unless they are locked or core.

### Lock/unlock

Add explicit lock/unlock operations. Locked memories stay visible, do not decay, and require user/admin action to delete or edit.

### Dream consolidation

Add a deterministic consolidation service that creates summaries from recent active memories and Hermes run lessons:

- daily summary
- weekly summary
- monthly summary

The first implementation should be rule-based and deterministic. Model-generated summaries can be added later behind a clear approval/audit path.

### Project/conversation isolation

Memory writes and searches should accept optional `project_id` and `conversation_id`.

Search order:

1. exact tenant/user/project/conversation match
2. same project
3. user-level core memories
4. broader tenant/user episodic memories

No memory from another tenant or another user should be returned.

## ContextBuilder integration

ContextBuilder should receive already-ranked memories. The memory service should supply a compact list sorted by:

1. core and locked memories
2. heat
3. confidence
4. project/conversation relevance
5. recency

Context sections should include safe labels such as `[core]`, `[locked]`, `[project:<id>]`, and `[summary:week]` only if they do not expose secrets.

## API and UI

Add backend APIs for:

- list/search with heat and summary filters
- lock memory
- unlock memory
- trigger consolidation/digest
- list summary memories

Update the Memory page to show:

- heat
- recall count
- last recalled time
- lock state
- project/conversation scope
- summary period

Keep edit/forget behavior explicit and audited.

## Safety and license boundaries

- Do not copy kiwi-mem source into CubeAgent because kiwi-mem is AGPL-3.0-or-later.
- Reimplement concepts from scratch using existing project patterns.
- Never auto-promote prompt-like external content into core memory.
- Never store API keys, passwords, bearer tokens, or secrets.
- Keep core memories gated by stable fact or user confirmation.
- Keep audit events for storage, recall, consolidation, lock/unlock, edit, forget, decay, and rejection.

## Test strategy

Start with failing tests before implementation:

- memory record validation for new fields
- recall reinforcement changes heat/count/timestamp
- locked memory does not decay
- low-heat unlocked episodic memory decays to tombstone
- project/conversation scoped search ranking
- consolidation creates daily/weekly/monthly summary records
- prompt-like and sensitive content remains rejected
- audit events are recorded for all new operations
- ContextBuilder receives ranked Hermes+ memories without leaking deleted/expired memories
- API permissions cover read/write/admin operations
- UI renders heat/lock/summary fields

## Deployment and verification

For this repository:

1. Keep the current production target as `prod-web-01`.
2. Run backend and frontend gates locally.
3. Deploy an incremental Hermes+ release to `prod-web-01`.
4. Run a real deployed probe for memory create/search/recall/lock/consolidation.
5. Push the completed source.
6. Check GitHub Actions until green.
7. Write the handoff.
8. Push final completed source to `Cube-agent` for later harness work.

## Explicit non-goals for this slice

- No open harness refactor in the original repository.
- No DeepSeek provider adapter overhaul in the original repository unless required to fix current CI.
- No pgvector/vector database dependency in the first Hermes+ slice.
- No AGPL source copying from kiwi-mem.
- No model-generated Dream summaries until deterministic summaries are stable.
