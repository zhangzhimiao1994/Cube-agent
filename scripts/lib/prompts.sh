#!/usr/bin/env bash

prompt_yes_no() {
  local message="$1"
  local default="${2:-yes}"
  if [[ "$ASSUME_YES" -eq 1 || "${AGENT_HUB_TEST:-0}" == "1" ]]; then
    [[ "$default" == "yes" ]]
    return
  fi
  read -r -p "$message [$default]: " answer
  answer="${answer:-$default}"
  [[ "$answer" == "yes" || "$answer" == "y" ]]
}
