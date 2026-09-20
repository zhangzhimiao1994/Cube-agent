# Strict Structured Handoff Repair Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans. Execute task-by-task with TDD; use in-task subagents only, never new top-level conversations.

**Goal:** Replace fabricated structured handoffs with strict validation and at most one real, accounted model correction.

**Architecture:** Validate actual final Crew gateway responses, reuse the existing model cursor/ledger/usage path for format-only correction, and reject framework raw substitution. Persist bounded repair linkage without claiming that queued checkpoints provide a DB acknowledgement.

**Tech Stack:** Python 3.12, pinned CrewAI, existing ModelGateway, jsonschema, asyncio, pytest and isolated Postgres.

**Spec:** `docs/superpowers/specs/2026-09-21-structured-handoff-repair-design.md`

## Order And Global Constraints

- Future plan only. Current task creates these two documents, not production code.
- Start after prompt + transport fixes pass real-provider acceptance and are
  pushed, with CI checked. Finish this strict repair slice before discuss guidance.
- Feynman/Cicero retain current file ownership until handover; preserve their work.
- Do not change tool permissions, approvals, reviewer verdict policy or selector/
  role routing. Format correction is tool-free and cannot restart business work.
- One correction submission per logical step/run; no allowance reset by recovery.
- No fabricated fields, defaults, coercion or framework-text provider attribution.
- Reuse jsonschema, existing cursor/hash/ledger/budget and usage commit paths.
- No implementation, provider call, handoff/root edit or commit is authorized by
  this documentation task. Main agent owns later release and provider-test approval.

## File Map And Review Focus

Future production scope: `src/agent_hub/runtime/crew/adapter.py`, specifically
structured validation, StepBridge/final gateway response handling, `_complete_agent`
raw checking, model/usage boundaries and checkpoint repair-link validation.
No provider transport edits planned; consume the integrated fixes.

Tests: extend `tests/unit/runtime/crew/test_adapter_failure_reason.py`; create
`tests/unit/runtime/crew/test_structured_handoff_repair.py` and
`tests/integration/runs/test_structured_handoff_repair.py`. The PG file uses real
RunRepository/RunService/Crew and local capture helpers, no provider network.

Review focus: fallback false success (task 1); parser/schema mismatch (task 1);
framework re-entry/raw replacement (task 2); retry/usage/cursor drift (task 3);
queued checkpoint mistaken for DB acknowledgement (task 4 and open follow-up).

## Task 1: Fail Closed With The Actual Schema

**Interfaces:** Keep `_validate_structured_role_output(plan, step, agent, text)` as
the outer guard. Introduce adapter-local `_parse_structured_role_output(agent,
text) -> Mapping[str, JsonValue]` for strict decoding and actual-schema validation;
use bounded RuntimeExecutionError codes/paths, without exposing response text.

- [ ] Add red tests for fallback_used, changed logical_model and project-scale
  invalid output; assert no manufactured fields or completed downstream contract.
  Reverse existing wrapped-for-handoff success tests rather than retaining a bypass.
- [ ] Parameterize nested duplicate keys, NaN/Infinity/-Infinity, 1e999, missing/
  extra fields and wrong types; keep legitimate JSON whitespace/key-order cases.
- [ ] Run `python -m pytest tests/unit/runtime/crew/test_structured_handoff_repair.py tests/unit/runtime/crew/test_adapter_failure_reason.py -k 'structured or handoff or wrapped' -q`.
  Record expected behavioral red; environment/import failures are not red evidence.
- [ ] Remove field-generating reconciliation from the acceptance path. Implement
  bounded strict JSON decoding plus jsonschema against `_agent_response_schema`.
  Reject bad configuration/references locally; never invoke a model to repair them.
- [ ] Re-run green. At this intermediate gate invalid output fails immediately;
  task 2 adds genuine correction, not synthesized replacements.

Test invariant for an invalid upstream result without a valid model correction:

```python
assert not any(e.kind == EventKind.STEP_COMPLETED and e.step_id == "draft" for e in events)
assert not any(e.step_id == "final_response" for e in events)
```

## Task 2: One Real Correction And Framework Output Integrity

