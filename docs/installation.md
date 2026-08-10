# Installation

## New server path

Start from a clean Linux VM with at least 1 GB RAM, 2 GB free disk, and outbound package access.

```bash
sudo bash install.sh --mode auto --yes
```

`--mode auto` selects:

1. Native mode when systemd plus apt/dnf support are detected.
2. Docker mode only when native is unsupported or Docker is explicitly requested.

## Network-limited servers

The installer supports mirror fallback for servers that cannot reliably access official registries.

```bash
sudo env AGENT_HUB_MIRROR_MODE=auto bash install.sh --mode auto --yes
```

Modes:

- `auto`: try official sources first, then switch to China mirrors if installation fails.
- `china`: use China mirrors immediately.
- `official`: never rewrite package sources or retry with mirror registries.

Mirror environment overrides:

- `AGENT_HUB_PYPI_MIRROR`, default `https://pypi.tuna.tsinghua.edu.cn/simple`
- `AGENT_HUB_UV_PYTHON_INSTALL_MIRROR`, default `https://registry.npmmirror.com/-/binary/python-build-standalone`
- `AGENT_HUB_NPM_MIRROR`, default `https://registry.npmmirror.com`
- `AGENT_HUB_DOCKER_REGISTRY_MIRROR`, used when Docker image pull/build fails

## HTTPS

If `AGENT_HUB_PUBLIC_URL` starts with `https://` and no certificate files are provided, Caddy uses automatic certificate management for a correctly resolved domain.

If you already have a certificate, pass both files during install:

```bash
sudo env AGENT_HUB_PUBLIC_URL=https://agent.example.com \
  AGENT_HUB_TLS_CERT_FILE=/root/certs/fullchain.pem \
  AGENT_HUB_TLS_KEY_FILE=/root/certs/privkey.pem \
  bash install.sh --mode auto --yes
```

Native mode copies the files into `/etc/agent-hub/tls/` and configures Caddy with them. The API continues to listen on `127.0.0.1` by default; Caddy is the only public web entry point.

## Automatic diagnostics

On failure, the installer runs `scripts/agent-hub doctor`. It checks:

- Linux/systemd/Docker availability
- memory and disk
- ports 80/443
- curl and health endpoints
- service status where available

Diagnostics print fix suggestions without printing secrets.

## Existing installation behavior

Existing `/etc/agent-hub/secrets.env` and `/var/lib/agent-hub` are preserved. Re-running the installer enters repair/upgrade behavior and never replaces generated secrets.

