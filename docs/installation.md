# Installation

## New server path

Start from a clean Linux VM with at least 1 GB RAM, 2 GB free disk, and outbound package access.

```bash
sudo bash install.sh --mode auto --yes
```

`--mode auto` selects:

1. Docker mode when Docker exists or the Linux distribution is unknown.
2. Native mode only when systemd plus apt/dnf support are detected.

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

