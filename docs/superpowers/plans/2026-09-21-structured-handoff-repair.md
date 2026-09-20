# Strict Structured Handoff Repair Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans. Execute task-by-task with TDD; use in-task subagents only, never new top-level conversations.

**Goal:** Replace fabricated handoffs and reviewer fail-open approval with strict validation and at most one real, accounted correction shared by worker/reviewer per step/run.

**Architecture:** Validate actual worker/reviewer gateway responses, reuse existing actor cursors/ledger/usage for correction, and reject framework raw substitution and skipped+approve. Persist shared repair linkage and candidate-bound review evidence without claiming queued checkpoints provide a DB acknowledgement.

**Tech Stack:** Python 3.12, pinned CrewAI, existing ModelGateway, jsonschema, asyncio, pytest and isolated Postgres.

**Spec:** `docs/superpowers/specs/2026-09-21-structured-handoff-repair-design.md`

## Verification Status

Implementation is complete locally; release/provider/CI gates remain pending.
Windows unit/API/contracts: 3359 passed, 34 skipped. Runtime integration without
PG-dependent fixtures: 170 passed. Ruff passes; strict mypy checks 381 files.
Isolated server PG/Crew verifies five correction/reviewer/shared-slot scenarios,
and a separate PG test verifies public checkpoint privacy, raw recovery hashes
and tenant isolation. Real Crew-to-SDK mock-wire correction contracts pass.
These are bounded correction and checkpoint tests, not real-provider correction
or fresh-worker/crash durability claims.

## Order And Global Constraints

- Active continuation plan; native prerequisite passed server/CI at 240b739.
- Start after prompt + transport fixes pass real-provider acceptance and are
  pushed, with CI checked. Finish this strict repair slice before discuss guidance.
- Native file ownership is handed over; use nonoverlapping subagent scopes.
- Do not change tool permissions, user authorization or selector/role routing.
  Preserve real approve/revise/reject; fixing reviewer exception-based approval is
  mandatory. Correction is tool-free and cannot restart business work.
- One correction submission per logical step/run, shared by worker and reviewer;
  not one per actor, not two. No reset through recovery, reviewer_retries or resume.
- No approve or downstream execution without verified candidate-bound review or
  explicit trusted user waiver. A waiver is not a model approval; no new bypass API.
- No fabricated fields, defaults, coercion or framework-text provider attribution.
- Reuse jsonschema, existing cursor/hash/ledger/budget and usage commit paths.
- User has authorized implementation continuation. Main agent owns integration,
  deployment/provider probes, commits, handoff and push/CI checks.

## File Map And Review Focus

Future production scope: `src/agent_hub/runtime/crew/adapter.py`, specifically
structured validation, StepBridge/ReviewBridge final gateway response handling,
`_complete_agent` raw checking, `_review`, `_execute_step` reviewer exception
closure (previously adapter.py:2852), model/usage boundaries and repair-link validation.
Prerequisite production scope additionally includes models/types.py,
models/responses.py, models/litellm_client.py and models/gateway.py for typed
rejected evidence and nonretryable propagation. Projection changes are permitted
only where required to keep rejected candidate text out of public events.

Tests: extend `tests/unit/runtime/crew/test_adapter_failure_reason.py`; create
`tests/unit/runtime/crew/test_structured_handoff_repair.py` and
`tests/integration/runs/test_structured_handoff_repair.py`. The PG file uses real
RunRepository/RunService/Crew and local capture helpers, no provider network.

Review focus: fallback/schema false success (task 1); reviewer skipped+approve
(tasks 1-2); framework re-entry/raw replacement (task 2); shared correction cap,
review candidate identity and usage/replay (task 3); queued checkpoint mistaken
for DB acknowledgement (task 4 and open follow-up).

## Task 0: Preserve Rejected Evidence Without Accepting It

