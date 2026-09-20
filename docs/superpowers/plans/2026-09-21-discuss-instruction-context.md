# Discuss Instruction Context Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. Use in-task subagents only when available; never create top-level conversations. Preserve other authors' changes.

**Goal:** Prove that authorized session guidance reaches actual discussion participant requests with trusted runtime evidence, then extend propagation to Hybrid.

**Architecture:** Reuse the service loader and immutable private InstructionContext. Augment fresh normalized AutoGen messages before durability hashing, capture runtime-owned round/ledger identity and emit bounded submission evidence. Keep Hybrid propagation and the two unfinished durability projects behind separate gates.

**Tech Stack:** Python 3.12, AutoGen AgentChat 0.7.5, existing ModelGateway, asyncio, Pydantic, pytest, SQLAlchemy/asyncpg and Postgres. No dependency upgrade or provider access.

**Spec:** `docs/superpowers/specs/2026-09-21-discuss-instruction-context-design.md`

## Status And Mainline

This is a future implementation plan, not a record of implemented changes or
passing tests. The current documentation task writes only this plan and its spec.
No production edit, test execution, handoff update, commit or deployment is part
of writing these documents.

Order: dispatch verified baseline -> tasks 1-4 standalone discuss -> task 5 Hybrid
propagation -> follow-up D1 discussion DB durability -> follow-up D2 Hybrid
stage-internal recovery. Tasks 1-4 form the next implementation batch; task 5 and
D1/D2 are not silently included or marked complete by that batch.

## Global Constraints

- Do not change selector prompts, candidate selection, role/model routing,
  termination, turn limits, reflection settings or tool-continuation policy.
- Do not change allowed_tools, capability/harness gateways, approval envelopes,
  sandbox profiles, actor_id, actor_role or permission checks.
- Guidance remains subordinate reference data, not executable policy or proof
  that the model obeyed it. SKILL.md is not an approved installed skill package.
- Keep source bodies out of event metadata, checkpoint state and handoff artifacts.
  Do not weaken existing direct/dispatch projections or ledger formats.
- No provider/network/model usage in tests. Do not start Docker/PG locally just
  to satisfy this plan; use the main agent's isolated server database when needed.
- No handoff/root-file changes, commits, pushes or deployment without a separate
  main-agent instruction. Do not revert concurrent edits or touch Feynman's files
  until the dispatch interface is integrated and ownership is handed over.

## File Map

All implementation/test paths below are repository-relative to Mofang-agent.
They are future scope, not files authorized for modification by the document task.

| Batch | Production files | Responsibility |
| --- | --- | --- |
| Discuss | `src/agent_hub/runtime/autogen/adapter.py` | Private context, participant injection, stable coordinates, bounded event drain and replay validation. |
| Discuss | `src/agent_hub/runtime/instruction_context.py` | Add typed discussion stage; reuse render and model_request_sha256. |
| Discuss | `src/agent_hub/runs/context_evidence.py` | Strict discussion identity/public projection. |
| Discuss | `src/agent_hub/runs/service.py` | Add DISCUSS to the existing loader guard after adapter readiness. |
| Hybrid follow-on | `src/agent_hub/runtime/hybrid.py`, `src/agent_hub/runs/service.py`, `src/agent_hub/runs/context_evidence.py` | Private child propagation, allowlisted forwarding, parent stage metadata and HYBRID loader guard. |

`runtime/contracts.py` and `runtime/defaults.py` are read/reference dependencies:
use validated_internal_clone and existing event forwarding; do not refactor them
without a demonstrated requirement. No Crew/direct production changes planned.

## Review Focus

1. Selector and participant sharing one model must not confuse actor identity or
   change selector inputs. Owned by tasks 1 and 2.
2. Prefetch and compact can race with evidence emission; captured coordinates
   must survive awaits and be unique across rounds. Owned by tasks 1 and 2.
3. Cancellation before invocation must not produce submitted evidence; after
   invocation, failure must not erase true evidence. Owned by task 2.
