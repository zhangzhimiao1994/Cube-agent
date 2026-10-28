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
