# Security

- Secrets are stored in `/etc/agent-hub/secrets.env` with mode `0600`.
- API responses and logs redact token, password, cookie, authorization, credential, and API key fields.
- Logs are level-filtered before output. The default is `WARNING` to avoid filling small servers
  with routine progress logs.
- Tenant and actor identity is carried through run, memory, plugin, and capability paths; unknown scopes fail closed.
- Skills and plugin packages are quarantined, hash-pinned, reviewed, and smoke-tested before activation.
- MCP tools, requested permissions, runtime dependencies, and health are shown explicitly in the admin console.
- High-risk installation and tool actions require explicit approval. Approval of an installation never pre-approves later tool use.
- Native capability execution crosses a narrow broker boundary into constrained transient systemd units. The broker validates caller identity, package hashes, paths, filesystem bindings, resource limits, network policy, timeout, and output limits.
- Remote HTTP and MCP adapters reject unsafe destinations and redirects. Missing or stale runtimes pause with an actionable resolution instead of falling back to an untrusted path.
- Hermes learns safe lessons and recommendations; remembered content does not grant permissions, bypass approvals, or execute actions.
- Audit records cover capability lifecycle, approvals, Hermes promotion/rejection, and other privileged mutations.

The installer does not modify cloud firewalls or security groups.
