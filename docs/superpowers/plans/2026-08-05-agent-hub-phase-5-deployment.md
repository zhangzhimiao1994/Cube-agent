# Agent Hub Phase 5 Deployment and Production Validation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship equivalent Docker Compose and native Linux deployments plus one interactive installer that ends with a healthy Web management URL and single-use setup code.

**Architecture:** Build one versioned application artifact and run it through different service wrappers. The installer owns discovery, secret generation, dependency preparation, migrations, startup, verification, and bootstrap output; shared maintenance commands own doctor, backup, restore, and upgrade. Production gates exercise security, recovery, and distro matrices.

**Tech Stack:** Docker Compose, PostgreSQL, Redis, LiteLLM Proxy, Caddy/Nginx, systemd, Bash, Bats, ShellCheck, Prometheus/OpenTelemetry, GitHub Actions or equivalent CI.

---

## File map

- `deploy/compose/`: images, Compose file, health checks, and example environment.
- `deploy/native/systemd/`: service, socket, target, tmpfiles, and sysusers definitions.
- `deploy/native/install-packages.sh`: apt/dnf package adapter.
- `install.sh`: unified interactive/unattended installer.
- `scripts/lib/`: reusable installer functions.
- `scripts/agent-hub`: doctor, backup, restore, upgrade, status, and logs CLI.
- `observability/`: metrics and dashboard definitions.
- `tests/install/`: Bats tests and disposable VM/container smoke tests.
- `.github/workflows/`: quality, security, and distro deployment matrices.

### Task 1: Add production readiness, metrics, and redaction

**Files:**
- Create: `src/agent_hub/observability/logging.py`
- Create: `src/agent_hub/observability/metrics.py`
- Create: `src/agent_hub/observability/tracing.py`
- Modify: `src/agent_hub/api/routers/system.py`
- Create: `tests/unit/observability/test_redaction.py`
- Create: `tests/integration/system/test_readiness.py`

- [ ] **Step 1: Write redaction and readiness tests**

```python
def test_structured_log_redacts_secrets(caplog, secure_logger) -> None:
    secure_logger.info("provider failed", api_key="sk-secret", authorization="Bearer abc")
    output = caplog.text
    assert "sk-secret" not in output
    assert "Bearer abc" not in output
    assert "[REDACTED]" in output


async def test_readiness_fails_when_redis_is_unavailable(client, stop_redis) -> None:
    await stop_redis()
    response = client.get("/health/ready")
    assert response.status_code == 503
    assert response.json()["checks"]["redis"] == "failed"
```

- [ ] **Step 2: Implement JSON logs and correlation IDs**

Redact keys matching secret/token/password/authorization/cookie and registered secret fingerprints before serialization. Attach run_id, tenant_id, trace_id, component, deployment_id, and event kind where available; never attach prompts or image bytes by default.

- [ ] **Step 3: Add readiness and Prometheus metrics**

Readiness checks PostgreSQL, Redis, migration head, outbox lag, active runtime generation, and process-specific dependencies. Metrics include run latency/error/status, queue depth, model capacity wait, active leases, 429/cooldown, tokens/cost, Feishu delivery, approvals, Skill sandbox, and scheduler lag.

- [ ] **Step 4: Test and commit**

Run: `uv run pytest tests/unit/observability tests/integration/system -q`

Expected: redaction, correlation, dependency readiness, stale migration, and metrics-label cardinality tests pass.

```bash
git add src/agent_hub/observability src/agent_hub/api tests
git commit -m "feat: add production observability and readiness"
```

### Task 2: Build pinned production images and Docker Compose

**Files:**
- Create: `Dockerfile`
- Create: `deploy/compose/docker-compose.yml`
- Create: `deploy/compose/.env.example`
- Create: `deploy/compose/Caddyfile`
- Create: `deploy/compose/healthcheck.sh`
- Create: `tests/install/compose.bats`

- [ ] **Step 1: Write Compose validation tests**

