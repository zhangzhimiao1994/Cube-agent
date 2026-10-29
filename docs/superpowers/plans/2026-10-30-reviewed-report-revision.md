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

## Follow-up P2: preserve public review/retry trace

- [x] Reproduce Hooke's terminal-filter finding with the real revision fixture: public revise/approve metadata plus one reporter retry, without old draft or feedback body.
- [x] After unchanged native graph/hash validation, preserve whitelisted trace metadata after its succeeded native receipt checkpoint; final report bodies retain the terminal atomic group. Use the UI's existing `decision.*` representation for review metadata with exact native event/candidate/model references; never fabricate a native REVIEW_COMPLETED with empty inputs or a replacement Artifact. Keep native records private and unchanged.
- [x] Verify cold restore reuses model/tool receipts and exposes only correctly attributed newly emitted trace, without replaying historical trace; verify completed restore does not duplicate review/retry records.
- [x] Run the P2-focused tests, necessary neighboring publication/projection checks, strict mypy and Ruff; self-review and local follow-up commit only. No server/deploy/model/GitHub action.

### P2 follow-up evidence

- Independent finding: final-only filter suppressed two reviewer verdicts and the reporter revision retry. Actual RED: the real fixture produced no public verdict metadata instead of revise/approve (1 failed, 3.21s).
- Only source change is `runs/service.py`: after the unchanged full native terminal graph/hash checks, project native reviews as `decision.report_review` metadata and retain sanitized `step.retrying`. Existing UI recognizes `decision.*` verdict records and `step.retrying` intent records. Extension-event actor/step identity stays in payload as required by the existing contract.
- Metadata contains a projection label, native event kind/sequence, exact model call key, candidate ID/hash, reviewer model ID/hash and business attempt. Native inputs, candidate/model artifacts and raw feedback are unchanged and private. No fabricated native REVIEW_COMPLETED or replacement Artifact is created.
- Verify native actor/verdict/retry against the linked actual reviewer receipt, including the existing fenced-JSON format. No free-text model feedback enters the projection. The initial terminal-only metadata scheduling was insufficient for crash durability and is superseded by the correction below; final report bodies still use the terminal atomic group.
- Final P2 + necessary adjacent carrier/transaction/restore/rejection verification:46PASS (23.19s); P2 metadata/trace/restore focus11PASS (7.66s). Strict mypy service1 file PASS, Ruff service/test2 files PASS, whitespace check PASS.
- Cold restore tests cover reporter feedback/prepared/running/succeeded/completed and second-reviewer prepared/succeeded. Successful receipts are reused, trace candidate/call identity is preserved, the initial checks covered only resumed segments, not full merged history; actual prefix-persist/crash/restore verification below is required. Completed recovery adds no duplicate trace. Verdict/actor/retry relabels are rejected.
- Final self-review: no remaining blocking finding within this P2. Follow-up independent review by parent/Hooke is pending. Only service, this plan and the corresponding revision test file enter the follow-up commit; adapter/delivery_report are parent-owned and unchanged here.
- Parent reports run7732 FAILED at19:32:19 due to tester post-tool finalization growth exhaustion, before its20:03 deadline. Parent/Hooke own that separate diagnosis; no growth, model, routing, deployed pins, server or paid-run action was taken here.

## Trace durability correction

- [x] RED: persist actual SQL/public prefix, crash after first review/retry/second reporter prepared checkpoint, cold restore and inspect the merged DB/public history. Require revise+approve and one retry without duplicates or OLD; a recovery-only approve assertion is insufficient.
- [x] Release whitelisted non-authorizing metadata once its actual succeeded reviewer receipt checkpoint verifies candidate/model hashes and ownership. Keep final approved report bodies under the unchanged terminal atomic gate. Reuse existing service recovery_events for stable model-call/candidate-based deduplication, never invent historical event coordinates or add an adapter API.
- [x] Run focused durability/trace and necessary carrier/transaction neighbors, strict service mypy/Ruff, self-review and local scoped follow-up commit. Adapter/delivery/growth and release ops remain parent/Hooke-owned and untouched.

### Durability evidence and integration handoff

- Actual RED: all3 SQL/public prefix crash cases (review/retry/second prepared) lacked the already-occurred first revise metadata (3 failed,4.97s).
- After correction: the complete focused revision test file35PASS (21.23s), including real SQL prefix persistence and public history merge. After the local typing/import cleanup, the6 core durability/delayed-receipt/conflict cases were rerun:6PASS (7.25s). Ruff on owned service/test2 files PASS.
- Persisted merged history contains exactly revise+approve and one reporter retry. No OLD/feedback body is public, no first-round model/tool is repeated, final report/body checkpoint remains atomic, DB sequences are contiguous, and native checkpoint hash/cursor stays unchanged.
- Receipt artifacts may arrive before their succeeded checkpoint: metadata waits and is committed with the delayed receipt checkpoint when necessary. Receipt/candidate recomputed hashes, owner, provenance, native verdict and checkpoint scope/plan hash are checked before projection.
- Existing service recovery_events supplies the committed prefix. Dedup uses event type plus native model call key and compares candidate/model hashes and all semantic fields; only source-event/native-vs-DB sequence differences are ignored. Original persisted coordinates are retained. Conflicting verdict/candidate hash is rejected; restored payloads grant no authorization or delivery success.
- The review-prefix crash test specifically covers a metadata event committed after the latest receipt checkpoint: cached native review emission on restore is suppressed by stable receipt identity, not by sequence. Retry/second-prepared crash tests preserve all already-persisted trace. Completed restore does not add trace.
- Final self-review: no remaining in-scope blocker; no adapter API or code was added. Parent/Hooke independently review the scoped service/tests/plan commit. Peer adapter/context_window growth changes remain untouched and excluded.
- A strict service scan initially found one JSON-union indexing site; it was corrected with an explicit Mapping cast. Per parent coordination, final combined source strict mypy and adjacent regression are reserved for the parent once both peers stabilize; no repeated111-case matrix was run here.
