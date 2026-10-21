"""Fixed offline Go outer unit; only Root supplies host paths.

systemd binds the published GoRoot leaf (0555, containing bin/go) through its
Root-only stage parent to /run/agent-hub-go-sdk. No parent permissions change.
The trusted runner creates owned 0700 children in the quota tmpfs work root.
Only that outer runner sees export; inner bwrap must expose no export mount.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from agent_hub.skills.sandbox.go_project_protocol import GoProjectInvocation
from agent_hub.skills.sandbox.python_project_command import _escaped

if TYPE_CHECKING:
    from agent_hub.skills.sandbox.broker import BrokerPolicy


def build_go_project_systemd_command(
    invocation: GoProjectInvocation,
    policy: BrokerPolicy,
    *,
    snapshot: Path,
    original: Path,
    plan: Path,
    toolchain_root: Path,
    export: Path,
) -> tuple[str, ...]:
    if type(invocation) is not GoProjectInvocation:
        raise ValueError('go_project_command_rejected')
    invocation = GoProjectInvocation.model_validate_json(
        invocation.model_dump_json(warnings='error'), strict=True,
    )
    binds = ((snapshot, 'source'), (original, 'original'), (plan, 'plan.json'),
             (toolchain_root, 'sdk'), (policy.source_path, 'trusted-source'))
    properties = [
        'DynamicUser=yes',
        f'SupplementaryGroups={policy.service_group}',
        'UMask=0077',
        'NoNewPrivileges=yes',
        'ProtectSystem=strict',
        'ProtectHome=yes',
        'PrivateTmp=yes',
        'PrivateDevices=yes',
        'PrivateNetwork=yes',
        'IPAddressDeny=any',
        'RestrictAddressFamilies=AF_UNIX AF_NETLINK',
        'ProtectKernelModules=yes',
        'ProtectControlGroups=yes',
        'RestrictSUIDSGID=yes',
        'LockPersonality=yes',
        'CapabilityBoundingSet=',
        'TasksMax=128',
        'Slice=system.slice',
        'KillMode=control-group',
        'TimeoutStopSec=2s',
        f'MemoryMax={invocation.memory_limit_bytes}',
        f'CPUQuota={invocation.cpu_quota_percent}%',
        f'RuntimeMaxSec={invocation.timeout_seconds}s',
        (f'TemporaryFileSystem=/run/agent-hub-go-work:rw,nosuid,nodev,'
         f'size={invocation.workspace_limit_bytes},nr_inodes={invocation.file_limit * 10},mode=1777'),
        'InaccessiblePaths=/var/lib/agent-hub',
    ]
    properties.extend(
        f'BindReadOnlyPaths={_escaped(path)}:/run/agent-hub-go-{name}' for path, name in binds
    )
    properties.append(f'BindPaths={_escaped(export)}:/run/agent-hub-go-export')
    command = [policy.systemd_run, '--wait', '--collect', '--pipe', '--quiet',
               '--unit', f'agent-hub-project-{invocation.execution_id}']
    for prop in properties:
        command.extend(('-p', prop))
    bootstrap = (
        "import sys;sys.path.insert(0,'/run/agent-hub-go-trusted-source');"
        'from agent_hub.skills.sandbox.go_project_runner import main;raise SystemExit(main())'
    )
    _escaped(Path(policy.python))
    command.extend((
        '/usr/bin/env', '-i', 'PATH=/usr/bin:/bin', 'LANG=C.UTF-8',
        'AGENT_HUB_GENERATED_PROJECT_VALIDATION_SANDBOX=systemd',
        Path(policy.python).as_posix().replace('%', '%%'), '-I', '-B', '-c', bootstrap,
    ))
    return tuple(command)
