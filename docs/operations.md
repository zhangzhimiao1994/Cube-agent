# Operations

Use `scripts/agent-hub` for routine maintenance.

```bash
scripts/agent-hub status
scripts/agent-hub logs
scripts/agent-hub doctor
scripts/agent-hub backup /tmp/agent-hub-backup.tar.gz
scripts/agent-hub backup verify /tmp/agent-hub-backup.tar.gz
scripts/agent-hub upgrade --version 0.2.0
```

Upgrades create a backup first. If readiness fails, the command restores the previous application version marker.

