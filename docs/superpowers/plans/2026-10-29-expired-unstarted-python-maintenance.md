# Expired Python cache maintenance

Scope: release only expired native READY caches for trusted terminal FAILED
runs older than maximum retention (86400s), with four null worker lease fields,
no provider continuations, pending approvals, undelivered outbox, self-repair
authority or uncertain route. Preserve run status/route/history and all projects.
Two independent service eligibility lanes are supported:

1. Original Python prep2/executing: exact producer state, no CP/model/tool/usage;
   only context/harness/terminal diagnostic event kinds are allowed.
2. Generated Python-ready Direct: exactly one native direct_action v2 CP with
   valid scope/state hash/segment reference; row/frame actor/request/project/
   session/permissions and stored preparation/recipe identity; native typed
   successful nontruncated actions; pending project.verify is unstarted (no
   CP-after tool events). Model scope parts, event call/request pairs, known
   costs and exact persisted usage must all close. Ordered native provenance
   and counts connect domains, never fabricated UUID/hash bridges.

Service owns DB eligibility under the run row lock, held through the internal
release_expired(invocation)->bool RPC. A readonly invocation is reconstructed
from the original actor/scope/contract/preparation and stored Python input recipe;
no observation, environment reference, approval, execution or current-user grant
is invented. Root owns exact key/ref/TTL/hash plus existing native release fences.
UNKNOWN/retained stages always veto; service eligibility is never deletion proof.
Expiry selection reads no mutable project, baseline or current instructions,
does not call normal generated.load/select, and sets install_allowed=False.
Normal execution/recovery validation is unchanged. The resume fingerprint also
hashes InstructionContext deliberately excluded by TaskContext.to_payload; expiry
does not reconstruct that memory-only binding or claim resume authority. CP/state/
reference hashes and native typed metadata remain mandatory. Later inspection is
added only to read-only decode context after exact same-run/pre-CP public/private
ID/type/hash/full-payload comparison. asyncpg UUID subclasses are normalized only
locally at the DB boundary; persisted records/native codecs are unchanged.

Ownership: this slice owns python_project_lifecycle.py, python_project_service.py,
project_verification.py, new python_direct_expiry.py and two focused tests. B owns the Root protocol/client/
cache/backend/broker slice. Parent owns defaults/runtime/project_inputs wiring.

Parent wiring (reported implemented, not edited by this slice): only defaults.py,
project_inputs.py and test_python_capacity_maintenance_wiring.py. One hook binds
preparer.capacity_maintenance=hook.maintain_capacity at bootstrap. Only Python/
captured, after concrete authorization, before executing/observe; timeout is
min(30s, remaining budget). Exceptions produce fixed safe diagnostics then leave
normal native admission authoritative; CancelledError propagates. Observed
recovery does not repeat maintenance. runtime.py did not need a change.

- [x] RED: maintenance missing and readonly executing invocation missing (2 failed).
- [x] GREEN: maintain_capacity()->int, at most8 candidates, one row lock per RPC;
      same method used by existing retry_loop and parent prepare callback.
- [x] Negative guards: age/status/lease/prep/scope/history/provider/approval;
      missing or mismatched readonly inputs; Root refusal/absence, no DB writes.
- [x] Native immutable-store reader regression, scoped SQL/lock tests, bounded
      pagination; relevant existing lifecycle/service tests, Ruff, strict mypy.
- [x] Review diff; record test limits. No deployment, server deletion, paid run,
      config/quota change, replay, generated source modification or CI.

Final verification:108 PASS (50 Direct proof +58 capacity lifecycle). Necessary
adjacent run:289 PASS (then-current99 new +190 existing lifecycle/FAILED cases).
Four owned source strict mypy PASS; six source/test files Ruff PASS. Explicit
RED->GREEN covers new lanes, asyncpg UUID, non-None inspection, excluded
instructions, outbox/lock waiting and client cancellation/drain. Native codecs
cover both producer raw-hash paths and segmented content. B independent final
review passed helper8187/lifecycle671b; parent whole12-app strict mypy/Ruff/diff
checks passed. Parent wiring independently reviewed; its23+cancel1 tests were not
rerun here. No unchanged full suite or paid acceptance was rerun.

Parent same-server read-only probe of helper SHA
8187dd60b40f6be5b3c116626f58178b16770fe64ccf59101f1e947d75b47875:
actual4c80 and7625 native DAO/frame/actions/scope/usage proof both TRUE, finite
diagnostics empty, zero GC/model/restore/DB writes. This proves evidence parsing,
not actual release or production capacity recovery. Portable fixtures do not
prove PostgreSQL lock concurrency or real Root FD deletion.

Remaining release boundary: parent integration/commit/release, actual Root
TTL/ref/unit/stage/predelete probes and controlled maintenance. UNKNOWN89d8 must
remain protected, unexpired65bd cannot be deleted, unsupported Hybrid6c remains
excluded. No source/project/history deletion, replay, configuration/hard-cap
increase, server writes, deployment, secrets, model calls or CI by this slice.
