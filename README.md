# Agent Hub

Agent Hub is a multi-agent orchestration server with Feishu and Web management, dispatch/group-chat task modes, model pools, Skills/MCP governance, multimodal image handling, and Hermes experience learning.

## One-click install on a new Linux server

```bash
curl -fsSL https://example.invalid/agent-hub/install.sh -o install.sh
sudo bash install.sh --mode auto --yes
```

The installer detects the host, chooses Docker for broad Linux compatibility, generates secrets, starts services, runs health checks, and prints:

- `Management URL: .../setup`
- `One-time setup code: ...`

The setup code is printed separately and is never placed in the URL.

If anything fails, the installer automatically runs diagnostics. You can rerun:

```bash
scripts/agent-hub doctor
```

## Deployment modes

- Docker mode: recommended for new cloud servers and unknown Linux distributions.
- Native mode: supported on Ubuntu 22.04/24.04, Debian 12/13, Rocky Linux 9, and AlmaLinux 9.

The installer does not modify cloud security groups. Open ports in your cloud console deliberately after confirming the management URL and TLS choice.

## Development validation

```bash
uv run ruff check .
uv run mypy --strict src tests
uv run pytest -q
npm --prefix web run lint
npm --prefix web run test -- --run
npm --prefix web run build
```

