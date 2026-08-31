# Hermes Learning Chinese Summary Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a Chinese one-sentence learning summary to every Hermes+ learning record so users can understand what each conversation memory learned.

**Architecture:** Store `learning_summary_zh` in Hermes payloads and expose it through the admin API. For legacy records, generate a deterministic fallback during payload normalization. The frontend Hermes learning table displays this field as the primary user-readable summary while preserving existing `summary` and `lesson` for detail and machine use.

**Tech Stack:** FastAPI/Pydantic backend, SQLAlchemy JSON payload persistence, React + TanStack Query + Zod frontend, pytest and Vitest/Testing Library tests.

---

## File Map

- Modify `src/agent_hub/api/routers/admin.py`: add `learning_summary_zh` to Hermes request/response normalization and deterministic Chinese summary helpers.
- Modify `src/agent_hub/hermes/advisor.py`: write `learning_summary_zh` when Hermes records automatic run outcomes.
- Modify `web/src/api/client.ts`: parse `learning_summary_zh` in `HermesInsightSchema`.
- Modify `web/src/pages/HermesPage.tsx`: add “学习摘要” sorting/filtering/search/display and show it in detail.
- Modify `tests/integration/runs/test_recovery.py`: verify persistent Hermes outcome payloads include Chinese summaries.
- Modify `web/src/pages/OperationalPages.test.tsx`: verify Hermes page displays the Chinese summary column and value.
- Modify `HANDOFF.md`: local-only project handoff note after verification.

---

### Task 1: Backend API Compatibility for Chinese Summary

**Files:**
- Modify: `src/agent_hub/api/routers/admin.py`
- Test: `tests/integration/runs/test_recovery.py`

- [ ] **Step 1: Write the failing API compatibility test**

Add an assertion to an existing Hermes payload test that creates a legacy row without `learning_summary_zh`, reads it through the admin service/API path, and expects the returned response to have a generated Chinese sentence:

```python
assert response.learning_summary_zh.startswith("本次学习到：")
assert "dispatch" in response.learning_summary_zh or "planning" in response.learning_summary_zh
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
pytest tests/integration/runs/test_recovery.py::test_persistent_hermes_runtime_advice_uses_only_confirmed_lessons -q
```

Expected: failure because `HermesInsightResponse` has no `learning_summary_zh` attribute.

- [ ] **Step 3: Add the response field and fallback helper**

In `HermesInsightResponse`, add:

```python
learning_summary_zh: str
```

In `_hermes_response_from_payload`, read `learning_summary_zh`; if missing or blank, call `_hermes_learning_summary_zh(...)`.

Add helper:

```python
def _hermes_learning_summary_zh(
    *,
    category: str,
    outcome: str,
    lesson: str,
    tags: list[str],
    weight: int,
) -> str:
    cleaned_lesson = " ".join(lesson.strip().split())
    if len(cleaned_lesson) > 72:
        cleaned_lesson = cleaned_lesson[:69].rstrip() + "..."
    if category == "scheduler":
        prefix = "本次学习到：调度层"
    elif outcome == "failure":
        prefix = "本次学习到：这类失败需要"
    elif outcome == "neutral":
        prefix = "本次记录到："
    else:
        prefix = "本次学习到："
    if cleaned_lesson:
        return f"{prefix}{cleaned_lesson}"
    tags_text = "、".join(tag for tag in tags[:3] if tag)
    if tags_text:
        return f"{prefix}应关注 {tags_text}。"
    return f"{prefix}这条 Hermes 经验可用于后续调度判断。"
```

Keep the final sentence bounded and valid for old payloads.

- [ ] **Step 4: Run test to verify it passes**

Run:

```bash
pytest tests/integration/runs/test_recovery.py::test_persistent_hermes_runtime_advice_uses_only_confirmed_lessons -q
```

Expected: pass.

---

### Task 2: Automatic Hermes Outcome Records Persist Chinese Summary

**Files:**
- Modify: `src/agent_hub/hermes/advisor.py`
- Test: `tests/integration/runs/test_recovery.py`

- [ ] **Step 1: Write failing persistence assertions**

In `test_persistent_hermes_records_scheduler_notice_details`, add:

```python
assert "learning_summary_zh" in payload
assert str(payload["learning_summary_zh"]).startswith("本次学习到：")
assert "调度" in str(payload["learning_summary_zh"])
```

