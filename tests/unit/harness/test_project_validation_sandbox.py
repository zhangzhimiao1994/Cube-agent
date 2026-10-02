"""Focused boundary tests, also runnable alone on an unprivileged Linux host."""

from __future__ import annotations

import importlib.util
import io
import json
import os
import runpy
import shutil
import socket
import subprocess
import sys
import tarfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

_SOURCE = (
    Path(__file__).resolve().parents[3]
    / "src/agent_hub/harness/project_validation_sandbox.py"
)
_SPEC = importlib.util.spec_from_file_location("validation_sandbox", _SOURCE)
assert _SPEC is not None and _SPEC.loader is not None
sandbox = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(sandbox)


@pytest.fixture
def assembly(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setattr(sandbox, "sandbox_available", lambda: True)
    monkeypatch.setattr(sandbox, "_NODE_HOME", Path("/absent-trusted-node-home"))
    return sandbox


def test_minimal_mounts_namespaces_and_environment(assembly: Any, tmp_path: Path) -> None:
    command = assembly.generated_command(("npm", "test"), cwd=tmp_path, config={})
    assert command[0] == "/usr/bin/bwrap"
    for option in (
        "--unshare-user", "--unshare-pid", "--unshare-ipc", "--unshare-uts",
        "--unshare-net", "--die-with-parent", "--new-session", "--clearenv",
    ):
        assert option in command
    assert command[command.index("--cap-drop") + 1] == "ALL"
    mounts = [tuple(command[i + 1:i + 3]) for i, value in enumerate(command) if value == "--bind"]
    assert mounts == [(str(tmp_path.resolve()), "/workspace")]
    readonly = [
        tuple(command[i + 1:i + 3]) for i, value in enumerate(command) if value == "--ro-bind"
    ]
    assert ("/", "/") not in readonly
    assert ("/usr", "/usr") in readonly and ("/lib", "/lib") in readonly
    assert any(
        command[i:i + 3] == ["--symlink", "usr/bin", "/bin"]
        for i in range(len(command) - 2)
    ), "npm build/start requires /bin/sh inside the sandbox"
    assert (("/lib64", "/lib64") in readonly) == Path("/lib64").exists()
    assert (str(_SOURCE.parents[2]), "/opt/validator/src") in readonly
    assert all(source not in {"/opt", "/etc", "/run", "/var", "/home"} for source, _ in readonly)
    for path in ("/tmp", "/run", "/var"):
        assert any(command[i:i + 2] == ["--tmpfs", path] for i in range(len(command) - 1))
    env = assembly.sandbox_environment({"OPENAI_API_KEY": "secret", "PATH": "/host/bin"})
    assert "OPENAI_API_KEY" not in env and env["PATH"] == "/usr/bin:/bin"
    assert env["HOME"] == "/tmp/validation-home" and env["TMPDIR"] == "/tmp"
    assert env["NPM_CONFIG_USERCONFIG"] != env["NPM_CONFIG_GLOBALCONFIG"]
    assert env["NPM_CONFIG_USERCONFIG"] == "/tmp/npm-user.npmrc"
    assert env["NPM_CONFIG_GLOBALCONFIG"] == "/tmp/npm-global.npmrc"
    assert command[-3:] == ["--", "/usr/bin/npm", "test"]


def test_shared_network_only_for_fixed_safe_install(assembly: Any, tmp_path: Path) -> None:
    command = assembly.generated_command(assembly._INSTALL, cwd=tmp_path, config={})
    assert "--unshare-net" not in command
    assert command[-5:] == ["/usr/bin/npm", *assembly._INSTALL[1:]]
    for unsafe in (
        ("npm", "install"), ("npm", "ci"), ("npm", "i"),
        (*assembly._INSTALL, "--ignore-scripts=false"),
    ):
        with pytest.raises(RuntimeError, match="fixed npm install"):
            assembly.generated_command(unsafe, cwd=tmp_path, config={})
    for private in (("node", "-e", "pass"), ("npm", "run", "build"), ("npx", "anything")):
        assert "--unshare-net" in assembly.generated_command(private, cwd=tmp_path, config={})


def test_trusted_node_home_is_only_additional_runtime_mount(
    assembly: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "trusted-node"
    home.mkdir()
    monkeypatch.setattr(assembly, "_NODE_HOME", home)
    command = assembly.generated_command(("npm", "test"), cwd=tmp_path, config={})
    assert any(
        command[i:i + 3] == ["--ro-bind", str(home), "/opt/validator/node"]
        for i in range(len(command) - 2)
    )
    assert command[-2:] == ["/opt/validator/node/bin/npm", "test"]
    assert assembly.sandbox_environment({})["PATH"].startswith("/opt/validator/node/bin:")


@pytest.mark.parametrize("registry", ("https://user:secret@example.test", "file:///etc", "https://x/?token=x"))
def test_registry_cannot_inject_credentials(assembly: Any, registry: str) -> None:
    with pytest.raises(RuntimeError, match="credential-free"):
        assembly.sandbox_environment({"NPM_CONFIG_REGISTRY": registry})


def test_missing_authorization_or_bwrap_is_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sandbox, "_PLATFORM", "linux")
    monkeypatch.setenv("AGENT_HUB_GENERATED_PROJECT_VALIDATION_SANDBOX", "systemd")
    monkeypatch.setattr(sandbox, "_BWRAP", tmp_path / "missing-bwrap")
    assert sandbox.sandbox_available() is False
    with pytest.raises(RuntimeError, match="sandbox required"):
        sandbox.generated_command(("npm", "test"), cwd=tmp_path, config={})
    monkeypatch.delenv("AGENT_HUB_GENERATED_PROJECT_VALIDATION_SANDBOX")
    assert sandbox.sandbox_available() is False


def _real_sandbox() -> None:
    if sys.platform != "linux" or not sandbox.sandbox_available():
        pytest.skip("requires authorized Linux bwrap execution")


def _execute(command: list[str], root: Path, timeout: float = 20) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command, cwd=root, env={"PATH": "/usr/bin:/bin", "API_SECRET_SENTINEL": "host-secret"},
        capture_output=True, text=True, timeout=timeout, check=False,
    )


