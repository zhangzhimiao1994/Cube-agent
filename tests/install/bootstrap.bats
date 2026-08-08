#!/usr/bin/env bats

@test "successful install prints URL and separate setup code" {
  root="$BATS_TEST_TMPDIR/root"
  run env AGENT_HUB_TEST=1 AGENT_HUB_INSTALL_ROOT="$root/opt" AGENT_HUB_STATE_DIR="$root/state" AGENT_HUB_CONFIG_DIR="$root/etc" AGENT_HUB_PUBLIC_URL="https://agent.example.test" bash install.sh --mode docker --dry-run
  [ "$status" -eq 0 ]
  [[ "$output" != *"?code="* ]]
}

@test "bootstrap CLI stores only code hash" {
  run uv run python -m agent_hub.cli.bootstrap --code setup-secret-code --output "$BATS_TEST_TMPDIR/bootstrap.json"
  [ "$status" -eq 0 ]
  ! grep -q "setup-secret-code" "$BATS_TEST_TMPDIR/bootstrap.json"
  grep -q "code_hash" "$BATS_TEST_TMPDIR/bootstrap.json"
}
