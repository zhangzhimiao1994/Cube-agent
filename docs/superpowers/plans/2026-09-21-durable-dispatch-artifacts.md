# Durable Dispatch Artifact Recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans task-by-task.

**Goal:** Resume nonterminal dispatch in a fresh worker with exact persisted outputs and ordered initial inputs.

**Architecture:** Private PostgreSQL ArtifactRepository behind the existing protocol; explicit Crew v9 input refs; API/worker assembly injection. Public artifact publication stays in the existing lease-checked event transaction.

**Tech Stack:** Python, SQLAlchemy async, PostgreSQL, Alembic, pytest, Crew.

**Spec:** `docs/superpowers/specs/2026-09-21-durable-dispatch-artifacts-design.md`

## Global Constraints

- No public orphan/private snapshot exposure, no public-table recovery fallback.
- Match existing limits, CAS, write ownership, abort and cancellation semantics.
- Preserve harness core, permissions, model routing and actual usage accounting.
- Scope: dispatch hydration only. DB-ack, discussion and Hybrid recovery are separate.
- No provider credentials, endpoints, local archives or handoff in commits.

## Current Verification

- Storage, v9 input snapshots and production wiring are implemented but not released.
- Isolated PostgreSQL: 37 passed, including fresh service/pool/runtime/repository
  recovery, exact original inputs and zero repeated completed worker calls.
- The recovery fixture blocks consumption after a real checkpoint transaction
  commits, then cancels the old execution. It does not fabricate events or claim
  a physical process crash. A separate test retains the non-replayable-event guard.
- Final synthesis consumes restored worker output, not raw root history by design.
  Verify prior-history evidence in that output and exact private root snapshots
  independently; do not change routing just to satisfy the test.
- Review found non-text root inputs misclassified as internal ledger artifacts;
  fixed with explicit input refs and forged-input rejection regressions.
- Enriched runtime failure diagnostics now have a strict allowlisted contract;
  invalid copied events reject before INSERT. Regression RED: 8 failures; focused
  GREEN: 36 cases. The combined 37-case PG run verifies diagnostic round-trip and
  prevents misclassification as a missing failure event.
- Per-run cancellation hardening added after review: service cancellation and worker
  lease heartbeat now prefer `cancel_run(run_id)` and release the RunRow transaction
  before runtime cleanup. Local regressions cover configured direct/dispatch/discuss/
  hybrid child isolation, cancelled-status cleanup and heartbeat loss. The real PG
  private-write cancellation test collects locally but still needs isolated server PG execution.
- Current full local verification after the cancellation fix: 3478 unit/API/contracts
  passed, 34 skipped; whole-tree Ruff and strict mypy (389 files) passed. The local
  runtime-integration rerun failed in setup because no PostgreSQL was reachable; the
  earlier candidate runtime-integration run passed 170 before this cancellation fix.
- Deployment, provider smoke and CI remain required. DB-ack and complete fresh-worker
  tool/reviewer/crash matrices remain separate durability work, not implied by this run.

## Review Focus

- Equal content/different ID must survive storage (Task 1).
- Concurrent owners/abort/late put must preserve surviving references (Task 1).
- New conversation UUIDs/content must not change restored root inputs (Task 2).
- Missing or tampered input refs must block all downstream calls (Tasks 2/3).
- Production must not silently keep its in-memory default (Task 3).

## Task 1: Private PostgreSQL Repository

Files: new `src/agent_hub/runs/artifacts.py`, private row models in
`src/agent_hub/db/models.py`, migration `alembic/versions/0023_runtime_artifacts.py`,
new `tests/integration/runs/test_runtime_artifact_repository.py` and focused unit tests.

Interface: `PostgresArtifactRepository(session_factory, *, max_artifacts_per_run=16384,
max_total_bytes_per_run=67108864, max_artifact_bytes=1048576, max_batch_size=16384,
max_write_reservations_per_run=65536)` implements `ArtifactRepository` unchanged.

- [x] Write PG tests first, using two fresh repository objects and real transactions.
  Assert exact ID/hash round-trip, same-ID conflict, scope rejection, public absence,
  different-ID/equal-hash preservation, capacity serialization and deletion cascade.
  Ownership regression core:

