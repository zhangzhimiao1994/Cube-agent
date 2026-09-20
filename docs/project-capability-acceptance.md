# Project Capability Acceptance

The project-scale runner has two distinct benchmark kinds:

- `fixture` (default): deterministic server regression fixtures. Their generated
  project, discussion, plugin, and repair records are synthetic. A passing matrix
  does not demonstrate real project construction or autonomous recovery.
- `capability`: real business requirements submitted through the normal runtime.
  These requests omit the fixture marker and always validate downloaded projects.

Use `scripts/agent-hub project-scale-acceptance --benchmark-kind capability
--scale small --flow direct --execute --wait-seconds 240 --json` in the approved
server validation environment. Supply acceptance credentials through the existing
environment variables. `AGENT_HUB_PROJECT_SCALE_BENCHMARK_KIND=capability` also
selects this mode when invoked through `harness-acceptance`.

Generated code must run in an isolated environment. Safe ZIP extraction and
filtered child-process environments are not a security sandbox.

## Independent Business Checks

The small-project contract is a persistent task API. After npm install, build,
and tests, the verifier starts the application on a temporary port and performs
HTTP checks independent of its own test suite:

- create two unique tasks and read them back;
- change status without changing the other task;
- reject missing IDs with structured 404 responses;
- delete and restore tasks;
- restart twice to verify active, deleted, and restored state persistence;
- terminate the complete process tree and remove the temporary data directory.

Checks use numeric loopback HTTP without proxies. Timeouts, unavailable tools,
unsupported platforms, or cleanup failures cannot produce a passing result.

Medium, large, and ultra prompts have distinct business requirements, but their
independent requirement evaluators are not implemented yet. Capability runs for
those scales fail that gate explicitly. Do not count them as verified capability.

## Reading Results

`requirements_validation` means the independently exercised contract passed.
`generated_project_validation` includes build, tests, and the applicable business
checks in capability mode. Author-written pass claims cannot substitute for those
checks. File completeness and meaningful implementation checks remain required.

`agent_standard_verification` is a separate gate. In capability mode, model text,
event payload pass flags, and ZIP-authored reading/verification claims cannot
establish this gate. Direct mode records actual session guidance loading and
model-request injection, but the complete contract still lacks approved-skill,
all-mode context, and planning-before-implementation coverage. The process gate
therefore still fails explicitly. Business behavior may pass independently;
`ok=false` retains the process failure. `capability_verified` remains false.

Capability repair requests preserve the original business requirements and pass
concrete failures back to the agent. They never request fabricated passing flags;
if requirements cannot fit the planner limit, repair fails explicitly instead of
silently truncating them. Missing runtime process instrumentation alone does not
trigger another generation request: a model cannot repair the absence of trusted
server records by changing its project. Real deliverable failures still trigger
the bounded repair attempt.
