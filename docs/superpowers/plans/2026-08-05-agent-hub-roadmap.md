# Agent Hub Implementation Roadmap

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build and deploy a production-oriented multi-agent platform with CrewAI dispatch, AutoGen discussion, multi-model and vision routing, Feishu/Web administration, safe Skills/MCP, durable state, and one-click Linux installation.

**Architecture:** A Python control plane owns channels, sessions, policies, configuration, and immutable runtime generations. CrewAI and AutoGen are isolated behind execution-runtime adapters, all model calls go through LiteLLM, and all capabilities go through a permission gateway. PostgreSQL is the durable source of truth, Redis/Celery handle asynchronous work, and React provides administration.

**Tech Stack:** Python 3.12, FastAPI, Pydantic, SQLAlchemy, Alembic, PostgreSQL, Redis, Celery, LiteLLM Proxy, CrewAI, AutoGen AgentChat, Feishu SDK, React, TypeScript, Vite, Vitest, Playwright, Docker Compose, systemd, Bash/Bats.

---

## Execution order

1. [Phase 1 — Platform Foundation](2026-08-05-agent-hub-phase-1-foundation.md)
   - Repository scaffold, settings, database, domain state machine, versioned configuration, secrets, bootstrap authentication, and RBAC.
   - Exit: API starts locally; first administrator can initialize the system; published configuration is immutable and rollbackable.
2. [Phase 2 — Models and Orchestration](2026-08-05-agent-hub-phase-2-orchestration.md)
   - LiteLLM model registry, capability-aware vision routing, dual mode classifier, Direct/CrewAI/AutoGen/Hybrid runtime adapters, durable runs, and checkpoints.
   - Exit: API-submitted tasks execute in all four modes and survive worker restart from safe checkpoints.
3. [Phase 3 — Capabilities and Memory](2026-08-05-agent-hub-phase-3-capabilities.md)
   - Capability policy, safe tools, Skill lifecycle and sandbox, MCP, layered memory, knowledge retrieval, context compaction, and scheduler.
   - Exit: approved capabilities execute with audit records; dangerous calls are denied or paused for approval.
4. [Phase 4 — Feishu and Web](2026-08-05-agent-hub-phase-4-interfaces.md)
   - Feishu WebSocket/Webhook, private/group messaging, cards and text fallback, image intake, OAuth/local login, and Web administration.
   - Exit: users can submit tasks and administrators can configure the system from Feishu and the browser.
5. [Phase 5 — Deployment and Production Validation](2026-08-05-agent-hub-phase-5-deployment.md)
   - Docker Compose, native systemd packages, unified `install.sh`, TLS, bootstrap setup, backup/restore/upgrade/doctor, observability, security tests, and distro smoke tests.
   - Exit: one command installs either deployment mode and prints a working management URL plus one-time setup code.

## Dependency rules

- Each phase starts only after the previous phase test suite passes and its acceptance checkpoint is committed.
- Runtime adapters depend on domain contracts, never on HTTP or Feishu message classes.
- Channel and Web layers call application services; they do not import CrewAI, AutoGen, LiteLLM, Skill runner, or database ORM internals.
- Secrets never enter YAML exports, channel messages, structured logs, test snapshots, or exception details.
- No phase may add a direct tool execution path outside `CapabilityGateway`.

## Spec coverage map

| Design requirement | Plan |
|---|---|
| Same API/model with multiple roles | Phase 2 Tasks 1–3 |
| Multiple providers, proxy APIs, fallback | Phase 2 Tasks 1–2 |
| Direct/Dispatch/Discuss/Hybrid | Phase 2 Tasks 4–7 |
| Low-confidence card or text question | Phase 2 Task 3; Phase 4 Task 3 |
| Vision image understanding | Phase 2 Task 3; Phase 4 Task 4 |
| Skill/MCP and safe operations | Phase 3 Tasks 1–5 |
| Memory, knowledge, compaction, scheduler | Phase 3 Tasks 6–8 |
| Feishu private/group, WS/Webhook | Phase 4 Tasks 1–4 |
| Web configuration, OAuth/local auth | Phase 1 Tasks 5–6; Phase 4 Tasks 5–8 |
| PostgreSQL/Redis/checkpoints/audit | Phase 1 Tasks 2–4; Phase 2 Task 8 |
| Docker and native Linux deployment | Phase 5 Tasks 1–3 |
| One-click installer and Web bootstrap | Phase 5 Tasks 4–6 |
| Recovery, observability, security, E2E | Phase 5 Tasks 7–10 |

## Global verification gate

Before marking the roadmap complete, run:

```bash
uv run ruff check .
uv run mypy src
uv run pytest -q
npm --prefix web run lint
npm --prefix web run test -- --run
npm --prefix web run build
docker compose -f deploy/compose/docker-compose.yml config --quiet
bash -n install.sh scripts/agent-hub scripts/lib/*.sh scripts/commands/*.sh deploy/native/*.sh
shellcheck install.sh scripts/agent-hub scripts/lib/*.sh scripts/commands/*.sh deploy/native/*.sh
```

Expected: every command exits `0`; the end-to-end matrix in Phase 5 passes for Docker, Ubuntu, Debian, and Rocky/AlmaLinux targets.
