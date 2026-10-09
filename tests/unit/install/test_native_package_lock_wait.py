"""Execute package functions offline with all package and mirror operations replaced."""

import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
BASH = r"C:\Program Files\Git\bin\bash.exe" if os.name == "nt" else shutil.which("bash")


def package_install(
    case: str = "success", *, manager: str = "apt", mirror_mode: str | None = None,
    lock_timeout: str | None = None,
) -> subprocess.CompletedProcess[str]:
    bash = BASH
    if bash is None:
        raise RuntimeError("Bash is required for offline package tests")
    source = (ROOT / "deploy/native/install-packages.sh").read_text(encoding="utf-8")
    functions = re.findall(
        r"^(?:apt_[a-z_]+|run_package_install|install_with_mirror_fallback)\(\) [({]\n.*?^[})]",
        source, re.MULTILINE | re.DOTALL,
    )
    mocks = r'''
set -Eeuo pipefail
# A virtual clock keeps lock budgets independent of host test-runner load.
unset SECONDS
SECONDS=0
manager=$MANAGER
mirror_mode=$MIRROR_MODE
packages=(python3 python3-venv bubblewrap)
update_calls=0
mock_mirror=0
sleep() {
  printf 'sleep %s\n' "$*"
  [[ "$CASE" != sleep_failure ]] || { printf 'SECRET_SENTINEL\n' >&2; return 9; }
  SECONDS=$((SECONDS + $1))
}
mktemp() {
  [[ "$CASE" != temp_failure ]] || { printf 'SECRET_SENTINEL\n' >&2; return 9; }
  command mktemp "$@"
}
rm() {
  command rm "$@" || return $?
  [[ "$CASE" != cleanup_failure && "$CASE" != cleanup_source_failure ]] || {
    printf 'SECRET_SENTINEL\n' >&2; return 9;
  }
}
emit_fetch_error() {
  if [[ "$CASE" == source_foreign ]]; then
    printf "E: Failed to fetch https://SECRET_SENTINEL.example/ubuntu/dists/jammy/InRelease  Temporary failure resolving 'SECRET_SENTINEL.example'\n" >&2
  elif [[ "$CASE" == source_auth ]]; then
    printf 'E: Failed to fetch http://archive.ubuntu.com/ubuntu/dists/jammy/InRelease  401 Unauthorized SECRET_SENTINEL\n' >&2
  elif [[ "$CASE" == source_connect ]]; then
    printf 'E: Failed to fetch http://archive.ubuntu.com/ubuntu/dists/jammy/InRelease  Could not connect to archive.ubuntu.com:80 (192.0.2.1). - connect (111: Connection refused)\n' >&2
  else
    printf "E: Failed to fetch http://archive.ubuntu.com/ubuntu/dists/jammy/InRelease  Temporary failure resolving 'archive.ubuntu.com'\n" >&2
  fi
  if [[ "$1" == update ]]; then
    printf 'E: Some index files failed to download. They have been ignored, or old ones used instead.\n' >&2
  else
    printf 'E: Unable to fetch some archives, maybe run apt-get update or try with --fix-missing?\n' >&2
  fi
  if [[ "$CASE" == source_mixed_lock ]]; then
    printf 'E: Could not get lock /var/lib/apt/lists/lock. It is held by process 89707 (apt)\n' >&2
  elif [[ "$CASE" == source_signature ]]; then
    printf 'E: The repository is not signed SECRET_SENTINEL\n' >&2
  elif [[ "$CASE" == source_unknown ]]; then
    printf 'E: unexpected failure SECRET_SENTINEL\n' >&2
  fi
}
apt-get() {
  printf 'apt-get %s\n' "$*"
  printf 'frontend=%s\n' "${DEBIAN_FRONTEND:-unset}"
  printf 'locale=%s\n' "${LC_ALL:-unset}"
  for file in "$TMPDIR"/*; do
    [[ ! -f "$file" ]] || printf 'private_mode=%s\n' "$(stat -c %a "$file")"
  done
  if [[ "$*" == *update* ]]; then
    update_calls=$((update_calls + 1))
    case "$CASE" in
      source_fetch|source_connect)
        if [[ "$mock_mirror" == 1 ]]; then return 0; fi
        emit_fetch_error update; return 100 ;;
      source_always|source_foreign|source_auth|source_signature|source_unknown|source_mixed_lock|cleanup_source_failure)
        emit_fetch_error update; return 100 ;;
      source_native_status)
        emit_fetch_error update; return 125 ;;
      lists_release|lists_legacy_release)
        if (( update_calls > 2 )); then return 0; fi ;;
      lists_timeout|sleep_failure|mixed_error|wrong_status) : ;;
      update_failure)
        printf 'E: package index failure SECRET_SENTINEL\n' >&2; return 100 ;;
      foreign_lock)
        printf 'E: Could not get lock /foreign/lock. It is held by process 89707 (apt)\n' >&2
        return 100 ;;
      permission_error)
        printf 'E: Could not open lock file /var/lib/apt/lists/lock - open (13: Permission denied)\n' >&2
        return 100 ;;
      url_error)
        printf 'E: Failed to fetch https://SECRET_SENTINEL/?E: Could not get lock /var/lib/apt/lists/lock. It is held by process 89707\n' >&2
        return 100 ;;
      *) return 0 ;;
    esac
    if [[ "$CASE" == lists_legacy_release ]]; then
      printf 'E: Could not get lock /var/lib/apt/lists/lock - open (11: Resource temporarily unavailable)\n' >&2
    else
      printf 'E: Could not get lock /var/lib/apt/lists/lock. It is held by process 89707 (apt)\n' >&2
    fi
    printf 'E: Unable to lock directory /var/lib/apt/lists/\n' >&2
    if [[ "$CASE" == mixed_error ]]; then printf 'E: index failure SECRET_SENTINEL\n' >&2; fi
    if [[ "$CASE" == wrong_status ]]; then return 42; fi
    return 100
  fi
  if [[ "$*" == *install* && "$CASE" == install_source_fetch && "$mock_mirror" == 0 ]]; then
    emit_fetch_error install; return 100
  fi
  if [[ "$*" == *install* && "$CASE" == lock_timeout ]]; then
    printf 'E: Could not get lock /var/lib/dpkg/lock-frontend. It is held by process 89707 (unattended-upgr)\n' >&2
    printf 'E: Unable to acquire the dpkg frontend lock (/var/lib/dpkg/lock-frontend), is another process using it?\n' >&2
    return 100
  fi
  return 0
}
dnf() {
  printf 'dnf %s\n' "$*"
  [[ "$CASE" != dnf_failure ]] || return 7
}
configure_china_package_mirror() { mock_mirror=1; printf 'mirror_configured\n'; }
'''
    entry = "install_with_mirror_fallback" if mirror_mode is not None else "run_package_install"
    # The condition deliberately reproduces the caller's errexit-suppressed context.
    invoke = f"\nif {entry}; then printf 'RESULT=ok\\n'; else rc=$?; exit \"$rc\"; fi\n"
    env = {k: v for k, v in os.environ.items()
           if k not in ("BASH_ENV", "ENV", "SHELLOPTS", "BASHOPTS", "DEBIAN_FRONTEND",
                        "AGENT_HUB_APT_LOCK_TIMEOUT_SECONDS")
           and not k.startswith("BASH_FUNC_")}
    env.update(CASE=case, MANAGER=manager, MIRROR_MODE=mirror_mode or "official")
    if lock_timeout is not None:
        env["AGENT_HUB_APT_LOCK_TIMEOUT_SECONDS"] = lock_timeout
    with tempfile.TemporaryDirectory(prefix="native-package-offline-") as directory:
        env["TMPDIR"] = directory
        result = subprocess.run([bash, "--noprofile", "--norc", "-s"],
                                input=mocks + "\n".join(functions) + invoke,
                                text=True, capture_output=True, env=env, timeout=10, check=False)
        if any(Path(directory).iterdir()):
            raise RuntimeError("Package stderr temporary file was not cleaned up")
        return result


