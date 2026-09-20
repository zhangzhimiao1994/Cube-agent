# Discuss Instruction Context

## Status And Delivery Order

Design only, 2026-09-21. No implementation or passing test result is asserted by
this document. The dispatch instruction-context slice is owned by its current
implementer; integrate against its finalized interface before starting this work.

Mainline order:

1. Finish dispatch verification/release under the main agent's ownership.
2. Deliver standalone discuss participant guidance at the actual gateway boundary,
   with trustworthy runtime identity, bounded public evidence and focused TDD.
3. Deliver Hybrid guidance propagation and evidence forwarding, including final
   direct synthesis, without claiming stage-internal recovery.
4. Complete standalone discussion DB write-ahead durability as a separate slice.
5. Complete Hybrid active-child checkpoint persistence and restoration as a
   separate slice, using the discussion durability work where applicable.

Steps 4 and 5 are required unfinished follow-up work, not optional omissions and
not capabilities delivered by steps 2 or 3. A deployment requiring crash-safe
in-flight recovery must wait for their own acceptance gates.

## Goal And Non-Goals

Extend session-root AGENTS.md/SKILL.md guidance to actual discussion participant
requests. Correlate service-owned file evidence with the exact submitted request
and the runtime participant that submitted it. Model text, booleans, generated
ZIP contents and claimed file reads are never evidence of context loading.

- Do not change selector prompts, candidate selection, role/model routing,
  termination, turn limits, reflection settings or tool-continuation policy.
- Do not change allowed_tools, capability/harness gateways, approval envelopes,
  sandbox profiles, actor_id, actor_role or permission checks.
- Do not activate installed skills, attachment guidance, planning approvals or
  project-scale capability verification as a side effect.
- Guidance remains subordinate reference data, not executable policy or proof
  that the model obeyed it. SKILL.md is not an approved installed skill package.
- Keep source bodies out of event metadata, checkpoint state and handoff artifacts.
  Do not weaken existing direct/dispatch projections or ledger formats.

Selector requests deliberately remain outside guidance coverage in the first
slice. They continue to use the existing model client and shared durability
ledger, but receive neither guidance nor a fabricated participant injection event.
Report coverage as participant requests, not every discussion model request.

## Existing Contracts And Gaps

References are repository-relative and line numbers describe the investigated
snapshot; locate the named functions again after dispatch integration.

| Location | Existing behavior and implication |
| --- | --- |
| `src/agent_hub/runs/service.py:1383` | Loads only direct/dispatch; existing scoped load, lease checks, authorization recheck and context.loaded are reusable. |
| `src/agent_hub/runtime/contracts.py:930`, `:1000` | Private instruction_context is excluded from public serialization; validated_internal_clone preserves it and validates tenant/run. |
| `src/agent_hub/runtime/instruction_context.py:44`, `:120`, `:131` | Full request digest, deterministic guidance rendering without load_id, and typed injection metadata already exist. |
| `src/agent_hub/runtime/autogen/adapter.py:1136` | GatewayChatCompletionClient.create normalizes messages, constructs ModelRequest, then calls before_model and the gateway. |
| `src/agent_hub/runtime/autogen/adapter.py:1249`, `:1281` | create_stream delegates to create; tool-result messages also pass through this normalization boundary. |
| `src/agent_hub/runtime/autogen/adapter.py:1492`, `:1507` | Participant clients and selector client are separately constructed; trusted participant IDs are available here. |
| `src/agent_hub/runtime/autogen/adapter.py:237`, `:308` | Model replay checks request hashes and rejects running uncertain outcomes; tools retain replay_safe and identity checks. |
| `src/agent_hub/runtime/autogen/adapter.py:373` | compact clears ledgers and resets cursors after explicit messages; a cursor alone is not run-unique. |
| `src/agent_hub/runtime/autogen/adapter.py:1443`, `:1718` | Ledger callback only updates _last_checkpoint; yielded message-boundary checkpoints are a different boundary. |
| `src/agent_hub/runs/service.py:1462` | Service persists yielded runtime events; no per-model DB acknowledgement is established by the in-memory callback. |
| `src/agent_hub/runs/context_evidence.py:62` | Public identity understands direct and Crew-specific dispatch coordinates, not discussion. |
| `src/agent_hub/runtime/hybrid.py:325`, `:352`, `:496` | Child contexts omit guidance, child checkpoints are discarded, and context.injected is not forwarded. |
| `src/agent_hub/runtime/hybrid.py:390` | Parent checkpoint has artifacts and next_stage, not active-child ledgers. |

## Discuss Request Path

