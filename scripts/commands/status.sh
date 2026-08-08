#!/usr/bin/env bash
set -Eeuo pipefail

if command -v docker >/dev/null 2>&1 && [[ -f /opt/agent-hub/compose/docker-compose.yml ]]; then
  docker compose -f /opt/agent-hub/compose/docker-compose.yml ps
elif command -v systemctl >/dev/null 2>&1; then
  systemctl status agent-hub.target --no-pager
else
  echo "No supported service manager found"
  exit 1
fi
