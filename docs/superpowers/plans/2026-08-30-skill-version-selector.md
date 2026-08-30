# Skill Version Selector Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Collapse duplicate skills by name in the main Skills UI/API while preserving same-name uploads as selectable secondary versions, and make same-name uploads ask whether to overwrite the current version or save as a new version.

**Architecture:** The backend returns one current `SkillResponse` per skill name and attaches same-name historical rows as version metadata. A new activation endpoint records the selected version so runtime/admin listing uses one active row per name. Upload accepts an explicit same-name strategy (`overwrite` or `new_version`); if omitted and a same-name skill exists, the API returns a conflict response that the frontend turns into a confirmation choice. The frontend renders a compact version selector under each skill row instead of showing duplicate top-level rows.

**Tech Stack:** FastAPI, Pydantic, SQLAlchemy async, React/Vite/Vitest.

---

### Task 1: Backend contract for grouped skill versions

**Files:**
- Modify: `src/agent_hub/api/routers/admin.py`
- Test: `tests/api/test_admin_resources.py`

- [x] **Step 1: Write failing tests**

Add tests proving:

```python
async def test_persistent_skill_list_groups_same_name_versions_and_defaults_latest(...)
```

Expected behavior:
- two DB skill rows with the same `name` return one top-level item;
- returned item has the latest row's `id`;
- returned item includes `versions` for both rows, newest first;
- legacy rows missing `content_sha256`, `package_version_id`, or `source_filename` are safely enriched from their archive file when present.

- [x] **Step 2: Verify RED**

Run:

```powershell
E:\code_x\agent-hub\.venv\Scripts\python.exe -m pytest tests\api\test_admin_resources.py -k "skill_list_groups" -q -p no:cacheprovider
```

Expected: fails because the response model has no `versions` and list still returns duplicate rows.

- [x] **Step 3: Implement minimal backend grouping**

Add an optional `SkillVersionResponse` model and `versions` / `current_version_id` fields to `SkillResponse`. Update persistent `list_skills()` to load DB rows with timestamps, group by `name`, select active/latest, and return one item per name with version metadata.

- [x] **Step 4: Verify GREEN**

Run the same targeted pytest command; expected pass.

### Task 2: Version activation endpoint

**Files:**
- Modify: `src/agent_hub/api/routers/admin.py`
- Test: `tests/api/test_admin_resources.py`

- [x] **Step 1: Write failing tests**

Add tests proving:

```python
async def test_persistent_skill_version_activation_selects_requested_version(...)
```

Expected behavior:
- selecting an existing version for a skill name changes the current top-level item;
- selecting a version whose name does not match the current skill group returns 404 or 409;
- selected version survives subsequent list calls.

- [x] **Step 2: Verify RED**

Run targeted pytest; expected failure because endpoint/state does not exist.

- [x] **Step 3: Implement minimal activation state**

Store active version selection in an existing admin `setting` payload keyed by skill name to avoid a schema migration. Expose a route such as `POST /api/v1/admin/skills/{skill_id}/versions/{version_id}/activate`.

- [x] **Step 4: Verify GREEN**

Run targeted pytest; expected pass.

### Task 3: Frontend secondary selector

**Files:**
- Modify: `web/src/api/client.ts`
- Modify: `web/src/pages/SkillsPage.tsx`
- Test: `web/src/pages/SkillsPage.test.tsx`

- [x] **Step 1: Write failing tests**

Add a test proving duplicate same-name skills render as one row with a version selector and selecting a secondary version calls the activation endpoint.

- [x] **Step 2: Verify RED**

Run:

```powershell
cd web
npm test -- --run src/pages/SkillsPage.test.tsx -t "skill versions" --reporter=dot
```

Expected: fails because the UI does not render the selector.

- [x] **Step 3: Implement compact selector**