RunService adds DISCUSS to the existing load-mode allowlist only after the adapter
and public projection are ready. Keep the existing scoped reader and permission
contract. Unavailable/unauthorized sources produce no injected guidance. A loaded
event alone never implies a subsequent injection.

AutoGenDiscussionRuntime.run validates an internal context clone. Its participant
clients receive the immutable instruction bundle and a trusted participant ID as
request-local constructor data; selector construction does not receive them.
Do not place guidance in shared configuration, environment variables, globals,
AssistantAgent transcript state or _task_text().

GatewayChatCompletionClient.create builds a fresh normalized message tuple and
adds one bounded subordinate-guidance block before constructing ModelRequest or
computing the legacy durability request hash. Preserve existing roles and tool
result wrappers. Recheck message/request limits after augmentation. Do not remove
model/user strings by searching for guidance delimiters: inject once from the
trusted bundle without mutating the source history.

Use the same path for initial calls, existing continuation/reflection calls and
create_stream. Do not enable new reflection or additional tool iterations merely
to exercise this path. Guidance therefore affects participant request content,
not the discussion scheduler or tool permissions.

## Trusted Identity And Replay

Use `stage="discuss_participant"`. `actor` is the validated
DiscussionParticipant.id, never logical_model, response text or a tool argument.
Two participants may share a logical model and still have distinct identities.

The coordinate contract is:

```text
run_id + stage + actor + round_index + ledger_position
```

- round_index is the count of accepted explicit discussion-message artifacts at
  the current ledger boundary. Start at zero; derive it from restored discussion
  message artifacts, not arbitrary input artifacts or a new random load_id.
  Accepted here means the runtime transcript/artifact boundary, not an
  acknowledged Postgres commit. This coordinate adds no DB durability guarantee.
- ledger_position is the position in the shared model ledger for that round.
  Selector entries consume positions even though they do not emit guidance
  evidence. Gaps in participant-only evidence are valid.
- Capture coordinates under the existing model lock in before_model. On compact,
  roll the round forward with the explicit-message boundary and cursor reset
  atomically under that lock. A prefetched request keeps its captured coordinates;
  never recalculate them from the artifact list after awaiting the gateway.
- New participant ledger entries carry actor, round_index and ledger_position
  alongside their existing request_hash/status/result references. Validate those
  fields on replay. Do not change the legacy request_hash algorithm or tool
  idempotency formula.
- Validate exact integer types (reject bool), round_index in 0..64 and
  ledger_position in 0..63, consistent with existing message/ledger checkpoint
  limits. Reject unrepresentable new guidance evidence before submitting a call;
  do not wrap counters or silently drop coordinate validation.

Define the evidence ledger_key as the SHA256 of UTF-8 canonical compact JSON:

```python
json.dumps(
    ["discuss_participant_v1", str(run_id), actor, round_index, ledger_position],
    ensure_ascii=False, separators=(",", ":"),
)
```

This is a new discussion evidence key, not a Crew ledger key. Public projection
can validate its structure and recompute it, but that alone is not authorization
or proof of a DB-persisted ledger entry. The trusted runtime producer and captured
request supply the evidence; arbitrary matching JSON is not accepted as proof.

Record both `request_sha256` (existing full ModelRequest helper) and
`ledger_request_sha256` (existing AutoGen request_hash). Include load_id and source
digests only in metadata, never in prompt content or request hashes as random
identifiers. Identical source content with a new load_id must not break replay.
Changed source content against a pending ledger must fail request-hash matching,
not clear the ledger or reissue the model call.

Compatibility policy: retain terminal-checkpoint zero-call restoration. For
nonterminal legacy entries without guidance coordinates, retain no-guidance legacy
behavior; guidance-bearing recovery must fail closed before model submission if
the required identity cannot be validated. Do not synthesize historical injection
events. Message-boundary checkpoints with an empty ledger derive the next round
from the validated restored transcript and may continue with freshly loaded
guidance. This is a new request, not proof of identical guidance across attempts.

## Actual Submission Evidence

The per-run model wrapper emits context.injected only when a nonempty guidance
block reaches the actual `complete_with_context(request)` invocation. Merely
preparing a request or scheduling an asyncio task is insufficient.

- Successful cached replay and refused uncertain replay emit no new injection.
- Pre-submit cancellation, failed request validation and limit rejection emit no
  injection. Cancellation/failure after actual invocation may leave valid submitted
  evidence; that event does not claim provider acceptance, success or compliance.
- Use a bounded per-run record channel drained by AutoGenDiscussionRuntime's
  existing event sequencer while awaiting framework items and on failure/cancel
  exits. Preserve FIFO order for produced evidence and collect created tasks.
