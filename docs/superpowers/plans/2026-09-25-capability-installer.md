# Capability Installer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the full trusted capability installer loop before starting Hermes-agent improvements.

**Architecture:** Add a small `agent_hub.capability_installer` module for catalog models, planning, and install orchestration. Reuse existing admin plugin persistence, package approval metadata, plugin runtime manifest source, audit records, and frontend API client.

**Tech Stack:** FastAPI, Pydantic v2, existing admin resource service, runtime capability manifest, pytest, Vitest/React Testing Library, Playwright for UI smoke.

**Spec:** `docs/superpowers/specs/2026-09-25-capability-installer-design.md`

## Global Constraints

- Use tests before production changes.
- Do not support arbitrary URL/code install in this slice.
- Do not store or echo secret values.
- Fail closed: failed installs leave no enabled plugin unless it existed before and rollback restores it.
- Keep UI copy concise and Chinese-first.

---

### Task 1: Catalog And Planning Core

**Files:**
- Create: `src/agent_hub/capability_installer/__init__.py`
- Create: `src/agent_hub/capability_installer/catalog.py`
- Create: `tests/unit/capability_installer/test_catalog.py`

**Interfaces:**
- Produces `CapabilityCatalogEntry`, `CapabilityInstallPlan`, `CapabilityInstallRisk`.
- Produces `TrustedCapabilityCatalog.resolve(query)`.
- Produces deterministic plan ids and plugin request projection.

- [ ] Step 1: Write failing tests for catalog resolve and install plan projection.
- [ ] Step 2: Run focused pytest and verify RED.
- [ ] Step 3: Implement catalog models, validation, default trusted entries, query matching, and plan projection.
- [ ] Step 4: Run focused pytest and verify GREEN.

### Task 2: Backend API And Install Service

**Files:**
- Create: `src/agent_hub/capability_installer/service.py`
- Modify: `src/agent_hub/api/routers/admin.py`
- Test: `tests/api/test_admin_resources.py`

**Interfaces:**
- Produces:
  - `GET /api/v1/admin/capability-installer/catalog`
  - `POST /api/v1/admin/capability-installer/resolve`
  - `POST /api/v1/admin/capability-installer/plan`
  - `POST /api/v1/admin/capability-installer/install`
  - `POST /api/v1/admin/capability-installer/cancel`
  - `POST /api/v1/admin/capability-installer/rollback`
- Consumes existing plugin service methods.

- [ ] Step 1: Write failing API tests for resolve, plan, cancel, install, manifest visibility, and rollback.
- [ ] Step 2: Run focused pytest and verify RED.
- [ ] Step 3: Implement install service and route models.
- [ ] Step 4: Run focused pytest and verify GREEN.

### Task 3: Runtime Missing-Capability Suggestion

**Files:**
- Modify likely run/detail projection files after inspection.
- Test: `tests/api/test_admin_resources.py` or narrower runtime/run detail tests.

**Interfaces:**
- Produces a structured `capability_install_proposal` in run details when a missing capability matches the trusted catalog.

- [ ] Step 1: Write failing test for run detail projection containing a catalog-backed install suggestion.
- [ ] Step 2: Run focused pytest and verify RED.
- [ ] Step 3: Implement projection without changing execution policy.
- [ ] Step 4: Run focused pytest and verify GREEN.

### Task 4: Frontend API Client And Capability Installer UI

**Files:**
- Modify: `web/src/api/client.ts`
- Modify: `web/src/pages/McpPage.tsx`
- Test: `web/src/api/client.test.ts`
- Test: `web/src/pages/OperationalPages.test.tsx` or `web/src/pages/McpPage.permissions.test.tsx`

**Interfaces:**
- Produces typed client methods and a Chinese UI for catalog search, plan review, install/cancel/rollback, and status.

- [ ] Step 1: Write failing client and UI tests.
- [ ] Step 2: Run focused Vitest and verify RED.
- [ ] Step 3: Implement client schemas and UI section.
- [ ] Step 4: Run focused Vitest and verify GREEN.

### Task 5: Responsive UI And End-To-End Smoke

**Files:**
- Modify UI/CSS as needed.
- Test: browser/manual scripts as available.

**Interfaces:**
- Verifies mobile and desktop capability installer flows do not overflow, trap scrolling, or hide controls.

- [ ] Step 1: Start dev server.
- [ ] Step 2: Smoke test mobile viewport.
- [ ] Step 3: Smoke test desktop viewport.
- [ ] Step 4: Fix any layout issues and re-test.

### Task 6: Documentation, Handoff, Commit, Push, CI

**Files:**
- Modify: `docs/skills-and-mcp.md`
- Modify: `HANDOFF.md` if durable state changed.

**Interfaces:**
- Produces checkpoint commit and remote CI status.

- [ ] Step 1: Run backend and frontend focused verification.
- [ ] Step 2: Update docs/handoff.
- [ ] Step 3: Commit and push.
- [ ] Step 4: Check GitHub run; fix and repeat until pass or external blocker.

### Task 7: Hermes-Agent Improvement Intake

**Files:**
- Create a follow-up spec/plan after reviewing `NousResearch/hermes-agent`.

**Interfaces:**
- Starts only after Tasks 1-6 are complete and checked.

- [ ] Step 1: Browse/review upstream repository.
- [ ] Step 2: List absorbable improvements.
- [ ] Step 3: Convert accepted direction into local implementation tasks.
