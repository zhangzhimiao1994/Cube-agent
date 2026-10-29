# Post-tool Output Room Implementation Plan

> **For agentic workers:** Use superpowers:executing-plans inline; no current-task subagent interface is exposed. Work each checkbox through actual RED/GREEN. Parent/Noether provide independent review.

**Goal:** Fix fresh post-tool growth falsely exhausting against an uncompressed input headroom while preserving old checkpoint requests exactly.

**Architecture:** Fresh runs persist context_window_compaction_version=2. Legacy missing/0 bypasses fit and version1 keeps the exact existing input-only fit. Version2 reserves the requested output in the same configured window using the gateway estimator, removes only older full readonly exchanges, and derives post-tool requests canonically at the final actual cap before hashing/linking.

**Tech Stack:** Python 3.12, existing Crew model/tool ledgers, ConfiguredModelGateway/ConservativeTokenEstimator, pytest, strict mypy/Ruff.

**Spec:** User instructions in this task; actual parent metadata sourceb4f0-parent-ops-oct30/final-growth-digests.json and model-capacity-metadata.json. Run7732 is FAILED and cannot be replayed/audited. Runtime default context window is65536 when published configv10 fields are null; provider prompt usage is not the estimator.

## Global Constraints

- Own only runtime/crew/adapter.py, runtime/crew/context_window.py, related tests/unit/runtime/crew files and this new plan. Noether exclusively owns service.py, unit/runs tests and its report revision plan.
- Shared worktree/index: do not stage or commit until parent supplies a serial window. Do not revert other changes. Report ready first.
- No server, model/provider calls, credentials, paid replay, GitHub, release freeze or config/budget/window/permission changes.
- Preserve complete native pairs, latest result exchange, verification/write/bundle, all instructions/schema/tools and private receipt/usage ledgers. Irreducible output room clips to a verified positive cap or rejects conservatively; never claim a larger cap than actual hard capacity.
- Old0/1 checkpoint policies never upgrade from route/fresh defaults. Saved prepared/running/rejected/succeeded/closed requests retain exact cap/hash/receipts and uncertainty/accounting behavior.
- Next release has5APP (adapter/context_window/delivery_report/service/python_project_lifecycle); parent owns lifecycle, Noether owns report P2 and releaseops. Tests/docs exact sets wait final commits. No freeze is authorized here.

## Review Focus

- High retained readonly input belowwindow but requested growth cannot fit until older observations leave; actual real gateway estimator, not byte/4 or provider usage.
- Native readonly call/result groups stay atomic; latest and mixed verification/write/bundle groups cannot be removed.
- Model maxoutput, configuredwindow, remaining role/run tokens/cost and originaldeadline remain hard. Clipped cap is refit from the same base before freezing.
- Canonical final-cap projection is identical after cold restoration at every receipt boundary, including legacy0/1; running remains UNKNOWN/no replay.
- Growth reservation/request/link hashes all describe the exact fitted request sent to the gateway; receipts and previous usage cannot change.

## Task 1: Reproduce and add versioned pure output fit

**Files:** context_window.py; new tests/unit/runtime/crew/test_post_tool_output_room_oct30.py; existing test_request_context_window_oct30.py when compatibility assertions need extension.
**Interfaces:** extend fit_request(request, gateway, *, reserve_output: bool=False) -> ModelRequest; reserve_output=False preserves old1 byte identity. Version2 checks estimated_input + actual requested maxoutput against the unchanged configured window, pruning only existing optional groups. If even required input plus one output token cannot fit, fail before transport; otherwise a verified smaller actual output cap is permitted.

- [x] Write actual high-input/desired-output RED using ConfiguredModelGateway (window65536, hardmax optional) and assert compacted request has real output room while legacy fit stays identical.
- [ ] Write integrated growth RED using existing native post-tool fixture with available time/tokens and older readonly observations, showing old8192-ish headroom causes growth exhausted before the next viable request.
- [ ] Implement minimal helper outputroom option; preserve native grouping/latest/required behavior and original1 default.
- [ ] Verify helper highinput, native pair, latest/verify/write/bundle, required irreducible and hard-cap cases GREEN.

## Task 2: Integrate canonical growth and restoration