4. New load_id, changed source and legacy checkpoints need different replay
   treatment, without resetting uncertain ledger entries. Owned by tasks 1 and 4.
5. PG success and completed-stage Hybrid restore must not be reported as
   in-flight crash durability. Owned by tasks 4 and 5; unresolved D1/D2 remain open.

## Task 1: Coordinates And Public Contract

**Modify:** `src/agent_hub/runtime/autogen/adapter.py`,
`src/agent_hub/runtime/instruction_context.py`,
`src/agent_hub/runs/context_evidence.py`.

**Tests:** create `tests/unit/runtime/autogen/test_instruction_context.py`;
extend `tests/unit/runs/test_context_evidence.py` and
`tests/integration/runtime/test_autogen_ledger.py`.

**Interfaces:** Add frozen adapter-local `DiscussionModelCoordinate(actor: str,
round_index: int, ledger_position: int)` with `ledger_key(run_id: UUID) -> str`.
Use canonical JSON/hash from the spec. Extend before_model with keyword-only
`actor: str | None = None`; selector keeps None. New participant entries retain
the coordinate captured under the model lock. A read-only
`active_model_coordinate() -> DiscussionModelCoordinate | None` returns the
snapshot for the running request while that lock remains held. Replay returns
completion as before and must never use this method to invent new evidence.

- [ ] Add `test_discuss_coordinates_survive_compact_and_same_model_roles` and
  `test_discuss_projection_recomputes_key_and_rejects_bad_identity`. Exercise
  exact integer validation, actor validation using DiscussionParticipant rules,
  unknown stage, run mismatch, altered key, negative/oversized coordinates and
  bool values. Keep legacy direct/dispatch test expectations unchanged.
- [ ] Run the two focused tests and retain their expected assertion/interface
  failures as red evidence. Import/environment failures are not behavioral red.
  Command: `python -m pytest tests/unit/runtime/autogen/test_instruction_context.py::test_discuss_coordinates_survive_compact_and_same_model_roles tests/unit/runs/test_context_evidence.py::test_discuss_projection_recomputes_key_and_rejects_bad_identity -q`.
- [ ] Implement the frozen coordinate, deterministic key and strict projection
  branch. Add `discuss_participant` to the metadata stage contract without adding
  unrestricted strings. Store/validate coordinates in new participant entries.
- [ ] Initialize the durability round from restored explicit message artifacts;
  on compact set the next round and clear/reset the ledger under the model lock.
  Test that an in-flight captured coordinate does not change during a later drain.
- [ ] Add legacy terminal/no-guidance compatibility and guidance-bearing missing
  coordinate rejection. Re-run focused tests and existing ledger tests green.

Core assertions (coordinate is a captured running-entry snapshot):

```python
assert payload["stage"] == "discuss_participant"
assert payload["actor"] == coordinate.actor
assert payload["round_index"] == coordinate.round_index
assert payload["ledger_position"] == coordinate.ledger_position
assert payload["ledger_key"] == coordinate.ledger_key(run_id)
assert first_round_key != next_round_key
assert "text" not in payload
```

## Task 2: Actual Participant Submission And Bounded Evidence

**Modify:** `src/agent_hub/runtime/autogen/adapter.py` only, using task 1 contracts.
**Tests:** `tests/unit/runtime/autogen/test_instruction_context.py`,
`tests/integration/runtime/test_autogen_adapter.py`,
`tests/integration/runtime/test_autogen_ledger.py`.

**Interfaces:** GatewayChatCompletionClient receives optional keyword-only
`instruction_context: InstructionContext | None`, `actor: str | None` and a
run-local evidence sink. The sink accepts typed metadata only and is bounded;
the runtime owns RunEvent construction and sequence assignment. Selector uses
existing defaults. Use the shared `model_request_sha256(request)` helper.

- [ ] Add `test_real_discussion_participants_receive_guidance_once_selector_unchanged`:
  real SelectorGroupChat and capture gateway, at least two participants sharing a
  logical model. Identify participants from the configured client, not response
  text. For the same scripted participant transcript, assert selector requests
  equal the no-guidance control inputs and carry no injected guidance marker;
  participant requests and metadata carry correct actors. Do not require real
  model answers to stay identical after participant guidance is added.
