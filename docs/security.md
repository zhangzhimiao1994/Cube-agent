# Security

- Secrets are stored in `/etc/agent-hub/secrets.env` with mode `0600`.
- API responses and logs redact token, password, cookie, authorization, credential, and API key fields.
- Logs are level-filtered before output. The default is `WARNING` to avoid filling small servers
  with routine progress logs.
- Skills are quarantined before approval.
- MCP tools are shown explicitly in the admin console.
- Hermes learns safe lessons and recommendations; it does not bypass approvals or execute dangerous actions.

The installer does not modify cloud firewalls or security groups.