def test_real_private_filesystem_loopback_and_host_network_denial(tmp_path: Path) -> None:
    _real_sandbox()
    root = tmp_path / "case"
    root.mkdir()
    secret = tmp_path / "outside-secret"
    secret.write_text("host-secret")
    (root / "escape").symlink_to(secret)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        script = r"""
const fs = require('node:fs'), http = require('node:http');
if (process.env.API_SECRET_SENTINEL) throw Error('host env leaked');
for (const p of ['/var/lib/agent-hub', '/run/agent-hub', '/etc/agent-hub', 'escape']) {
  try { fs.readFileSync(p); throw Error('host file visible: ' + p); }
  catch (e) { if (!['ENOENT','EACCES','EROFS'].includes(e.code)) throw e; }
}
try { fs.writeFileSync('/usr/forbidden-sandbox-write', 'x'); throw Error('usr writable'); }
catch (e) { if (!['ENOENT','EACCES','EROFS'].includes(e.code)) throw e; }
fs.writeFileSync('owned-write', 'ok');
const server = http.createServer((req, res) => res.end('private-loopback'));
server.listen(0, '127.0.0.1', async () => {
  try {
    const response = await fetch('http://127.0.0.1:' + server.address().port);
    if (await response.text() !== 'private-loopback') throw Error('loopback failed');
    await new Promise(resolve => server.close(resolve));
    try { await fetch('http://127.0.0.1:' + process.argv[1], {signal: AbortSignal.timeout(1000)});
      throw Error('host listener visible'); }
    catch (e) { if (e.message === 'host listener visible') throw e; }
    console.log('isolated-ok');
  } catch (e) { console.error(e); process.exitCode=1; server.close(); }
});
"""
        result = _execute(sandbox.generated_command(
            ("node", "-e", script, str(port)), cwd=root, config={},
        ), root)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "isolated-ok"
    assert (root / "owned-write").read_text() == "ok"
    assert secret.read_text() == "host-secret"


def test_real_install_cannot_execute_lifecycle_scripts(tmp_path: Path) -> None:
    _real_sandbox()
    (tmp_path / "package.json").write_text(json.dumps({
        "private": True, "scripts": {"preinstall": "node -e \"throw Error('lifecycle ran')\""},
    }))
    result = _execute(sandbox.generated_command(sandbox._INSTALL, cwd=tmp_path, config={}), tmp_path)
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "package-lock.json").is_file()


