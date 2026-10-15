"""Real nonroot bwrap and offline installed SDKs; not model or ordinary-login acceptance."""

from __future__ import annotations

import asyncio
import base64
import csv
import hashlib
import io
import json
import os
import socket
import sys
import sysconfig
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4
from zipfile import ZipFile

import pytest

from agent_hub.api.routers.admin import PluginCapabilityRequest, PluginResourceResponse
from agent_hub.capability_installer.environment import (
    CapabilityEnvironmentManager,
    CliArtifactSpec,
    PythonEnvironmentSpec,
    tenant_environment_id,
)
from agent_hub.capability_installer.managed_scope import ManagedEnvironmentPluginAdapter
from agent_hub.capability_installer.scoped_probe import build_scoped_cli_probe
from agent_hub.execution_scope_policy import load_execution_scope_policy
from agent_hub.plugins.runtime import PluginInvocationContext

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        sys.platform != "linux"
        or os.environ.get("AGENT_HUB_NATIVE_MANAGED_ENVIRONMENT_PROBE") != "1",
        reason="explicit owned native Linux managed environment opt-in required",
    ),
]
TENANT = UUID("11111111-1111-4111-8111-111111111111")


def wheel(cache: Path) -> Path:
    files = {
        "managed_fixture/__init__.py": b"VALUE = 9\n",
        "managed_fixture-1.0.0.dist-info/METADATA": b"Metadata-Version: 2.1\nName: managed-fixture\nVersion: 1.0.0\n",
        "managed_fixture-1.0.0.dist-info/WHEEL": b"Wheel-Version: 1.0\nGenerator: native-fixture\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
    }
    rows = io.StringIO(newline="")
    writer = csv.writer(rows)
    for name, data in files.items():
        digest = (
            base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode("ascii")
        )
        writer.writerow((name, "sha256=" + digest, str(len(data))))
    record = "managed_fixture-1.0.0.dist-info/RECORD"
    writer.writerow((record, "", ""))
    files[record] = rows.getvalue().encode("utf-8")
    path = cache / "managed_fixture-1.0.0-py3-none-any.whl"
    with ZipFile(path, "x") as archive:
        for name, data in files.items():
            archive.writestr(name, data)
    return path


