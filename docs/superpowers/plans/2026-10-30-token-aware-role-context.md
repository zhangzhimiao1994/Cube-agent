# Token-aware role context

The deployed ordinary-user Hybrid maintenance run b4f0da98 failed at tester call5,
before transport, with model.context_window_exceeded. It had completed38 tools,
including code write, project verification and bundle. Necessary context reads
accumulated; equal argument hashes were across roles, not a same-role reread loop.
No completed-only product audit may run against this failed run.

The Crew byte watermark is not the gateway token estimate: JSON punctuation,
tools and schema cost tokens. Fit the exact request before recording its ledger
hash, using the same read-only estimator and authorized candidate windows as the
gateway. Keep task/instructions/schema/tools/permissions/output limit unchanged;
only omit oldest complete untrusted result exchanges, keep the latest exchange,
and include a digest pointing back to the unchanged artifact/checkpoint ledger.
No malformed native pairs, uncertain receipt replay, new provider calls, window
configuration changes, or fabricated verification.

1. Focused RED tests: punctuation-heavy feedback below the old byte limit but
   above tokens; exact estimator/schema overhead; native exchange boundaries;
   irreducible required instructions; unknown window and already-fitting identity.
2. Implement pure request compaction and gateway read-only input estimate.
3. Opt in fresh Crew checkpoints with a version; missing version preserves old
   request hashes. Integrate before request hash/ledger preparation, including
   reviewers. Test restored hashes and UNKNOWN/cancel/receipt guards.
4. Review and targeted adjacent checks once; then build and deploy a new isolated
   release. Preserve all old failed runs and their artifacts/claims. Only a
   genuinely changed-capability fresh user task can validate the next product
   delivery. No GitHub or CI.
