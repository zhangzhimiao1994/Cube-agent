# Expired terminal Python capacity implementation plan

> **For agentic workers:** Use superpowers:subagent-driven-development. Implement and review in this task; do not create another top-level conversation.

**Goal:** Recover capacity from confirmed expired terminal READY environments before a new original-project baseline, while preserving every unknown or retained execution.

**Architecture:** Extend the existing trusted lifecycle scan and metadata-only invocation reader. Continue to use the original Root `release_expired` path for native admission, TTL, filesystem identity, READY measurement, unit/cgroup and retained-stage checks. Do not retry `project.verify` merely because its result uses a quota error code: the same code can describe an already consumed execution.

**Tech stack:** Existing Python, SQLAlchemy/Postgres, native systemd broker and typed project contracts.

**Spec:** Current ordinary-user run `19ac4dc1-b0c1-448c-8063-cda1fa9a948a` failed with `project_verification.python_project_environment_quota`; installed capacity and seven-owner read-only audits are in `E:/code_x/review-model-release-ops-oct30/direct-read-context-retention-real-user-nov06`. Five expired legacy FAILED owners have original preparation `observed` and are excluded from the existing maintenance selector.

## Constraints and review focus

- No old run state/authorization/opt-in marker is backfilled. FAILED and UNKNOWN executions are never resumed or replayed.
- Keep the original 1 GiB disk reserve and existing cache/environment limits.
- Lock and revalidate the tenant/run owner; reject active workers/leases, pending approvals/outbox/provider consumers, uncertain execution and retained state.
- Validate observed preparation structure, observation hash, original reference, scope and Python recipe without executing or recapturing a baseline.
- Reuse the existing pre-admission maintenance callback; do not add a duplicate cleanup loop or an automatic post-quota verification retry.
- No source, configuration, history, artifact or wheel deletion. Root remains the authority for exact expired READY deletion.

## Work

- [ ] Add genuine RED cases in lifecycle and metadata-selector tests: legacy observed eligible, malformed/hash/scope drift rejected, active/pending/UNKNOWN retained, original executing behavior unchanged.
- [ ] Extend `src/agent_hub/capabilities/python_project_service.py` metadata-only expired invocation selection to a strictly validated observed state.
- [ ] Add a dedicated expired-terminal-observed branch to `src/agent_hub/capabilities/python_project_lifecycle.py`; retain bounded pagination, original Root RPC and fail-closed diagnostics.
- [ ] Verify the targeted suites, Linux/Windows strict typing and Ruff; independently review the complete production diff. Preserve the existing 16 staged reports.
- [ ] Freeze the reviewed baseline-context and capacity slices, verify the required changed boundaries and deploy while services are quiet. Previously passed unchanged checks are not repeated merely to wait.
- [ ] On the server, prove cache selection, native release and capacity recovery with original references/locks and unchanged protected state. Then validate the online original-baseline, autonomous repair and real delivery chain under its own concrete scope. No claim of product acceptance from component tests or cleanup alone.

Direct general terminal repair checkpoints and remaining Discuss/Hybrid delivery are subsequent required mainline work, not covered by this slice.
