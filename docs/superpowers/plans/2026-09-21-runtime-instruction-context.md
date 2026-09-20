# Runtime Instruction Context Implementation Plan

> **For agentic workers:** Use superpowers:subagent-driven-development or superpowers:executing-plans. Preserve existing unrelated files and the harness core.

**Goal:** Produce trustworthy direct-mode project-guidance load and injection evidence.

**Architecture:** RunService loads bounded session guidance through the run-scoped
reader, puts it in a dedicated immutable context field, and records safe source
metadata. DirectRuntime injects the bounded context and records request-bound
metadata at its gateway boundary. No benchmark gate is relaxed.

**Tech Stack:** Python 3.12, existing Pydantic contracts, asyncio runtimes,
Postgres repository, pytest, existing ModelGateway.

**Spec:** `docs/superpowers/specs/2026-09-21-runtime-instruction-context-design.md`

## Global Constraints

- No changes to harness scheduling, actor authority, or tool permissions.
- Do not commit local handoffs, credentials, public endpoints, or temporary probes.
- Default missing context preserves existing direct chat behavior.
- Project SKILL.md is not an approved installed skill.
- Model/history/ZIP claims cannot create trusted process evidence.
- Only direct execution is activated in this deliverable.

## Review Focus

- Read bytes versus decoded/injected bytes must have accurately labeled hashes.
- Budget rejection and replay must not produce successful injection records.
- Invalid or unavailable optional files must not expose host paths or block chat.
- Tenant/session permissions must come from the persisted run on every load.
- Public projections must not leak raw guidance, credentials, or untrusted paths.

## Task 1: Loader And Dedicated Contract

Files: `capabilities/scoped_read.py`, new `runtime/instruction_context.py`,
`runtime/contracts.py`, corresponding unit tests.

- [ ] Add red tests using real scoped AGENTS.md/SKILL.md files and assert exact
  loaded text, digests, bounded content, and no successful record for unauthorized,
  missing, invalid UTF-8, link, or forged routing input.
- [ ] Add a dedicated immutable context model with run/tenant binding and safe
  serialization; keep content out of repr. Preserve default empty roundtrips.
- [ ] Implement secure-reader metadata without extra path-based rereads; retain
  existing tool response compatibility and redacted errors.
- [ ] Implement fixed root-file discovery with per-file and total bounds.
- [ ] Run loader, scoped-reader and runtime-contract tests plus Ruff/mypy.

Example invariant for gateway and loader tests:

```python
assert sha256(injected_text.encode("utf-8")).hexdigest() == evidence["injected_sha256"]
assert "instruction_context" not in submitted_routing
assert "raw-guidance-marker" not in json.dumps(event.to_payload())
```

## Task 2: Service And Direct Gateway Integration

Files: `runs/service.py`, `runtime/direct.py`, `app.py`, `runtime/worker.py`,
direct/service and wiring tests. Public evidence projection is shared by
`runs/repository.py` and `api/routers/admin.py` through `runs/context_evidence.py`.

- [ ] Add failing tests that capture actual gateway requests, not model output.
- [ ] Inject loader dependency in both production service constructors, and load
  only authorized direct context before execution; persist metadata through the
  existing event transaction path.
- [ ] Add subordinate JSON guidance to direct prompt with escaping and existing
  budget checks. Preserve existing prompts when no guidance is available.
- [ ] Record actual injection after validation/budget checks at gateway submission,
  including load identity and hashes, without storing prompt text in events.
- [ ] Test fixture, cancellation, budget, restored completed checkpoint, changed
  files across executions, concurrent scopes and forged history claims.
- [ ] Run affected tests then unit/API suite and static checks.

## Task 3: Review And Release

- [ ] Independent security/correctness review; fix all blocking findings.
- [ ] Server feature probe with real isolated persisted run and files, plus
  authenticated general acceptance. Report provider versus capture probes clearly.
- [ ] Commit scoped files, deploy, validate, push, and wait for successful CI.
- [ ] Update local handoff current state and keep pending modes/skills/plan evidence
  explicitly unaccepted. Continue the next capability slice without another prompt.
