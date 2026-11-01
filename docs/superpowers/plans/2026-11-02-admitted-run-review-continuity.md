# 已准入任务与审查资格分离 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 自动审查资格仅因时间到期时，新的 v2 已准入任务沿原池继续普通工作，自动审查仍要求当前资格。

**Architecture:** 由服务器证据 reader 产生带原始 SHA、出处、有效期和原合同指纹的可信 activation。新 profile 保存这份绑定；首次创建 pool 的同一事务保存 profile seal 和实际准入时刻。恢复仅接受已有原 pool 与 seal，预算检查与自动审查 freshness 分开。

**Tech Stack:** Python 3.12、现有 SQLAlchemy/PostgreSQL 准入账本、现有四模式 worker 与物理预算边界。

**Spec:** `docs/superpowers/specs/2026-11-02-admitted-run-review-continuity.md`

## Global Constraints

- 旧 v1 保持原拒绝策略，不改写、补齐或升级历史。
- 原 grant、余额、UNKNOWN holds、review 费用/次数和原 deadline 不重置。
- 当前 tenant/run/actor、授权输入、worker lease、source、config、catalog、合同指纹继续逐次核验。
- 过期证据不能批准新任务、新 pool 或自动审查；删除、替换与显式撤销仍拒绝。
- 原16个暂存报告保持，不提交运维材料或凭据；不重放旧失败/UNKNOWN 运行。

## Review Focus

- 提交后到首次建池前资格到期：首次建池拒绝，不能据旧提交快照晚建池。
- 只剩 profile、没有原 pool/seal：拒绝恢复，不能重建或补写历史。
- UNKNOWN 已占用余额：恢复不能归还、重新发放或跳过原账本。
- 审查等待期间资格到期：发出请求与消费 permit 前仍拒绝自动批准。
- 资格文件删除/替换与纯时间到期不同：即使余额有效，删除/替换仍撤销原池调用。

### Task 1: 保留服务器验证事实与原始证据绑定

**Files:** Modify `src/agent_hub/runtime/review_assembly.py`; test `tests/unit/runtime/test_review_profile_expiry_continuity_v2.py` and existing review-evidence tests.

**Interfaces:** Add frozen `VerifiedReviewActivation` with `record_reference`, `evidence_sha256`, `valid_until`, `source_sha256`, `contracts_sha256`, verified contracts and worker verification. Add server-owned evidence source with `current()` and `admitted(binding, *, admitted_at)`; both re-read pinned original bytes, and only the latter evaluates temporal validity at an already sealed admission time. Existing tuple loader remains strict-fresh.

- [ ] Pin RED: `test_v2_submission_persists_verified_activation_binding`; assert actual file SHA and contract/source identities, not caller-supplied metadata.
- [ ] Implement the typed reader without removing the public loader's expiry rejection.
- [ ] Verify unknown paths, duplicate JSON keys, removed/replaced bytes, wrong source/contracts and expired fresh reads reject.
- [ ] Run only the evidence-focused tests, normal strict type checks and Ruff for changed files.

### Task 2: 原池创建事务中的不可变 seal

**Files:** Modify `src/agent_hub/models/run_admission_repository.py`; create `tests/unit/models/test_run_continuity_seal.py`; extend the existing PostgreSQL admission integration fixture for atomicity.

**Interfaces:** Optional paired `_OpenArguments` fields `continuity_profile_sha256: str` and `activation_valid_until: datetime`; add `get_continuity_seal(tenant_id: UUID, run_id: UUID) -> tuple[str, datetime] | None`. Store profile SHA and actual admission time in the existing immutable `opened` evidence in the same transaction as pool creation. Validate the SHA against the run's persisted v2 profile; require actual admission time before activation expiry.

For a persisted v2 profile both fields are mandatory, and the supplied expiry must equal that profile's expiry. Use the database's actual `clock_timestamp()` after acquiring the run/pool lock, not transaction-start time or caller time.

- [ ] RED: `test_v2_open_persists_seal_atomically`, `test_missing_seal_is_never_backfilled`, `test_first_open_after_activation_expiry_rejected`.
- [ ] RED: one field only, both omitted for v2, or a forged later expiry reject. A real PostgreSQL waiter whose evidence expires while waiting for the lock leaves neither pool nor seal; rollback leaves neither.
- [ ] Add paired-field validation and preserve legacy callers with neither field.
- [ ] Reject an existing pool with a different or absent seal when v2 is requested. Never create another pool or reset a counter.
- [ ] Verify concurrent open/rollback against real PostgreSQL; preserve existing admission tests.

### Task 3: v2 恢复、普通物理调用与自动审查分别检查

**Files:** Modify `src/agent_hub/runtime/review_assembly.py`; extend `tests/unit/runtime/test_review_profile_expiry_continuity_v2.py` and native review-mode integration cases.

**Interfaces:** New submissions with the concrete trusted evidence source emit profile `version=2` and its activation binding. `prepare` accepts v2 continuity only after matching the existing pool, its immutable seal, authenticated input and current execution identity. Existing v1 and legacy tuple-loader behavior remain strict. A private owned review-purpose context identifies the whole `review()` await and all dispatch/permit callbacks.

- [ ] RED: same original pool and deadline, current worker and unchanged proof bytes, only expiry advanced: ordinary coding resumes; charged and held balances are identical.
- [ ] Implement v2 lookup before the historical-evidence path; require the original pool and seal, never open one during recovery.
- [ ] Split activation checking at `prepare`, closure `check`, `_dispatch_check` and review dispatch. Preserve the final synchronous source/expiry fence before any review send.
- [ ] Verify expiry during DAO/secret waits rejects review; default/manual exact-scope actions acquire no automatic-review authority.
- [ ] Verify a review's nested provider request labelled primary cannot bypass its owned review-purpose fence; revoke/change proof after restore but before reserve/issue still rejects, without refunding UNKNOWN or replaying effects.
- [ ] Verify v1, missing pool/seal, forged profile, actor/run/lease drift, removal, replacement, config/catalog/contracts/source drift and original deadline expiry reject.
- [ ] Run affected four-mode/native paths once after source stabilization; do not repeat earlier passing components without a new change.

### Task 4: 部署与真实普通用户交付闭环

**Files:** Existing release/native acceptance tooling and concise `HANDOFF.md` index; no generated project source leaves the server.

- [ ] Preserve and safely close the old expired test run after a fresh identity/state check; do not replay or extend it.
- [ ] Independently review source and native proof for the new release. Deploy only the verified revision and inspect the actual installed source/configuration and ordinary-user capabilities.
- [ ] Run one bounded new real-user construction only after the material fix and its verification; original limits and concrete-action approvals remain explicit. Reuse no old pass or UNKNOWN.
- [ ] Verify actual dependency installation, isolated build/tests, installed wheel outside the source tree, timestamp ZIP, Chinese Main instructions, table/code blocks and downloadable filenames. Run the independent CLI oracle and two viewport UI checks on the same actual run.
- [ ] Push the completed change and check exact-HEAD GitHub CI; fix actual failures and preserve failed evidence. Update handoff with results and remaining limitations, without claiming an incomplete product passed.