@pytest.mark.parametrize("kind", ["cli", "python"])
async def test_actual_installed_version_is_readonly_scoped_and_reusable(
    tmp_path: Path, kind: str
) -> None:
    assert getattr(os, "geteuid", lambda: -1)() == 10001
    program = Path(sys.executable).resolve()
    private = tmp_path / "host-private"
    private.mkdir()
    canary = private / "canary"
    canary.write_text("synthetic-private", encoding="ascii")
    workspace = tmp_path / "workspaces"
    project = workspace / "project"
    project.mkdir(parents=True)
    (project / "input.txt").write_text("authorized-data", encoding="ascii")
    root = tmp_path / "environments"
    policy_file = tmp_path / "scope.json"
    runtime = [program, Path(sysconfig.get_path("stdlib")), Path("/lib64/ld-linux-x86-64.so.2")]
    runtime.extend(
        Path("/lib/x86_64-linux-gnu") / name
        for name in (
            "libpthread.so.0",
            "libdl.so.2",
            "libutil.so.1",
            "libm.so.6",
            "librt.so.1",
            "libc.so.6",
        )
    )
    policy_file.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "bubblewrap_executable": "/usr/bin/bwrap",
                "runtime_readonly_paths": [str(path) for path in runtime],
                "native_probe_program": "/usr/bin/true",
                "bindings": [
                    {
                        "kind": "plugin",
                        "tenant_id": str(TENANT),
                        "source_id": source_id,
                        "working_directory": str(project),
                        "approved_programs": [],
                        "managed_environment_id": "fixture-tool",
                    }
                    for source_id in ("fixture-tool", "fixture-companion")
                ],
            }
        ),
        encoding="ascii",
    )
    manager = CapabilityEnvironmentManager(
        root,
        cli_probe=build_scoped_cli_probe(policy_file, (root, private, workspace)),
    )
    physical = tenant_environment_id(TENANT, "fixture-tool")
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(4)
        listener.settimeout(1)
        port = listener.getsockname()[1]
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            accepted, _ = listener.accept()
            accepted.close()
        code = (
            "import json, pathlib, socket, sys\n"
            "if sys.argv[1:] == ['--version']:\n    print('fixture 1.0.0'); raise SystemExit(0)\n"
            "assert pathlib.Path('input.txt').read_text() == 'authorized-data'\n"
            f"for target in (pathlib.Path({str(canary)!r}), pathlib.Path('unauthorized')):\n"
            "    try:\n        target.write_text('bad')\n"
            "    except OSError:\n        pass\n"
            "    else:\n        raise SystemExit(31)\n"
            f"try:\n    pathlib.Path({str(canary)!r}).read_bytes()\n"
            "except OSError:\n    pass\nelse:\n    raise SystemExit(32)\n"
            f"try:\n    socket.create_connection(('127.0.0.1', {port}), timeout=0.2).close()\n"
            "except OSError:\n    pass\nelse:\n    raise SystemExit(33)\n"
            "pathlib.Path('/tmp/managed-scratch').write_text('allowed')\n"
        )
        if kind == "cli":
            artifact = tmp_path / "artifact"
            artifact.write_text(
                "#!" + str(program) + "\n" + code + "print('verified-cli')\n", encoding="ascii"
            )
            record = await asyncio.to_thread(
                manager.deploy_cli,
                CliArtifactSpec(
                    physical,
                    "1.0.0",
                    artifact,
                    hashlib.sha256(artifact.read_bytes()).hexdigest(),
                    "tool",
                ),
            )
            arguments: dict[str, Any] = {"args": ["--inspect"]}
        else:
            cache = tmp_path / "wheels"
            cache.mkdir()
            archive = wheel(cache)
            lock = tmp_path / "requirements.lock"
            lock.write_text(
                "managed-fixture==1.0.0 --hash=sha256:"
                + hashlib.sha256(archive.read_bytes()).hexdigest()
                + "\n",
                encoding="ascii",
            )
            record = await asyncio.to_thread(
                manager.build_python,
                PythonEnvironmentSpec(
                    physical,
                    "1.0.0",
                    lock,
                    cache,
                    "managed_fixture",
                ),
            )
            code += "import managed_fixture; assert managed_fixture.VALUE == 9; print('verified-python')\n"
            arguments = {"args": ["-I", "-c", "exec(" + repr(code) + ")"]}
        policy = load_execution_scope_policy(
            policy_file,
            protected_paths=(root, private, workspace),
            environment_manager=manager,
            authorized_workspace_root=workspace,
        )
        capability = PluginCapabilityRequest(
            id="fixture.inspect",
            adapter="local_command",
            permission_class="plugin.use",
            sandbox_profile="local_process",
        )
        plugin = PluginResourceResponse(
            id="fixture-tool",
            name="Native managed fixture",
            status="running",
            health="healthy",
            timeout_seconds=20,
            resource_config={"command": str(record.path / record.executable)},
            capabilities=[capability],
        )
        for source_id in ("fixture-tool", "fixture-companion"):
            selected_capability = capability.model_copy(update={"id": source_id + ".inspect"})
            selected_plugin = plugin.model_copy(
                update={"id": source_id, "capabilities": [selected_capability]}
            )
            adapter = policy.plugin_adapters(TENANT, selected_plugin)["local_command"]
            assert isinstance(adapter, ManagedEnvironmentPluginAdapter)
            await adapter.preflight(plugin=selected_plugin)
            result = await adapter.invoke(
                plugin=selected_plugin,
                capability=selected_capability,
                arguments=arguments,
                context=PluginInvocationContext(
                    TENANT, TENANT, uuid4(), "native-fixture", uuid4().hex
                ),
            )
            assert result["ok"] is True, json.dumps(result, sort_keys=True)
            assert str(result["stdout"]).strip() == "verified-" + kind
            assert manager.active(physical) == record
            assert manager.references(physical, record.version) == ()
            assert not list(record.path.rglob("managed_fixture*.pyc"))
    assert canary.read_text("ascii") == "synthetic-private"
    assert not (project / "unauthorized").exists()