```bash
@test "compose declares every required service" {
  run docker compose -f deploy/compose/docker-compose.yml config --services
  [ "$status" -eq 0 ]
  for service in api feishu worker litellm skill-runner postgres redis caddy; do
    [[ "$output" == *"$service"* ]]
  done
}
```

- [ ] **Step 2: Create a non-root multi-stage image**

The build stage runs `uv sync --frozen --no-dev`; the runtime copies the virtual environment, source, migrations, and built Web assets. Create UID/GID 10001, set a read-only compatible work directory, and expose only the API port. Pin the Python base image by digest in the committed file.

- [ ] **Step 3: Define Compose services and isolation**

API, Feishu, Worker, and LiteLLM share only required config/secrets. Skill Runner has no Docker socket, no privileged mode, dropped capabilities, read-only root, tmpfs workspace, pids/memory/CPU limits, and isolated network. PostgreSQL and Redis are not published publicly. Caddy is the only public service.

- [ ] **Step 4: Verify and commit**

Run: `docker compose -f deploy/compose/docker-compose.yml config --quiet && bats tests/install/compose.bats && docker build -t agent-hub:test .`

Expected: config and Bats exit `0`; image build succeeds; container user is non-root.

```bash
git add Dockerfile deploy/compose tests/install/compose.bats
git commit -m "feat: add hardened docker compose deployment"
```

### Task 3: Add native systemd deployment for apt and dnf distributions

**Files:**
- Create: `deploy/native/install-packages.sh`
- Create: `deploy/native/systemd/agent-hub.target`
- Create: `deploy/native/systemd/agent-hub-api.service`
- Create: `deploy/native/systemd/agent-hub-feishu.service`
- Create: `deploy/native/systemd/agent-hub-worker.service`
- Create: `deploy/native/systemd/agent-hub-litellm.service`
- Create: `deploy/native/systemd/agent-hub-skill@.service`
- Create: `deploy/native/agent-hub.sysusers`
- Create: `deploy/native/agent-hub.tmpfiles`
- Create: `tests/install/native.bats`

- [ ] **Step 1: Write distro detection and unit hardening tests**

```bash
@test "ubuntu debian rocky and alma map to supported package managers" {
  for fixture in ubuntu debian rocky almalinux; do
    run bash deploy/native/install-packages.sh --detect tests/install/fixtures/os-release-$fixture
    [ "$status" -eq 0 ]
  done
}

@test "api service has mandatory hardening" {
  run systemd-analyze security --offline=yes deploy/native/systemd/agent-hub-api.service
  [ "$status" -eq 0 ]
  grep -q '^NoNewPrivileges=yes' deploy/native/systemd/agent-hub-api.service
  grep -q '^ProtectSystem=strict' deploy/native/systemd/agent-hub-api.service
}
```

- [ ] **Step 2: Implement package mapping**

Ubuntu 22.04/24.04 and Debian 12/13 use apt; Rocky/AlmaLinux 9 use dnf. Install CA certificates, curl, openssl, PostgreSQL client/server when local is selected, Redis when local is selected, Caddy/Nginx, systemd tools, build prerequisites needed by locked Python dependencies, and a supported Python 3.12/uv runtime without replacing the system Python.

- [ ] **Step 3: Create hardened service units**

All services run as `agent-hub`, read `/etc/agent-hub/secrets.env`, write only `/var/lib/agent-hub` and `/run/agent-hub`, restart on failure with bounded delay, and stop gracefully. Skill template uses DynamicUser, PrivateTmp, PrivateDevices, NoNewPrivileges, ProtectSystem=strict, RestrictSUIDSGID, SystemCallFilter, MemoryMax, CPUQuota, RuntimeMaxSec, and network denial by default.

- [ ] **Step 4: Verify and commit**

Run: `bash -n deploy/native/install-packages.sh && shellcheck deploy/native/install-packages.sh && bats tests/install/native.bats`

