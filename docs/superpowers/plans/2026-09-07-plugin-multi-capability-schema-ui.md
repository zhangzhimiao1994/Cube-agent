# Plugin Multi-Capability Schema UI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let operators edit multiple plugin capabilities and per-capability input/output schemas from `/mcp`.

**Architecture:** Keep backend resource contracts unchanged unless investigation finds a gap. Add frontend state helpers for capability rows, parse schema text into JSON objects/null, and reuse existing plugin save flow.

**Tech Stack:** React, TanStack Query, TypeScript, Vitest, Testing Library, FastAPI/Pydantic backend.

**Spec:** `HANDOFF.md` entry `2026-09-07 13:13 CST - prod-web-03 deployed 39471e8 plugin adapter schema catalog`.

## Global Constraints

- Communicate in Chinese by default.
- Use TDD: write failing tests before production code.
- Do not create new top-level Codex tasks for delegation.
- Update `HANDOFF.md` after completion.
- After GitHub push, verify CI and fix failures before deployment.

---

### Task 1: Multi-Capability Form State

**Files:**
- Modify: `web/src/pages/McpPage.tsx`
- Test: `web/src/pages/OperationalPages.test.tsx`

**Interfaces:**
- Consumes: existing `PluginResource` and `PluginCapability` client types.
- Produces: plugin save payload with `capabilities: PluginCapability[]`.

- [ ] **Step 1: Write failing UI test**

```typescript
it("saves multiple plugin capabilities with JSON schemas", async () => {
  // Navigate to /mcp, add a capability row, fill schemas, save,
  // then assert POST /api/v1/admin/plugins has two capabilities.
});
```

- [ ] **Step 2: Verify RED**

Run: `npm run test -- --run src/pages/OperationalPages.test.tsx -t "multiple plugin capabilities"`
Expected: FAIL because the page has no add-capability control.

- [ ] **Step 3: Implement minimal UI**

Add capability row state, add/remove buttons, and map rows into the save payload.

- [ ] **Step 4: Verify GREEN**

Run the same targeted test and confirm it passes.

### Task 2: Schema Parsing And Edit Backfill

**Files:**
- Modify: `web/src/pages/McpPage.tsx`
- Test: `web/src/pages/OperationalPages.test.tsx`

**Interfaces:**
- Consumes: `input_schema` / `output_schema` values from plugin resources.
- Produces: textareas that parse valid JSON object schemas or empty/null.

- [ ] **Step 1: Write failing edit-backfill test**

```typescript
it("loads existing plugin capability schemas into editable JSON fields", async () => {
  // Click edit, assert schema textareas contain JSON, save, and assert payload preserves objects.
});
```

- [ ] **Step 2: Verify RED**

Run: `npm run test -- --run src/pages/OperationalPages.test.tsx -t "capability schemas"`
Expected: FAIL because schema fields do not exist.

- [ ] **Step 3: Implement schema helpers**

Add `formatSchemaText`, `parseSchemaText`, and validation errors for invalid JSON or non-object values.

- [ ] **Step 4: Verify GREEN**

Run the targeted tests and confirm they pass.

### Task 3: Verification, Commit, Deploy

**Files:**
- Modify: `HANDOFF.md`

**Interfaces:**
- Consumes: local verification output and CI result.
- Produces: pushed commit and deployed prod release.

- [ ] Run frontend lint, targeted tests, full frontend tests, build, backend relevant tests, ruff, mypy, and `git diff --check`.
- [ ] Commit and push.
- [ ] Check GitHub Actions. Fix and repeat if needed.
- [ ] Deploy to `prod-web-03`.
- [ ] Run prod doctor, service checks, static asset checks, and OpenAPI/UI-relevant checks.
- [ ] Update `HANDOFF.md`.