```python
await repo.reserve_write(tenant, run, ref, write_id=first)
await repo.put(tenant, run, artifact, write_id=first)
await fresh.put(tenant, run, artifact, write_id=second)
assert not await repo.abort_write(tenant, run, ref, write_id=first)
assert await fresh.get_many(tenant, run, (ref,)) == (artifact,)
with pytest.raises(ArtifactRepositoryError):
    await repo.put(tenant, run, artifact, write_id=first)
```

- [x] Run in isolated server PG before implementation and retain the missing
  repository/behavior RED. Do not treat absent local PG as a test pass.
- [x] Prove canonical JSON TEXT preserves finite float representation and escaped
  NUL through real PG. Add raw SQL body/hash/size tampering negatives; do not relax hashes.
- [x] Add private tables and migration, tenant/run validation and row-lock ordering,
  exact immutable payload validation, transactional owner/tombstone behavior and limits.
- [x] Run the PG cases green; compare against existing in-memory repository tests.
- [x] Independent review checks privacy, race cancellation, limits and migration.

## Task 2: Original Input Snapshot And Crew v9

Files: `src/agent_hub/runtime/crew/adapter.py`,
`tests/unit/runtime/crew/test_input_snapshot_recovery.py`, affected checkpoint tests.

Interface: checkpoint `input_refs` is an ordered tuple/list of exact ID/hash objects,
including empty inputs, bound to `artifact_registry`; runtime version `9`.

- [x] Reproduce partial replay failure using old context input IDs, a fresh runtime
  and new/empty conversation context; completed calls must not execute again.
- [x] Persist initial inputs without artifact-created events before first model call.
  Include them in registry and exact input refs; enforce at most 64 unique refs.
- [x] Assert serialized events (including `inputs`, not only payload/message) never
  expose snapshot bodies; retain safe ID/digest references and ordinary worker outputs.
- [x] Hydrate ordered root inputs from checkpoint/private repository. Do not append
  fresh conversation inputs on replay. Revalidate input graph/digests and reject v8.
- [x] Test changed, missing, duplicate, oversized and cross-scope refs; no-input
  control; preserved review/correction/source graph and budgets; run green.
- [x] Independent review checks replay provenance, private input exposure and no
  weakening of all existing checkpoint-integrity tests.

## Task 3: Production Assembly And Fresh-Service PG Recovery

Files: `src/agent_hub/runtime/defaults.py`, `src/agent_hub/app.py`,
`src/agent_hub/runtime/worker.py`, assembly tests and
`tests/integration/runs/test_dispatch_durable_recovery.py`.

Interface: optional `artifact_repository: ArtifactRepository | None = None` through
configured registry and ConfigBackedDispatchRuntime; production supplies PostgreSQL.

- [x] Add a failing assembly regression asserting the provided repository reaches
  every freshly constructed Crew child; API and worker supply their session factory.
- [x] Wire the optional parameter and production store without touching public API
  schemas or placing private resources into TaskContext/routing metadata.
- [x] PG test executes until a committed nonterminal checkpoint, then constructs a
  fresh service, configured runtime and repository. No old artifacts may be manually
  supplied. Assert exact old inputs, no repeated completed model/tool calls and valid
  review/final result. Include missing/corrupt/scope negative tests with zero calls.
- [ ] Exercise an old writer's abort/late put after a replacement worker's surviving
  ownership/public checkpoint; fail this design if a referenced value disappears.

## Task 4: Verification And Release

- [x] Run whole unit/API/contracts, Ruff and strict mypy using workspace-local
  temp/cache directories; run isolated PG repository/recovery tests. Runtime
  integration passed before the cancellation fix but still needs rerun with reachable PG.
- [x] Independent final review and corrective regression tests.
- [ ] Inspect active/checkpoint versions before rollout; migration is additive and
  old binaries keep public storage. Do not downgrade nonterminal v9 checkpoints.
- [ ] Deploy, run real feature-specific server verification plus real-provider smoke,
  then push and inspect GitHub CI to green. Keep runtime and rollback releases.
- [ ] Record exact scope/limits in local handoff and continue DB-ack durability; do
  not equate hydration with crash-safe submission or completed business capability.
