# Provider contract diagnostics and recovery implementation plan

> For agentic workers: use subagent-driven-development; preserve the current production release until reviewed verification succeeds.

**Goal:** Diagnose provider request-contract failures without exporting request/body text, and correct an explicitly rejected plain assistant-turn boundary within the existing request budget.

**Architecture:** Extend the final-wire request-shape DTO with optional, bounded message-tail metadata while accepting legacy records. Classify only exact, trusted provider rejection signatures. Reuse the existing single-negotiation transport path for an ordinary assistant-last request only when a trusted HTTP 400 explicitly rejects its message order. Do not convert generic/unknown 400s into retries.

**Tech stack:** Python 3.12, existing OpenAI-compatible client, HTTPX, pytest, strict mypy.

## Evidence and design

C7 `afaa5b4a-49f6-41e6-98fa-56301a727958` crossed the formerly invalid diagnostic checkpoint and reached checkpoint105. It failed on HTTP400 at 19:06:49Z, about 50 minutes before its original deadline. Request metadata records 15 messages (7 user, 7 assistant, 1 system), 6 tools, max_completion_tokens24576, no native continuation. The actual DeepSeek V4 Flash deployment permits 393216 output/1048576 context tokens; exceeding that configured cap is not established. The provider body was available in the SDK but not retained beyond an unknown finite classification; existing logs cannot recover its raw reason. Assistant-last remains a hypothesis, not a confirmed C7 cause.

User authorization is continued online harness diagnosis, controlled repair and real-user acceptance without repeated clarification. Implementation choices below are Main's scoped engineering decisions.

Alternatives: blanket retry/fallback for 400 risks repeating invalid or unauthorized requests; status-only manual stop leaves the harness unable to diagnose. Choose finite diagnostics plus correction only when the trusted provider explicitly proves an ordinary message-order defect. Native histories, policy/auth errors and unknowns retain their current boundaries.

## Constraints and review focus

- Preserve C7 FAILED/UNKNOWN/claims, original 10M tokens/3600 seconds and existing provider configuration. Do not requeue/replay C7 or spend a new paid acceptance during this implementation.
- Never emit body/message/COT/request text or credentials. Only fixed enums, booleans and bounded counts may enter diagnostics.
- New atomic optional request-shape profile: `last_role`, `last_content_kind`, `last_content_empty`, `last_assistant_prefix`, `empty_content_role_counts`, `native_reasoning_field_count`, `empty_native_reasoning_field_count`. Defaults preserve legacy DTO round trips; strict counts and tail consistency reject impossible profiles. Capture final payload, not a pre-transformation estimate.
- New rejection class `assistant_turn`, parameter family `messages`, classification source `exact_signature`. Accept only narrow provider-owned validation signatures; an arbitrary `invalid_request_error` is insufficient.
- Negotiate at most once under the original monotonic deadline. Preserve deployment, tools/schema/required-tool checks, output limit and all admission accounting. Do not fabricate reasoning content or tool results; skip native continuations, existing assistant prefixes, unresolved tool calls and ambiguous input.
- The correction appends a fixed harness continuation cue requesting the next assistant response for the existing task. It grants no new user authority or tool permission.

## Task 1: Finite diagnostics (Curie)

- [x] Add failing tests for legacy/new DTO compatibility, exact signature vs unknown400, malicious/oversized bodies and absence of source/secret text.
- [x] Extend `src/agent_hub/models/provider_diagnostics.py` and only required DTO consumers; preserve strict validation and existing typed evidence binding.
- [x] Run the targeted new and existing diagnostic tests, ruff and strict type checks. Record actual RED/GREEN; do not call providers.

## Task 2: Single controlled wire correction (Ohm)

- [x] Add failing client-boundary tests for a trusted assistant-turn rejection, successful corrected response and unknown/native/second-rejection negative cases.
- [x] Extend `src/agent_hub/models/litellm_client.py` using Task1's typed evidence and existing `_NegotiatedOutcome` path; enforce remaining timeout and unchanged request authority.
- [x] Verify both success and final rejection/cancellation preserve physical accounting and do not chain multiple negotiations. No live model calls.

## Task 3: Integration review and release preparation (Hegel/Main)

- [x] Independently review exact diffs and test request timeout/admission/required-tool invariants through the gateway/transport boundary; do not repeat unchanged component suites.
- [x] Integrate the already verified Q test-type-only changes after source work stabilizes; preserve all original assertions and the 16 staged reports.
- [ ] Run necessary combined tests and whole-repository strict checks once. Report remaining failures honestly.
- [ ] Freeze the corrected source, deploy while services are idle with real integrity checks, then conduct a separately bounded real-user acceptance. Diagnostic improvement alone does not establish that C7's unknown400 is fixed or that product acceptance passed.