Add equivalent assertions to the non-scheduler Hermes outcome test if present.

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
pytest tests/integration/runs/test_recovery.py::test_persistent_hermes_records_scheduler_notice_details -q
```

Expected: failure because persisted payload does not include `learning_summary_zh`.

- [ ] **Step 3: Add minimal persistence implementation**

In `PersistentHermesRunAdvisor.record_outcome`, set:

```python
learning_summary_zh = _learning_summary_zh(
    category="scheduler",
    outcome="success" if outcome.status is RunStatus.COMPLETED else "failure",
    lesson=lesson,
    tags=tags,
    weight=weight,
)
```

Add it to payload:

```python
"learning_summary_zh": learning_summary_zh,
```

Add local helper with the same deterministic behavior as the API helper, or import a shared helper only if that avoids circular imports.

- [ ] **Step 4: Run tests to verify they pass**

Run:

```bash
pytest tests/integration/runs/test_recovery.py::test_persistent_hermes_records_scheduler_notice_details -q
pytest tests/integration/runs/test_recovery.py::test_persistent_hermes_runtime_advice_uses_only_confirmed_lessons -q
```

Expected: both pass.

---

### Task 3: Frontend Schema and Hermes Table Display

**Files:**
- Modify: `web/src/api/client.ts`
- Modify: `web/src/pages/HermesPage.tsx`
- Test: `web/src/pages/OperationalPages.test.tsx`

- [ ] **Step 1: Write failing frontend test assertions**

In the Hermes page test fixture, add:

```ts
learning_summary_zh: "本次学习到：需要辩论审查时优先使用讨论模式。",
```

Add assertions:

```ts
expect(await screen.findByText("学习摘要")).not.toBeNull();
expect(await screen.findByText("本次学习到：需要辩论审查时优先使用讨论模式。")).not.toBeNull();
```

- [ ] **Step 2: Run frontend test to verify it fails**

Run:

```bash
npm --prefix web test -- OperationalPages.test.tsx --runInBand
```

Expected: failure because the schema drops the new field or the table does not render it.

- [ ] **Step 3: Add schema field**

In `HermesInsightSchema`, add:

```ts
learning_summary_zh: z.string(),
```

- [ ] **Step 4: Render and filter by Chinese summary**

In `HermesPage.tsx`:

- Extend `HermesSortKey` with `"learningSummaryZh"`.
- Add a column filter key such as `learningSummaryZh`.
- Include `insight.learning_summary_zh` in global search.
- Add a table column labelled `学习摘要`.
- Display `insight.learning_summary_zh` in that column.
- Keep existing `summary` visible only if useful in detail, not as the main list explanation.

- [ ] **Step 5: Run frontend test to verify it passes**

Run:

```bash
npm --prefix web test -- OperationalPages.test.tsx --runInBand
```

Expected: pass.

---

### Task 4: Verification, Commit, Push, Deploy, Handoff

**Files:**
- Modify: `HANDOFF.md`

- [ ] **Step 1: Run targeted backend tests**

Run:

```bash
pytest tests/integration/runs/test_recovery.py::test_persistent_hermes_records_scheduler_notice_details -q
pytest tests/integration/runs/test_recovery.py::test_persistent_hermes_runtime_advice_uses_only_confirmed_lessons -q
```

Expected: pass.

- [ ] **Step 2: Run targeted frontend test**

Run:

```bash
npm --prefix web test -- OperationalPages.test.tsx --runInBand
```

Expected: pass.

- [ ] **Step 3: Run backend and frontend quality checks**

Run:

```bash
ruff check src tests
pytest tests/unit/hermes/test_advisor.py -q
npm --prefix web run lint
npm --prefix web run build
```

Expected: pass.

- [ ] **Step 4: Update local handoff**

Add a local-only `HANDOFF.md` entry with:

- Chinese summary field added.
- Tests run and results.
- Remaining future work: Hermes+ retrieval and injection is not part of this change.

- [ ] **Step 5: Commit and push**

Run:

```bash
git status --short
git add src/agent_hub/api/routers/admin.py src/agent_hub/hermes/advisor.py web/src/api/client.ts web/src/pages/HermesPage.tsx tests/integration/runs/test_recovery.py web/src/pages/OperationalPages.test.tsx
git -c user.name="zhangzhimiao" -c user.email="41898282+zhangzhimiao1994@users.noreply.github.com" commit -m "feat: add Hermes Chinese learning summaries"
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

Deploy the pushed source to `prod-web-01`, update `/opt/agent-hub/current`, restart services, and run a Hermes API smoke check proving `learning_summary_zh` appears in `/api/v1/admin/hermes`.

Expected: service health OK and smoke check returns Hermes records with Chinese summaries.

---

## Self-Review

- Spec coverage: the plan covers backend persistence, API compatibility, frontend display, tests, commit/push, CI, deployment, and local handoff.
- Placeholder scan: no open-ended implementation placeholders remain; each task names exact files and commands.
- Type consistency: the field name is consistently `learning_summary_zh` in Python payloads, Pydantic response models, Zod schema, and React rendering.