class NativePackageLockTests(unittest.TestCase):
    def test_install_uses_finite_native_dpkg_lock_wait(self) -> None:
        result = package_install()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("apt-get -o DPkg::Lock::Timeout=120 install -y python3 python3-venv bubblewrap",
                      result.stdout)
        self.assertIn("frontend=noninteractive", result.stdout)
        self.assertEqual(result.stdout.count("apt-get "), 2)

    def test_update_failure_cannot_be_masked_by_successful_install(self) -> None:
        result = package_install("update_failure")
        self.assertEqual(result.returncode, 100, result.stderr)
        self.assertNotIn("install -y", result.stdout)
        self.assertNotIn("RESULT=ok", result.stdout)

    def test_lock_wait_expiry_stays_failure(self) -> None:
        result = package_install("lock_timeout")
        self.assertEqual(result.returncode, 100, result.stderr)
        self.assertNotIn("RESULT=ok", result.stdout)
        self.assertEqual(result.stdout.count("install -y"), 1)

    def test_auto_update_failures_never_reach_install(self) -> None:
        result = package_install("update_failure", mirror_mode="auto")
        self.assertEqual(result.returncode, 100, result.stderr)
        self.assertEqual(result.stdout.count("apt-get update"), 1)
        self.assertNotIn("mirror_configured", result.stdout)
        self.assertNotIn("install -y", result.stdout)
        self.assertNotIn("RESULT=ok", result.stdout)

    def test_china_update_failure_is_closed(self) -> None:
        result = package_install("update_failure", mirror_mode="china")
        self.assertEqual(result.returncode, 100, result.stderr)
        self.assertEqual(result.stdout.count("mirror_configured"), 1)
        self.assertNotIn("install -y", result.stdout)

    def test_official_failure_does_not_switch_mirrors(self) -> None:
        result = package_install("update_failure", mirror_mode="official")
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("mirror_configured", result.stdout)
        self.assertNotIn("install -y", result.stdout)

    def test_mirror_selection_success_behavior_is_unchanged(self) -> None:
        for mode in ("official", "auto", "china"):
            with self.subTest(mode=mode):
                result = package_install(mirror_mode=mode)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.count("mirror_configured"), int(mode == "china"))
                self.assertEqual(result.stdout.count("install -y"), 1)

    def test_dnf_behavior_is_unchanged(self) -> None:
        for case, status in (("success", 0), ("dnf_failure", 7)):
            with self.subTest(case=case):
                result = package_install(case, manager="dnf")
                self.assertEqual(result.returncode, status, result.stderr)
                self.assertIn("dnf install -y python3 python3-venv bubblewrap", result.stdout)
                self.assertNotIn("apt-get", result.stdout)
                self.assertNotIn("DPkg::Lock::Timeout", result.stdout)

    def test_confirmed_lists_lock_retries_until_release(self) -> None:
        for case in ("lists_release", "lists_legacy_release"):
            with self.subTest(case=case):
                result = package_install(case, mirror_mode="auto", lock_timeout="3")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.count("apt-get update"), 3)
                self.assertEqual(result.stdout.count("sleep 1"), 2)
                self.assertIn("DPkg::Lock::Timeout=3 install -y", result.stdout)
                self.assertNotIn("mirror_configured", result.stdout)

    def test_lists_lock_timeout_never_installs_or_switches_mirror(self) -> None:
        result = package_install("lists_timeout", mirror_mode="auto", lock_timeout="2")
        self.assertEqual(result.returncode, 100, result.stderr)
        self.assertLessEqual(result.stdout.count("apt-get update"), 3)
        self.assertIn("APT_FAILURE=lists_lock_timeout", result.stderr)
        self.assertNotIn("install -y", result.stdout)
        self.assertNotIn("mirror_configured", result.stdout)

    def test_install_lock_failure_never_switches_mirror(self) -> None:
        result = package_install("lock_timeout", mirror_mode="auto")
        self.assertEqual(result.returncode, 100, result.stderr)
        self.assertIn("APT_FAILURE=install_lock", result.stderr)
        self.assertEqual(result.stdout.count("install -y"), 1)
        self.assertNotIn("mirror_configured", result.stdout)
        self.assertNotIn("sleep 1", result.stdout)

    def test_nonlock_and_mixed_errors_are_immediate_private_failures(self) -> None:
        for case in ("update_failure", "foreign_lock", "permission_error", "url_error",
                     "mixed_error", "wrong_status", "source_native_status", "source_foreign",
                     "source_auth", "source_signature", "source_unknown", "source_mixed_lock"):
            with self.subTest(case=case):
                result = package_install(case, mirror_mode="auto", lock_timeout="2")
                self.assertEqual(result.returncode, 100, result.stderr)
                self.assertEqual(result.stdout.count("apt-get update"), 1)
                self.assertNotIn("sleep 1", result.stdout)
                self.assertNotIn("install -y", result.stdout)
                self.assertNotIn("mirror_configured", result.stdout)
                self.assertNotIn("SECRET_SENTINEL", result.stdout + result.stderr)

    def test_invalid_timeout_fails_before_apt_or_mirror_writes(self) -> None:
        for value in ("", "0", "-1", "01", "601", "999999999999999999", "$(id)"):
            with self.subTest(value=value):
                result = package_install(mirror_mode="china", lock_timeout=value)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertNotIn("apt-get ", result.stdout)
                self.assertNotIn("mirror_configured", result.stdout)
        for value in ("1", "600"):
            with self.subTest(value=value):
                result = package_install(lock_timeout=value)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("DPkg::Lock::Timeout=" + value + " install -y", result.stdout)

    def test_stderr_is_private_and_cleaned_on_all_exit_paths(self) -> None:
        for case in ("success", "update_failure", "lock_timeout", "lists_timeout",
                     "temp_failure", "sleep_failure", "cleanup_failure", "cleanup_source_failure"):
            with self.subTest(case=case):
                result = package_install(case, mirror_mode="auto", lock_timeout="2")
                self.assertNotIn("SECRET_SENTINEL", result.stdout + result.stderr)
                if case != "success":
                    self.assertNotEqual(result.returncode, 0)
                self.assertNotIn("mirror_configured", result.stdout)
                if case == "temp_failure":
                    self.assertNotEqual(result.returncode, 0)
                    self.assertNotIn("apt-get ", result.stdout)
                else:
                    self.assertIn("private_mode=600", result.stdout)
                    self.assertIn("locale=C", result.stdout)

    def test_definite_official_fetch_failure_falls_back_once(self) -> None:
        for case in ("source_fetch", "source_connect", "install_source_fetch"):
            with self.subTest(case=case):
                result = package_install(case, mirror_mode="auto")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.count("mirror_configured"), 1)
                self.assertEqual(result.stdout.count("apt-get update"), 2)
                self.assertEqual(result.stdout.count("install -y"),
                                 2 if case == "install_source_fetch" else 1)
                self.assertNotIn("sleep 1", result.stdout)
                self.assertNotIn("SECRET_SENTINEL", result.stdout + result.stderr)

    def test_fetch_failure_public_status_is_100_and_fallback_is_bounded(self) -> None:
        for mode in ("auto", "official", "china"):
            with self.subTest(mode=mode):
                result = package_install("source_always", mirror_mode=mode)
                self.assertEqual(result.returncode, 100, result.stderr)
                self.assertEqual(result.stdout.count("apt-get update"), 2 if mode == "auto" else 1)
                self.assertEqual(result.stdout.count("mirror_configured"), int(mode != "official"))
                self.assertNotIn("install -y", result.stdout)


if __name__ == "__main__":
    unittest.main()