- Resolve record-channel backpressure before the submission boundary. After the
  final cancellation check, record submission and enter the gateway invocation
  without another queue/callback await that could leave false injected evidence.
  Cover this ordering with a gateway-entry sentinel and a saturated-channel test.
- Do not use a background repository writer, unbounded list, model output parser
  or delayed success-only reconstruction. Integrate records with existing tool
  record draining without changing tool execution or framework turn scheduling.
- A hard process crash can still lose buffered evidence. Missing evidence remains
  unknown, never backfilled as a successful injection. This limitation belongs to
  the unfinished DB durability slice below.

Extend InstructionContext.injection_metadata and context_event_projection with a
strict discussion branch. Preserve session_load/direct/dispatch identities and
legacy direct behavior. Unknown explicit stages stay context_unknown. Only typed,
bounded metadata and canonical AGENTS.md/SKILL.md paths reach public events; raw
source bodies, host paths and unknown payload keys are discarded.

## Hybrid Follow-On, Not The First Slice

After standalone discuss acceptance, pass the same private bundle explicitly from
parent to each new child TaskContext, retaining validated tenant/run and existing
actor/routing/budget fields. The bundle is shared within one execution attempt;
recovery can reload a new bundle and must not pretend otherwise.

Forward context.injected through the existing allowlist and renumbering path.
Preserve child stage/actor/ledger identity. Add a separately validated
hybrid_stage_index from the actual parent stage loop (0..3) and hybrid_mode from
the selected child, not model-provided strings. Public composite identity is the
child coordinate plus parent stage index: discuss -> dispatch -> discuss must
not collapse into one discussion stage. Keep the child's key intact; do not relabel
direct synthesis as a discussion participant or rewrite Crew keys.

Keep stage order, artifact handoff, cancellation, role selection and tools intact.
Fresh child contexts remain isolated; guidance is the explicit private exception
to the current artifact-only handoff. No nested checkpoint is introduced by this
propagation-only slice. Update existing tests that assert child checkpoint=None
only when the later recovery slice deliberately changes that contract.

## Unfinished Durability Work And Release Claims

### Standalone Discuss DB Durability: Not Implemented

The current running-entry callback updates memory, not an acknowledged database
write. Existing ledger tests that call runtime.save_checkpoint() demonstrate
runtime replay behavior, not worker-process crash safety. Framework prefetch and
buffered evidence enlarge the distinction. Do not publish exactly-once or
no-repeat-paid-call guarantees based on this guidance slice.

A dedicated follow-up must persist a running checkpoint before gateway/tool
side effects and acknowledge it under the current lease, handle result/evidence
commit ordering, and test recovery from the database in a fresh worker. It must
explicitly address partial transcript, ledger compaction and framework prefetch.
No provider/model usage is needed: use a blocking capturing gateway and a second
service instance against an isolated Postgres database.

### Hybrid Stage-Internal Recovery: Not Implemented

The parent discards child checkpoints and cannot restore an interrupted child
model/tool ledger. Completed-stage skipping is not stage-internal durability.
In-flight calls/side effects may be repeated under existing recovery behavior;
guidance forwarding neither introduces a new guarantee nor repairs this gap.

A dedicated follow-up must persist active stage identity and nested child
checkpoint, validate runtime version/tenant/run/artifact digests, restore only the
matching child, and propagate uncertain outcomes without silently restarting the
stage. Include dispatch, both discussion occurrences and final synthesis, plus
parent/child sequence and commit-boundary tests. Define checkpoint compatibility
before changing the current strict parent schema.

## Acceptance And Test Boundary

The standalone slice passes only with actual AutoGen participant requests captured
through the real model client, canonical runtime injection events and a real
Postgres RunRepository/RunService test of loading, scope and public projection.
No external provider/network/model usage is required. Framework tests and a PG
service test are distinct evidence classes and must be reported separately.

Required regressions: same-model/different-actor, multiple rounds with compact,
prefetch coordinate capture, tool-result continuation, create_stream, unchanged
selector requests, missing/no-read/revoked scope, concurrent tenants, exact-once
per-request augmentation, changed-source pending replay, new-load/same-content
replay, uncertain and completed restore, pre/post-submit cancellation, limits,
bounded drain/cleanup, malformed identity and public-body/path redaction.

Hybrid acceptance adds identical bundles across phases, two distinct discuss
stage identities, final synthesis, monotonic forwarded event sequences and
completed-stage boundary recovery. It explicitly does not close either unfinished
durability item above. Neither guidance slice proves model compliance, an approved
architecture plan, installed-skill execution or project capability verification.
