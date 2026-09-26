# Team Skill Tap Revisions Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the Hermes team Skill Tap lifecycle with usable offline snapshot import, immutable source revisions, atomic activation, and rollback.

**Architecture:** Extend the existing tenant-scoped `skill_source` and generic admin-resource storage instead of creating a parallel plugin system. Every remote sync or trusted snapshot import creates an immutable revision that maps source paths to scanned Skill version IDs; source activation atomically switches the existing active-version map only after every member is approved and its package hash is intact.

**Tech Stack:** FastAPI, Pydantic v2, SQLAlchemy/PostgreSQL JSONB, React, TanStack Query, Zod, Pytest, Vitest, Playwright.

**Spec:** `docs/superpowers/plans/2026-09-26-active-run-queue-control.md` Task 9 and the approved Hermes team Skill Tap requirement.

## Global Constraints

- Reuse the existing scan, quarantine, approval, version activation, trust, audit, and tenant-isolation paths.
- Never enable a synchronized Skill automatically.
- Snapshot import requires a trusted enabled source, a 40-character commit SHA, and an archive whose SHA-256 matches the source pin when configured.
- Source deletion preserves immutable revisions and installed Skill provenance.
- Production has no direct GitHub route; snapshot import must be a real supported fallback, not a placeholder.

## Review Focus

- A source changed or had trust revoked during sync/import must not publish a revision.
- A revision containing any unapproved or hash-mismatched Skill must not partly activate.
- Re-importing identical content must preserve source/revision provenance without duplicating executable versions.
- Rollback must restore the entire previous active-version mapping, including removal of Skills introduced only by the newer revision.
- Cross-tenant revision IDs and source IDs must remain inaccessible.

---

### Task 1: Trusted Snapshot Import

**Files:**
- Modify: `src/agent_hub/skills/sources.py`
- Modify: `src/agent_hub/api/routers/admin.py`
- Test: `tests/unit/skills/test_sources.py`
- Test: `tests/api/test_admin_resources.py`

**Interfaces:**
- Produces: `snapshot_from_archive(request, commit_sha, archive_bytes) -> SkillSourceSnapshot`
- Produces: `POST /api/v1/admin/skill-sources/{source_id}/import-snapshot`

- [ ] Write failing tests for commit/hash validation, subdirectory filtering, trust changes, tenant isolation, and successful candidate import.
- [ ] Run focused tests and confirm failures are caused by the missing import interface.
- [ ] Implement the minimal archive snapshot helper and multipart API through the existing scan/quarantine path.
- [ ] Run focused backend tests to green.
- [ ] Commit the snapshot-import slice.

### Task 2: Immutable Tap Revisions And Atomic Activation

**Files:**
- Modify: `src/agent_hub/api/routers/admin.py`
- Test: `tests/api/test_admin_resources.py`
- Test: `tests/integration/skills/test_lifecycle.py`

**Interfaces:**
- Produces: source revision response/item models stored as `skill_source_revision` admin resources.
- Produces: list/detail/activate/rollback endpoints below each Skill source.

- [ ] Write failing tests for revision creation, same-content provenance, approval gating, atomic activation, rollback, audit, source deletion retention, and concurrent source changes.
- [ ] Run focused tests and confirm expected failures.
- [ ] Persist a revision only after the whole scan succeeds; activate under one source advisory lock and one database transaction.
- [ ] Run focused and PostgreSQL integration tests to green.
- [ ] Commit the revision lifecycle slice.

### Task 3: Team Tap Management UI

**Files:**
- Modify: `web/src/api/client.ts`
- Modify: `web/src/api/client.test.ts`
- Modify: `web/src/pages/SkillsPage.tsx`
- Modify: `web/src/pages/SkillsPage.test.tsx`
- Modify: `web/src/styles.css`
- Test: `web/e2e/admin.spec.ts`

**Interfaces:**
- Consumes: snapshot import and revision APIs from Tasks 1-2.
- Produces: bounded responsive revision history, snapshot upload, approval readiness, activate, and rollback controls.

- [ ] Write failing client/component tests for upload, history, blocked activation, activation, rollback, loading, and error states.
- [ ] Run Vitest and confirm expected failures.
- [ ] Implement typed client methods and the Chinese management UI without nested cards or mobile overflow.
- [ ] Run Vitest and desktop/mobile Playwright to green.
- [ ] Commit the UI slice.

### Task 4: Deployment And Real Acceptance

**Files:**
- Modify: `docs/skills-and-mcp.md`
- Update local: `HANDOFF.md`

**Interfaces:**
- Consumes: Tasks 1-3.
- Produces: deployed, reversible, audited production lifecycle.

- [ ] Run full backend, Mypy, Ruff, Vitest, build, and Playwright suites.
- [ ] Push and require a successful GitHub quality run.
- [ ] Deploy the exact revision and verify API, worker, Caddy, migration, and revision.
- [ ] With the `test` account, create and trust a source, import a real non-placeholder archive snapshot, approve all candidates, activate revision 1, import/approve/activate revision 2, rollback, verify runtime visibility and audit, then remove probe records.
- [ ] Verify desktop/mobile UI, clean server temp files and old releases, and record only the durable result in `HANDOFF.md`.