**Interfaces:** Add adapter-local `_repair_structured_role_output` inside the
StepBridge final-response path. It consumes original completion/model evidence,
schema, existing call cursor, model/usage boundaries and remaining budget; returns
only an actual validated GatewayCompletion. No independent provider client.

- [ ] Add real-Crew/capture tests: invalid -> valid gives exactly one correction;
  invalid -> invalid fails without dependent execution; valid initially costs no
  correction. Compare captured response text to accepted artifact text.
- [ ] Add raw tests: harmless equivalent JSON formatting preserves provider text;
  invalid/changed raw or true versus 1 fails framework mismatch without another
  correction or provider-attributed replacement.
- [ ] Observe red with `python -m pytest tests/unit/runtime/crew/test_structured_handoff_repair.py -k 'correction or framework_raw' -q`.
- [ ] Route correction through existing prepared/running/model-result and usage
  boundaries with next cursor index, purpose=step, same schema and guidance.
  Include bounded untrusted candidate/errors/source context. Do not append a
  second guidance block. No tools on this request; returned tool calls never run.
- [ ] Validate before returning to Crew; retain the outer guard. Remove structured
  raw-to-GatewayCompletion substitution; use strict, type-preserving equality.
- [ ] Assert unknown required business facts are never filled by runtime code;
  schema-inexpressible uncertainty fails. Add exhausted budget, cancelled request,
  unsupported transport and repeated framework-call cases; run green.

## Task 3: Persisted Bound, Replay And Actual Usage

**Interfaces:** A bounded per-step repair-link checkpoint mapping records original
model key/artifact/candidate digests, reserved correction key and outcome. Reuse
the existing model ledger entries/cursor, not a new purpose or provider ledger.
Define its strict schema/version and old-checkpoint rejection before serializing.

- [ ] Add red tests that outer business/recovery retries and framework re-entry
  cannot reset the one-correction allowance. Persist reservation before submission.
- [ ] Add restore tests for prepared same-key continuation, succeeded zero-call
  replay, running uncertain refusal, changed request/schema/source mismatch and
  tampered linkage. Include contiguous call indices and artifact graph validation.
- [ ] Add usage tests: original-invalid plus corrected-valid/invalid consumption
  sums once; replay adds zero; post-response schema/raw rejection retains usage;
  missing usage blocks correction as unaccounted. Preserve unknown-cost metadata.
- [ ] Run `python -m pytest tests/unit/runtime/crew/test_structured_handoff_repair.py -k 'replay or usage or budget or linkage or retry' -q` and retain red evidence.
- [ ] Implement minimal repair-link state/validation and response accounting through
  usage_boundary. Separate transport result success from schema/business success.
  Do not overwrite original results or copy original usage onto the correction.
- [ ] Enforce existing remaining step/run time, cost and tokens before a paid
  call, with output capped to remaining allowance. Do not restart tool execution.
- [ ] Re-run green, including interruption after response/usage commit and before
  business artifact creation; report these as checkpoint/runtime tests, not DB-ack.

## Task 4: Verification And Release Gate

- [ ] Run tasks 1-3 tests together and existing Crew replay, tool authorization,
  instruction-context, schema transport and reviewer regressions. Verify the
  correction request/evidence includes the original trusted actor and guidance.
- [ ] Implement the isolated PG integration with actual Crew, temporary session/
  storage paths, deterministic capture gateway and one dependent step. Verify
  persisted two-call evidence/usage, accepted output, public error redaction,
  repeated completed execute with zero calls, and finally cancel/delete all runs.
  PG unavailable is a blocker, not a pass; do not launch Docker implicitly.
- [ ] Run local scope checks using the project interpreter:

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

- [ ] Begin a separate durability slice with a PG red test inspecting committed
  running/repair reservation from another DB session while the gateway is blocked.
  Never substitute in-memory save_checkpoint for the repository state.
- [ ] Add acknowledged persistence before side effects and explicit outcome/usage
  commit ordering under the worker lease; design compatibility independently.
- [ ] Test fresh-worker recovery before submission, after submission/before result,
  after response/before commit and after commit/before business handoff. Unknown
  outcomes fail closed; do not reconstruct successful fields or injected events.
- [ ] Close this item only with separate DB crash evidence. Runtime replay,
  normal-flow PG and real-provider success do not close it automatically.