**Files:** adapter.py; same new runtime/crew test file and existing growth/context tests only when needed.
**Interfaces:** _fit_context_request selects fresh2/legacy0/1; post-tool candidate hardlimits bound desired without first consuming old input headroom, fits desired, validates actual capacity/budget, then reconstructs from the same original correction base at final actualcap before requestSHA/reservation/link. Existing saved rows reconstruct through their persisted policy.

- [ ] Fresh version2 checkpoint default/persistence/validation; missing0 and stored1 exact preserved, no route upgrade.
- [ ] Root correction and each new growth compute/refit finalcap canonically before hash freeze; saved rows reuse fixed caps and correct versioned reconstruction.
- [ ] Watch actual integration RED become GREEN; assert grow completes without tools replay or altered receipt/usage.
- [ ] Cover provider/context clipping, irreducible requiredlatest, stepbudget/deadline bounds and native finalization.
- [ ] Cover reservation/hash/link identity and cold prepared/running/rejected/succeeded/closed restoration; old0/1 behavior remains exact.

## Task 3: Verify and hand off without touching shared index

- [ ] Run focused new tests and appropriate adjacent context/growth/native tests with active-worktree PYTHONPATH and fresh E:/code_x/.tmp basetemp; no old broad matrix.
- [ ] Run strict mypy on2 ownedAPP and Ruff on owned Python files; inspect git diff/check and other-owner preservation.
- [ ] Self-review finalcap fixed point and all restore boundaries, update actual results here, then report ready for parent/Noether review. Do not stage/commit until parent grants serial window.

## Progress / Rulings

- Root cause confirmed from unchanged deployedc1 growth AST: old capacity clamp precedes larger-output fit; existing fit checks input only. Metadata: correction8192 then growth8433, remaining1892s and child1,865,474tokens. Publishedraw limits/fallback null use runtime window65536; do not infer window by summing providerusage and cap.
- Ruling: user-authorized inline execution proceeds without another plan approval. User bounds verification to relevant adjacent tests, overriding skill full-suite defaults. Shared-index commit steps deferred by explicit parent instruction.
- Actual RED: pure fresh fit accepted estimator57009 + output16384 >65536. Native integration after fixing duplicate fixture call IDs raised post-tool finalization growth exhausted. Windows sandbox asyncio self-pipe stalled before assertions; bounded offline tests now use approved local execution context with no remote/provider calls.
- Ruling: required-only output room may clip to a verified positive cap rather than reject an unreachable desired cap; provider hardmax is clamped before fit. Canonical final-cap projection is rebuilt from the original correction base; old0/1 stay on the original branch.

- Native restore boundary RED: closed0/1/2 rejected valid post-handoff business rows; reused the existing native_continuation tail mode at the caller without changing any cap/hash/receipt.
- Adjacent one-shot fixtures have no growth-enabled deployment profile. Full new capacity derivation is limited to the existing growth opt-in; fresh2 pure fit still records its actual clipped cap. One context-cap expectation updated from the unreachable desired9000 to actual8500.

## Ready For Parent Review (not staged/committed)

- Actual RED: estimator57009 + requested16384 > configured65536 accepted by old fit; a valid native readonly/post-tool pipeline raised growth exhausted. Old0/1 saturation counterfactuals remain unchanged; fresh2 completes after fitting requested output.
- New focused suite:48 PASS (19.56s), including policy0/1/2 source/rootprepared/rootrunning/rootrejected/growthprepared/growthrunning/growthrejected/growthsucceeded/closed/completed, and provider-clipped prepared/running/succeeded exact caps/hashes. Running has no new calls; completed/private receipts/usage stay unchanged.
- Adjacent context/growth/native:171 PASS on the same production bytes (the prior combined run had two new-fixture counterfactual expectations corrected; final48 above reran all new cases). No full repository or unrelated runs/capabilities matrix was run.
- Strict mypy:2 ownedAPP PASS. Ruff:5 ownedPython PASS. git diff --check for ownedpaths PASS. No server/provider, paid call, config/fallback/quota changes, release freeze, stage or commit.
- Final self-review: checked canonical final-cap fixed point, hardmodel/window limits, native groups/latest/required guards, durable request/link hashes and no-replay restore. No current-task subagent tool exists; parent/Noether independent review remains required.
- Ready working files:2APP +3runtimecrew tests +this plan. Parent shared index remains untouched. Wait for serial commit window; do not stage automatically.
