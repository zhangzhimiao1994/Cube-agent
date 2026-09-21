# Durable Dispatch Artifact Recovery

## Goal And Scope

Restore a nonterminal dispatch checkpoint in a fresh worker using exact private
artifact bodies and the original ordered input snapshot, without repeating calls
already completed in that checkpoint. This slice covers dispatch storage and
hydration, not acknowledged model submission, exactly-once billing, discussion or
Hybrid active-child recovery.

## Existing Failure

ConfigBackedDispatchRuntime creates Crew with a fresh InMemoryArtifactRepository.
RunService loads a DB checkpoint but supplies only rebuilt conversation inputs.
Crew's registry contains emitted outputs, not all initial inputs. Public
RunArtifactRow is unsuitable: it is immediately visible in history/downloads and
deduplicates equal hashes across distinct artifact IDs.

## Private Persistence

Implement the existing ArtifactRepository protocol in
`src/agent_hub/runs/artifacts.py` as `PostgresArtifactRepository(session_factory)`.
Use separate private artifact and write-reservation tables. Each operation validates
the tenant/run pair against RunRow and locks that row for mutation/limit ordering.
Private tables cascade on run deletion and are never read by public API, history,
download or event projection paths.

Artifact key: `(tenant_id, run_id, artifact_id)`. Store full immutable payload as
canonical JSON TEXT, recomputed digest and encoded byte size. JSONB is unsuitable
for the private body because numeric normalization changes float/negative-zero
representations and embedded NUL strings are not accepted. Equal IDs with different digests fail;
equal digests with different IDs remain distinct. Reparse and rehash on read.
Writes use the existing `write_id` ownership semantics: reserved/written/aborted,
multiple owners for an identical artifact, and durable aborted tombstones that
reject late put. Abort cannot remove another owner's artifact. Unowned puts remain
non-abortable, matching the existing protocol. Transactions must release on error
or cancellation; error strings never include payloads or SQL credentials.

Retain existing positive limits: 16,384 artifacts, 64 MiB per run, 1 MiB each,
16,384 references per batch and 65,536 reservations per run. Enforce under the
same run lock; callers cannot evade totals by using concurrent repository instances.

## Publication And Ownership Boundaries

Only the existing RunService event transaction publishes accepted artifacts and
checkpoints under the worker lease. Private put is not publication. A crash between
private put and publication may leave a bounded private orphan; it must not appear
in events, history, download or conversation input. No public-table backfill or
fallback is permitted.

This first slice does not grant private storage a new lease-renewal authority.
Write ownership is explicit immutable `write_id`; Crew creates a new random ID per
write, removes it from pending before publishing, and never checkpoints pending
writes. A stale attempt may leave an orphan but must not overwrite/delete another
owner's value. Tests must reject this design if they find a stale abort can remove
a published checkpoint dependency. Lease-bound DB acknowledgement remains the next
separate gate, not an implicit claim of this store.

## Ordered Input Snapshot

Bump Crew checkpoint version to 9 and add exact ordered `input_refs` (ID and hash),
including an explicit empty list. On first execution, persist initial artifacts
privately and put them in the registry before any call can reference them. Do not
emit a public artifact-created event for this private snapshot.

On restore, hydrate those refs exclusively from the private store and use them as
root inputs. Fresh conversation-history IDs/content must not silently replace or
append to the original snapshot. Preserve instruction authorization revalidation
as a separate boundary. Every input ref must be unique, bounded to the TaskContext
limit of 64, and match the exact registry/body digest. Missing/tampered/cross-scope
dependencies fail before model or tool submission; never rebuild them from public
projections. Version 8 rejects explicitly rather than guessing missing input state.

## Assembly

Add an optional ArtifactRepository argument through configured_runtime_registry and
ConfigBackedDispatchRuntime into Crew. API and worker composition roots supply a
PostgresArtifactRepository using their existing session factory. Standalone runtime
tests retain an explicit in-memory option. Do not change Hybrid or discussion claims
in this slice; their active-child and DB-ack work remains pending.

## Acceptance

- PG repository instances round-trip distinct IDs, enforce CAS/hash/scope/limits,
  owner abort and tombstone semantics, and cascade on run deletion.
- Real PG preserves finite exponential floats, negative zero and escaped NUL
  without changing types, digests or byte size; raw body/hash/size tampering rejects.
- Private put before public event commit remains invisible in public artifacts,
  raw download sources, history and events.
- Event serialization must not expose input snapshot bodies through RunEvent.inputs;
  publish bounded references instead, without hiding ordinary accepted worker output.
- Actual Crew partial checkpoint resumes with new service/configured runtime/store
  instances and original input identities, without manually injecting old inputs.
- Missing, corrupt, wrong-run and wrong-tenant artifacts fail closed before calls.
- Two owners plus stale abort preserve the surviving reference; late put after
  abort cannot revive the aborted write.
- API and worker assembly tests prove production uses durable storage.
- Regression, Ruff, strict mypy, isolated server PG, real-provider smoke, deployment
  verification and GitHub CI gate precede completion claims.

A successful completed-run no-op, shared in-memory replay, or synthetic project
fixture does not satisfy fresh-worker recovery or real business capability.