- [ ] Add `test_guidance_on_tool_result_continuation_and_create_stream` using
  existing supported continuation settings and FunctionExecutionResultMessage.
  Preserve tool results, capabilities, approval identity and selector policy.
  Do not turn on new framework reflection behavior in production to pass a test.
- [ ] Run these tests red against the integrated dispatch baseline. The expected
  failure is absent participant request guidance/evidence, not missing AutoGen.
  Command: `python -m pytest tests/integration/runtime/test_autogen_adapter.py -k 'guidance or selector_unchanged' -q`.
- [ ] Validate the internal TaskContext clone, pass the private bundle only to
  participant clients, and augment a fresh normalized tuple once before request
  validation/hash. Recheck existing message/request limits including new content.
- [ ] Capture task 1 coordinates after before_model only for non-replay calls.
  Add a wrapper whose executed body records metadata at the gateway invocation
  boundary; do not mark a created-but-never-run asyncio task as submitted.
  Resolve bounded-channel backpressure first. Test that no intervening awaited
  enqueue/callback can publish evidence and then cancel before gateway entry.
- [ ] Drain bounded metadata records from the existing runtime polling/event
  loop, including exception and cancellation exits. Preserve produced event
  order; clean up all wrapper/drain tasks. Avoid background DB writes. Queue
  capacity/backpressure must be tested without changing framework turn selection.
- [ ] Add pre-submit-cancel, post-submit-failure/cancel, validation/limit rejection,
  blocked-consumer and concurrent-tenant tests. Assert no false injected event,
  no lost evidence during orderly failure and no dangling tasks/shared scope.
- [ ] Re-run focused tests plus existing cancellation/tool/stream regressions
  green; inspect actual captured ModelRequest, not just rendered helper output.

For each newly submitted participant request with loaded guidance:

```python
request_text = "\n".join(str(message.content) for message in request.messages)
assert request_text.count("<PROJECT_GUIDANCE_JSON>") == 1
assert marker in request_text
assert str(bundle.load_id) not in request_text
assert evidence["load_id"] == str(bundle.load_id)
assert evidence["request_sha256"] == model_request_sha256(request)
assert evidence["ledger_request_sha256"] == durability.request_hash(request)
assert marker not in json.dumps(evidence)
```

Do not assert that the final compacted checkpoint retains every earlier model
entry. Correlate with the captured running entry for that invocation; DB durability
of that entry remains D1. Evidence means gateway submission, not provider success.

## Task 3: Service Loading And Permission Boundary

**Modify:** `src/agent_hub/runs/service.py`.
**Tests:** `tests/unit/runs/test_instruction_context.py` and new
`tests/integration/runs/test_discuss_instruction_context.py`.

**Interfaces:** Reuse InstructionContextLoader.load and authorized_for, RunRepository,
RunService and AutoGenDiscussionRuntime. Export a directly callable async function:

`async def test_persisted_discuss_guidance_reaches_real_participants(database_url: str, tmp_path: Path) -> None`

This signature is an interface declaration, not the test implementation.
Implement the function with actual temporary scoped files, repository/service and
AutoGen framework. Do not import helper modules from another large test file or
require pytest imports/decorators for direct server invocation.

- [ ] Add a service unit test that fails because DISCUSS is not loaded. Cover
  post-load authorization loss using the existing service test pattern.
- [ ] Observe that behavioral red, then add DISCUSS to the current loader guard;
  leave HYBRID excluded. Preserve all lock/status/lease/permission checks and the
  existing loaded-vs-injected distinction. Run service tests green.
- [ ] Implement the PG test using local CapturingGateway/queue helpers, real
  RunRepository and RunService, and actual AutoGenDiscussionRuntime. Seed session
  AGENTS.md/SKILL.md plus a different-session marker under tmp_path. Configure
  two participants and a deterministic selector response script, no external tools.
