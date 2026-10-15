from __future__ import annotations

import hashlib
import inspect
from pathlib import Path
from typing import NoReturn

import pytest

from agent_hub.capability_installer.environment import (
    CapabilityEnvironmentManager,
    CliArtifactSpec,
    EnvironmentBuildError,
)


def _no_host(*args: object, **kwargs: object) -> NoReturn:
    raise AssertionError("scoped CLI probe must never use the host command runner")


@pytest.mark.parametrize("failed", [False, True])
def test_explicit_cli_namespace_probe_replaces_host_probe_and_cleans_failures(
    tmp_path: Path, failed: bool
) -> None:
    assert "cli_probe" in inspect.signature(CapabilityEnvironmentManager).parameters
    source = tmp_path / "artifact"
    source.write_bytes(b"trusted-cli-fixture")
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    calls: list[tuple[Path, Path]] = []

    def probe(executable: Path, staging: Path) -> None:
        calls.append((executable, staging))
        assert executable.parent == staging
        assert hashlib.sha256(executable.read_bytes()).hexdigest() == digest
        if failed:
            raise OSError("namespace unavailable")

    manager = CapabilityEnvironmentManager(
        tmp_path / "environments", runner=_no_host, cli_probe=probe
    )
    spec = CliArtifactSpec("scoped-cli", "1.0.0", source, digest, "tool")
    if failed:
        with pytest.raises(EnvironmentBuildError, match="namespace|smoke"):
            manager.deploy_cli(spec)
        with pytest.raises(KeyError):
            manager.active(spec.capability_id)
    else:
        assert manager.deploy_cli(spec) == manager.active(spec.capability_id)
    assert len(calls) == 1
    assert not list((tmp_path / "environments/.staging").iterdir())