Render the current skill as the top-level card/row. If `versions.length > 1`, render a secondary `<select>` or compact dropdown showing upload time, status, and short hash/source filename. On change, call the activation endpoint and refresh skills.

- [x] **Step 4: Verify GREEN**

Run targeted Vitest; expected pass.

### Task 4: Same-name upload choice

**Files:**
- Modify: `src/agent_hub/api/routers/admin.py`
- Modify: `web/src/api/client.ts`
- Modify: `web/src/pages/SkillsPage.tsx`
- Modify: `web/src/pages/RunsPage.tsx`
- Test: `tests/api/test_admin_resources.py`
- Test: `web/src/pages/SkillsPage.test.tsx`
- Test: `web/src/pages/OperationalPages.test.tsx`

- [x] **Step 1: Write failing tests**

Backend test:

```python
def test_skill_archive_upload_same_name_requires_strategy(...)
```

Expected behavior:
- uploading a changed same-name skill without a strategy returns `409 skill_version_choice_required`;
- uploading with `strategy=overwrite` replaces the current version row/archive;
- uploading with `strategy=new_version` keeps the previous row and creates a selectable version.

Frontend test:

```tsx
it("asks whether to overwrite or save a new version when uploading the same skill name", ...)
```

Expected behavior:
- conflict response opens a compact confirmation UI;
- choosing overwrite re-submits with the overwrite strategy;
- choosing new version re-submits with the new-version strategy.

- [x] **Step 2: Verify RED**

Run targeted pytest/Vitest; expected failure because upload choice is not implemented.

- [x] **Step 3: Implement minimal upload choice**

Add an upload strategy parameter via query string or form field. The frontend should not rely on browser-native confirm; use the app's existing notification/modal pattern.

- [x] **Step 4: Verify GREEN**

Run targeted pytest/Vitest; expected pass.

- [x] **Step 5: Cover secondary upload entry points**

Chat composer Skill install now handles the same `409 skill_version_choice_required` response with an in-app overwrite/new-version choice. Feishu `/skill install` reports the conflict and sends the user to Web management for the explicit choice.

### Task 5: Verification, deploy, and production audit

**Files:**
- Modify: `HANDOFF.md`, `progress.md`, `findings.md`

- [x] **Step 1: Run local gates**

Run targeted backend/frontend tests, ruff, mypy, frontend lint/build, and `git diff --check`.

- [x] **Step 2: Commit and push**

Commit with repository identity, push to GitHub, then check GitHub Actions until green.

- Completed commits:
  - `a2cf1b3 Prompt chat skill upload version choice`
  - `799e722 Expose skill upload strategy in OpenAPI`
- GitHub Actions:
  - `33309084699` for `a2cf1b3` -> success.
  - `33309489572` for `799e722` -> success.

- [x] **Step 3: Deploy to `prod-web-01`**

Deploy release, verify `/health`, services, and the Skills API response shape.

- Current production release:
  - `/opt/agent-hub/releases/20260830-194500-skill-upload-strategy-schema`
  - `agent-hub-api`, `agent-hub-worker`, and `caddy` active.
  - `/health` returned `{"status":"ok"}`.

- [x] **Step 4: Production validation**

Confirm production skill main list count equals unique names, duplicate historical rows appear only inside `versions`, and no destructive deletion is needed for this pass.

- OpenAPI probe confirmed `SkillResponse` exposes `current_version_id`, `versions`, `source_filename`, `package_version_id`, and `content_sha256`; upload exposes the `strategy` query parameter; activation endpoint returns `SkillResponse`.
- Runtime model probe confirmed the deployed `SkillResponse` has the version fields.
- Frontend bundle probe confirmed the chat upload choice markers are present.
- Production DB read-only summary confirmed 446 raw skill rows, 104 unique skill names, 94 duplicate name groups, and 342 historical version rows. These historical rows are retained for secondary version selection; no destructive DB cleanup was performed.
- Deployment temporary files and release dist backups created by this pass were removed after verification.
