# Cube Agent

Cube Agent (魔方 Agent) is a self-hosted agent operations platform for running model-backed conversations, multi-agent work, governed tools, scheduled tasks, and durable learning from one responsive Web console.

The product name is **Cube Agent**. The Python package and service names remain `agent_hub` for compatibility.

[中文说明](README.zh-CN.md) · [Installation](docs/installation.md) · [Operations](docs/operations.md) · [Security](docs/security.md)

## Current Scope

Cube Agent is built for teams that need more than a chat UI: projects and conversations are persistent, execution is approval-aware, capabilities have an auditable lifecycle, and learned knowledge is separated from raw conversation history.

Currently implemented:

- A project-based conversation workspace with renaming, archiving, branching, attachments, queued follow-up messages, direction changes, and cancellation.
- Main-agent routing across `auto`, `direct`, `dispatch`, `discuss`, and `hybrid` modes.
- User-question checkpoints, full-history question search, deep links, and project/archive-aware history filtering.
- Model pools for ordinary reasoning/tool use and capability-tagged multimedia generation.
- Governed Skills, MCP servers, plugins, capability installation proposals, schedules, channels, OpenClaw operations, audit logs, and structured run diagnostics.
- Hermes learning candidates plus PostgreSQL-backed working, episodic, and core memory with user, tenant, project, and conversation scopes.
- Responsive desktop and mobile management surfaces for runs, artifacts, execution environments, Skills, plugins, logs, and settings.

The dedicated Open Harness / DeepSeek Harness redesign is **not** part of the current public workflow. Existing runtime and project-generation paths are available, but the UI does not advertise a separate Vibe Coding mode or promise an unrestricted autonomous coding environment.

## Architecture

```text
Browser / channel adapters
          |
       Caddy
          |
   FastAPI control plane -------- PostgreSQL
          |                         durable state,
          |                         audit, memory
          +---------------------- Redis
          |                         coordination
          |
       Worker ------------------- LiteLLM
          |                         model routing
          |
   capability gateway
      |          |
 systemd       Docker
  broker       sandbox
      |
 versioned Skill / plugin environments
```

The Web application is React, TypeScript, and Vite. The backend uses Python 3.12, FastAPI, SQLAlchemy, Alembic, PostgreSQL, Redis, and LiteLLM. Native Linux deployments run the API, worker, LiteLLM, and the privileged Skill broker as separate systemd units. Docker Compose is also supported.

The API and worker remain unprivileged. Native Skill execution crosses a Unix socket into a constrained systemd broker; Docker execution is only reported as available when a usable Docker CLI and daemon are present.

## Quick Start

### Native Linux installation

On a clean supported systemd host (Ubuntu 22.04/24.04, Debian 12/13, Rocky Linux 9, or AlmaLinux 9):

```bash
git clone https://github.com/zhangzhimiao1994/Cube-agent.git
cd Cube-agent
sudo bash install.sh --mode auto --yes
```

`auto` selects native mode when systemd and a recognized apt/dnf family are detected; otherwise it selects Docker. Distribution-version validation happens after that selection, so use `--mode docker` explicitly on an unsupported native distribution. For a network-constrained server in China:

```bash
sudo env AGENT_HUB_MIRROR_MODE=auto bash install.sh --mode auto --yes
```

The installer creates or preserves secrets, initializes the database, runs migrations, starts services, performs health checks, and prints a `/setup` URL with a one-time setup code. Existing `/etc/agent-hub/secrets.env` and `/var/lib/agent-hub` are preserved during repair or upgrade runs.

### Docker Compose

```bash
cp deploy/compose/.env.example deploy/compose/.env
# Replace every placeholder secret in deploy/compose/.env.
docker compose -f deploy/compose/docker-compose.yml \
  --env-file deploy/compose/.env up -d --build
```

The Compose stack contains migration/bootstrap jobs plus API, worker, LiteLLM, PostgreSQL, Redis, and Caddy services. The initial LiteLLM model list is empty; configure a reachable provider before expecting model-backed runs to work.

For HTTPS, offline image transfer, mirrors, minimum host requirements, and repair behavior, see [Installation](docs/installation.md).

## First Setup

1. Open the installer-provided `/setup` URL and create the first super administrator.
2. Add at least one normal model under **Models**.
3. Configure the main-agent model and decision policy.
4. Create a project and choose its workspace. Conversations created inside that project inherit the shared workspace.
5. Enable only the execution backends, channels, Skills, MCP servers, plugins, multimedia providers, and OpenClaw operations that the deployment actually supports.