- [ ] For the allowed run, assert actual participant guidance, same load_id/source
  digests, distinct trusted actors, canonical injected records, run.completed,
  no foreign-session content and no guidance body/internal path in public events.
- [ ] Run a no-read second case: no injected guidance/events, yet ordinary model
  execution may complete. Re-execute a completed run and assert no extra model
  calls or injection events. Finally cancel/delete every created run and dispose
  the database, including assertion/error paths.
- [ ] Main agent runs this function against an isolated migrated Postgres database.
  Report PG unavailable as an environment blocker, not a pass or behavioral red.

The test proves persisted loading/evidence in a normally completed service run.
It does not prove running-entry DB acknowledgement or crash-safe replay.

## Task 4: Replay Regression And Standalone Release Gate

**Tests:** `tests/integration/runtime/test_autogen_ledger.py`,
`tests/integration/runtime/test_autogen_adapter.py`, and tasks 1-3 test files.

- [ ] Add red tests for successful ledger replay without a new gateway invocation
  or injected event, running uncertain rejection, changed-source request mismatch,
  identical-source/new-load_id replay and legacy incompatible guidance recovery.
- [ ] Implement only missing guidance/identity validation in the adapter; retain
  existing request_hash and tool idempotency behavior. No uncertain reset/retry.
- [ ] Run those tests green with restored checkpoints and zero-call sentinels;
  include terminal restore and compacted transcript-boundary continuation. Label
  manual runtime checkpoint tests as runtime-level, not Postgres crash tests.
- [ ] Run focused suites with Python 3.12 and installed pinned dependencies:

```text
python -m pytest tests/unit/runtime/autogen/test_instruction_context.py tests/unit/runs/test_context_evidence.py tests/unit/runs/test_instruction_context.py tests/integration/runtime/test_autogen_adapter.py tests/integration/runtime/test_autogen_ledger.py -q
python -m ruff check --no-cache src/agent_hub/runtime/autogen/adapter.py src/agent_hub/runtime/instruction_context.py src/agent_hub/runs/context_evidence.py src/agent_hub/runs/service.py tests/unit/runtime/autogen/test_instruction_context.py tests/unit/runs/test_context_evidence.py tests/unit/runs/test_instruction_context.py tests/integration/runtime/test_autogen_adapter.py tests/integration/runtime/test_autogen_ledger.py tests/integration/runs/test_discuss_instruction_context.py
python -m mypy --strict src/agent_hub/runtime/autogen/adapter.py src/agent_hub/runtime/instruction_context.py src/agent_hub/runs/context_evidence.py src/agent_hub/runs/service.py tests/unit/runtime/autogen/test_instruction_context.py tests/unit/runs/test_context_evidence.py tests/unit/runs/test_instruction_context.py tests/integration/runtime/test_autogen_adapter.py tests/integration/runtime/test_autogen_ledger.py tests/integration/runs/test_discuss_instruction_context.py
```

- [ ] Run existing direct/dispatch guidance regressions and unchanged tool gateway
  authorization tests. Use temporary cache directories where required; report
  exact command, red failure, green count and omitted environmental checks.
- [ ] Independent in-task review checks request capture, public projection,
  selector invariance, coordinate stability and claims. Main agent owns server
  verification/release. Do not claim D1/D2 completion from this gate.

## Task 5: Separate Hybrid Propagation Slice

Start only after tasks 1-4 pass and the main agent assigns this next batch.

**Modify:** `src/agent_hub/runtime/hybrid.py`, `src/agent_hub/runs/service.py`,
`src/agent_hub/runs/context_evidence.py`.
**Tests:** `tests/unit/runtime/test_hybrid_context.py`,
`tests/unit/runtime/test_hybrid.py`, `tests/integration/runtime/test_hybrid_runtime.py`,
`tests/unit/runs/test_context_evidence.py`; add
`tests/integration/runs/test_hybrid_instruction_context.py` for a real mixed-runtime
PG test using capture gateways and temporary Crew storage.