@pytest.mark.parametrize("fault", (None, "patch_noop", "volatile"))
def test_real_requirements_cli_checks_http_and_restart_persistence(
    tmp_path: Path, fault: str | None,
) -> None:
    _real_sandbox()
    app = runpy.run_path(str(Path(__file__).with_name("test_project_requirements.py")))["_APP"]
    (tmp_path / "package.json").write_text(json.dumps({"scripts": {"start": "node server.cjs"}}))
    (tmp_path / "server.cjs").write_text(app)
    # Generated Python modules must not shadow the trusted stdlib CLI.
    (tmp_path / "json.py").write_text("raise RuntimeError('generated module loaded')")
    if fault:
        (tmp_path / "fault.txt").write_text(fault)
    failures = sandbox.validate_requirements(tmp_path, "small", 15)
    if fault is None:
        assert failures == ()
    else:
        assert failures and ("PATCH" if fault == "patch_noop" else "persistence") in failures[0]
    requests = [json.loads(line) for line in (tmp_path / "requests.jsonl").read_text().splitlines()]
    assert any(item["method"] == "POST" for item in requests)
    launches = [json.loads(line) for line in (tmp_path / "launches.jsonl").read_text().splitlines()]
    assert launches and all(item["leakedSecret"] is None for item in launches)
    assert all(str(item["data"]).startswith("/tmp/") for item in launches)


def test_real_timeout_destroys_namespace_and_children(tmp_path: Path) -> None:
    _real_sandbox()
    script = "require('node:child_process').spawn('/usr/bin/node',['-e',\"setTimeout(()=>require('node:fs').writeFileSync('/workspace/leaked','bad'),1000)\"],{detached:true,stdio:'ignore'}).unref();setInterval(()=>{},1000)"
    with pytest.raises(subprocess.TimeoutExpired):
        _execute(sandbox.generated_command(("node", "-e", script), cwd=tmp_path, config={}), tmp_path, 0.5)
    # A private second invocation gives the detached child ample time to reveal a leak.
    result = _execute(sandbox.generated_command(
        ("node", "-e", "setTimeout(()=>{},1500)"), cwd=tmp_path, config={},
    ), tmp_path)
    assert result.returncode == 0, result.stderr
    assert not (tmp_path / "leaked").exists()


def _npm_runtime() -> tuple[str, Path]:
    node = shutil.which("node")
    npm = shutil.which("npm")
    if not node or not npm:
        pytest.skip("requires real Node and npm")
    cli = Path(npm).resolve()
    if sys.platform == "win32":
        cli = Path(node).parent / "node_modules/npm/bin/npm-cli.js"
    if override := os.environ.get("AGENT_HUB_TEST_NPM_CLI"):
        cli = Path(override).resolve(strict=True)
    assert cli.is_file()
    return node, cli


def _install_locally(assembly: Any, root: Path, registry: str) -> subprocess.CompletedProcess[str]:
    node, cli = _npm_runtime()
    argv = assembly.generated_command(assembly._INSTALL, cwd=root, config={
        "NPM_CONFIG_REGISTRY": registry,
    })
    inner = argv[argv.index("--") + 1:]
    if Path(inner[0]).name == "npm":
        inner = [node, str(cli), *inner[1:]]
    else:
        inner = [node, *inner[1:]]
        inner[3] = str(cli)
    env = assembly.sandbox_environment({"NPM_CONFIG_REGISTRY": registry})
    env.update({
        "PATH": os.environ.get("PATH", ""), "SYSTEMROOT": os.environ.get("SYSTEMROOT", ""),
        "HOME": str(root), "NPM_CONFIG_CACHE": str(root / "cache"),
        "NPM_CONFIG_USERCONFIG": str(root / "user.npmrc"),
        "NPM_CONFIG_GLOBALCONFIG": str(root / "global.npmrc"),
    })
    return subprocess.run(inner, cwd=root, env=env, capture_output=True, text=True,
                          timeout=40, check=False)


