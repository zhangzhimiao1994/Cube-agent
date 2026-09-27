# Persistent Layered Memory Implementation Plan

**Spec:** `docs/superpowers/specs/2026-09-27-persistent-layered-memory-design.md`

**Goal:** Make PostgreSQL-backed layered memory the canonical source for both management and runtime context.

## Task 1: Extend the backward-compatible memory API contract

**Files:** `src/agent_hub/api/routers/admin.py`, `tests/api/test_admin_resources.py`, `web/src/api/client.ts`, `web/src/pages/MemoryPage.tsx`, frontend tests.

- Add failing API and UI tests for layer, category, confidence, actor ownership, and legacy defaults.
- Extend request/response schemas without breaking existing payloads.
- Default new user memories to the current actor and keep governed tenant memories explicit.
- Render and edit layer/category/scope fields with Chinese labels.
- Run focused backend/frontend tests, type checks, and commit.

## Task 2: Add the persistent scoped memory reader

**Files:** new `src/agent_hub/memory/persistent.py`, memory exports, focused unit/integration tests.

- Add failing tests for actor isolation, project/conversation filtering, legacy rows, deterministic ranking, top-N bounds, and recall reinforcement.
- Implement a typed projection over `AdminResourceRow(kind="memory")`.
- Update recall metadata transactionally and ignore invalid payloads safely.
- Run focused pytest, ruff, and strict mypy, then commit.

## Task 3: Inject explicit memory into every run mode

**Files:** `src/agent_hub/runs/service.py`, runtime memory-context helper and adapters, app/worker wiring, run/runtime tests.

- Add failing tests proving explicit direct/dispatch/discuss/hybrid submissions receive scoped memory without changing mode.
- Pass project and conversation identity into advice lookup.
- Attach explicit recall as `routing_decision.memory.items` before every submission branch; keep Hermes mode advice separate.
- Teach all runtimes to consume the new envelope while retaining legacy Hermes-envelope compatibility.
- Preserve bounded safe runtime envelopes and fail-open timeout behavior.
- Run focused and broad runtime suites, type checks, and commit.

## Task 4: Responsive acceptance and release closeout

- Run full backend/frontend quality gates and `git diff --check`.
- Verify the memory UI and a recalled-memory run on desktop and mobile.
- Deploy to `prod-web-03`, run a real persistence/restart/runtime probe, and remove probe data and old release artifacts.
- Push the branch, verify GitHub checks, update `HANDOFF.md`, and reconcile stale phase statuses in `task_plan.md`.

## Review Focus

- Cross-tenant or cross-user leakage.
- Explicit memories accidentally controlled by Hermes learning policy.
- Memory text changing permissions, tool availability, or selected mode.
- Legacy payload parsing and invalid-row containment.
- Recall writes blocking run submission or causing lock contention.
