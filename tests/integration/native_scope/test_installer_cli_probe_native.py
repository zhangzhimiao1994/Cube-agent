"""Actual nonroot namespace during installer probe; no generated/user delivery claim."""

from __future__ import annotations

import hashlib
import os
import socket
import sys
import sysconfig
from collections.abc import Mapping
from pathlib import Path
from typing import NoReturn

import pytest

from agent_hub.capability_installer.environment import (
    CapabilityEnvironmentManager,
    CliArtifactSpec,
    EnvironmentBuildError,
)
from agent_hub.capability_installer.scoped_probe import build_scoped_cli_probe
from agent_hub.plugins.runtime import LocalProcessFactory, _run_local_command

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        sys.platform != "linux" or os.environ.get("AGENT_HUB_NATIVE_INSTALLER_PROBE") != "1",
        reason="explicit native nonroot installer opt-in required",
    ),
]


def _no_host(*args: object, **kwargs: object) -> NoReturn:
    raise AssertionError("namespace probe cannot fall back to the host runner")


@pytest.mark.parametrize("missing", [False, True])
def test_installer_namespace_probe_denies_host_reads_writes_and_live_tcp(
    tmp_path: Path, missing: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    import json

    from agent_hub.capability_installer import scoped_probe

    diagnostics: list[dict[str, object]] = []

    async def recorded_command(
        argv: tuple[str, ...],
        *,
        timeout_seconds: float,
        environment: Mapping[str, str],
        max_output_bytes: int,
        process_factory: LocalProcessFactory | None = None,
    ) -> tuple[int, str, str]:
        result = await _run_local_command(
            argv,
            timeout_seconds=timeout_seconds,
            environment=environment,
            max_output_bytes=max_output_bytes,
            process_factory=process_factory,
        )
        if result[0] != 0:
            # This generated fixture emits no user data or credentials.
            diagnostics.append({"exit_code": result[0], "fixture_stderr": result[2][:512]})
        return result

    monkeypatch.setattr(scoped_probe, "_run_local_command", recorded_command)

    assert getattr(os, "geteuid", lambda: -1)() == 10001
    # /tmp ancestors of the mounted stage are intentionally private writable
    # scratch. Use an unmounted sibling directory, not a shadow-creatable file.
    private_area = tmp_path / "private-area"
    private_area.mkdir()
    private = private_area / "private"
    private.write_text("synthetic-private", encoding="ascii")
    assert private.read_text("ascii") == "synthetic-private"
    source = tmp_path / "fixture-cli"
    program = Path(sys.executable).resolve()
    cache = tmp_path / "environments"
    policy = tmp_path / "policy.json"
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            pass
        script = (
            "#!" + str(program) + "\n"
            "import os, pathlib, socket, sys\n"
            "sys.dont_write_bytecode = True\n"
            "assert os.getuid() == 10001\n"
            "assert sys.argv[1:] == ['--version']\n"
            f"private = pathlib.Path({str(private)!r})\n"
            "for mode in ('r', 'w'):\n"
            "    try:\n"
            "        with private.open(mode) as f:\n"
            "            f.read() if mode == 'r' else f.write('bad')\n"
            "    except OSError:\n"
            "        pass\n"
            "    else:\n"
            "        raise SystemExit(20)\n"
            "try:\n"
            "    pathlib.Path('unauthorized').write_text('bad')\n"
            "except OSError:\n"
            "    pass\n"
            "else:\n"
            "    raise SystemExit(21)\n"
            f"try:\n    socket.create_connection(('127.0.0.1', {port}), timeout=0.2).close()\n"
            "except OSError:\n"
            "    pass\n"
            "else:\n"
            "    raise SystemExit(22)\n"
            "pathlib.Path('/tmp/permitted-scratch').write_text('good')\n"
            "print('fixture-cli 1.0.0')\n"
        )
        source.write_text(script, encoding="ascii")
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
        policy.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "bubblewrap_executable": "/usr/bin/bwrap",
                    "runtime_readonly_paths": [str(path) for path in runtime],
                    "native_probe_program": "/usr/bin/true",
                    "bindings": [],
                }
            ),
            encoding="ascii",
        )
        if missing:
            policy.unlink()
        probe = build_scoped_cli_probe(policy, (cache, private))
        manager = CapabilityEnvironmentManager(cache, runner=_no_host, cli_probe=probe)
        spec = CliArtifactSpec(
            "scoped-native",
            "1.0.0",
            source,
            hashlib.sha256(source.read_bytes()).hexdigest(),
            "tool",
        )
        if missing:
            with pytest.raises(EnvironmentBuildError):
                manager.deploy_cli(spec)
            with pytest.raises(KeyError):
                manager.active(spec.capability_id)
        else:
            try:
                record = manager.deploy_cli(spec)
            except EnvironmentBuildError as error:
                current: BaseException | None = error
                seen: set[int] = set()
                while current is not None and id(current) not in seen and len(seen) < 8:
                    seen.add(id(current))
                    diagnostics.append(
                        {"type": type(current).__name__, "fixture_error": str(current)[:512]}
                    )
                    current = current.__cause__ or current.__context__
                pytest.fail("Native fixture diagnostics: " + json.dumps(diagnostics))
            assert manager.active(spec.capability_id) == record
            assert (record.path / record.executable).is_file()
        assert private.read_text("ascii") == "synthetic-private"
        assert not list((cache / ".staging").iterdir())
