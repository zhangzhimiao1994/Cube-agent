# Documentation

This directory separates current operator and product guidance from historical design records.

## Current Guides

- [Installation](installation.md): native systemd and Docker installation, mirrors, TLS, and repair behavior.
- [Operations](operations.md): health checks, logs, backup, restore, upgrades, and release cleanup.
- [Security](security.md): trust boundaries, approvals, secret handling, isolation, and audit expectations.
- [Troubleshooting](troubleshooting.md): first-response diagnostics for installation and runtime failures.
- [Model pools](model-pools.md): provider capacity, quota scopes, queueing, and fallback.
- [Skills and MCP](skills-and-mcp.md): capability discovery, approval, immutable environments, Team Skill Tap, MCP, and execution backends.
- [Remote Skill execution](remote-skill-execution.md): SSH and HTTPS cloud sandbox protocol, configuration, and safety checks.
- [Hermes learning and memory](hermes.md): layered memory, learning candidates, review, provenance, search, and journey views.
- [Workspace file-read policy](workspace-file-read-policy.md): project workspace boundaries and file-access rules.
- [Role planning and decisions](role-planning-and-decisions.md): how the main Agent chooses modes, roles, and execution posture.
- [Project capability acceptance](project-capability-acceptance.md): scale and mode acceptance expectations.
- [Feishu setup](feishu-setup.md): Feishu credentials and transports.

## Historical Design Records

[`superpowers/`](superpowers/README.md) contains dated implementation plans and design decisions. They are retained for engineering provenance. Paths, host names, phase labels, unchecked boxes, and deployment instructions inside those files describe the state at the time they were written and are not the current operating runbook.

Use the top-level README files and the current guides above for installation and operation. Use `HANDOFF.md` only inside an active development checkout; it is a local current-state index and is intentionally not part of the public release documentation.
