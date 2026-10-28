# Crew Report Publication RED Evidence Plan

> **For agentic workers:** Use superpowers:executing-plans for this explicitly authorized RED-only slice.

**Goal:** Reproduce premature public Crew report bodies and rejection of a legitimate bound review candidate during recovery.

**Architecture:** Drive the native Crew adapter with offline model responses, the installed parser boundary, an in-memory artifact repository and owned temporary capability fixtures. Feed its real events through the current publication batching and public serialization boundaries. Prepared/running reviewer checkpoints must remain independently durable; report body carriers must wait for the native completed final-step checkpoint.

**Tech Stack:** Python, pytest, native Crew adapter, existing offline fixtures.

**Spec:** Current task authorization: tests and this plan only; no production implementation in this slice.

## Global Constraints

- Base commit `567c375b11ca46e244f411391cd575f57c168ed4`, whose tree equals a10; preserve this commit and all release packages.
- Work only on `crew-report-publication-red-oct29` in this isolated worktree.
- No server, credentials, network, real model, deployment or CI; no edits to the Direct/Hybrid worktree or production source.
- RED is the deliverable. Do not mark expected failures xfail or implement a fix to make them pass.

## Review Focus

- Native model_response text can expose the report before the candidate artifact.
- Candidate and review/step event inputs can expose the same report independently.
- Prepared/running/unknown reviewer states must not publish a final Main or erase model/tool ledgers.
- Reject/revise/approve alone are not the completed final-step recovery boundary.
- A legitimate delivery_report_binding must survive prepared-review candidate matching and cold recovery.

## Task 1: Native publication and recovery regressions

**Files:** Create `tests/unit/runs/test_crew_report_publication_red_oct29.py` only.

**Consumes:** Native `CrewDispatchRuntime.run`, `_delivery_publication_batches`, the real public event serializer, existing offline Crew/parser/capability fixtures.

**Produces:** Reproducible expected-behavior assertions that fail on this baseline for early public bodies, missing final atomic boundary and legitimate bound candidate recovery.

- [x] Generate a native report with a reviewer; verify the fixture's report binding and native final checkpoint before using it as evidence.
- [x] Assert each real body carrier is withheld from the public projection before completed final-step checkpoint; reviewer intent/receipt checkpoints remain observable.
- [x] Assert prepared/running/reject/revise/unknown prefixes do not publish a report; final completed CP releases one final report publication batch.
- [x] Assert native candidate matching accepts the legitimate bound report, and exercise the prepared-checkpoint recovery path without replaying writer/tools.
- [x] Run the dedicated file with the isolated tree's `src` first on `sys.path`, an owned `--basetemp`, no cache/bytecode; record expected assertion failures separately from fixture errors.
- [x] Run focused Ruff and report exact file SHA, branch/base and verification limitations. Stop before production changes.

## Verified RED handoff

- Final dedicated run: **12 failed, 1 passed in 9.10s**, exit 1, no setup errors/xfails. The passing control verifies the actual native binding, reviewer and completed final checkpoint. These are intentionally unresolved behavior regressions, not an implementation PASS.
- Four actual native body carriers reached public serialization before the completed CP: reporter model_response seq47, bound candidate seq49, review.completed.inputs seq55, step.completed.inputs seq57. They formed four singleton publication groups rather than one completed publication.
- Prepared/running reviewer checkpoints were emitted independently and retained three successful real capability tool-result ledger rows, but body withholding assertions failed. Reject/revise also leaked. The UNKNOWN fixture interrupts the provider boundary without a receipt; its latest actual native ledger retains one review row in `running` and no review.completed.
- Matching the legitimate candidate against its real report receipt returns False. Cold recovery reconstructs only the exact prepared checkpoint's hash-bound private registry, then fails with `runtime checkpoint review artifact lineage is invalid` before reviewer continuation. No fake checkpoint fields are used.
- Test SHA256: `21ad6c489ee88c0891e00efdb1df2a30e43e0a80fcf5be7eee6ad4251ffa6e47`.
- Ruff dedicated file: PASS. `git diff --exit-code 567c375b -- src`: exit 0. No production edits, packaging changes or external execution.
- The Windows sandbox test process stalled and was interrupted; it is not counted. Successful fixture/RED verification used local execution outside that sandbox, with the fixture's socket/HTTP network blockers active. Capability execution uses the existing fake Executor inside an owned temporary project; no actual Node/npm command or model runs.

Reproduction from this worktree in PowerShell (use a fresh owned basetemp):

