# Cube Agent (魔方 agent)

Cube Agent is a self-hosted multi-agent operations console. The internal Python package is still named `agent_hub`, but the product-facing UI is Cube Agent / 魔方 agent.

It combines a Web console, Feishu/channel entry points, model pools, workflow and role routing, governed Skills/MCP, scheduled tasks, multimedia generation routing, OpenClaw computer/server operations, Hermes learning, and audit logs.

[中文使用说明](README.zh-CN.md)

## What You Can Do

- Chat with the main agent in Web or supported channels, continue historical conversations, attach files, and use Handoff or Vibe Coding as independent conversation toggles.
- Configure normal chat/tool models separately from multimedia AI models.
- Route image/video/audio generation only to models marked with the matching generation capability.
- Use MiniMax/Hailuo text-to-video through the multimedia executor when a valid deployment and key are configured.
- Upload single-skill or multi-skill `.zip`, `.tar`, `.tar.gz`, and `.tgz` archives for scan, review, approval, use, and deletion.
- Run OpenClaw operations through a system switch, approval mode, allowlisted commands, sessions, and local or remote adapters.
- Create one-time or cron schedules. Chat-detected schedule requests become proposals that require user confirmation.
- Review Hermes learning, logs, and audit records. `run.submit` audit records include the user, role, run, conversation, mode, attachments, and a message hash.

## Quick Install

On a clean supported Linux server:

```bash
git clone https://github.com/zhangzhimiao1994/mutilagent.git
cd mutilagent
sudo bash install.sh --mode auto --yes
```

`auto` prefers native mode on supported systemd Linux hosts: Ubuntu 22.04/24.04, Debian 12/13, Rocky Linux 9, and AlmaLinux 9. Docker mode is available as an optional fallback.

If the server does not have `git`:

```bash
tmp="$(mktemp -d /tmp/agent-hub-install.XXXXXX)"
curl -fL https://github.com/zhangzhimiao1994/mutilagent/archive/refs/heads/main.tar.gz -o "$tmp/source.tar.gz"
mkdir -p "$tmp/source"
tar -xzf "$tmp/source.tar.gz" --strip-components=1 -C "$tmp/source"
cd "$tmp/source"
sudo bash install.sh --mode auto --yes
```

Do not extract the archive directly into `/root` with `--strip-components=1`; that flattens the source tree and leaves later commands in the wrong directory.

For China-region package mirrors:

```bash
sudo env AGENT_HUB_MIRROR_MODE=auto bash install.sh --mode auto --yes
```

For HTTPS with your own certificate:

```bash
sudo env AGENT_HUB_PUBLIC_URL=https://agent.example.com \
  AGENT_HUB_TLS_CERT_FILE=/root/certs/fullchain.pem \
  AGENT_HUB_TLS_KEY_FILE=/root/certs/privkey.pem \
  bash install.sh --mode auto --yes
```

After installation, the script prints a management URL ending in `/setup` and a one-time setup code. Create the first super admin there.

## First Setup

1. Sign in to the Web console.
2. Open **Models** and add at least one normal model for the main agent.
3. Open **Main Agent** and choose the model, control mode, decision policy, Hermes policy, and review limits.
4. Open **System Settings** and enable only the system features you need: Vibe Coding, multimedia generation, and OpenClaw.
5. Configure optional modules: Skills, MCP, channels, multimedia models, schedules, memories, and users.

## Models

Models are split into two categories.

**Normal Models** are used for chat, reasoning, tool calling, structured output, coding, and multimodal understanding when the deployment is marked with the relevant capability. Providers include OpenAI, DeepSeek, Anthropic, Kimi/Moonshot, Qwen/DashScope, Qwen Token Plan, MiniMax, OpenAI-compatible relays, and Anthropic-message relays.

**Multimedia AI** is used for generation jobs, not ordinary chat. Presets include Sora, OpenAI Audio, MiniMax Hailuo, MiniMax Audio, Google Veo, Kling, Alibaba Wan, Seedance, Seedream, and relay/custom providers. Capability tags such as `image_generation`, `video_generation`, and `audio_generation` control routing. A video request is rejected before submission if the selected deployment is not recognized as a video-capable model.

MiniMax/Hailuo video generation is implemented by the current multimedia executor. Other preset providers are stored and routed by capability, and can be extended by adding provider clients behind the common multimedia provider interface.

## Chat And Modes

The chat page supports:

- `auto`: main agent decides the execution mode.
- `direct`: use one selected model directly.
- `dispatch`: route work to configured agents.
- `discuss`: run a discussion-style workflow.
- `hybrid`: combine dispatch and discussion.

Handoff and Vibe Coding are independent toggles. They can be enabled together, disabled before send, and are recorded in the submitted run. If the context becomes too long, the system can start a compressed continuation branch based on the main agent model context window.

Schedule-like messages such as daily or weekly reminders are detected as schedule proposals. The system shows the plan first and only creates the schedule after confirmation.

## OpenClaw

OpenClaw is a system-level feature switch for controlled computer and server operations.

Supported operation kinds are:

- `server_command`
- `desktop_action`
- `screen_read`
- `file_read`

Permission modes are:

- `ask`: require approval before operations.
- `read_only`: allow only read-style operations.
- `auto_review`: auto-review low-risk operations and require approval for higher risk.
- `trusted_auto`: for trusted environments only.

Operations use configured command allowlists and adapter records. Local Linux server commands can run through the bundled adapter. Remote adapters can also perform bounded `file_read` operations without an argv command when `OPENCLAW_ADAPTER_ALLOWED_FILE_ROOTS_JSON` is configured with explicit absolute roots; output is capped by `OPENCLAW_ADAPTER_FILE_READ_LIMIT_BYTES`. Windows, Linux desktop, macOS, screen, and filesystem targets should be connected with dedicated credentials and least privilege.

Useful command:

```bash
scripts/agent-hub openclaw-adapter
```

## Skills And MCP

Skills are uploaded as archives, scanned, and approved before use. Accepted outer archive names are `.zip`, `.tar`, `.tar.gz`, and `.tgz`.

A package may contain a single Skill or multiple Skill directories. Multi-skill bundles can include extra directory layers; the scanner looks for valid skill manifests and reports skipped entries. Each individual Skill still goes through path traversal checks, size limits, file count limits, dependency pinning checks, forbidden extension checks, permission diffing, and approval.

MCP servers are configured separately with transport, command or URL, allowed tools, executable allowlists, domain allowlists, and timeouts.

## Channels

The channel layer connects external chat platforms to the main agent. The console includes configuration surfaces for Feishu, DingTalk, WeCom, WeChat, Telegram, Slack, QQ, and custom webhook entries. Feishu has first-class setup documentation and runtime integration.

See [docs/feishu-setup.md](docs/feishu-setup.md).

## Logs, Audit, And Hermes

The Logs center separates audit logs, model errors, mode errors, feature errors, agent errors, and channel errors. Each log table supports search, column filters, sorting, selection, and JSON export.

Audit records cover administrative changes and user-triggered conversation submissions. For `run.submit`, the audit details include:

- `user_id` and `user_role`
- `run_id`
- `conversation_id` and `reference_conversation_id`
- requested mode and accepted mode
- workflow, selected agents, direct model, Vibe Coding flag, and attachment count
- message preview and `message_sha256`

Hermes stores learning records separately from chat. Records can be confirmed or deleted individually or in bulk.

## Operations

Common commands after native installation:

```bash
scripts/agent-hub status
scripts/agent-hub logs
scripts/agent-hub doctor
scripts/agent-hub backup /tmp/agent-hub-backup.tar.gz
scripts/agent-hub backup verify /tmp/agent-hub-backup.tar.gz
scripts/agent-hub restore /tmp/agent-hub-backup.tar.gz
scripts/agent-hub upgrade
```

See [docs/operations.md](docs/operations.md) and [docs/installation.md](docs/installation.md).

## Security Notes

- The installer does not modify cloud security groups. Open public ports deliberately in your cloud console.
- API keys and secrets are stored as secret references and must not be submitted through chat.
- OpenClaw, Skills, MCP, and tool execution are governed by explicit capabilities, allowlists, approval records, and audit logs.
- Logs and audit details are designed to avoid leaking raw secrets.

See [docs/security.md](docs/security.md).

## Development

```bash
uv run ruff check .
uv run mypy --strict src tests
uv run pytest -q
npm --prefix web run lint
npm --prefix web run test -- --run
npm --prefix web run build
```

## More Documentation

- [Installation](docs/installation.md)
- [Operations](docs/operations.md)
- [Model pools](docs/model-pools.md)
- [Skills and MCP](docs/skills-and-mcp.md)
- [Hermes](docs/hermes.md)
- [Feishu setup](docs/feishu-setup.md)
- [Security](docs/security.md)
- [Troubleshooting](docs/troubleshooting.md)