def test_real_git_prepare_node_preload_cannot_reach_host_network(
    assembly: Any, tmp_path: Path,
) -> None:
    node, cli = _npm_runtime()
    git = shutil.which("git")
    if not git:
        pytest.skip("requires real Git for the vulnerable control")
    requests: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            requests.append(self.path)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *args: object) -> None:
            pass

    repository = tmp_path / "dependency"
    repository.mkdir()
    (repository / "package.json").write_text(json.dumps({
        "name": "prepare-fixture", "version": "1.0.0",
        "scripts": {"prepare": "node -e \"process.exit(0)\""},
    }))
    for args in (("init",), ("add", "package.json"),
                 ("-c", "user.name=fixture", "-c", "user.email=fixture@example.invalid",
                  "commit", "-m", "fixture")):
        result = subprocess.run([git, *args], cwd=repository, capture_output=True,
                                text=True, timeout=10, check=False)
        assert result.returncode == 0, result.stderr
    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        registry = f"http://127.0.0.1:{server.server_port}"
        try:
            for protected in (False, True):
                root = tmp_path / ("protected" if protected else "vulnerable")
                root.mkdir()
                payload = root / "preload.cjs"
                payload.write_text(
                    "require('node:http').get(" + json.dumps(registry + "/preload")
                    + ",r=>r.resume()).on('error',()=>{});"
                )
                (root / ".npmrc").write_text("node-options=--require=" + payload.as_posix())
                (root / "package.json").write_text(json.dumps({
                    "private": True, "dependencies": {
                        "prepare-fixture": "git+" + repository.as_uri(),
                    },
                }))
                before = len(requests)
                if protected:
                    result = _install_locally(assembly, root, registry)
                    assert len(requests) == before, "Git preparation ran project preload on host network"
                    assert result.returncode != 0, "non-registry dependency must fail closed"
                else:
                    env = dict(os.environ, NPM_CONFIG_CACHE=str(root / "cache"),
                               NPM_CONFIG_USERCONFIG=str(root / "user.npmrc"),
                               NPM_CONFIG_GLOBALCONFIG=str(root / "global.npmrc"))
                    env.pop("NODE_OPTIONS", None)
                    env.pop("NPM_CONFIG_NODE_OPTIONS", None)
                    result = subprocess.run([node, str(cli), *assembly._INSTALL[1:]], cwd=root,
                                            env=env, capture_output=True, text=True,
                                            timeout=40, check=False)
                    assert len(requests) > before, result.stderr
        finally:
            server.shutdown()
            thread.join(timeout=3)


@pytest.fixture
def registry_server() -> Any:
    routes: dict[str, bytes] = {}
    requests: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            requests.append(self.path)
            payload = routes.get(self.path)
            self.send_response(200 if payload is not None else 404)
            self.send_header("Content-Type", "application/json" if not self.path.endswith(".tgz")
                             else "application/octet-stream")
            self.end_headers()
            self.wfile.write(payload or b"{}")

        def log_message(self, *args: object) -> None:
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}", routes, requests
        finally:
            server.shutdown()
            thread.join(timeout=3)


def _registry_package(registry_server: Any, dependencies: dict[str, str] | None = None,
                      field: str = "dependencies") -> None:
    url, routes, _ = registry_server
    manifest = {"name": "safe-fixture", "version": "1.0.0", field: dependencies or {},
                "scripts": {"preinstall": "node -e \"throw Error('lifecycle ran')\""}}
    payload = json.dumps(manifest).encode()
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w:gz") as tar:
        info = tarfile.TarInfo("package/package.json")
        info.size = len(payload)
        tar.addfile(info, io.BytesIO(payload))
    routes["/safe-fixture/-/safe-fixture-1.0.0.tgz"] = archive.getvalue()
    routes["/safe-fixture"] = json.dumps({
        "name": "safe-fixture", "dist-tags": {"latest": "1.0.0"},
        "versions": {"1.0.0": {**manifest, "dist": {
            "tarball": url + "/safe-fixture/-/safe-fixture-1.0.0.tgz",
        }}},
    }).encode()


@pytest.mark.parametrize("spec", [
    "git+https://example.invalid/repo.git", "github:owner/repo", "file:./dependency",
    "link:./dependency", "https://example.invalid/package.tgz", "./dependency",
])
@pytest.mark.parametrize("field", ["dependencies", "optionalDependencies"])
def test_real_install_rejects_non_registry_root_sources(
    assembly: Any, tmp_path: Path, registry_server: Any, spec: str, field: str,
) -> None:
    url, _, requests = registry_server
    (tmp_path / "package.json").write_text(json.dumps({"private": True, field: {"unsafe": spec}}))
    result = _install_locally(assembly, tmp_path, url)
    assert result.returncode != 0, result.stdout
    assert "generated dependency source rejected" in result.stderr
    assert not requests, "root dependency must be rejected before any download"


