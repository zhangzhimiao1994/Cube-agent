# Known incomplete receipts and terminal cache cleanup

## Problem and scope

The ordinary-user run7732 failed with confirmed post-tool incomplete receipts.
Readonly production checks found the existing failed-cleanup opt-in, no pending
approval, cleared worker fields and a certain route, but failed native-history
verification. The environment cache reached8/8. The verifier accepted only
completed structured-output rejections and therefore retained safe READY
environments for the newer known incomplete response classes.

Own only capabilities/python_project_lifecycle.py, its existing failed-hybrid
cleanup tests, and this plan. Other agents own runtime context and report traces.
Do not delete or move caches manually, change quotas/configuration, alter old
runs, backfill opt-ins, restore tasks, or call models.

## Decision

Recognize confirmed incomplete rejected model receipts only when the typed
evidence reports incomplete/incomplete, no final text, known usage, and one of
the native incomplete failure classes. Preserve the existing completed-output
rule. Keep the original checkpoint hash, tenant/run/model/provenance binding,
complete succeeded-call/tool reconciliation, persisted opt-in, approvals/lease
and route fences. The root broker independently retains its process/stage and
environment-reference checks. UNKNOWN and damaged/missing evidence still deny
cleanup. No rejected partial output becomes a successful model result.

## Verification

- [x] Two actual native-runtime fixture cases RED: root finalization and growth
  exhaustion had conclusive receipts but produced no lifecycle release.
- [x] Minimal receipt classification fix; both cases GREEN.
- [x]46 failed-hybrid cleanup cases pass, including9 new rehashed uncertainty,
  usage, provenance, reason, running model/tool and missing-receipt negatives.
- [x] Existing failed-environment/project-lifecycle adjacent cases pass.
- [x] Strict mypy for the changed source and Ruff for both Python files pass.
- [x] Noether independent static review PASS; parent commits only this slice.
- [ ] After a reviewed release, verify installed native history and the normal
  trusted lifecycle reclaim the eligible READY environment, with UNKNOWN and
  unsafe entries unchanged. This is not a whole product acceptance claim.

Production evidence remains metadata-only in
E:/code_x/sourceb4f0-parent-ops-oct30/cleanup-readiness.json. Local tests use a
recording cleanup executor; no production deletion has been performed here.
