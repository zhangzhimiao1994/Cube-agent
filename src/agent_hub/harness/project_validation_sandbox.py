"""Minimal Linux execution boundary for generated projects (stdlib-only CLI)."""

from __future__ import annotations

import json
import math
import os
import runpy
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from urllib.parse import urlsplit

_PLATFORM = sys.platform
_BWRAP = Path("/usr/bin/bwrap")
_NODE_HOME = Path("/opt/agent-hub/node")
_INSTALL = ("npm", "install", "--ignore-scripts", "--no-audit", "--no-fund")
_ERROR = "bwrap sandbox required: Linux, environment authorization and /usr/bin/bwrap"
_VALIDATORS = {
    "small": "_validate_small_task_api",
    "medium": "_validate_medium_crm_api",
    "large": "_validate_large_order_ops_api",
    "ultra": "_validate_ultra_portfolio_api",
}

# Only trusted npm code runs with shared networking. Gate both manifests/locks and
# live transitive resolution; blocking child processes also excludes Git prepare.
_INSTALL_GUARD = r"""
const fs = require('node:fs'), path = require('node:path');
const Module = require('node:module'), cp = require('node:child_process');
let denied = false;
function reject(reason) {
  denied = true;
  process.stderr.write('generated dependency source rejected: ' + reason + '\n');
  throw Error('generated dependency source rejected: ' + reason);
}
process.on('exit', () => { if (denied) process.exitCode = 1; });
for (const key of ['spawn', 'spawnSync', 'exec', 'execSync', 'execFile', 'execFileSync', 'fork']) {
  cp[key] = () => reject('subprocess execution is forbidden');
}
Module.syncBuiltinESMExports();
process.env.NODE_OPTIONS = '';
process.env.NPM_CONFIG_NODE_OPTIONS = '';
process.env.npm_config_node_options = '';
process.env.NPM_CONFIG_IGNORE_SCRIPTS = 'true';
const cli = fs.realpathSync(process.argv[1]);
const npa = require(require.resolve('npm-package-arg', {paths: [path.dirname(cli)]}));
function parse(name, version) {
  try { return version === undefined ? npa(name) : npa.resolve(name, version); }
  catch { return reject('invalid dependency specification'); }
}
const registry = new URL(process.env.NPM_CONFIG_REGISTRY);
function registryTarball(value) {
  let url;
  try { url = new URL(value); } catch { return false; }
  return url.origin === registry.origin && url.pathname.startsWith(registry.pathname)
    && url.pathname.endsWith('.tgz') && !url.username && !url.password
    && !url.search && !url.hash;
}
function check(spec, allowTarball) {
  if (spec.type === 'alias') { check(spec.subSpec, false); return spec; }
  if (['version', 'range', 'tag'].includes(spec.type)) return spec;
  if (allowTarball && spec.type === 'remote' && registryTarball(spec.fetchSpec)) return spec;
  return reject('unsupported dependency source ' + spec.type);
}
function inspect(pkg, legacy = false) {
  if (!pkg || typeof pkg !== 'object' || Array.isArray(pkg)) reject('invalid package metadata');
  if (pkg.link || (pkg.workspaces && Object.keys(pkg.workspaces).length)) reject('local workspaces/links');
  if (pkg.resolved && !registryTarball(pkg.resolved)) reject('non-registry lock resolution');
  if (legacy && pkg.version) check(parse(pkg.version), true);
  for (const field of ['dependencies', 'devDependencies', 'optionalDependencies', 'peerDependencies']) {
    for (const [name, value] of Object.entries(pkg[field] || {})) {
      if (legacy && value && typeof value === 'object') inspect(value, true);
      else {
        if (typeof value !== 'string') reject('invalid dependency specification');
        check(parse(name, value), false);
      }
    }
  }
}
inspect(JSON.parse(fs.readFileSync('package.json', 'utf8')));
if (fs.existsSync('node_modules')) reject('pre-existing node_modules');
for (const filename of ['npm-shrinkwrap.json', 'package-lock.json']) {
  if (!fs.existsSync(filename)) continue;
  const lock = JSON.parse(fs.readFileSync(filename, 'utf8'));
  inspect(lock, true);
  for (const pkg of Object.values(lock.packages || {})) inspect(pkg);
}
const originalLoad = Module._load;
Module._load = function(request, parent, isMain) {
  const loaded = originalLoad.call(this, request, parent, isMain);
  if (request !== 'npm-package-arg') return loaded;
  // npm probes absent lock metadata with npa(null) and catches its native error.
  // Preserve that null sentinel without weakening rejection of malformed sources.
  const guarded = (...args) => {
    if (args[0] === null) return loaded(...args);
    try { return check(loaded(...args), true); }
    catch (error) { if (denied) throw error; return reject('invalid dependency specification'); }
  };
  Object.assign(guarded, loaded);
  guarded.resolve = (...args) => {
    try { return check(loaded.resolve(...args), true); }
    catch (error) { if (denied) throw error; return reject('invalid dependency specification'); }
  };
  return guarded;
};
process.argv = [process.execPath, cli, ...process.argv.slice(2)];
require(cli);
"""


