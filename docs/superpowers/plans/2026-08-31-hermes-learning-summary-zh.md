# Hermes Learning Chinese Summary Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a Chinese one-sentence learning summary to every Hermes+ learning record so users can understand what each conversation memory learned.

**Architecture:** Reuse the existing `user_summary` field in Hermes payloads and expose it through the admin API. For legacy records, the backend already generates a deterministic fallback during payload normalization. The frontend Hermes learning table displays this field as the primary user-readable Chinese summary while preserving existing `summary` and `lesson` for detail and machine use.

**Tech Stack:** FastAPI/Pydantic backend, SQLAlchemy JSON payload persistence, React + TanStack Query + Zod frontend, pytest and Vitest/Testing Library tests.

---

## File Map

- Modify `web/src/pages/HermesPage.tsx`: rename the existing “一句话总结” column and filter label to “中文学习摘要”.
- Modify `web/src/pages/OperationalPages.test.tsx`: verify Hermes page displays the Chinese summary column and existing `user_summary` value.
- Modify `HANDOFF.md`: local-only project handoff note after verification.

---

### Task 1: Confirm Existing Backend API Compatibility

**Files:**
- Inspect: `src/agent_hub/api/routers/admin.py`
- Inspect: `src/agent_hub/hermes/advisor.py`
- Test: `tests/api/test_admin_resources.py`
- Test: `tests/integration/runs/test_recovery.py`

- [ ] **Step 1: Verify the existing API field and fallback path**

Confirm the code already contains:

```python
class HermesInsightResponse(BaseModel):
    user_summary: str
```

Confirm `_hermes_response_from_payload` falls back to `_hermes_user_summary(...)` when `user_summary` is absent or blank.

- [ ] **Step 2: Run backend compatibility tests**

Run:

```bash
.\.venv\Scripts\python.exe -m pytest tests/api/test_admin_resources.py::test_hermes_records_feedback_and_recommends_from_prior_lessons -q
.\.venv\Scripts\python.exe -m pytest tests/integration/runs/test_recovery.py::test_persistent_hermes_records_scheduler_notice_details -q
```

Expected: pass, proving backend already persists and returns Chinese user summaries.

---

### Task 2: Frontend Column Label

**Files:**
- Modify: `web/src/pages/HermesPage.tsx`
- Test: `web/src/pages/OperationalPages.test.tsx`

- [ ] **Step 1: Write the failing frontend test assertion**

In the Hermes page test, require the clearer column label:

```ts
expect(screen.getByRole("columnheader", { name: /中文学习摘要/ })).not.toBeNull();
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
npm --prefix web test -- OperationalPages.test.tsx
```

Expected: failure because the current column is labelled “一句话总结”.

- [ ] **Step 3: Rename the visible table and filter labels**

In `web/src/pages/HermesPage.tsx`, change:

```tsx
<SortHeader column="user_summary" label="一句话总结" ...>一句话总结</SortHeader>
```

to:

```tsx
<SortHeader column="user_summary" label="中文学习摘要" ...>中文学习摘要</SortHeader>
```

Also change the filter aria label from `按 Hermes 一句话总结筛选` to `按 Hermes 中文学习摘要筛选`.

- [ ] **Step 4: Run test to verify it passes**

Run:

```bash
npm --prefix web test -- OperationalPages.test.tsx
```

Expected: pass.

---

### Task 3: Verification, Commit, Push, Deploy, Handoff

**Files:**
- Modify: `HANDOFF.md`

- [ ] **Step 1: Run targeted backend tests**

Run:

```bash
.\.venv\Scripts\python.exe -m pytest tests/integration/runs/test_recovery.py::test_persistent_hermes_records_scheduler_notice_details -q
.\.venv\Scripts\python.exe -m pytest tests/integration/runs/test_recovery.py::test_persistent_hermes_runtime_advice_uses_only_confirmed_lessons -q
.\.venv\Scripts\python.exe -m pytest tests/api/test_admin_resources.py::test_hermes_records_feedback_and_recommends_from_prior_lessons -q
```

Expected: pass.

- [ ] **Step 2: Run targeted frontend test**

Run:

```bash
npm --prefix web test -- OperationalPages.test.tsx
```

Expected: pass.

- [ ] **Step 3: Run backend and frontend quality checks**

Run:

```bash
.\.venv\Scripts\python.exe -m ruff check src tests
.\.venv\Scripts\python.exe -m pytest tests/unit/hermes/test_advisor.py -q
npm --prefix web run lint
npm --prefix web run build
```

Expected: pass.

- [ ] **Step 4: Update local handoff**

Add a local-only `HANDOFF.md` entry with:

- Existing Hermes `user_summary` Chinese summary field confirmed and surfaced as “中文学习摘要”.
- Tests run and results.
- Remaining future work: Hermes+ retrieval and injection is not part of this change.

- [ ] **Step 5: Commit and push**

Run:

```bash
git status --short
git add docs/superpowers/specs/2026-08-31-hermes-learning-summary-zh-design.md docs/superpowers/plans/2026-08-31-hermes-learning-summary-zh.md web/src/pages/HermesPage.tsx web/src/pages/OperationalPages.test.tsx
git -c user.name="zhangzhimiao" -c user.email="41898282+zhangzhimiao1994@users.noreply.github.com" commit -m "fix: clarify Hermes Chinese learning summary label"
git push
```

Expected: push succeeds. Do not add `HANDOFF.md`.

- [ ] **Step 6: Check GitHub run status**

Run:

```bash
gh run list --limit 5
```

Expected: latest run for pushed commit passes. If it fails, fetch logs, fix, verify, commit, push, and repeat.

- [ ] **Step 7: Deploy runtime-affecting change to prod-web-01**

Deploy the pushed source to `prod-web-01`, update `/opt/agent-hub/current`, restart services, and run a Hermes API smoke check proving `user_summary` appears in `/api/v1/admin/hermes`.

Expected: service health OK and smoke check returns Hermes records with Chinese summaries.

---

## Self-Review

- Spec coverage: the plan covers existing backend persistence/API compatibility, frontend display, tests, commit/push, CI, deployment, and local handoff.
- Placeholder scan: no open-ended implementation placeholders remain; each task names exact files and commands.
- Type consistency: the field name is consistently `user_summary` in Python payloads, Pydantic response models, Zod schema, and React rendering.
