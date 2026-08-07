#!/usr/bin/env bats

@test "doctor prints suggested fixes instead of secrets" {
  run scripts/agent-hub doctor
  [[ "$output" != *"POSTGRES_PASSWORD"* ]]
}

@test "backup manifest verifies payload" {
  export AGENT_HUB_STATE_DIR="$BATS_TEST_TMPDIR/state"
  mkdir -p "$AGENT_HUB_STATE_DIR"
  echo ok > "$AGENT_HUB_STATE_DIR/file"
  run scripts/agent-hub backup "$BATS_TEST_TMPDIR/backup.tar.gz"
  [ "$status" -eq 0 ]
  run scripts/agent-hub backup verify "$BATS_TEST_TMPDIR/backup.tar.gz"
  [ "$status" -eq 0 ]
}

@test "failed readiness rolls back application version" {
  export AGENT_HUB_STATE_DIR="$BATS_TEST_TMPDIR/state"
  mkdir -p "$AGENT_HUB_STATE_DIR"
  echo 0.1.0 > "$AGENT_HUB_STATE_DIR/version"
  run env AGENT_HUB_FAKE_NEW_VERSION_UNHEALTHY=1 scripts/agent-hub upgrade --version 0.2.0
  [ "$status" -ne 0 ]
  [ "$(scripts/agent-hub version)" = "0.1.0" ]
}