```powershell
$env:PYTHONPATH = "$PWD/src;$PWD"
& E:/code_x/Mofang-agent/.venv/Scripts/python.exe -B -m pytest tests/unit/runs/test_crew_report_publication_red_oct29.py -q -p no:cacheprovider --basetemp=E:/code_x/crew-report-red-oct29-reproduce-01 --tb=line
```

Implementation follow-up must keep native reviewer/model/tool intent and receipt checkpoints durable, withhold every body carrier, publish only at the native completed final-step CP, and validate bound candidates without accepting arbitrary extra content. Transaction rollback, PostgreSQL concurrency and post-fix cold-replay idempotence are not proven by this RED-only slice.

## Independently authorized GREEN stage

The subsequent parent instruction explicitly authorizes implementation in the integration worktree `structured-recovery-oct28`, now based on `43149a96` (Direct/Hybrid through `1f321ca4`). The earlier RED-only scope above remains the historical evidence boundary, not an implementation acceptance claim. Preserve its result and original test SHA.

**Goal:** Make the native Crew publication and legitimate prepared-review recovery regressions pass without changing model calls, tools, authority or budgets.

**Minimal boundaries:** Crew adapter and report binding validators; RunService publication batching; the dedicated Crew regression file. Extend existing configured-wrapper forwarding only if the actual integration requires it. Preserve all Direct/Hybrid and Discuss behavior.

- [x] Keep candidate/model bodies in the existing private artifact repository; pass reviewer/model intent, receipt, usage and nonterminal checkpoints independently.
- [x] Withhold every selected report body carrier, including event inputs/output previews, until native reviewer approval and the completed final-step checkpoint have been validated against current private ledgers and exact scope.
- [x] Publish final report events and terminal CP through one existing RunRepository transaction. Reject/failure/uncertain and incomplete prefixes release no report.
- [x] Strictly validate legitimate bound review candidates using current native report/model/tool evidence; reject arbitrary extra content, wrong scope/hash and forged binding. Preserve prepared/running recovery and no-replay rules.
- [x] Add real repository insertion-failure rollback and committed-boundary cold-recovery/no-duplicate coverage, plus malformed binding/reviewer evidence regressions.
- [x] Run dedicated RED-to-GREEN verification, then one relevant regression group, Ruff and strict mypy. Record actual results and limitations separately from historical RED; commit locally only.

No upload, deployment, server, credentials, real model, CI or frozen-567-package changes are authorized.

### GREEN verification and scope

- Dedicated file: **28 PASS**. One subsequent related regression group: **301 PASS in 40.46s** (dedicated Crew, shared Direct/Hybrid publication, Crew delivery/verification/reviewer cancellation and recovery, configured runtime tests). No full-suite claim.
- Ruff on all five changed production files and the dedicated test: PASS. `mypy --strict --follow-imports=silent --no-incremental` on the five changed production files: PASS, no issues.
- The original twelve RED cases pass. New regression evidence first reproduced native-valid plan IDs rejected by the added matcher regex, a swallowed runtime.completed, silent incomplete EOF and a missing private model artifact in prepared-to-public recovery. Native plan identifier validation is now reused.
- Fixes stay within DISPATCH publication; Direct/Hybrid and Discuss paths remain separate. ConfigBackedDispatch now forwards native validation through owned tenant/run/token binding, admission/planned/preseed wrappers, without rebuilding or restoring work.
- Prepared CP persisted with no public report, lost in-memory pending, new runtime/repository sessions, then reviewer continuation: the original report/model artifacts are publicly published with the same ID/SHA exactly once. Rollback after final CP insertion failure preserves earlier receipt/usage CPs and private evidence; recovery from that latest running CP completes without another model/tool call. Recovery after a committed terminal CP emits only runtime.completed.
- Real RunService execution through the configured/admission/planned/preseed/native chain ends COMPLETED with one completion event and one report artifact. Missing validated terminal CP cannot authorize runtime.completed; incomplete EOF raises a stable error.
- DB event sequences are reassigned contiguously on publication. Native CP next_sequence and state hash remain unchanged; the former is a runtime cursor, not the public DB event number.
- Native final-report attempt/index remain exactly zero. Reviewer revision cannot add a report model call; mismatched candidates fail closed. No additional retries, fallback, authority or budget were introduced.
- Two fixture corrections are explicit: SQLite schema needed RunStepRow because the whole native stream now persists step.completed; post-final recovery batching must receive the restored terminal-CP context, as real RunService does. Neither correction weakens the product gates.
- Limits: SQLite exercises actual RunRepository SQL/transactions, not PostgreSQL concurrent isolation. New runtime instances and exact private hash-bound registries model restart, but no real OS-process/private-PostgreSQL restart or live cancellation timing was executed. No server/model/CI or frozen release package was touched.