Expected: supported distro fixtures pass, unknown distro fails clearly, unit files parse, and hardening assertions pass.

```bash
git add deploy/native tests/install/native.bats tests/install/fixtures
git commit -m "feat: add native systemd deployment"
```

### Task 4: Build the unified installer core

**Files:**
- Create: `install.sh`
- Create: `scripts/lib/common.sh`
- Create: `scripts/lib/detect.sh`
- Create: `scripts/lib/prompts.sh`
- Create: `scripts/lib/secrets.sh`
- Create: `scripts/lib/install_docker.sh`
- Create: `scripts/lib/install_native.sh`
- Create: `tests/install/installer.bats`

- [ ] **Step 1: Write parser, mode, idempotency, and secret-log tests**

```bash
@test "mode flag bypasses interactive selection" {
  run env AGENT_HUB_TEST=1 bash install.sh --mode native --dry-run
  [ "$status" -eq 0 ]
  [[ "$output" == *"mode=native"* ]]
}

@test "rerun never replaces existing secrets" {
  run bash tests/install/helpers/run-installer-twice.sh
  [ "$status" -eq 0 ]
  [ "$(cat "$TEST_ROOT/first-master-hash")" = "$(cat "$TEST_ROOT/second-master-hash")" ]
}
```

- [ ] **Step 2: Implement safe entrypoint and argument parsing**

```bash
#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/scripts/lib/common.sh"
source "$SCRIPT_DIR/scripts/lib/detect.sh"
source "$SCRIPT_DIR/scripts/lib/prompts.sh"
source "$SCRIPT_DIR/scripts/lib/secrets.sh"

MODE=""
CONFIG_FILE=""
DRY_RUN=0
while (($#)); do
  case "$1" in
    --mode) MODE="${2:?missing mode}"; shift 2 ;;
    --config) CONFIG_FILE="${2:?missing config}"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    --help) usage; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
done
[[ $EUID -eq 0 ]] || die "run with sudo bash install.sh"
[[ -z "$MODE" || "$MODE" == docker || "$MODE" == native ]] || die "mode must be docker or native"
trap 'installer_failed "$LINENO" "$?"' ERR
main
```

- [ ] **Step 3: Implement detection and interactive choices**

Detect architecture, distro/version, memory, disk, occupied ports, existing installation, Docker availability, systemd availability, local/external PostgreSQL/Redis, domain/IP, DNS match, and HTTPS option. Existing installations offer repair, upgrade, or exit; they never overwrite data or secrets.

- [ ] **Step 4: Implement generated secrets and staged installation**

Generate 32-byte master key, database password, JWT signing material, LiteLLM internal key, and one-time setup code with `openssl rand`. Write secrets atomically with owner root and mode 0600. Use an installation journal with completed stages so failure cleanup removes only newly created transient files and can resume safely.

- [ ] **Step 5: Verify and commit**

Run: `bash -n install.sh scripts/lib/*.sh && shellcheck install.sh scripts/lib/*.sh && bats tests/install/installer.bats`

Expected: parsing, both modes, unsupported distro, low resources, port conflict, existing install, resume, secret permissions, and log redaction tests pass.

```bash
git add install.sh scripts/lib tests/install/installer.bats tests/install/helpers
git commit -m "feat: add unified safe installer core"
```

### Task 5: Finish installation, migration, health, TLS, and setup output

**Files:**
- Create: `scripts/lib/database.sh`
- Create: `scripts/lib/tls.sh`
- Create: `scripts/lib/verify.sh`
- Create: `src/agent_hub/cli/bootstrap.py`
- Create: `tests/install/bootstrap.bats`

- [ ] **Step 1: Write bootstrap output and one-time-code tests**

```bash
@test "successful install prints URL and separate setup code" {
  run bash tests/install/helpers/fake-success-install.sh
  [ "$status" -eq 0 ]
  [[ "$output" == *"Management URL: https://agent.example.test/setup"* ]]
  [[ "$output" == *"One-time setup code:"* ]]
  [[ "$output" != *"?code="* ]]
}
```

