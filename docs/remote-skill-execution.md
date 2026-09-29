# Remote Skill execution

Agent Hub supports local `systemd` and Docker Skill sandboxes plus four remote execution
backends: SSH, Modal, Daytona, and Vercel Sandbox. A remote backend is selectable only after
its health probe confirms protocol `agent-hub.skill-sandbox` version `1`.

## SSH runner

Install this project on the remote Linux host so that `agent-hub-skill-remote-runner` is on the
SSH user's `PATH`. The runner delegates execution to the existing privileged Skill broker; set
`AGENT_HUB_REMOTE_RUNNER_BACKEND` to `systemd` or `docker` on the remote host.

Configure the Agent Hub service with:

- `AGENT_HUB_SKILL_SSH_HOST`
- `AGENT_HUB_SKILL_SSH_USER`
- `AGENT_HUB_SKILL_SSH_KNOWN_HOSTS_FILE`
- `AGENT_HUB_SKILL_SSH_PORT` (optional, default `22`)
- `AGENT_HUB_SKILL_SSH_IDENTITY_FILE` (optional)
- `AGENT_HUB_SKILL_SSH_RUNNER` (optional)

Host-key verification is mandatory. Password prompts, redirects, shell interpolation, and an
empty `known_hosts` configuration are not supported.

## HTTPS cloud adapters

Configure one or more provider endpoints and optional bearer tokens:

- `AGENT_HUB_SKILL_MODAL_ENDPOINT` and `AGENT_HUB_SKILL_MODAL_TOKEN`
- `AGENT_HUB_SKILL_DAYTONA_ENDPOINT` and `AGENT_HUB_SKILL_DAYTONA_TOKEN`
- `AGENT_HUB_SKILL_VERCEL_ENDPOINT` and `AGENT_HUB_SKILL_VERCEL_TOKEN`

Endpoints must use HTTPS and expose:

- `GET /v1/health`
- `POST /v1/invoke`
- `POST /v1/terminate`

The health response must be:

```json
{"protocol":"agent-hub.skill-sandbox","version":1,"ready":true}
```

Invocation requests include the verified Skill package, JSON input, sandbox profile, resource
limits, and bounded read-only inputs. Invocation responses contain a strict `SkillResult` and may
include a base64 ZIP workspace archive. Agent Hub rejects protocol drift, redirects, oversized
transfers, checksum mismatches, unsafe archive paths, and malformed results.