- [x] Red tests for native invalid JSON/schema preserving exact bounded text,
  digest and actual usage; unknown/invalid usage remains unknown. Incomplete,
  refusal and bad-tool output cannot become correction-eligible.
- [x] Add immutable RejectedOutputEvidence and evidence-bearing ModelResponseError;
  string/repr/log output must not include original text, schema or credentials.
- [x] Preserve rejection through leased gateway execution as GatewayRejectedOutput
  with gateway-selected identity and priced cost or None. All response-contract
  errors bypass automatic logical fallback; network policy is unchanged.
- [x] Test actual SDK MockTransport, fallback configured but one call, exact usage,
  unknown price, cancellation and capacity release; retain ordinary Chat/native
  success tests. Do not expose rejected output as GatewayCompletion.
- [x] Agree runtime-facing fields with the adapter owner before task 2. Runtime
  must explicitly persist/restore rejected outcome and unknown accounting state;
  failing-model ledger without evidence is insufficient.

## Task 1: Fail Closed With The Actual Schema

**Interfaces:** Keep `_validate_structured_role_output(plan, step, agent, text)` as
the outer guard. Introduce adapter-local `_parse_structured_role_output(agent,
text) -> Mapping[str, JsonValue]` for strict decoding and actual-schema validation;
use bounded RuntimeExecutionError codes/paths, without exposing response text.
Factor only the decoding/jsonschema portion for `_review` to use the exact
`_REVIEW_RESPONSE_SCHEMA`; do not apply a worker schema to a reviewer response.

- [x] Add red tests for fallback_used, changed logical_model and project-scale
  invalid output; assert no manufactured fields or completed downstream contract.
  Reverse existing wrapped-for-handoff success tests rather than retaining a bypass.
- [x] Parameterize nested duplicate keys, NaN/Infinity/-Infinity, 1e999, missing/
  extra fields and wrong types; keep legitimate JSON whitespace/key-order cases.
- [x] Reverse reviewer exception soft-skip tests for timeout, capacity, empty/invalid
  response and exhausted retries. Assert no fabricated approve, completed step/
  contract or downstream call. Retain tests where a real retry returns valid review.
- [x] Run `python -m pytest tests/unit/runtime/crew/test_structured_handoff_repair.py tests/unit/runtime/crew/test_adapter_failure_reason.py -k 'structured or handoff or wrapped or reviewer' -q`.
  Record expected behavioral red; environment/import failures are not red evidence.
- [x] Remove field-generating reconciliation from the acceptance path. Implement
  bounded strict JSON decoding plus jsonschema against `_agent_response_schema`.
  Reject bad configuration/references locally; never invoke a model to repair them.
- [x] Replace skipped/timeout_skipped+approve with explicit failed/unverified review
  closure. Preserve candidate evidence without acceptance. Check downstream and
  partial-result closure cannot turn this failure back into successful approval.
- [x] Re-run green. At this intermediate gate invalid output fails immediately;
  task 2 adds genuine correction, not synthesized replacements.

Test invariant for an invalid upstream result without a valid model correction:

```python
assert not any(e.kind == EventKind.STEP_COMPLETED and e.step_id == "draft" for e in events)
assert not any(e.step_id == "final_response" for e in events)
```

## Task 2: One Shared Correction And Verified Review

**Interfaces:** Add adapter-local `_repair_structured_role_output` inside the
StepBridge final-response path. It consumes original completion/model evidence,
schema, existing call cursor, model/usage boundaries and remaining budget; returns
only an actual validated GatewayCompletion. No independent provider client.
Provide the equivalent `_review`/ReviewBridge path using its own actor, schema and
cursor, guarded by the same per-step repair reservation from task 3. No separate
reviewer allowance. Strict refusal is the safe behavior until this gate passes.

- [x] Add real-Crew/capture tests: invalid -> valid gives exactly one correction;
  invalid -> invalid fails without dependent execution; valid initially costs no
  correction. Compare captured response text to accepted artifact text.
