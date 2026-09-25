# Skills and MCP

Skills are uploaded, scanned, quarantined, then approved. Requested permissions are displayed before enabling.

MCP servers expose health and allowed tools in the admin console. Dangerous tools should require explicit approval before use.

## Capability Installer

The trusted capability installer lets the main Agent propose a missing capability from a curated catalog during a run. The conversation/run detail card is the primary path: review the Chinese summary, risk level, permissions, and follow-up approval requirements, then install or cancel the proposal. Installation confirms the current generated `plan_id`; stale or mismatched plans are rejected.

The MCP page also includes a management/debug entry for searching the same catalog, resolving aliases such as `strix`, generating an install plan, confirming installation, canceling a plan, or rolling back installer-owned plugins.

The installer does not download arbitrary internet plugins. Catalog entries install manifest-only/http-json plugin resources through the existing plugin service, then reload the runtime capability manifest. Installer rollback state is stored separately from plugin `resource_config`, so installed plugins remain valid through the ordinary plugin API. High-risk capabilities remain action-approval gated after installation; confirming installation does not auto-approve future tool use.