def sandbox_available() -> bool:
    # This authorizes attempting bwrap; only a successful launch creates isolation.
    return (
        _PLATFORM == "linux"
        and os.environ.get("AGENT_HUB_GENERATED_PROJECT_VALIDATION_SANDBOX")
        in {"bwrap", "systemd"}
        and _BWRAP.is_file()
        and os.access(_BWRAP, os.X_OK)
    )


def sandbox_environment(config: Mapping[str, str]) -> dict[str, str]:
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": "/tmp/validation-home",
        "USERPROFILE": "/tmp/validation-home",
        "TMPDIR": "/tmp", "TMP": "/tmp", "TEMP": "/tmp",
        "NPM_CONFIG_CACHE": "/tmp/npm-cache",
        "NPM_CONFIG_USERCONFIG": "/tmp/npm-user.npmrc",
        "NPM_CONFIG_GLOBALCONFIG": "/tmp/npm-global.npmrc",
        "NPM_CONFIG_AUDIT": "false", "NPM_CONFIG_FUND": "false",
        "NPM_CONFIG_UPDATE_NOTIFIER": "false",
        "NODE_OPTIONS": "", "NPM_CONFIG_NODE_OPTIONS": "",
        "NPM_CONFIG_FETCH_RETRIES": "2",
        "NPM_CONFIG_FETCH_RETRY_MINTIMEOUT": "1000",
        "NPM_CONFIG_FETCH_RETRY_MAXTIMEOUT": "10000",
    }
    registry = config.get(
        "NPM_CONFIG_REGISTRY", "https://registry.npmmirror.com",
    )
    parsed = urlsplit(registry)
    if (
        parsed.scheme not in {"http", "https"} or not parsed.hostname
        or parsed.username is not None or parsed.password is not None
        or parsed.query or parsed.fragment or any(char.isspace() for char in registry)
    ):
        raise RuntimeError("bwrap sandbox: registry must be a credential-free HTTP(S) URL")
    env["NPM_CONFIG_REGISTRY"] = registry
    if _NODE_HOME.is_dir():
        env["PATH"] = "/opt/validator/node/bin:/usr/bin:/bin"
    return env


def sandbox_command(
    command: Sequence[str], *, cwd: Path, shared_network: bool,
    config: Mapping[str, str],
) -> list[str]:
    if not sandbox_available():
        raise RuntimeError(_ERROR)
    root = cwd.resolve(strict=True)
    if not root.is_dir():
        raise RuntimeError("bwrap sandbox requires a case directory")
    trusted_src = Path(__file__).resolve().parents[2]
    argv = [
        _BWRAP.as_posix(), "--die-with-parent", "--new-session", "--cap-drop", "ALL",
        "--unshare-user", "--unshare-pid", "--unshare-ipc", "--unshare-uts",
        "--ro-bind", "/usr", "/usr",
        "--symlink", "usr/bin", "/bin",
        "--ro-bind", "/lib", "/lib",
        "--proc", "/proc", "--dev", "/dev",
        "--tmpfs", "/tmp", "--tmpfs", "/run", "--tmpfs", "/var",
        "--dir", "/tmp/validation-home",
        "--bind", str(root), "/workspace",
        "--ro-bind", str(trusted_src), "/opt/validator/src",
        "--chdir", "/workspace", "--clearenv",
    ]
    for source in ("/lib64", "/etc/resolv.conf", "/etc/hosts", "/etc/ssl/certs"):
        if Path(source).exists():
            argv.extend(("--ro-bind", source, source))
    if _NODE_HOME.is_dir():
        argv.extend(("--ro-bind", str(_NODE_HOME.resolve(strict=True)), "/opt/validator/node"))
    if not shared_network:
        argv.append("--unshare-net")
    elif (root / ".npmrc").exists() or (root / ".npmrc").is_symlink():
        argv.extend(("--ro-bind", "/dev/null", "/workspace/.npmrc"))
    for key, value in sandbox_environment(config).items():
        argv.extend(("--setenv", key, value))
    return [*argv, "--", *command]


def generated_command(
    command: Sequence[str], *, cwd: Path, config: Mapping[str, str],
) -> list[str]:
    name = Path(command[0]).name.casefold()
    if name not in {"npm", "node", "npx"}:
        raise RuntimeError("bwrap sandbox: unsupported generated executable")
    normalized = (name, *command[1:])
    shared_network = normalized == _INSTALL
    if (
        name == "npm" and len(command) > 1 and command[1] in {"install", "i", "ci"}
        and not shared_network
    ):
        raise RuntimeError("bwrap sandbox: only fixed npm install --ignore-scripts is allowed")
    binary = f"/opt/validator/node/bin/{name}" if _NODE_HOME.is_dir() else f"/usr/bin/{name}"
    if shared_network:
        node = "/opt/validator/node/bin/node" if _NODE_HOME.is_dir() else "/usr/bin/node"
        inner = (node, "-e", _INSTALL_GUARD, binary, *command[1:])
    else:
        inner = (binary, *command[1:])
    return sandbox_command(
        inner, cwd=cwd,
        shared_network=shared_network, config=config,
    )


