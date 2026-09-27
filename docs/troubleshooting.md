# Troubleshooting

Run:

```bash
scripts/agent-hub doctor
```

Common results:

- Capability runtime missing: open the run's recovery card, review the proposed environment or configuration plan, and approve it only when its source, permissions, and hashes are trusted. A catalog placeholder is not a usable runtime.
- Docker missing: use the native systemd execution backend when the capability supports it, or install and configure Docker Engine only for capabilities that explicitly require Docker.
- Native unsupported: rerun `sudo bash install.sh --mode docker --yes`.
- Port conflict: stop the existing web server or set `HTTP_PORT`/`HTTPS_PORT`.
- Readiness failed: check `scripts/agent-hub logs` and verify PostgreSQL/Redis.
- Disk pressure: run `scripts/agent-hub prune-releases --keep 1` first to preview, then repeat with `--execute` after checking the protected current release. Also remove completed upload and probe artifacts from the deployment staging directory.
- Plugin will not enable: inspect its adapter, credentials, capability environment, package hash, and smoke-check result. Fix the reported blocker and resume the original run instead of creating a manifest-only replacement.