- [x] Add raw tests: harmless equivalent JSON formatting preserves provider text;
  invalid/changed raw or true versus 1 fails framework mismatch without another
  correction or provider-attributed replacement.
- [x] Add reviewer-invalid -> genuine approve/revise/reject correction tests when
  the shared slot is unused. Assert exact candidate ID/digest, configured reviewer,
  actual schema and no tools; valid reject/revise never becomes approve.
- [x] Add no-authorized-waiver failure tests. Only exercise waiver success if an
  existing trusted policy supports it; keep its audit distinct from model approval.
- [x] Observe red with `python -m pytest tests/unit/runtime/crew/test_structured_handoff_repair.py -k 'correction or framework_raw or reviewer' -q`.
- [x] Route correction through existing prepared/running/model-result and usage
  boundaries with next cursor index, purpose=step, same schema and guidance.
  Include bounded untrusted candidate/errors/source context. Do not append a
  second guidance block. No tools on this request; returned tool calls never run.
- [x] For reviewer correction use purpose=review and its existing review cursor;
  bind result provenance to the unchanged candidate. Never ask it to approve by
  default or treat a model-written verified flag as trusted evidence.
- [x] Validate before returning to Crew; retain the outer guard. Remove structured
  raw-to-GatewayCompletion substitution; use strict, type-preserving equality.
- [x] Assert unknown required business facts are never filled by runtime code;
  schema-inexpressible uncertainty fails. Add exhausted budget, cancelled request,
  unsupported transport and repeated framework-call cases; run green.

## Task 3: Persisted Bound, Replay And Actual Usage

**Interfaces:** A bounded per-step repair-link checkpoint mapping records original
model key/artifact/candidate digests, actor/purpose, reserved correction key and
outcome, including reviewed candidate ID/digest. Reuse
the existing model ledger entries/cursor, not a new purpose or provider ledger.
Define its strict schema/version and old-checkpoint rejection before serializing.

- [x] Cover that outer business/recovery retries and framework re-entry
  cannot reset the one-correction allowance. Persist reservation before submission.
- [x] Assert worker correction then reviewer failure cannot make a second
  correction; reviewer correction then worker revision failure also cannot. Reserve
  the shared slot under checkpoint synchronization, including concurrent entry.
- [x] Add restore tests for prepared same-key continuation, succeeded zero-call
  replay, running uncertain refusal, changed request/schema/source mismatch and
  tampered linkage. Include contiguous call indices and artifact graph validation.
- [x] Cover review replay against changed candidate ID/digest, legacy skipped+
  approve checkpoints and forged verified flags. Fail closed; never resume those
  as valid approval or unlock dependents. Genuine matching verified review replays
  without another model call or usage charge.
- [x] Add usage tests: original-invalid plus corrected-valid/invalid consumption
  sums once; replay adds zero; post-response schema/raw rejection retains usage;
  missing usage blocks correction as unaccounted. Preserve unknown-cost metadata.
- [x] Repeat accounting/cancel/restore tests for reviewer original and correction:
  cancellation before/after submission, timeout, uncertain running state and
  response commit before review publication. Every case preserves no-false-approve.
- [x] Run `python -m pytest tests/unit/runtime/crew/test_structured_handoff_repair.py -k 'replay or usage or budget or linkage or retry' -q` and retain red evidence.
- [x] Implement minimal repair-link state/validation and response accounting through
  usage_boundary. Separate transport result success from schema/business success.
  Do not overwrite original results or copy original usage onto the correction.
- [x] Enforce existing remaining step/run time, cost and tokens before a paid
  call, with output capped to remaining allowance. Do not restart tool execution.
- [x] Re-run green, including interruption after response/usage commit and before
  business artifact creation; report these as checkpoint/runtime tests, not DB-ack.

## Task 4: Verification And Release Gate

