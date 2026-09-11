# Operations

Use `scripts/agent-hub` for routine maintenance.

```bash
scripts/agent-hub status
scripts/agent-hub logs
scripts/agent-hub doctor
scripts/agent-hub backup /tmp/agent-hub-backup.tar.gz
scripts/agent-hub backup verify /tmp/agent-hub-backup.tar.gz
scripts/agent-hub upgrade --version 0.2.0
scripts/agent-hub prune-releases --keep 2
scripts/agent-hub prune-releases --keep 2 --execute
```

Upgrades create a backup first. If readiness fails, the command restores the previous application version marker.
Release pruning is a dry run by default. It always protects the active `current` release target and only removes old directories under the configured native release directory when `--execute` is passed.

## Logs

Runtime logs are JSON and are filtered before they are written. New installs default to:

```bash
AGENT_HUB_LOG_LEVEL=WARNING
```

Valid levels are `DEBUG`, `INFO`, `WARNING`, `ERROR`, and `CRITICAL`. Set `ERROR` to collect less
noise on small servers, or temporarily set `INFO`/`DEBUG` while diagnosing a problem.
