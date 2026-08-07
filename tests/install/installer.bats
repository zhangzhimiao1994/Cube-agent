#!/usr/bin/env bats

@test "mode flag bypasses interactive selection" {
  run env AGENT_HUB_TEST=1 AGENT_HUB_INSTALL_ROOT="$BATS_TEST_TMPDIR/opt" AGENT_HUB_STATE_DIR="$BATS_TEST_TMPDIR/state" AGENT_HUB_CONFIG_DIR="$BATS_TEST_TMPDIR/etc" bash install.sh --mode native --dry-run
  [ "$status" -eq 0 ]
  [[ "$output" == *"mode=native"* ]]
}

@test "auto mode selects docker for unknown linux" {
  run env AGENT_HUB_TEST=1 AGENT_HUB_INSTALL_ROOT="$BATS_TEST_TMPDIR/opt" AGENT_HUB_STATE_DIR="$BATS_TEST_TMPDIR/state" AGENT_HUB_CONFIG_DIR="$BATS_TEST_TMPDIR/etc" bash install.sh --mode auto --dry-run
  [ "$status" -eq 0 ]
  [[ "$output" == *"selected mode="* ]]
}

@test "rerun never replaces existing secrets" {
  root="$BATS_TEST_TMPDIR/root"
  run env AGENT_HUB_TEST=1 AGENT_HUB_INSTALL_ROOT="$root/opt" AGENT_HUB_STATE_DIR="$root/state" AGENT_HUB_CONFIG_DIR="$root/etc" bash install.sh --mode docker --dry-run
  [ "$status" -eq 0 ]
  first="$(sha256sum "$root/etc/secrets.env" | cut -d' ' -f1)"
  run env AGENT_HUB_TEST=1 AGENT_HUB_INSTALL_ROOT="$root/opt" AGENT_HUB_STATE_DIR="$root/state" AGENT_HUB_CONFIG_DIR="$root/etc" bash install.sh --mode docker --dry-run
  [ "$status" -eq 0 ]
  second="$(sha256sum "$root/etc/secrets.env" | cut -d' ' -f1)"
  [ "$first" = "$second" ]
}
