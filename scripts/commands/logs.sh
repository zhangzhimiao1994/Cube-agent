#!/usr/bin/env bash
set -Eeuo pipefail

if command -v docker >/dev/null 2>&1 && [[ -f /opt/agent-hub/compose/docker-compose.yml ]]; then
  docker compose -f /opt/agent-hub/compose/docker-compose.yml logs -f --tail=200
elif command -v journalctl >/dev/null 2>&1; then
  journalctl -u 'agent-hub-*' -f -n 200
else
  echo "No supported log backend found"
  exit 1
fi