@pytest.mark.parametrize("field", ["dependencies", "optionalDependencies"])
@pytest.mark.parametrize("spec", ["git+https://example.invalid/repo.git", "file:./dependency",
                                  "https://example.invalid/package.tgz", "link:./dependency"])
def test_real_install_rejects_live_transitive_sources(
    assembly: Any, tmp_path: Path, registry_server: Any, field: str, spec: str,
) -> None:
    url, _, requests = registry_server
    _registry_package(registry_server, {"unsafe": spec}, field)
    (tmp_path / "package.json").write_text(json.dumps({
        "private": True, "dependencies": {"safe-fixture": "1.0.0"},
    }))
    result = _install_locally(assembly, tmp_path, url)
    assert result.returncode != 0, result.stdout
    assert "generated dependency source rejected" in result.stderr
    assert "/safe-fixture" in requests, "must exercise registry metadata resolution"


@pytest.mark.parametrize("filename", ["package-lock.json", "npm-shrinkwrap.json"])
@pytest.mark.parametrize("entry", [
    {"version": "1.0.0", "resolved": "git+https://example.invalid/repo.git"},
    {"version": "1.0.0", "resolved": "https://example.invalid/package.tgz"},
    {"resolved": "dependency", "link": True},
])
def test_real_install_rejects_lock_source_bypasses(
    assembly: Any, tmp_path: Path, registry_server: Any, filename: str, entry: dict[str, object],
) -> None:
    url, _, requests = registry_server
    (tmp_path / "package.json").write_text('{"private":true}')
    (tmp_path / filename).write_text(json.dumps({
        "lockfileVersion": 3, "packages": {"": {}, "node_modules/unsafe": entry},
    }))
    result = _install_locally(assembly, tmp_path, url)
    assert result.returncode != 0, result.stdout
    assert "generated dependency source rejected" in result.stderr
    assert not requests


@pytest.mark.parametrize("spec", ["1.0.0", "npm:safe-fixture@1.0.0"])
def test_real_registry_tarball_install_ignores_project_options_and_lifecycle(
    assembly: Any, tmp_path: Path, registry_server: Any, spec: str,
) -> None:
    url, _, requests = registry_server
    _registry_package(registry_server)
    payload = tmp_path / "preload.cjs"
    payload.write_text("require('node:fs').writeFileSync('preload-ran','bad');")
    (tmp_path / ".npmrc").write_text("node-options=--require=" + payload.as_posix()
                                     + "\nignore-scripts=false\n")
    (tmp_path / "package.json").write_text(json.dumps({
        "private": True, "dependencies": {"safe-fixture": spec},
        "scripts": {"preinstall": "node -e \"throw Error('root lifecycle ran')\""},
    }))
    result = _install_locally(assembly, tmp_path, url)
    assert result.returncode == 0, result.stderr
    assert "generated dependency source rejected" not in result.stderr
    assert (tmp_path / "node_modules/safe-fixture/package.json").is_file()
    assert any(request.endswith(".tgz") for request in requests)
    assert not (tmp_path / "preload-ran").exists()


def test_install_masks_project_npmrc_without_masking_build(
    assembly: Any, tmp_path: Path,
) -> None:
    (tmp_path / ".npmrc").write_text("node-options=--require=/workspace/preload.cjs")
    install = assembly.generated_command(assembly._INSTALL, cwd=tmp_path, config={})
    assert any(install[i:i + 3] == ["--ro-bind", "/dev/null", "/workspace/.npmrc"]
               for i in range(len(install) - 2))
    env = assembly.sandbox_environment({"NODE_OPTIONS": "--require=evil", "NPM_CONFIG_NODE_OPTIONS": "evil"})
    assert env["NODE_OPTIONS"] == env["NPM_CONFIG_NODE_OPTIONS"] == ""
    build = assembly.generated_command(("npm", "run", "build"), cwd=tmp_path, config={})
    assert "--unshare-net" in build
    assert "/workspace/.npmrc" not in build
