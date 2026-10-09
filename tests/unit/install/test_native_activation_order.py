"""Offline activation fragment only; every external command is replaced."""
import os
import shutil
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
BASH = r"C:\Program Files\Git\bin\bash.exe" if os.name == "nt" else shutil.which("bash")


def activation(case: str = "success") -> subprocess.CompletedProcess[str]:
    bash = BASH
    if bash is None:
        raise RuntimeError("Bash is required for offline activation tests")
    source = (ROOT / "scripts/lib/install_native.sh").read_text()
    begin = source.index("  systemctl daemon-reload\n")
    end = source.index('  mark_stage "native-up"', begin)
    fragment = source[begin:end]
    mocks = r'''
set -euo pipefail
worker=old
api=old
systemctl() {
  printf '%s\n' "$*"
  if [[ "$1" == enable && "$2" == --now && "$3" == agent-hub.target ]]; then
    printf 'UNSAFE_ADMISSIONS worker=%s\n' "$worker"
    api=new
  fi
  if [[ "$1" == restart && "$2" == agent-hub-worker.service ]]; then
    [[ "$CASE" != worker_restart_failure ]] || return 9
    worker=new
  fi
  if [[ "$1" == restart && "$2" == agent-hub-api.service ]]; then
    [[ "$worker" == new ]] || printf 'UNSAFE_ADMISSIONS worker=%s\n' "$worker"
    [[ "$CASE" != api_restart_failure ]] || return 9
    api=new
  fi
  if [[ "$1" == start && "$2" == agent-hub.target ]]; then
    [[ "$worker" == new && "$api" == new ]] || return 97
  fi
}
require_native_service_active() {
  printf 'active_check %s\n' "$1"
  [[ "$CASE:$1" != worker_inactive:agent-hub-worker.service ]] || return 9
}
require_native_readiness() { printf 'ready_check\n'; }
require_native_plugin_package_runtime() { :; }
require_native_preview_broker() { :; }
'''
    env = {k: v for k, v in os.environ.items()
           if k not in ("BASH_ENV", "ENV", "SHELLOPTS", "BASHOPTS") and not k.startswith("BASH_FUNC_")}
    env["CASE"] = case
    return subprocess.run([bash, "--noprofile", "--norc", "-s"], input=mocks + fragment,
                          capture_output=True, text=True, env=env, timeout=10, check=False)


class NativeActivationTests(unittest.TestCase):
    def test_missing_bash_is_an_explicit_fixture_failure(self) -> None:
        with patch.dict(activation.__globals__, BASH=None), self.assertRaises(Exception) as caught:
            activation()
        self.assertIsInstance(caught.exception, RuntimeError)
        self.assertIn("Bash", str(caught.exception))

    def test_api_is_last_and_target_does_not_restart_worker(self) -> None:
        result = activation()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("UNSAFE_ADMISSIONS", result.stdout)
        lines = result.stdout.splitlines()
        worker = lines.index("restart agent-hub-worker.service")
        api = lines.index("restart agent-hub-api.service")
        target = lines.index("start agent-hub.target")
        self.assertLess(lines.index("restart agent-hub-litellm.service"), worker)
        self.assertLess(worker, lines.index("active_check agent-hub-worker.service"))
        self.assertLess(lines.index("active_check agent-hub-worker.service"), api)
        self.assertLess(api, target)
        self.assertLess(lines.index("active_check agent-hub-api.service"), target)
        self.assertEqual(lines.count("restart agent-hub-worker.service"), 1)
        self.assertNotIn("enable --now agent-hub.target", lines)

    def test_worker_failure_never_opens_admissions(self) -> None:
        for case in ("worker_restart_failure", "worker_inactive"):
            with self.subTest(case=case):
                result = activation(case)
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn("restart agent-hub-api.service", result.stdout)
                self.assertNotIn("UNSAFE_ADMISSIONS", result.stdout)
                self.assertNotIn("start agent-hub.target", result.stdout)

    def test_api_failure_never_starts_target(self) -> None:
        result = activation("api_restart_failure")
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("start agent-hub.target", result.stdout)


if __name__ == "__main__":
    unittest.main()