- [x] Run tasks 1-3 tests together and existing Crew replay, tool authorization,
  instruction-context, schema transport and reviewer regressions. Verify the
  correction request/evidence includes the original trusted actor and guidance.
- [x] Implement the isolated PG integration with actual Crew, temporary session/
  storage paths, deterministic capture gateway and one dependent step. Verify
  persisted original/correction evidence and actual usage, accepted output only
  after genuine approval, public error redaction,
  repeated completed execute with zero calls, and finally cancel/delete all runs.
  PG unavailable is a blocker, not a pass; do not launch Docker implicitly.
- [x] Add PG reviewer-failure and worker-used-slot/reviewer-invalid cases: no
  approve, downstream call or completed contract, and no second correction. Keep
  the valid-review control. Report normal-flow persistence, not DB-ack crash safety.
- [x] Run local scope checks using the project interpreter:

```text
python -m ruff check --no-cache src/agent_hub/runtime/crew/adapter.py tests/unit/runtime/crew/test_adapter_failure_reason.py tests/unit/runtime/crew/test_structured_handoff_repair.py tests/integration/runs/test_structured_handoff_repair.py
python -m mypy --strict src/agent_hub/runtime/crew/adapter.py tests/unit/runtime/crew/test_adapter_failure_reason.py tests/unit/runtime/crew/test_structured_handoff_repair.py tests/integration/runs/test_structured_handoff_repair.py
```

- [ ] Main agent authorizes real-provider acceptance: actual wire schema preserved,
  real Crew/role schema precedence intact, actual responses strictly validated;
  report ordinary success separately from a genuinely observed correction. Do not
  claim provider correction tested when only the deterministic capture exercised it.
- [ ] Main agent owns deployment, push and CI check. Only then continue discuss.
  Report tests, provider evidence, omitted checks and the DB-ack limitation below.

## Unfinished Follow-Up: Crash / DB Commit Acknowledgement

Not delivered by the strict repair slice. model_state_boundary emitting a queued
checkpoint is not a lease-bound Postgres commit acknowledgement. A process crash
can lose correction reservation/usage state before persistence, so no crash-safe
single-submission or exactly-once billing claim is allowed.

The production ConfigBackedDispatchRuntime also constructs a fresh Crew child
without an external ArtifactRepository. The child defaults to in-memory storage;
RunService supplies conversation history, not the current run's persisted
RunArtifactRow bodies. Database checkpoint persistence alone therefore cannot
prove fresh-worker artifact recovery. Treat this as the first durability gate,
not a new claim about normal-flow or shared-repository replay tests.

- [ ] Add a real PG fresh-service/fresh-configured-runtime recovery test with
  committed model artifacts and a partial checkpoint. Do not reuse an in-memory
  artifact repository or inject recovered artifacts into TaskContext in the test.
  Verify current-run tenant-scoped DB hydration, hashes, missing/corrupt artifact
  rejection and continuation without repeating already completed model calls.
- [ ] Wire durable artifact retrieval at the existing runtime/service ownership
  boundary before claiming process-restart recovery; keep rejected private text
  out of public artifact storage. Then cover dispatch, discussion and Hybrid.
- [ ] Begin a separate durability slice with a PG red test inspecting committed
  running/repair reservation from another DB session while the gateway is blocked.
  Never substitute in-memory save_checkpoint for the repository state.
- [ ] Add acknowledged persistence before side effects and explicit outcome/usage
  commit ordering under the worker lease; design compatibility independently.
- [ ] Test fresh-worker recovery before submission, after submission/before result,
  after response/before commit and after commit/before business handoff. Unknown
  outcomes fail closed; do not reconstruct successful fields or injected events.
  Include reviewer outcomes and the shared worker/reviewer reservation; missing
  or stale review evidence cannot become approve after recovery.
- [ ] Close this item only with separate DB crash evidence. Runtime replay,
  normal-flow PG and real-provider success do not close it automatically.
