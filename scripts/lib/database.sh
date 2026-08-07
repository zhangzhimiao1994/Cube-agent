#!/usr/bin/env bash

validate_dsn() {
  local dsn="$1"
  [[ "$dsn" == postgresql://* || "$dsn" == postgresql+asyncpg://* ]] || die "database DSN must be PostgreSQL"
}

run_migrations() {
  log "running database migrations"
  if command -v alembic >/dev/null 2>&1; then
    alembic upgrade head
  else
    warn "alembic not on PATH; migrations will run inside the application image/service"
  fi
}
