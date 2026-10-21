"""Go uses a fixed offline outer unit, binding SDK leaves across Root-only stages."""

from __future__ import annotations

import importlib
import importlib.util
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from agent_hub.skills.sandbox.broker import BrokerPolicy
from tests.unit.skills.sandbox.test_go_project_protocol import invocation


@pytest.fixture
def api() -> Any:
    name = 'agent_hub.skills.sandbox.go_project_command'
    assert importlib.util.find_spec(name) is not None, 'Go project systemd command missing'
    return importlib.import_module(name)


@pytest.fixture
def call() -> Any:
    name = 'agent_hub.skills.sandbox.go_project_protocol'
    assert importlib.util.find_spec(name) is not None, 'Go project v8 protocol missing'
    return invocation(importlib.import_module(name))


def locations() -> dict[str, Path]:
    return {'snapshot': Path('/root/stage/source'), 'original': Path('/root/stage/original'),
            'plan': Path('/root/stage/plan.json'),
            'toolchain_root': Path('/root/published/payload/go'), 'export': Path('/root/stage/export')}


def properties(args: tuple[str, ...]) -> list[str]:
    return [args[index + 1] for index, value in enumerate(args) if value == '-p']


def test_fixed_mounts_keep_root_stage_private_and_export_outer_only(api: Any, call: Any) -> None:
    args = api.build_go_project_systemd_command(call, BrokerPolicy(), **locations())
    props = properties(args)
    assert args[:7] == ('/usr/bin/systemd-run', '--wait', '--collect', '--pipe', '--quiet',
                        '--unit', 'agent-hub-project-go_1')
    assert [value for value in props if value.startswith('BindReadOnlyPaths=')] == [
        'BindReadOnlyPaths=/root/stage/source:/run/agent-hub-go-source',
        'BindReadOnlyPaths=/root/stage/original:/run/agent-hub-go-original',
        'BindReadOnlyPaths=/root/stage/plan.json:/run/agent-hub-go-plan.json',
        'BindReadOnlyPaths=/root/published/payload/go:/run/agent-hub-go-sdk',
        'BindReadOnlyPaths=/opt/agent-hub/current/src:/run/agent-hub-go-trusted-source',
    ]
    assert [value for value in props if value.startswith('BindPaths=')] == [
        'BindPaths=/root/stage/export:/run/agent-hub-go-export',
    ]
    assert not any('payload:/run/' in value or 'stage:/run/' in value for value in props)


def test_hardening_and_quota_properties_match_python_outer_policy(api: Any, call: Any) -> None:
    call = call.model_copy(update={'timeout_seconds': 321, 'memory_limit_bytes': 123456789,
                                   'cpu_quota_percent': 75, 'workspace_limit_bytes': 987654,
                                   'file_limit': 432})
    props = properties(api.build_go_project_systemd_command(call, BrokerPolicy(), **locations()))
    assert set(props).issuperset({
        'DynamicUser=yes', 'SupplementaryGroups=agent-hub', 'UMask=0077',
        'NoNewPrivileges=yes', 'ProtectSystem=strict', 'ProtectHome=yes', 'PrivateTmp=yes',
        'PrivateDevices=yes', 'PrivateNetwork=yes', 'IPAddressDeny=any',
        'RestrictAddressFamilies=AF_UNIX AF_NETLINK', 'ProtectKernelModules=yes',
        'ProtectControlGroups=yes', 'RestrictSUIDSGID=yes', 'LockPersonality=yes',
        'CapabilityBoundingSet=', 'TasksMax=128', 'Slice=system.slice',
        'KillMode=control-group', 'TimeoutStopSec=2s', 'MemoryMax=123456789',
        'CPUQuota=75%', 'RuntimeMaxSec=321s', 'InaccessiblePaths=/var/lib/agent-hub',
        'TemporaryFileSystem=/run/agent-hub-go-work:rw,nosuid,nodev,size=987654,nr_inodes=4320,mode=1777',
    })
    assert not any(value.startswith('Environment=') for value in props)


def test_bootstrap_imports_only_fixed_trusted_code_and_ignores_host_environment(api: Any, call: Any) -> None:
    args = api.build_go_project_systemd_command(call, BrokerPolicy(), **locations())
    start = args.index('/usr/bin/env')
    assert args[start:start + 9] == (
        '/usr/bin/env', '-i', 'PATH=/usr/bin:/bin', 'LANG=C.UTF-8',
        'AGENT_HUB_GENERATED_PROJECT_VALIDATION_SANDBOX=systemd',
        '/opt/agent-hub/current/.venv/bin/python', '-I', '-B', '-c',
    )
    assert args[start + 9:] == ((
        "import sys;sys.path.insert(0,'/run/agent-hub-go-trusted-source');"
        'from agent_hub.skills.sandbox.go_project_runner import main;raise SystemExit(main())'
    ),)
    assert not any('PYTHONPATH' in arg or 'GOFLAGS' in arg for arg in args)


@pytest.mark.parametrize('field', ['snapshot', 'original', 'plan', 'toolchain_root', 'export'])
@pytest.mark.parametrize('value', ['/bad/../go', '/bad\n/go', '/bad:inject/go', 'relative/go'])
def test_every_host_path_is_validated_before_command_is_returned(
    api: Any, call: Any, field: str, value: str,
) -> None:
    with pytest.raises(ValueError):
        api.build_go_project_systemd_command(call, BrokerPolicy(), **(locations() | {field: Path(value)}))


def test_systemd_percent_and_word_separators_are_literal_path_bytes(api: Any, call: Any) -> None:
    roots = locations() | {'toolchain_root': Path('/root/a %n\'"/go')}
    args = api.build_go_project_systemd_command(call, BrokerPolicy(), **roots)
    assert 'BindReadOnlyPaths=/root/a\\x20%%n\\x27\\x22/go:/run/agent-hub-go-sdk' in args
    custom = replace(BrokerPolicy(), python='/opt/venv%name/bin/python',
                     source_path=Path('/opt/source %n'))
    args = api.build_go_project_systemd_command(call, custom, **locations())
    assert '/opt/venv%%name/bin/python' in args
    assert 'BindReadOnlyPaths=/opt/source\\x20%%n:/run/agent-hub-go-trusted-source' in args


@pytest.mark.parametrize('field,value', [
    ('install_allowed', True), ('sandbox_profile', 'restricted'),
    ('execution_id', '../host'), ('memory_limit_bytes', 0),
])
def test_copied_models_cannot_bypass_native_command_validation(
    api: Any, call: Any, field: str, value: object,
) -> None:
    with pytest.raises(ValueError):
        api.build_go_project_systemd_command(call.model_copy(update={field: value}),
                                              BrokerPolicy(), **locations())


def test_command_construction_never_relaxes_root_or_sdk_permissions(
    api: Any, call: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    def no_chmod(*args: Any, **kwargs: Any) -> None:
        pytest.fail('command builder changed filesystem permissions')

    monkeypatch.setattr(Path, 'chmod', no_chmod)
    api.build_go_project_systemd_command(call, BrokerPolicy(), **locations())