- [ ] Add red tests for missing child instruction_context and dropped injected
  events. Exercise dispatch/discuss/direct and discuss/dispatch/discuss/direct.
- [ ] Explicitly pass the validated immutable bundle into each child context;
  preserve actor/routing/budget/artifact fields. Add HYBRID to service loading only
  when all child paths are covered. No per-child file reload or shared global scope.
- [ ] Extend the event allowlist only for context.injected. Preserve child keys
  and stage/actor; add trusted hybrid_stage_index and hybrid_mode from the parent
  loop. Extend projection with exact bounded types, not arbitrary payload copying.
- [ ] Assert same bundle/load_id across an attempt, distinct two-discuss composite
  identities, final direct guidance and monotonic forwarded sequences. No guidance
  body or absolute host path in handoff artifacts/public metadata.
- [ ] Run completed-stage checkpoint restore, cancellation and partial-failure
  regressions unchanged. Keep child checkpoints out of this propagation-only
  implementation; retain their omission as an explicit unresolved recovery limit.
- [ ] Main agent runs the real mixed-runtime PG test without provider access and
  checks completed re-execution does not call models. Report this as normal-flow
  propagation plus stage-boundary recovery only; D1/D2 remain unfinished.

## D1: Unfinished Follow-Up, Discussion DB Durability

**Not implemented or completed by tasks 1-5.** Expected modules include
`runtime/autogen/adapter.py`, `runs/service.py`, checkpoint persistence in
`runs/repository.py` and isolated PG recovery tests; exact schema/acknowledgement
API needs its own focused design before production edits.

- [ ] Start with a PG red test blocking inside the gateway and inspect the actual
  repository checkpoint from another session. Never substitute save_checkpoint()
  for the persisted checkpoint. Fail if no committed running intent is available.
- [ ] Define and implement an acknowledged write-ahead checkpoint/event boundary
  under the worker lease before side effects; separately commit result artifacts
  and outcomes. Retain tool replay_safe/approval rules and actor identity.
- [ ] Test fresh-worker restoration at pre-submit, post-submit/pre-result,
  post-result/pre-message and post-message boundaries. Assert uncertain model and
  unsafe-tool outcomes do not repeat, while permitted replay-safe tools retain
  their existing idempotency contract.
- [ ] Test prefetch, round rollover/compaction, lease loss, delayed persistence,
  evidence ordering and compatibility. A hard crash must not manufacture injected
  evidence from an intent that never reached the gateway.
- [ ] Close D1 only with real DB recovery evidence and a separate reviewed release.

## D2: Unfinished Follow-Up, Hybrid Active-Child Recovery

**Not implemented or completed by tasks 1-5.** Existing next_stage checkpoints only
skip completed stages. Child running model/tool state is currently discarded.

- [ ] Add a PG red test interrupting each active child stage, including both
  discussion occurrences and final synthesis; recover only from parent state
  actually persisted in the repository. Demonstrate the missing child ledger.
- [ ] Design a versioned parent checkpoint with active stage identity and nested
  child checkpoint; validate tenant/run/runtime version, plan and artifact digests.
  Reject incompatible/uncertain states instead of silently restarting a stage.
- [ ] Implement persistence and child restoration with D1's acknowledged boundary
  where applicable. Coordinate parent/child sequence and stage transitions so a
  child completion cannot be committed as the wrong stage or skipped prematurely.
- [ ] Test in-flight unsafe tools, model outcomes, stage-boundary commits,
  tampering, cancellations, and old checkpoint policy without changing workflow
  ordering or tool privileges. Keep completed-stage skips covered as regressions.
- [ ] Close D2 only with fresh-worker DB recovery tests and its own release gate.

## Completion Report Contract

Report independently: source-loading scope, participant actual-request coverage,
public evidence, runtime-level replay tests, normal-flow PG results, Hybrid
propagation, D1 status and D2 status. Until separately verified, both durability
statuses remain **unfinished**. A green framework/PG normal-flow test is not an
approved architecture plan, model-compliance proof or capability_verified=true.
