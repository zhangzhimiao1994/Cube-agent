# Operations

Use `scripts/agent-hub` for routine maintenance.

```bash
scripts/agent-hub status
scripts/agent-hub logs
scripts/agent-hub doctor
scripts/agent-hub backup /tmp/agent-hub-backup.tar.gz
scripts/agent-hub backup verify /tmp/agent-hub-backup.tar.gz
scripts/agent-hub restore /tmp/agent-hub-backup.tar.gz --target /tmp/agent-hub-restore
scripts/agent-hub prune-releases --keep 2
scripts/agent-hub prune-releases --keep 2 --execute
```

The backup helper archives only `AGENT_HUB_STATE_DIR` (normally `/var/lib/agent-hub`). It does not include PostgreSQL, Redis, or `/etc/agent-hub/secrets.env`; operate independent database and secret backups. `backup verify` checks that the tar archive is readable, not that its generated SHA-256 manifest matches every payload file.

`restore` extracts a previously inspected backup into the explicit target directory. It does not replace live state, restore a database, or restart services.

The current `scripts/agent-hub upgrade --version VERSION` helper only backs up the state directory and changes its version marker. It is a low-level rollback rehearsal, not a release downloader, release switcher, service restart, or readiness check. Use the installer or the versioned release deployment procedure for a real application upgrade.

Release pruning is a dry run by default. It always protects the active `current` release target and only removes old directories under the configured native release directory when `--execute` is passed.

## Docker Skill Runner

Build the isolated runner image from the current checkout:

```bash
scripts/agent-hub build-skill-runner
```

The default image is `agent-hub-skill-runner:latest`. Use explicit source and image values for another checkout or registry tag:

```bash
scripts/agent-hub build-skill-runner --source /srv/cube-agent --image registry.example/cube/skill-runner:1.0
```

The command invokes Docker directly without evaluating shell text. The resulting target runs as UID/GID `65532:65532` and starts `python -m agent_hub.skills.runner`; the Docker sandbox still applies its read-only filesystem, dropped capabilities, network policy, resource limits, and package/workspace mounts at invocation time.

The `skill-runner` target uses a separate `skill-runner-build` stage. It does not
build the platform or web stages or copy their virtualenv. Its dependency manifest,
`deploy/compose/skill-runner-requirements.txt`, contains only PyYAML, Pydantic, and
Pydantic's runtime dependency closure, with every version taken from `uv.lock`.
Installation uses wheels only, disables dependency resolution and caching, and
keeps installer tooling out of the runner virtualenv. The image copies only the
`agent_hub` and `skills` package initializers plus `runner.py`, `package.py`, and
`manifest.py`; CrewAI, AutoGen, and the web/API stack are not installed.

When updating these dependencies, refresh every pin from `uv.lock` and run
`tests/unit/test_deployment_contracts.py` to check the dependency closure and
minimal runner execution. Skills must remain self-contained: the runner still
rejects runtime dependency installation. Measure image size and build cache usage
on the intended build host before deployment; a Docker build is required to
verify the actual image size and container isolation.

## Logs

Runtime logs are JSON and are filtered before they are written. New installs default to:

```bash
AGENT_HUB_LOG_LEVEL=WARNING
```

Valid levels are `DEBUG`, `INFO`, `WARNING`, `ERROR`, and `CRITICAL`. Set `ERROR` to collect less
noise on small servers, or temporarily set `INFO`/`DEBUG` while diagnosing a problem.
