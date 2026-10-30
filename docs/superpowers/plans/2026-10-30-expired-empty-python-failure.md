# Expired Empty Python Failure Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Release a proven expired empty failed reservation through normal trusted maintenance, after immutable private archive.

**Architecture:** Inspect under owner/native locks; release owner transaction for deterministic ArtifactRepo put/readback; re-lock and revalidate owner/archive before native commit. Only the existing broker/worker/store are used.

**Tech Stack:** Python, SQLAlchemy/PostgresArtifactRepository, strict broker DTOs, Linux FD/systemd/admission fences.

**Spec:** `docs/superpowers/specs/2026-10-30-expired-empty-python-failure-design.md`

## Global constraints / review focus

- Seven APP paths only: lifecycle; sandbox python_project_protocol/client/broker/backend/cache; sandbox broker.py. Two new dedicated tests below. No schema/config/new-directory/ArtifactRepo-source changes. A compact root retirement control (not a full archive) is necessary once enforcement.
- Existing 8-entry/max_bytes/1GiB reserve/86400 TTL and every UNKNOWN fence remain. No model/public tool, old-history mutation, policy backfill or050b release.
- Stable archive content excludes inspect time and random maintenance IDs. Repository put/readback must not recursively lock the owner.
- Continuation bytes alone are not pending authority. Bind live/unsettled route/checkpoint/outbox state; never invent a ProviderContinuationRow status.
- Linux root filesystem + deployed automatic worker validation are parent-owned gates, not local mocked PASS claims.

## Task 1: Three-phase service RED

**Files:** `tests/unit/capabilities/test_python_expired_empty_failure_oct30.py`; `src/agent_hub/capabilities/python_project_lifecycle.py`.

- [ ] Add actual-path RED: legacy FAILED/no preparation/no policy flag is found by maintain_capacity; locked inspect, unlocked permanent put/readback, re-locked archive/owner verification and commit occur in order. Run/CP/route/version remain unchanged; only commit True counts a freed slot.
- [ ] Positive must match actual legacy owner: source=self_repair with historical keys, one settled provider row, generated python_ready/hybrid, no original preparation/failed flag. Historical source/row existence do not block; actual owner status/version/outbox/lease/authority do.
- [ ] Add negatives: changed owner/version/scope, lease/approval/outbox, genuine UNKNOWN/continuation authority, missing/conflicting/readback-bad archive, deadline/cancellation; deterministic archive retry does not create a new identity.
- [ ] Observe RED, then implement `_release_expired_empty_failure` and a separately bounded cursor branch in existing maintain_capacity; inject the existing ArtifactRepo through the existing builder. Preserve existing READY/terminal predicates.

## Task 2: Native protocol and exact-slot RED

**Files:** `tests/unit/skills/sandbox/test_python_expired_empty_failure_oct30.py`; six existing sandbox APP paths `python_project_protocol.py`, `python_project_client.py`, `broker.py`, `python_project_broker.py`, `python_project_backend.py`, `python_project_cache.py`.

- [ ] Add strict DTO RED for inspect/commit and no-path/no-install/no-grant; malformed/foreign snapshots and unexpected response phases refuse.
- [ ] Add Linux-isolated fixture RED using existing native install-failure record producer where supported: inspect is readonly; commit preserves externally archived bytes and releases exactly one expired two-file slot. No projects/models are executed for maintenance. Mocks are labelled and cannot replace native evidence.
- [ ] Add TTL/extra-member/READY/unpublished/bad-marker/FD-swap/root-mode/link/queued Job/either-unit/retained-other-stage negatives and mutation-error UNKNOWN preservation. Add mandatory Root-only restart/same-scope replay RED: DB archive alone must not make consumed installation fresh; compact retirement fence blocks reserve before allocation, including changed recipe. Verify its bytes count in quota and it cannot disguise a real environment as non-slot metadata.
- [ ] Observe RED; add one strict internal action/DTO, existing transport/helper wiring, root autonomous selection and snapshot revalidation, and common origin/stage fences. Do not construct an environment reference for an absent environment.

## Task 3: Focused integration and handoff

- [ ] Run dedicated tests, then necessary adjacent lifecycle/expiry/cache/protocol suites once; extend only for a new failure. Use root venv Python with explicit active-worktree/src PYTHONPATH and a fresh E:/code_x/.tmp pytest basetemp each time.
- [ ] Strict mypy seven APP paths; Ruff changed Python; git diff --check. Self-review exact seven APP+two tests+these two docs and locally commit only this slice.
- [ ] Report actual RED/GREEN/HEAD and remaining native gates. Parent independently reviews, runs Linux fixture and release; then confirms real worker automatic archive/reclaim of bdb8->7 before ordinary-user fresh chain. No hand deletion or old-run replay.

## Current local verification and parent-native interface

- Implemented exact seven APP + two tests. Initial Root once RED and legacy maintenance RED were observed. Independent phase-exemption P2 reproduced in ten additional negatives, then narrowed to bound native layers.
- Latest dedicated result: 44 passed (2.29s). Necessary adjacent run initially305 passed/60 Linux skips plus8 old broker-call compatibility failures; restoring the old call shape fixed the affected files,40 passed/4 skips. Only affected/new items were repeated. Seven APP strict mypy and nine Python Ruff passed; diff check passed. These are local logical/contract tests, not Linux native deletion evidence.
- Exact AST comparison retained old `_release_expired_pre_model`, terminal hook, retry loop, READY release/TTL paths and `_validate_failed_outcome` functions. No Hybrid scope/boundary APP changes remain.
- Parent Linux fixture must use persistent-disk private roots: original `_host_disk` rejects tmpfs, so `/run` is unsuitable when tmpfs. Do not patch that guard.
- Stable DTOs: `PythonFailedMaintenanceInvocation`, `PythonFailedMaintenanceRequest`, `PythonFailedReservationSnapshot` in python_project_protocol. Native entry: `execute_python_project_request(request, policy=..., configuration=..., stage=...)`. Inspect returns `response.failure_snapshot`; its `.archive()` returns the exact deterministic Artifact to persist/read back. Commit passes `phase='commit', snapshot=snapshot, archive_id=artifact.id, archive_sha256=artifact.content_sha256` and a separate fresh maintenance invocation. Commit returns `failure_removed`.
- For each native phase create its own correctly bound RUNNING stage with the existing `ProjectExecutionBroker._metadata`; the fixture must normally retire its previous owned phase stage after confirming it ended, otherwise the next phase's whole-run guard correctly refuses it. Cache failure fixture may use production `PythonProjectCacheLease.record_install_failure` with an explicitly synthetic isolated failure, or clearly labelled canonical synthetic controls; never mutate production records. Only real systemctl/Job/cgroup/stage/FD checks count as native proof.
- Still pending: fixed-commit independent delta review, parent isolated Linux guards/once/quota fixture, deployment and actual worker permanent archive+automatic8->7, then ordinary-user fresh end-to-end. Do not claim these gates passed from the local tests above.