def validate_requirements(root: Path, scale: str, timeout_seconds: float) -> tuple[str, ...]:
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        return ("timeout_seconds must be finite and positive",)
    if scale not in _VALIDATORS:
        return ("bwrap sandbox: independent evaluator unavailable for this scale",)
    try:
        command = sandbox_command(
            (
                "/usr/bin/python3", "-I",
                "/opt/validator/src/agent_hub/harness/project_validation_sandbox.py",
                "requirements", scale, str(timeout_seconds),
            ),
            cwd=root, shared_network=False, config={},
        )
        completed = subprocess.run(
            command, cwd=root, env={"PATH": "/usr/bin:/bin"},
            stdin=subprocess.DEVNULL, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout_seconds, check=False,
        )
        if completed.returncode != 0:
            return (f"bwrap sandbox validator failed: exit={completed.returncode}",)
        payload = json.loads(completed.stdout)
        if not isinstance(payload, list) or not all(
            isinstance(reason, str) and reason for reason in payload
        ):
            return ("bwrap sandbox validator returned an invalid result",)
        return tuple(payload)
    except subprocess.TimeoutExpired:
        return ("timeout: bwrap sandbox requirements validation deadline exceeded",)
    except (OSError, RuntimeError, ValueError) as exc:
        return (f"bwrap sandbox requirements unavailable: {exc}",)


def validate_scale_load(root: Path, scale: str, timeout_seconds: float) -> dict[str, object]:
    return _validate_scale_check(root, scale, timeout_seconds, operation="portfolio-load",
                                 profile="ultra-load-v1")


def validate_scale_storage(root: Path, scale: str, timeout_seconds: float) -> dict[str, object]:
    return _validate_scale_check(root, scale, timeout_seconds, operation="portfolio-storage",
                                 profile="ultra-load-storage-v1")


def _validate_scale_check(
    root: Path, scale: str, timeout_seconds: float, *, operation: str, profile: str,
) -> dict[str, object]:
    # Host-only imports: the isolated CLI must never import the normal harness package.
    from agent_hub.harness.project_validation_result import (
        scale_validation_unknown,
        validate_scale_validation_result,
    )

    def unknown(reason: str) -> dict[str, object]:
        return scale_validation_unknown(reason, profile=profile)

    def strict_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate result key: {key}")
            result[key] = value
        return result

    def reject_constant(value: str) -> object:
        raise ValueError(f"nonfinite result number: {value}")

    try:
        if (
            type(timeout_seconds) not in (int, float)
            or not math.isfinite(timeout_seconds) or timeout_seconds <= 0
        ):
            return unknown("timeout_seconds must be finite and positive")
        if scale != "ultra":
            return unknown("bwrap sandbox: load evaluator unavailable for this scale")
        command = sandbox_command(
            (
                "/usr/bin/python3", "-I",
                "/opt/validator/src/agent_hub/harness/project_validation_sandbox.py",
                operation, scale, str(timeout_seconds),
            ),
            cwd=root, shared_network=False, config={},
        )
        completed = subprocess.run(
            command, cwd=root, env={"PATH": "/usr/bin:/bin"},
            stdin=subprocess.DEVNULL, capture_output=True, text=True,
            encoding="utf-8", errors="strict", timeout=timeout_seconds, check=False,
        )
        if completed.returncode != 0:
            return unknown(
                f"bwrap sandbox load validator failed: exit={completed.returncode}",
            )
        payload = json.loads(
            completed.stdout, object_pairs_hook=strict_object, parse_constant=reject_constant,
        )
        return validate_scale_validation_result(payload, expected_profile=profile)
    except subprocess.TimeoutExpired:
        return unknown("timeout: bwrap sandbox load validation deadline exceeded")
    except (OSError, RuntimeError, ValueError, OverflowError) as exc:
        return unknown(f"bwrap sandbox load validation unavailable: {exc}")


def _main() -> None:
    if len(sys.argv) != 4:
        raise SystemExit(2)
    if sys.argv[1] == "requirements" and sys.argv[2] in _VALIDATORS:
        validator = _VALIDATORS[sys.argv[2]]
    elif sys.argv[1] in {"portfolio-load", "portfolio-storage"} and sys.argv[2] == "ultra":
        try:
            timeout = float(sys.argv[3])
        except ValueError:
            raise SystemExit(2) from None
        if not math.isfinite(timeout) or timeout <= 0:
            raise SystemExit(2)
        validator = ("_validate_ultra_portfolio_storage" if sys.argv[1] == "portfolio-storage"
                     else "_validate_ultra_portfolio_load")
    else:
        raise SystemExit(2)
    # Load only the trusted stdlib validator; -I excludes generated cwd/PYTHONPATH.
    validators = runpy.run_path(str(Path(__file__).with_name("project_requirements.py")))
    result = validators[validator](Path("/workspace"), float(sys.argv[3]))
    print(json.dumps(result))


if __name__ == "__main__":
    _main()