- [ ] **Step 2: Implement database and migration gates**

Validate external DSNs without echoing credentials. For local mode create least-privilege database/user. Before migration, compare current/head revisions and create a backup for upgrades. Start application services only after `alembic upgrade head` succeeds.

- [ ] **Step 3: Implement TLS selection**

When domain DNS resolves to the host, configure Caddy/Nginx for ACME. For IP-only installs, default to binding management to the private interface with an explicit chosen port and print instructions for SSH tunnel or deliberate firewall exposure. Never claim to modify cloud security groups.

- [ ] **Step 4: Issue and print bootstrap material only after verification**

Run live/readiness checks, database read/write, Redis lease, LiteLLM reachability, Worker ping, and Web asset fetch. Then call the bootstrap CLI to store only the code hash with 15-minute expiry and print management URL plus code separately. Consuming the code closes setup immediately.

- [ ] **Step 5: Test and commit**

Run: `bats tests/install/bootstrap.bats && uv run pytest tests/integration/auth/test_bootstrap.py tests/integration/system/test_readiness.py -q`

Expected: migration failure, health failure, DNS mismatch, IP-only, setup expiry, replay, and successful bootstrap tests pass.

```bash
git add scripts/lib src/agent_hub/cli tests/install/bootstrap.bats tests/integration
git commit -m "feat: finish verified one click installation"
```

### Task 6: Add doctor, backup, restore, and upgrade commands

**Files:**
- Create: `scripts/agent-hub`
- Create: `scripts/commands/doctor.sh`
- Create: `scripts/commands/backup.sh`
- Create: `scripts/commands/restore.sh`
- Create: `scripts/commands/upgrade.sh`
- Create: `tests/install/maintenance.bats`

- [ ] **Step 1: Write backup integrity and failed-upgrade tests**

```bash
@test "backup manifest verifies every payload" {
  run scripts/agent-hub backup --output "$BATS_TEST_TMPDIR/backup.tar.zst"
  [ "$status" -eq 0 ]
  run scripts/agent-hub backup verify "$BATS_TEST_TMPDIR/backup.tar.zst"
  [ "$status" -eq 0 ]
}

@test "failed readiness rolls back application version" {
  run env AGENT_HUB_FAKE_NEW_VERSION_UNHEALTHY=1 scripts/agent-hub upgrade --version 0.2.0
  [ "$status" -ne 0 ]
  [ "$(scripts/agent-hub version)" = "0.1.0" ]
}
```

- [ ] **Step 2: Implement maintenance commands for both modes**

Doctor checks service status, permissions, migration, database, Redis, model gateway, Feishu connector, Worker, disk, clock, TLS, and URLs. Backup captures PostgreSQL, config revisions, encrypted Secret rows, Artifact manifest/data, Skill packages, and version metadata. Restore verifies checksums and requires explicit target. Upgrade verifies release checksum/signature, backs up, migrates, restarts, checks health, and rolls back application plus compatible migration path on failure.

- [ ] **Step 3: Test and commit**

Run: `bash -n scripts/agent-hub scripts/commands/*.sh && shellcheck scripts/agent-hub scripts/commands/*.sh && bats tests/install/maintenance.bats`

Expected: doctor, backup, corrupted backup, restore target, upgrade success, signature failure, migration failure, and application rollback tests pass.

```bash
git add scripts/agent-hub scripts/commands tests/install/maintenance.bats
git commit -m "feat: add deployment maintenance commands"
```

### Task 7: Add security and resilience tests

**Files:**
- Create: `tests/security/test_tenant_isolation.py`
- Create: `tests/security/test_prompt_injection.py`
- Create: `tests/security/test_secret_leaks.py`
- Create: `tests/resilience/test_worker_crash.py`
- Create: `tests/resilience/test_provider_limits.py`
- Create: `tests/resilience/test_feishu_duplicates.py`

