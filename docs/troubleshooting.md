# Troubleshooting

Run:

```bash
scripts/agent-hub doctor
```

Common results:

- Docker missing: install Docker Engine or use a cloud image that includes Docker.
- Native unsupported: rerun `sudo bash install.sh --mode docker --yes`.
- Port conflict: stop the existing web server or set `HTTP_PORT`/`HTTPS_PORT`.
- Readiness failed: check `scripts/agent-hub logs` and verify PostgreSQL/Redis.

