# Skills, Plugins, MCP, and Execution Environments

The capability system separates discovery, trust, installation, activation, and execution. A manifest or catalog result is not treated as an executable capability.

Skills and plugin packages are scanned, quarantined, reviewed, and pinned to immutable source and content hashes before activation. Requested permissions and runtime dependencies are displayed before enabling. MCP servers expose health and allowed tools in the admin console; dangerous actions remain approval-gated.

## Capability Installer

The trusted capability installer lets the main Agent propose a missing capability from a curated catalog during a run. The conversation/run detail card is the primary path: review the Chinese summary, risk level, permissions, and follow-up approval requirements, then install or cancel the proposal. Installation confirms the current generated `plan_id`; stale or mismatched plans are rejected.

The MCP page also includes a management/debug entry for searching the same catalog, resolving aliases such as `strix`, generating an install plan, confirming installation, canceling a plan, or rolling back installer-owned plugins.

The installer does not silently download arbitrary internet code. Catalog entries resolve to a trusted installation recipe or a real configured backend, produce an explicit plan, and require approval before materialization. Missing runtimes pause the original run with a concrete install/configure action instead of leaving a fake enabled plugin or failing without recovery guidance.

Installer rollback state is stored separately from plugin `resource_config`, so installed plugins remain valid through the ordinary plugin API. High-risk capabilities remain action-approval gated after installation; confirming installation does not auto-approve future tool use.

## Capability environments

Executable capabilities run from immutable, versioned environments. Dependency material is offline or hash verified, smoke-tested before activation, and switched atomically. Failed activation keeps the previous environment available for rollback. Cleanup respects active references and configured quotas.

Linux native execution uses a socket-activated privileged broker and constrained transient systemd units. The API and worker stay unprivileged. The broker enforces package path and hash, caller identity, filesystem bindings, resource limits, timeout, output limits, private networking policy, and termination. Docker can be configured as an additional backend, but a missing Docker daemon does not make the systemd backend unavailable.

Remote HTTP and MCP transports validate destinations and redirects to prevent server-side request forgery. Capability dispatch performs preflight and execution-time checks so a runtime cannot become silently stale between planning and execution.

## Team Skill Tap Revisions

A team Skill source can synchronize from its configured repository or import a trusted ZIP snapshot when the deployment cannot reach GitHub. Before importing a snapshot, configure the exact 40-character commit SHA and archive SHA-256 on the trusted, enabled source. The server rejects a commit or archive that does not match those pins and never treats an uploaded archive as active code by itself.

Each successful synchronization or snapshot import creates an immutable source revision. Review every scanned Skill candidate, approve the intended versions, and then activate the revision from its history. Activation is all-or-nothing: every member must be approved and its stored package hash must still match. The active source revision and the global Skill version mapping switch together, so a partially ready team release cannot leak into runtime.

Rollback restores the complete mapping captured before that revision was activated, including removal of Skills introduced only by the newer revision. Audit records retain the source, revision, actor, and operation. An active source or one of its active member versions cannot be deleted until it has been rolled back or replaced. Revisions remain as provenance after source deletion.

## Adding a new capability

1. Register a trusted catalog entry or Team Skill source with immutable provenance.
2. Define permissions, runtime dependencies, execution adapter, and smoke check.
3. Generate and review the installation plan.
4. Materialize the environment and run its smoke check.
5. Activate it atomically, then verify it through a real task.
6. Keep rollback and audit data until no active run references the version.

An Agent may discover a missing capability during conversation and propose this flow. It must still stop for required approval and must not invent a successful installation when the backend, credentials, or package material are unavailable.