- [ ] **Step 1: Implement adversarial fixtures**

Fixtures cover cross-tenant UUID guessing, forged JWT/OAuth state/card action, malicious Skill archives, MCP schema injection, knowledge prompt injection, image metadata secrets, repeated Feishu events, Redis lease expiry, model 429 storms, Worker termination, and uncertain external writes.

- [ ] **Step 2: Assert safe outcomes**

Every attack must result in deny, quarantine, redaction, pending approval, bounded retry, or explicit failure. No test may merely assert that an exception occurred; assert durable state, absence of side effects, emitted audit event, and user-visible status.

- [ ] **Step 3: Run and commit**

Run: `uv run pytest tests/security tests/resilience -q`

Expected: all adversarial and recovery scenarios pass; no secret fixture value appears in captured logs or API bodies.

```bash
git add tests/security tests/resilience
git commit -m "test: add production security and resilience suite"
```

### Task 8: Add Docker and native distro installation matrices

**Files:**
- Create: `.github/workflows/quality.yml`
- Create: `.github/workflows/deploy-matrix.yml`
- Create: `tests/install/smoke.sh`
- Create: `tests/install/fixtures/unattended-docker.yml`
- Create: `tests/install/fixtures/unattended-native.yml`

- [ ] **Step 1: Define quality and deployment jobs**

Quality runs Python lint/type/test, Web lint/test/build, ShellCheck/Bats, dependency audit, secret scan, and container scan. Deployment matrix runs Compose and disposable Ubuntu 22.04/24.04, Debian 12/13, Rocky 9, and AlmaLinux 9 targets with the same release artifact.

- [ ] **Step 2: Implement smoke verification**

`smoke.sh` installs unattended, consumes the setup code, logs in, publishes a minimal config with fake LiteLLM, submits Direct/Dispatch/Discuss/Hybrid tasks, tests a serial model queue plus two-key pool, submits a sample image, exercises Skill approval/MCP, restarts Worker, runs backup/restore verification, and checks redacted audit output.

- [ ] **Step 3: Run available local jobs and commit**

Run: `uv run pytest -q && npm --prefix web run test -- --run && npm --prefix web run build && bats tests/install && bash tests/install/smoke.sh --mode docker`

Expected: all local checks pass; CI matrix definitions validate; Docker smoke ends with `AGENT_HUB_SMOKE_OK`.

```bash
git add .github tests/install
git commit -m "ci: add deployment and production validation matrix"
```

### Task 9: Write operator documentation and run the final acceptance gate

**Files:**
- Create: `README.md`
- Create: `docs/installation.md`
- Create: `docs/operations.md`
- Create: `docs/security.md`
- Create: `docs/feishu-setup.md`
- Create: `docs/model-pools.md`
- Create: `docs/skills-and-mcp.md`
- Create: `docs/troubleshooting.md`

- [ ] **Step 1: Document exact first-run paths**

README starts with verified release download, checksum verification, `sudo bash install.sh`, mode selection, the setup URL/code flow, cloud security-group note, and `scripts/agent-hub doctor`. Separate pages cover Feishu permissions/transports, model/key concurrency examples, Skill/MCP policy, backups, upgrades, and incident response.

- [ ] **Step 2: Add executable documentation checks**

Extract shell blocks marked `testable` and run them in CI. Validate internal Markdown links and assert that examples contain no real-looking secrets. Include these exact model-pool examples: one key with concurrency 8; one serial key with concurrency 1; four independent serial keys providing four slots; queue timeout then fallback.

- [ ] **Step 3: Run the global verification gate**

Run:

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
bats tests/install
```

Expected: every command exits `0`; CI deployment matrix is green; the installer prints a reachable management URL and one-time setup code in both modes.

- [ ] **Step 4: Commit the release-ready checkpoint**

```bash
git add README.md docs src tests web deploy scripts install.sh .github
git commit -m "docs: complete agent hub deployment handoff"
```
