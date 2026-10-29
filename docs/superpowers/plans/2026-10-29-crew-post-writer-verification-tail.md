# Crew post-writer verification tail

Baseline: integration `96b032cf36b77ab96723ffe89585f5689a311cff`.
The original RED stage added only this plan and a dedicated regression file.
The later authorized GREEN stage below changes only Crew delivery_report.py.
No deployment, paid call or failed-run replay is part of this slice.

## Observed failure and scope

The latest private checkpoint metadata has 38 terminal tools (37 succeeded,
one earlier failed verification). Its unique offending row is a succeeded
`project.verify` in `tester_step`, a descendant of the final engineering writer
`implementer_step` and an ancestor of the final synthesizer. Crew rejects it
because it is outside the writer/ancestor set and is not a readonly tool name.
The real receipt/source relationship is still being investigated separately.
Do not treat the metadata-only observation as proof of a valid receipt.

## Minimal GREEN design

Keep the readonly set unchanged. Add one narrowly scoped predicate for an
additional succeeded verification outcome in `delivery_evidence`. It must:

1. Be ordered after the unique final writer and before the final synthesizer by
   the validated plan DAG. Cross-step round numbers are not a global clock.
2. Resolve its exact native tool-result envelope using `_trusted_result`, including
   actor, arguments hash, artifact ID/SHA and source model linkage. No unresolved,
   UNKNOWN, partial, failed or artifact-free verification receives an exception.
3. Use `_successful_source(context=...)` and
   `original_contract_binding_matches` to validate its receipt against independent
   current-run context; require the final writer's already verified source and
   original contract. A self-consistent receipt body alone is insufficient.
4. Preserve all writer verification/bundle checks. Never replace the writer's
   selected verification or bundle with the tester's later receipt; a later
   verification may have a different execution ID/receipt digest.
5. Reject any post-writer write, other mutating/unknown action, inconsistent
   artifact or missing receipt. Keep every tail row in `tool_prefix_sha256` and
   report/checkpoint bindings. No prefix truncation or rebinding an old report.

No new request, model choice, fallback, authority, permission, budget or outer
step retry is part of this fix. Generic unknown diagnostics remain conservative;
only a verified predicate can distinguish the admissible tail from refusals.

## RED evidence

The dedicated tests build a valid native DispatchPlan with the same dependency
relationships and 38 terminal ledger positions. Python receipts come from the
existing local Python service fixture (fake executor, no model/server); all tool
and model artifacts are synthetic. They are not copies of private user content.
First validate the same writer/bundle without the extra tester verification so
that a fixture error cannot masquerade as the reported failure.

Expected RED: admissible tester verification is rejected at
`delivery report tool prefix is unresolved or unproven`. The report/full-prefix
positive must fail at the same guard. Negative cases retain source/contract/run,
receipt, ID/SHA/arguments/actor, DAG, write-tail and UNKNOWN refusal requirements.
Passing negative cases before GREEN only establish the conservative baseline.

After actual finite receipt evidence arrives, review the predicate's scope,
implement the minimum GREEN change, and run focused tests/Ruff/strict checks.
Independent review, deployment and a separately authorized new case are later
gates; never replay the consumed failed run.

## Authorized GREEN result and review boundary

Parent supplied the finite native receipt proof: both Python verifications pass
all three phases with 33 tests, equal source/recipe/environment and original
contract, and the bundle preserves the frozen test. That is metadata evidence;
the candidate's actual full private checkpoint/artifact proof is still pending.
The external metadata report has SHA256
`87744b689e33e109a85557585716911da5f2741f06b8d0e21fcdad13c5d46060`.

The first completed RED run was **2 failed / 15 passed**: both positive report
paths failed at the exact unresolved-or-unproven prefix guard, while the same
writer without the extra verifier passed. Windows sandbox socketpair hangs were
separately diagnosed and are not RED evidence; bounded offline reruns used a
working native asyncio runtime. No processes were left running for retries.

After parent review, two more positive cases first reproduced the refusal:
existing Node original-project verification and a role with unused write
permission. Their RED run was **2 failed / 1 passed**. The implementation now:

- Defers candidate successor checks until the original writer's native verify
  and bundle have been selected, then requires the strict pure predicate before
  returning any report phase. The readonly name set remains unchanged.
- Uses the existing Python contextual receipt validation and, for trusted Node
  original-input preparation v1, Node recovery's exact `_validate_receipt`.
  Node is selected by trusted preparation, not by a result's claimed kind.
- Matches original contracts and delivered source, independently validates both
  receipts, and retains the writer's receipt/bundle identity. Python also matches
  recipe/runtime/environment, Node matches the validated Node runtime.
- Does not reject unused configured capabilities; it still requires verify to
  be an allowed step tool. Actual tester writes and unresolved outcomes remain
  rejected by the full-prefix gate. No new step retry or paid/model call exists.

Final focused verification: **180 PASS**, including the dedicated **27 cases**,
existing Crew report/engineering gates, diagnostic classifications and existing
Node original-verification tests. Ruff and strict mypy on the changed source pass.
The dedicated fixture now also carries an actual typed failed verification from
the local service fixture. All executors/models remain synthetic.

Scope: original Python and original Node project maintenance. Plain/generated
project policies and other languages are unchanged; this is not a generic
permission to treat project.verify as readonly. Same-run provenance is supplied
by the caller's validated native checkpoint and tenant/run-scoped artifact
resolution; receipt shape/hash equality alone never establishes that provenance.

For the final same-server evidence check, use the full pure `delivery_evidence`
entry with CP407's validated plan/context/current-run artifact registry, rather
than calling only `_verified_successor_verification`: the complete entry also
checks writes/UNKNOWN, writer readiness, every envelope and the full-prefix hash.
Do not restore or run the consumed case. No real checkpoint candidate proof,
deployment or new acceptance case has been executed by this slice.
