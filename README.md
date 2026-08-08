# Agent Hub

Agent Hub is a multi-agent orchestration server with Feishu and Web management, dispatch/group-chat task modes, model pools, Skills/MCP governance, multimodal image handling, and Hermes experience learning.

## One-click install on a new Linux server

```bash
git clone git@github.com:zhangzhimiao1994/mix-agent-.git
cd mix-agent-
sudo bash install.sh --mode auto --yes
```

For China-region servers without stable access to official package registries:

```bash
sudo env AGENT_HUB_MIRROR_MODE=auto bash install.sh --mode auto --yes
```

`auto` tries official sources first, then falls back to China mirrors for OS packages, PyPI/uv, npm, and Docker registry mirrors where possible. Use `AGENT_HUB_MIRROR_MODE=china` to use China mirrors immediately.

For HTTPS with your own certificate:

```bash
sudo env AGENT_HUB_PUBLIC_URL=https://agent.example.com \
  AGENT_HUB_TLS_CERT_FILE=/root/certs/fullchain.pem \
  AGENT_HUB_TLS_KEY_FILE=/root/certs/privkey.pem \
  bash install.sh --mode auto --yes
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

The installer does not modify cloud security groups. Open ports in your cloud console deliberately after confirming the management URL and TLS choice. Caddy is the public entry point; the API, LiteLLM, database, and Redis stay on private local/internal networks by default.

## Development validation

```bash
uv run ruff check .
uv run mypy --strict src tests
uv run pytest -q
npm --prefix web run lint
npm --prefix web run test -- --run
npm --prefix web run build
```