A Windows desktop session can use the native Explorer folder picker for a local project workspace. Linux desktops use `zenity` or `kdialog` when available. Remote browsers receive a tenant-scoped server directory browser and never receive absolute server paths.

## Conversations And Runs

The conversation page is the primary work surface. The main agent can select a mode automatically or an operator can choose a supported mode explicitly:

| Mode | Behavior |
| --- | --- |
| `auto` | The main agent chooses the execution strategy. |
| `direct` | One selected model handles the run. |
| `dispatch` | Work is delegated to configured agents. |
| `discuss` | Agents collaborate through a discussion workflow. |
| `hybrid` | Dispatch and discussion are combined. |

While a run is active, a new message can be queued or used to change direction. Queued messages can be edited or cancelled before release. Run details expose structured agent activity, tool lifecycle, approvals, generated artifacts, diagnostics, and resumable runtime blockers without dumping raw internal payloads into the main transcript.

Each user question becomes a conversation checkpoint. Checkpoints can be searched within the current conversation, while server-backed full-history search finds older questions across projects and archived conversations using cursor pagination and bounded excerpts.

Long conversations are compacted before they exceed the selected model's context window. Compaction preserves the original goal and recent decisions; it is separate from Hermes learning.

## Models And Multimedia

Normal model deployments handle chat, reasoning, structured output, tool use, coding, or multimodal understanding when their deployment advertises those capabilities. The configuration layer supports OpenAI, DeepSeek, Anthropic, Moonshot/Kimi, Qwen/DashScope, MiniMax, and compatible relay endpoints.

Multimedia deployments are routed separately by capability tags such as `image_generation`, `video_generation`, and `audio_generation`. MiniMax/Hailuo text-to-video has a concrete executor. Other presets require a configured provider implementation and valid credentials; storing a preset alone does not make that provider executable.

## Plugins, Skills, And MCP

These are related but intentionally different capability types:

- **Plugins** register reviewed capabilities and adapters. The trusted capability installer can search a curated catalog, resolve aliases, generate a plan, request approval, install, cancel, health-check, and roll back installer-owned entries.
- **Skills** are versioned packages uploaded as `.zip`, `.tar`, `.tar.gz`, or `.tgz`. They are quarantined, scanned, permission-reviewed, and approved before activation.
- **MCP servers** are configured with a transport, command or URL, tool allowlist, executable/domain allowlists, and timeouts.

The capability installer is not a general internet package manager. It does not install arbitrary URLs or code merely because a user asks. A catalog entry becomes usable only after its declared runtime, commands, credentials, transport, and health checks are real. Missing prerequisites produce an actionable approval/configuration blocker instead of a fake enabled plugin.

Team Skill sources support trusted repository synchronization or pinned offline ZIP snapshots. Imports create immutable source revisions; every member must be approved before atomic activation. Rollback restores the complete prior Skill mapping.

See [Skills and MCP](docs/skills-and-mcp.md).

## Execution Environments

Cube Agent separates a capability manifest from the environment that executes it. Capability environments are immutable and versioned, with hash-verified materialization, smoke checks, atomic switching, rollback, quota cleanup, and reference protection.

Supported execution backends:

- **systemd**: the default native Linux backend. A socket-activated privileged broker creates constrained transient units and enforces caller identity, package path/hash, resource limits, network policy, filesystem bindings, output limits, timeout, and termination.
- **Docker**: an optional sandbox backend. It is unavailable unless both the Docker CLI and daemon pass runtime probes.

If a run needs an absent runtime, Cube Agent can pause it in `waiting_approval`, show the concrete install/configure/enable action, and resume the same run after the blocker is resolved. Model-provider failures and unsafe recovery conditions remain terminal rather than being mislabeled as installable dependencies.

## Hermes Learning And Memory

Hermes is an experience and memory layer, not online model training and not an autonomous permission system.

After eligible work, Hermes can create an evidence-backed learning candidate. Candidates move through pending, approved, rejected, and ledger states. Approval promotes a locked, scoped long-term memory; rejection, deletion, or forgetting removes it from future recall. Mutations are serialized and audited.

Persistent memory is divided into:

- **Working memory** for bounded near-term context.
- **Episodic memory** for task and experience records.
- **Core memory** for durable approved facts or preferences.

