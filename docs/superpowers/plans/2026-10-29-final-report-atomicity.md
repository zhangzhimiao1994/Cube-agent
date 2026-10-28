# Final Report Atomicity Implementation Plan

> **For agentic workers:** Use superpowers:executing-plans task by task. Keep this first slice limited to Direct and Hybrid; Crew reviewer publication is a separate slice.

**Goal:** Publish a current, bound Direct/Hybrid Main artifact and its matching recovery checkpoint in one existing RunService transaction.

**Architecture:** Extend the service publication batching boundary with mode-specific proof checks. Direct buffers its artifact/checkpoint pair. Hybrid buffers Main, in-stage checkpoint, end-stage checkpoint, and terminal parent checkpoint together; only the last is a safe recovery boundary. Preserve native validators, private storage, terminal-event ordering and Discuss batching. Never buffer model/tool intents.

**Tech Stack:** Python 3.12, immutable runtime contracts, pytest/pytest-asyncio, SQLAlchemy transaction boundary, Ruff, strict mypy.

**Spec:** The current-task root-cause review and first-slice authorization (2026-10-29); baseline `a10f273612c89d20de32d8677403e9fd3578be07`.

## Constraints

- Work only in this existing isolated worktree; no frozen 567 package edits, production/configuration/credentials, network, model calls, GitHub or CI.
- Preserve strict integer policy gates and legacy recovery. No Crew implementation or global report-policy propagation.
- Reject wrong run/tenant/hash, mismatched native checkpoint and incomplete Hybrid current-run child lineage before public Main publication.
- Reuse the existing per-batch database transaction. Completed status remains a subsequent event.

## Files

- Modify `src/agent_hub/runs/service.py`: dispatch Direct/Hybrid final publication envelopes into the existing transaction loop; keep Discuss behavior.
- Create `tests/unit/runs/test_final_report_atomicity_oct29.py`: real native fixture output, bounded envelope defects, service rollback and cold recovery, Discuss/legacy compatibility.
- Runtime producers may change only if actual event fixtures prove an unavoidable boundary defect; do not change Crew.

## Task 1: RED regressions

- [x] Capture real offline Direct and Hybrid report/checkpoint outputs using existing deterministic model/tool fixtures.
- [x] Assert Direct's two events and Hybrid's four events are one batch; missing Hybrid terminal parent checkpoint publishes no Main.
- [x] Assert wrong run/tenant/hash/native policy and Hybrid child/output-lineage mismatches fail closed.
- [x] Exercise the service's existing transaction boundary using real RunRepository/SQLAlchemy sessions and the project's SQLite type-adaptation harness: checkpoint INSERT failure rolls back Main/events/checkpoints; a fresh repository/session reconstructs the committed envelope for native cold recovery.
- [x] Pin Discuss and unselected legacy behavior unchanged. Observe Direct/Hybrid RED before their respective implementations; run unchanged-a10 transaction negative controls without modifying the source tree.

## Task 2: GREEN implementation

- [x] Add mode-specific validation/batching while leaving the Discuss path and transaction loop intact; reuse the already-read current-run event prefix for recovery evidence.
- [x] Bind Direct artifact ID/SHA/policy to its native completed checkpoint. Bind Hybrid's four-event envelope to the wrapped Direct checkpoint, current native lineage and terminal parent checkpoint (`terminal=True`, all stages finished, `child_checkpoint=None`).
- [x] Recover after the Hybrid envelope commit but before runtime.completed with remaining checkpoint token/time=0 and zero model/tool calls.
- [x] Run the focused regressions to GREEN; retain RED regressions for an unrestorable budget state and conflicting typed recovery identities before implementing their guards.

## Task 3: Verification and local commit

- [x] Run the new focused file plus existing Direct/Hybrid final-report, service followup-policy and relevant terminal tests.
- [x] Run Ruff and strict mypy on changed production code; review the finite diff and confirm no Crew/package changes.
- [x] Record actual counts/limitations in local handoff, update this checklist, and commit only the plan, product change and dedicated test file on a local branch. No push or deployment.

## Verification snapshot and limits

- Dedicated regression file: **33 PASS**. Direct RED: seven behavioral assertions; Hybrid terminal-envelope RED: thirteen behavioral assertions. Final recovery-identity RED also observed before its guard.
- Unchanged-a10 transaction negative controls: **2 expected failures**; Direct leaked `(events=1, Main=1, checkpoints=0)`, Hybrid leaked `(3,1,2)`. New implementation: all three counts zero after terminal checkpoint INSERT failure.
- Real repository/session rollback and committed-envelope cold recovery: **4 PASS**, included in the dedicated file. Hybrid cold recovery uses the unchanged original grant, spent equal to grant, and remaining token/time zero. No new budget or runtime relaxation.
- Adjacent Direct/Hybrid final-report and service followup-policy files: **34 PASS**; `test_terminal_hooks.py`: **70 PASS**. Ruff on both Python files and strict mypy on `service.py` PASS; `git diff --check` clean.
- SQL evidence uses real SQLite transactions with existing project type adaptation, not PostgreSQL. PostgreSQL locking/concurrency and the authenticated full RunService/worker journey are not verified. Hybrid private hydration uses the existing in-memory native artifact fixture; public persisted bytes come from real repository/session reconstruction.
- **Local, not deployed.** No frozen 567 package changes, network, secrets, models, GitHub or CI. Crew reviewer publication remains the second slice. Parent independent review is pending.
