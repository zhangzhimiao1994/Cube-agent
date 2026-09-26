# Skills and MCP

Skills are uploaded, scanned, quarantined, then approved. Requested permissions are displayed before enabling.

MCP servers expose health and allowed tools in the admin console. Dangerous tools should require explicit approval before use.

## Capability Installer

The trusted capability installer lets the main Agent propose a missing capability from a curated catalog during a run. The conversation/run detail card is the primary path: review the Chinese summary, risk level, permissions, and follow-up approval requirements, then install or cancel the proposal. Installation confirms the current generated `plan_id`; stale or mismatched plans are rejected.

The MCP page also includes a management/debug entry for searching the same catalog, resolving aliases such as `strix`, generating an install plan, confirming installation, canceling a plan, or rolling back installer-owned plugins.

The installer does not download arbitrary internet plugins. Catalog entries install manifest-only/http-json plugin resources through the existing plugin service, then reload the runtime capability manifest. Installer rollback state is stored separately from plugin `resource_config`, so installed plugins remain valid through the ordinary plugin API. High-risk capabilities remain action-approval gated after installation; confirming installation does not auto-approve future tool use.

## Team Skill Tap Revisions

A team Skill source can synchronize from its configured repository or import a trusted ZIP snapshot when the deployment cannot reach GitHub. Before importing a snapshot, configure the exact 40-character commit SHA and archive SHA-256 on the trusted, enabled source. The server rejects a commit or archive that does not match those pins and never treats an uploaded archive as active code by itself.

Each successful synchronization or snapshot import creates an immutable source revision. Review every scanned Skill candidate, approve the intended versions, and then activate the revision from its history. Activation is all-or-nothing: every member must be approved and its stored package hash must still match. The active source revision and the global Skill version mapping switch together, so a partially ready team release cannot leak into runtime.

Rollback restores the complete mapping captured before that revision was activated, including removal of Skills introduced only by the newer revision. Audit records retain the source, revision, actor, and operation. An active source or one of its active member versions cannot be deleted until it has been rolled back or replaced. Revisions remain as provenance after source deletion.