Recall is actor- and tenant-isolated and may also be scoped to a project or conversation. Memory is injected into every runtime mode through a bounded, delimiter-safe envelope. It cannot grant permissions, select a mode, bypass approval, or override tool policy. Values that look like secrets are rejected.

See [Hermes](docs/hermes.md).

## Channels, Schedules, And OpenClaw

The channel layer provides configuration surfaces for Feishu, DingTalk, WeCom, WeChat, Telegram, Slack, QQ, and custom webhooks. Feishu has the currently documented first-class runtime path; another platform's presence in the console should not be read as proof that every vendor feature is implemented.

Messages with a concrete time and executable action can become schedule proposals. A proposal must be confirmed before a one-time or cron schedule is created.

OpenClaw provides governed computer and server operations such as `server_command`, `desktop_action`, `screen_read`, and `file_read`. Availability depends on a configured adapter. Permission modes range from approval-required to trusted automation, with command allowlists, bounded file roots, fixed driver commands, capability health checks, and audit records.

See [Feishu setup](docs/feishu-setup.md) and [Operations](docs/operations.md).

## Production Operations

Native installations are managed through `scripts/agent-hub`:

```bash
scripts/agent-hub status
scripts/agent-hub logs
scripts/agent-hub doctor
scripts/agent-hub backup /tmp/agent-hub-backup.tar.gz
scripts/agent-hub backup verify /tmp/agent-hub-backup.tar.gz
scripts/agent-hub restore /tmp/agent-hub-backup.tar.gz --target /tmp/agent-hub-restore
scripts/agent-hub prune-releases --keep 2
scripts/agent-hub prune-releases --keep 2 --execute
```

The backup helper archives only `AGENT_HUB_STATE_DIR` (normally `/var/lib/agent-hub`). It does not back up PostgreSQL, Redis, or `/etc/agent-hub/secrets.env`; keep independent database and secret backups. `backup verify` checks archive readability, and `restore` only extracts into the explicit target for review.

`scripts/agent-hub upgrade` is currently a low-level version-marker rehearsal, not a release downloader or service upgrader. Use the installer or the versioned release deployment procedure for an application upgrade. Release pruning is a dry run unless `--execute` is supplied and always protects the active `current` target. Caddy is the intended public entry point; the API remains bound to loopback in a native deployment.

## Development And Testing

Prerequisites: Python 3.12, Node.js/npm, PostgreSQL for integration tests, and Docker only for Docker-specific tests or deployment.

```bash
uv sync --all-groups
npm --prefix web install

uv run ruff check .
uv run mypy --strict src tests
uv run pytest -q
npm --prefix web run lint
npm --prefix web run test -- --run
npm --prefix web run build
```

The repository includes unit, contract, API, PostgreSQL integration, resilience, migration, frontend component, and responsive browser tests. Tests that require PostgreSQL, Docker, external providers, or platform-specific adapters need those dependencies to be running; a passing unit suite does not certify an unavailable external runtime.

## Security Boundaries

- Secrets live in `/etc/agent-hub/secrets.env` with mode `0600` in native deployments. Logs and API projections redact credential-like fields.
- The installer does not alter cloud firewalls or security groups.
- Skills are quarantined and scanned for traversal, size, file-count, forbidden-extension, dependency-pinning, and permission issues before approval.
- Plugin, Skill, MCP, OpenClaw, and scheduler actions are capability-scoped, approval-aware, and audited.
- Remote HTTP/MCP transports reject unsafe destinations and redirect-based SSRF.
- The system fails closed when a runtime, credential, backend, or health check is missing. A stored manifest is not evidence that a capability can execute.
- A Strix entry does not provide a completed penetration-test environment by itself. Real scans still require an authorized target, a functioning Strix runtime, its backend prerequisites, and valid model credentials.
- Hermes memory never grants authority and does not store obvious secrets.

Read [Security](docs/security.md) before exposing a deployment to untrusted users or networks.

## Documentation

- [Documentation index](docs/README.md)
- [Installation](docs/installation.md)
- [Operations](docs/operations.md)
- [Model pools](docs/model-pools.md)
- [Skills and MCP](docs/skills-and-mcp.md)
- [Hermes](docs/hermes.md)
- [Feishu setup](docs/feishu-setup.md)
- [Security](docs/security.md)
- [Troubleshooting](docs/troubleshooting.md)
