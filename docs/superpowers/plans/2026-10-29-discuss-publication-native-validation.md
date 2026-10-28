# Discuss Publication Native Validation

> Execute inline with writing-plans and test-driven-development. No additional task or worktree.

**Goal:** Reject a Discuss report publication checkpoint that the actual AutoGen runtime cannot restore, while retaining legitimate nonterminal report checkpoints.

**Architecture:** Reuse AutoGen's existing complete `_validate_checkpoint` through the optional read-only publication interface. ConfigBackedDiscussion delegates only to its active child bound to the original tenant/run/execution token. Keep the existing report envelope, native cursor, budgets, private storage and DB transaction unchanged.

**Spec:** Current task instruction; integration base `8c55785ad4c4b5b7c8ac7c4792f2f986b183efc5`. Local only: no server, credentials, model service, CI, deployment or frozen-package changes.

- [x] RED: Generate actual AutoGen report events using existing offline gateway/capability fixtures. Mutate the legitimate nonterminal Main checkpoint to next_sequence=0 or timeout>3600, recompute its hash, and require rejection before any report carrier is emitted.
- [x] GREEN: Add native validator delegation and active-child tenant/run/token forwarding; call it before yielding the Discuss publication batch. Do not require terminal=True or reconstruct runtime instances.
- [x] Preserve valid report publication, prepared/committed/terminal cold recovery, unchanged report identity and no model/tool replay. Preserve legacy Discuss without selecting the new gate.
- [x] Exercise actual RunRepository SQL transactions with the existing SQLite type-adaptation harness: checkpoint INSERT failure rolls back the whole report batch; fresh sessions recover without duplicate public Main.
- [x] Run the dedicated file, one relevant regression group, Ruff and strict mypy. Review the diff, update local handoff and commit locally.

Review focus: forged checkpoint state/hash; wrong or missing execution identity; validation while the configured generator is paused; cleanup after execution; private evidence retained after public rollback. SQLite proves repository transaction behavior, not PostgreSQL concurrency or a real cross-process restart.

## Verification and limits

- Base: `8c55785ad4c4b5b7c8ac7c4792f2f986b183efc5`; all changes stay in this integration worktree.
- Initial RED: both actual nonterminal Main CP mutations failed with DID NOT RAISE in the publisher, while the native validator rejected each CP. Four control mutations (turns, soft_limit, hard_limit, extension_size) also reproduced publication accepting states rejected by `_DiscussionControl.from_state`.
- Additional SQL RED: a committed nonterminal Main followed by cold recovery published its identical report artifact twice. Deduplication now uses only the current run's exact durable checkpoint prefix, exact artifact equality and recomputed hash. Missing/foreign/changed prefix claims reject. No native cursor or checkpoint hash is rewritten.
- Dedicated first GREEN: 11 PASS. Final dedicated file has 15 cases, all included in the single completed related group: **237 PASS in 39.22s**. This total includes shared Direct/Hybrid and Crew publication, delivery followup policy, AutoGen report binding/engineering completion/artifact rollback, and five ConfigBackedDiscussion tests. Do not add overlapping counts.
- Ruff: three changed product files and both test files PASS. Strict mypy (`--strict --follow-imports=silent --no-incremental`): service.py, runtime/autogen/adapter.py, runtime/defaults.py PASS. `git diff --check` PASS.
- The prior hand-written Discuss CP test now uses real native model/tool/report/control evidence and the same three-event envelope. No validator was relaxed to accept the old incomplete fixture.
- ConfigBacked/Admission/Planned validation runs while the actual generator is paused at the nonterminal report CP. Wrong tenant/run/token, absent token, jointly forged checkpoint+context tenant, and validation after generator exit reject. Validation does not rebuild runtime or increase model calls.
- The existing `engineering_report_valid` construction/recovery checks remain unchanged. Publication additionally reuses both native state validation and the native control parser, without requiring terminal=True or adding work, permissions, retries or budget.
- The first related collection attempt hit an existing missing fixture: `tests/unit/runtime/autogen/test_capability_approval_resume.py` imports absent `test_ordered_tool_calls.py`. No tests executed in that attempt; that file was excluded and is explicitly not claimed. The remaining group completed as above; no fixture-restoration scope expansion.
- SQL tests use the existing actual RunRepository/SQLAlchemy SQLite type-adaptation harness and fresh sessions. Private artifacts survive in the existing in-memory native repository. No PostgreSQL concurrency, real process restart, server, credentials, paid provider, CI or deployment was exercised.

Validated product SHA256:

- service.py: `d51aa87acd136c3169ec45edfccce5a92a196478a0b4a5bbbf08dcdaa2ae811b`
- runtime/autogen/adapter.py: `789410875041df1416611e4f87798f5f863186a905a37e69cf52f7516622f482`
- runtime/defaults.py: `8a0988abc42081bf74d40775378bec258819d1f6c1941f456eb775691708dd95`
- Dedicated test: `dff20e8f41696e605386a27cd13d3aff241cb4d153fbef5624658e1882d191ba`
