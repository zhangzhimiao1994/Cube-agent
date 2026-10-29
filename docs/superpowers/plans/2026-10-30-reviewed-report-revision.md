# Reviewed Report Revision Implementation Plan

> **For agentic workers:** Use superpowers:executing-plans in the current task; no subagent tool is exposed. The parent and Hooke provide independent review after implementation.

**Goal:** Let an explicitly opted-in Dispatch reporter revise an actual reviewed draft and deliver only the final approved natural-language report.

**Architecture:** Keep report policy1 and its hashes unchanged. A fresh policy1 run with an actually configured final reporter reviewer and positive retries writes a separate typed revision checkpoint marker; no new routing flag or default reviewer is required. Allow one no-tool reporter call per proven business revision. Validate every historical candidate against its own native model receipt and preserve all private ledger evidence. Publish only the final approved candidate atomically.

**Tech Stack:** Python 3.12, existing Crew runtime/ledger, pytest, strict mypy, Ruff.

**Spec:** Current user request: each configured role owns its task, resolves in-scope errors autonomously, and the final approved report describes actual verified delivery naturally. Do not change default routing or reviewer configuration.

## Global Constraints

- Exclusive scope: `src/agent_hub/runtime/crew/{adapter,delivery_report}.py`, `src/agent_hub/runs/service.py`, corresponding tests, this plan and local handoff.
- HEAD baseline `64fb7b80`; production c1e15 and run7732 remain untouched. No server, paid model, deployment, GitHub or generated-project changes.
- Old policy1/legacy checkpoints never gain revision authority from a refreshed route. No same-round second call, UNKNOWN replay, implicit recovery upgrade or renewed budget.
- Preserve all model/tool receipts and rejected drafts privately; preserve native scope/hash/provenance/feedback graph, current verify/bundle bindings, carrier integrity and final checkpoint transaction.
- Use root `.venv/Scripts/python.exe`, explicit active-worktree `PYTHONPATH`, and a fresh `E:/code_x/.tmp` pytest basetemp each run.

## Review Focus

- Rehashed cross-round candidate/model/reviewer/feedback substitutions must fail before publication or another paid boundary.
- A durable revision feedback checkpoint must resume without replaying the first writer/reviewer or tools.
- A second reporter prepared receipt may proceed; running without receipt stops; succeeded reuses its outcome and usage.
- No-review same-round framework duplication remains rejected; malformed opt-in and legacy route upgrades cannot authorize revision.
- Old draft bodies in model responses, review inputs and feedback chains remain private while intent/usage checkpoints remain durable.

## Task 1: Native revision and publication

**Files:** modify the three production files above, `tests/unit/runs/test_crew_report_publication_red_oct29.py`; add focused revision tests if needed.

**Interfaces:** consume existing model attempt encoding, review candidate/feedback graph and checkpoint state. Extend report binding with an explicit native receipt selector for revision-enabled callers; preserve the original call signature behavior for policy1. Persist an independent revision policy marker selected from the actual fresh plan without changing default routing.

- [x] Change `test_native_report_revision_keeps_the_single_model_attempt_boundary` into revise/approve success: 8 model calls, 3 tools, only approved second draft public, exactly one completion; run it and observe the existing guard RED.
- [x] Add typed fresh opt-in/checkpoint compatibility and one-call-per-native-business-revision admission, with historical candidate validation and final-only publication.
- [x] Add cold restore at feedback and second reporter prepared/running/succeeded boundaries; prove exact model counts, no tool replay, no renewed usage and UNKNOWN stop.
- [x] Add cross-round candidate/model/reviewer/feedback substitutions and opt-in/legacy/no-review duplicate regressions; retain private receipts and the approved final reference.
- [x] Run targeted tests and fix failures within scope; no old broad matrix.

## Task 2: Verification and local delivery

- [x] Run adjacent Crew report/publication/carrier, review cancellation and checkpoint-ledger tests; expand only for a new failure.
- [x] Run strict mypy on the changed production modules and Ruff on all changed Python files.
- [x] Self-review the complete diff for authorization, restore, budget, final publication and compatibility; record limitations honestly.
- [x] Update this checklist and concise local handoff, then commit only the owned paths locally; no push/deploy. Report actual RED/GREEN results and commit for parent/Hooke review.

## Verification and rulings

- Baseline: `64fb7b80cd7e51604426b9c5b99d0d833261a90c`; branch `fix/direct-hybrid-final-report-atomicity-oct29`.
- Actual initial RED: the updated revise/approve test failed at the original single-native-model-call guard (1 failed, 2.94s). The corresponding success path then passed with 8 native model receipts / 3 tools.
- Self-review RED: six stale/rehashed final-model carrier mutations and one relabelled recovery-attempt checkpoint each failed to raise (7 failed). These now reject using exact native model references and the revision receipt shape.
- Final verification: 111 passed in 75.61s across the new revision tests and adjacent Crew delivery/publication/carrier/reviewer-cancellation/ledger-capacity tests. New test file contains 23 cases. No broad matrix or external provider was run.
- Final strict mypy: no issues in the 3 changed production modules. Ruff: all 5 changed Python files pass. `git diff --check` passes.
- Ruling: fresh opt-in comes from policy1 plus the actual configured final reviewer and positive retries, not a manually injected routing marker. Missing stored revision marker stays0 even after route refresh. No factory/default-routing file was changed.
- Ruling: native restore already requires the matching `TaskContext.checkpoint`; preserve that identity guard rather than introduce a second restore API. Its negative regression proves zero additional model/tool calls.
- Final review: self-review (no subagent tool). Checked all three source diffs for admission proof, attempt encoding, UNKNOWN, usage, private history, per-candidate receipt selection, carrier integrity and atomic final publication. Parent and Hooke independent review remains pending.
- Deliberately outside this slice: adding reviewers to default plans, revision in Direct/Discuss/Hybrid final Main, deployment and live paid acceptance. Existing routing remains unchanged; production c1e15/run7732 were not accessed.
