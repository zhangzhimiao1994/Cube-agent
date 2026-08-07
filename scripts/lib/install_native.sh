#!/usr/bin/env bash

install_native_mode() {
  "$SCRIPT_DIR/deploy/native/install-packages.sh" --local-db --local-redis
  mkdir -p "$INSTALL_ROOT/releases" "$STATE_DIR"
  install -m 0644 "$SCRIPT_DIR/deploy/native/agent-hub.sysusers" /usr/lib/sysusers.d/agent-hub.conf 2>/dev/null || true
  install -m 0644 "$SCRIPT_DIR/deploy/native/agent-hub.tmpfiles" /usr/lib/tmpfiles.d/agent-hub.conf 2>/dev/null || true
  if command -v systemd-sysusers >/dev/null 2>&1; then
    systemd-sysusers /usr/lib/sysusers.d/agent-hub.conf
  fi
  install -m 0644 "$SCRIPT_DIR"/deploy/native/systemd/* /etc/systemd/system/
  systemctl daemon-reload
  systemctl enable --now agent-hub.target
  mark_stage "native-up"
}
