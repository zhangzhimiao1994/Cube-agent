# Agent Hub Phase 1 Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver a runnable API foundation with typed settings, PostgreSQL migrations, durable domain state, versioned configuration, encrypted secrets, bootstrap authentication, and RBAC.

**Architecture:** Keep domain and application services independent of FastAPI and SQLAlchemy. Repositories translate between domain records and ORM rows; HTTP routers depend on services through FastAPI dependencies. Configuration is draft/publish based, and secrets are referenced by ID rather than embedded in configuration.

**Tech Stack:** Python 3.12, uv, FastAPI, Pydantic v2, SQLAlchemy 2 async, Alembic, PostgreSQL, Redis, cryptography, argon2-cffi, PyJWT, pytest, Ruff, mypy.

---

## File map

- `pyproject.toml`: Python dependencies and tooling.
- `src/agent_hub/app.py`: FastAPI application factory and lifespan.
- `src/agent_hub/settings.py`: environment-backed process settings.
- `src/agent_hub/db/base.py`, `session.py`, `models.py`: persistence infrastructure and ORM rows.
- `src/agent_hub/domain/runs.py`: task modes, statuses, and state transitions.
- `src/agent_hub/config/schema.py`, `repository.py`, `service.py`: validated, versioned configuration.
- `src/agent_hub/security/secrets.py`: envelope encryption and secret references.
- `src/agent_hub/auth/passwords.py`, `tokens.py`, `service.py`, `dependencies.py`: local login, bootstrap, JWT, and RBAC.
- `src/agent_hub/api/routers/system.py`, `auth.py`, `config.py`: initial HTTP surface.
- `alembic/`: database migrations.
- `tests/unit/`, `tests/integration/`: fast and PostgreSQL-backed tests.

### Task 1: Scaffold the Python application

**Files:**
- Create: `pyproject.toml`
- Create: `src/agent_hub/__init__.py`
- Create: `src/agent_hub/app.py`
- Create: `src/agent_hub/settings.py`
- Create: `tests/unit/test_app.py`

- [ ] **Step 1: Write the failing application test**

```python
from fastapi.testclient import TestClient

from agent_hub.app import create_app


def test_health_endpoint() -> None:
    client = TestClient(create_app())
    response = client.get("/health/live")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
```

- [ ] **Step 2: Run the test and confirm the package is missing**

Run: `uv run pytest tests/unit/test_app.py -q`

Expected: FAIL with `ModuleNotFoundError: No module named 'agent_hub'`.

- [ ] **Step 3: Add project metadata and the minimal app**

```toml
[project]
name = "agent-hub"
version = "0.1.0"
requires-python = ">=3.12,<3.13"
dependencies = [
  "alembic>=1.16,<2",
  "argon2-cffi>=23.1,<26",
  "asyncpg>=0.30,<1",
  "cryptography>=45,<47",
  "fastapi>=0.116,<1",
  "httpx>=0.28,<1",
  "pydantic-settings>=2.10,<3",
  "pyjwt[crypto]>=2.10,<3",
  "redis>=6,<7",
  "sqlalchemy[asyncio]>=2.0.41,<3",
  "uvicorn[standard]>=0.35,<1",
]

[dependency-groups]
dev = ["mypy>=1.17,<2", "pytest>=8.4,<9", "pytest-asyncio>=1,<2", "ruff>=0.12,<1"]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.pytest.ini_options]
testpaths = ["tests"]
asyncio_mode = "auto"

[tool.ruff]
line-length = 100

[tool.mypy]
python_version = "3.12"
strict = true
```

```python
# src/agent_hub/settings.py
from functools import lru_cache
from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AGENT_HUB_", env_file=".env")
    environment: str = "development"
    database_url: str = "postgresql+asyncpg://agent_hub:agent_hub@localhost/agent_hub"
    redis_url: str = "redis://localhost:6379/0"
    jwt_signing_key: SecretStr = SecretStr("development-only-change-me")
    master_key: SecretStr = SecretStr("AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=")


@lru_cache
def get_settings() -> Settings:
    return Settings()
```

```python
# src/agent_hub/app.py
from fastapi import FastAPI


def create_app() -> FastAPI:
    app = FastAPI(title="Agent Hub", version="0.1.0")

    @app.get("/health/live", tags=["system"])
    async def health_live() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()
```

- [ ] **Step 4: Lock dependencies and run quality checks**

Run: `uv lock && uv run pytest tests/unit/test_app.py -q && uv run ruff check src tests && uv run mypy src`

Expected: dependency lock succeeds; one test passes; Ruff and mypy exit `0`.

- [ ] **Step 5: Commit the scaffold**

```bash
git add pyproject.toml uv.lock src tests
git commit -m "chore: scaffold agent hub backend"
```

### Task 2: Add database sessions and the initial schema

**Files:**
- Create: `src/agent_hub/db/base.py`
- Create: `src/agent_hub/db/session.py`
- Create: `src/agent_hub/db/models.py`
- Create: `alembic.ini`
- Create: `alembic/env.py`
- Create: `alembic/versions/0001_initial.py`
- Create: `tests/integration/test_database.py`

- [ ] **Step 1: Write a PostgreSQL integration test**

```python
import pytest
from sqlalchemy import select

from agent_hub.db.models import TenantRow


@pytest.mark.integration
async def test_tenant_round_trip(db_session) -> None:
    tenant = TenantRow(slug="default", name="Default")
    db_session.add(tenant)
    await db_session.commit()
    loaded = await db_session.scalar(select(TenantRow).where(TenantRow.slug == "default"))
    assert loaded is not None
    assert loaded.name == "Default"
```

- [ ] **Step 2: Start test services and verify the migration is absent**

Run: `docker compose -f tests/compose.yml up -d postgres redis && uv run pytest tests/integration/test_database.py -q`

Expected: FAIL because `agent_hub_tenants` does not exist.

- [ ] **Step 3: Define the async database base, session factory, and core rows**

```python
# src/agent_hub/db/base.py
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass
```

```python
# src/agent_hub/db/session.py
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine


def build_session_factory(database_url: str) -> async_sessionmaker[AsyncSession]:
    engine = create_async_engine(database_url, pool_pre_ping=True)
    return async_sessionmaker(engine, expire_on_commit=False)
```

```python
# src/agent_hub/db/models.py
import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from agent_hub.db.base import Base


class TenantRow(Base):
    __tablename__ = "agent_hub_tenants"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    slug: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class UserRow(Base):
    __tablename__ = "agent_hub_users"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("agent_hub_tenants.id"))
    username: Mapped[str] = mapped_column(String(100), nullable=False)
    password_hash: Mapped[str | None] = mapped_column(Text)
    feishu_open_id: Mapped[str | None] = mapped_column(String(128))
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    __table_args__ = (UniqueConstraint("tenant_id", "username"),)


class ConfigRevisionRow(Base):
    __tablename__ = "agent_hub_config_revisions"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("agent_hub_tenants.id"))
    version: Mapped[int] = mapped_column(nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    document: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    __table_args__ = (UniqueConstraint("tenant_id", "version"),)
```

- [ ] **Step 4: Create and apply migration `0001_initial.py`**

The migration must create `agent_hub_tenants`, `agent_hub_users`, and `agent_hub_config_revisions` with the same columns and constraints as the ORM rows.

Run: `uv run alembic upgrade head && uv run pytest tests/integration/test_database.py -q`

Expected: migration succeeds and the round-trip test passes.

- [ ] **Step 5: Commit database infrastructure**

```bash
git add alembic.ini alembic src/agent_hub/db tests/compose.yml tests/integration
git commit -m "feat: add durable database foundation"
```

### Task 3: Implement the run state machine

**Files:**
- Create: `src/agent_hub/domain/runs.py`
- Create: `tests/unit/domain/test_runs.py`

- [ ] **Step 1: Write transition tests**

```python
import pytest

from agent_hub.domain.runs import InvalidTransition, RunStatus, TaskMode, transition


def test_wait_for_user_mode_before_running() -> None:
    assert transition(RunStatus.PLANNING, RunStatus.WAITING_USER_MODE) is RunStatus.WAITING_USER_MODE
    assert transition(RunStatus.WAITING_USER_MODE, RunStatus.RUNNING) is RunStatus.RUNNING


def test_completed_run_cannot_restart() -> None:
    with pytest.raises(InvalidTransition):
        transition(RunStatus.COMPLETED, RunStatus.RUNNING)


def test_all_modes_are_stable_wire_values() -> None:
    assert [mode.value for mode in TaskMode] == ["auto", "direct", "dispatch", "discuss", "hybrid"]
```

- [ ] **Step 2: Run tests and verify the module is absent**

Run: `uv run pytest tests/unit/domain/test_runs.py -q`

Expected: FAIL with module import error.

- [ ] **Step 3: Implement explicit transitions**

```python
from enum import StrEnum


class TaskMode(StrEnum):
    AUTO = "auto"
    DIRECT = "direct"
    DISPATCH = "dispatch"
    DISCUSS = "discuss"
    HYBRID = "hybrid"


class RunStatus(StrEnum):
    QUEUED = "queued"
    PLANNING = "planning"
    WAITING_USER_MODE = "waiting_user_mode"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    RETRYING = "retrying"
    PAUSED = "paused"
    SYNTHESIZING = "synthesizing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class InvalidTransition(ValueError):
    pass


ALLOWED: dict[RunStatus, set[RunStatus]] = {
    RunStatus.QUEUED: {RunStatus.PLANNING, RunStatus.CANCELLED},
    RunStatus.PLANNING: {RunStatus.WAITING_USER_MODE, RunStatus.RUNNING, RunStatus.FAILED, RunStatus.CANCELLED},
    RunStatus.WAITING_USER_MODE: {RunStatus.RUNNING, RunStatus.CANCELLED},
    RunStatus.RUNNING: {RunStatus.WAITING_APPROVAL, RunStatus.RETRYING, RunStatus.PAUSED, RunStatus.SYNTHESIZING, RunStatus.FAILED, RunStatus.CANCELLED},
    RunStatus.WAITING_APPROVAL: {RunStatus.RUNNING, RunStatus.CANCELLED, RunStatus.FAILED},
    RunStatus.RETRYING: {RunStatus.RUNNING, RunStatus.FAILED, RunStatus.CANCELLED},
    RunStatus.PAUSED: {RunStatus.RUNNING, RunStatus.CANCELLED},
    RunStatus.SYNTHESIZING: {RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED},
    RunStatus.COMPLETED: set(),
    RunStatus.FAILED: set(),
    RunStatus.CANCELLED: set(),
}


def transition(current: RunStatus, target: RunStatus) -> RunStatus:
    if target not in ALLOWED[current]:
        raise InvalidTransition(f"cannot transition {current.value} -> {target.value}")
    return target
```

- [ ] **Step 4: Run tests and commit**

Run: `uv run pytest tests/unit/domain/test_runs.py -q`

Expected: three tests pass.

```bash
git add src/agent_hub/domain tests/unit/domain
git commit -m "feat: define durable run state machine"
```

### Task 4: Add typed, versioned configuration

**Files:**
- Create: `src/agent_hub/config/schema.py`
- Create: `src/agent_hub/config/repository.py`
- Create: `src/agent_hub/config/service.py`
- Create: `tests/unit/config/test_schema.py`
- Create: `tests/integration/config/test_publish.py`

- [ ] **Step 1: Write schema and publish behavior tests**

```python
from pydantic import ValidationError
import pytest

from agent_hub.config.schema import AgentDefinition, PlatformConfig


def test_agent_requires_existing_logical_model() -> None:
    with pytest.raises(ValidationError):
        PlatformConfig(agents=[AgentDefinition(id="researcher", model="missing")], models={})


async def test_publish_creates_immutable_version(config_service, admin_id) -> None:
    draft = await config_service.create_draft(admin_id, {"models": {}, "agents": []})
    published = await config_service.publish(draft.id, admin_id)
    assert published.status == "published"
    assert published.version == 1
```

- [ ] **Step 2: Confirm tests fail**

Run: `uv run pytest tests/unit/config tests/integration/config -q`

Expected: FAIL because configuration classes and service are absent.

- [ ] **Step 3: Implement the configuration schema**

```python
from typing import Literal, Self
from pydantic import BaseModel, Field, model_validator


class DeploymentDefinition(BaseModel):
    provider: str
    model: str
    api_base: str | None = None
    secret_ref: str
    quota_scope_id: str
    max_concurrency: int = Field(default=1, ge=1, le=1000)
    target_utilization: float = Field(default=0.8, ge=0.5, le=0.9)
    reserved_slots: int = Field(default=0, ge=0)
    rpm: int | None = Field(default=None, ge=1)
    tpm: int | None = Field(default=None, ge=1)
    capabilities: set[Literal["text", "vision", "tool_calling", "structured_output"]] = Field(default_factory=lambda: {"text"})


class LogicalModelDefinition(BaseModel):
    deployments: list[DeploymentDefinition] = Field(min_length=1)
    fallback_model: str | None = None


class AgentDefinition(BaseModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9_-]{1,63}$")
    role: str = "assistant"
    prompt: str = "You are a helpful assistant."
    model: str
    skills: list[str] = Field(default_factory=list)


class PlatformConfig(BaseModel):
    models: dict[str, LogicalModelDefinition]
    agents: list[AgentDefinition]

    @model_validator(mode="after")
    def references_exist(self) -> Self:
        missing = {agent.model for agent in self.agents if agent.model not in self.models}
        if missing:
            raise ValueError(f"unknown logical models: {sorted(missing)}")
        return self
```

- [ ] **Step 4: Implement repository-backed draft, publish, and rollback**

`ConfigService.publish()` must lock the tenant revision sequence, validate `PlatformConfig`, mark the previous version superseded, insert the next version, and publish a `config.published` event only after commit. `rollback(version)` creates a new published version from the selected historical document rather than mutating history.

Run: `uv run pytest tests/unit/config tests/integration/config -q`

Expected: schema rejection, first publish, concurrent version allocation, immutable history, and rollback tests pass.

- [ ] **Step 5: Commit configuration versioning**

```bash
git add src/agent_hub/config tests/unit/config tests/integration/config
git commit -m "feat: add versioned platform configuration"
```

### Task 5: Encrypt dynamic secrets and deduplicate credentials

**Files:**
- Create: `src/agent_hub/security/secrets.py`
- Modify: `src/agent_hub/db/models.py`
- Create: `alembic/versions/0002_secrets.py`
- Create: `tests/unit/security/test_secrets.py`

- [ ] **Step 1: Write encryption and fingerprint tests**

```python
from agent_hub.security.secrets import SecretCipher


def test_secret_round_trip_and_stable_fingerprint(master_key: bytes) -> None:
    cipher = SecretCipher(master_key)
    first = cipher.seal("sk-example")
    second = cipher.seal("sk-example")
    assert first.ciphertext != second.ciphertext
    assert first.fingerprint == second.fingerprint
    assert cipher.open(first) == "sk-example"
    assert "sk-example" not in repr(first)
```

- [ ] **Step 2: Run the test and confirm failure**

Run: `uv run pytest tests/unit/security/test_secrets.py -q`

Expected: FAIL because `SecretCipher` is absent.

- [ ] **Step 3: Implement AES-GCM encryption and HMAC fingerprinting**

```python
import base64
import hashlib
import hmac
import os
from dataclasses import dataclass
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


@dataclass(frozen=True, repr=False)
class SealedSecret:
    nonce: str
    ciphertext: str
    fingerprint: str


class SecretCipher:
    def __init__(self, key: bytes) -> None:
        if len(key) != 32:
            raise ValueError("master key must be 32 bytes")
        self._key = key

    def seal(self, value: str) -> SealedSecret:
        nonce = os.urandom(12)
        encrypted = AESGCM(self._key).encrypt(nonce, value.encode(), b"agent-hub-secret-v1")
        fingerprint = hmac.new(self._key, value.encode(), hashlib.sha256).hexdigest()
        return SealedSecret(base64.b64encode(nonce).decode(), base64.b64encode(encrypted).decode(), fingerprint)

    def open(self, sealed: SealedSecret) -> str:
        plain = AESGCM(self._key).decrypt(base64.b64decode(sealed.nonce), base64.b64decode(sealed.ciphertext), b"agent-hub-secret-v1")
        return plain.decode()
```

- [ ] **Step 4: Add a unique `(tenant_id, fingerprint)` secret row and migration**

Run: `uv run alembic upgrade head && uv run pytest tests/unit/security tests/integration -q`

Expected: encryption test passes and inserting the same credential twice returns the existing secret reference.

- [ ] **Step 5: Commit secret storage**

```bash
git add src/agent_hub/security src/agent_hub/db alembic tests
git commit -m "feat: add encrypted secret references"
```

### Task 6: Implement bootstrap login and RBAC

**Files:**
- Create: `src/agent_hub/auth/models.py`
- Create: `src/agent_hub/auth/passwords.py`
- Create: `src/agent_hub/auth/tokens.py`
- Create: `src/agent_hub/auth/service.py`
- Create: `src/agent_hub/auth/dependencies.py`
- Create: `tests/unit/auth/test_rbac.py`
- Create: `tests/integration/auth/test_bootstrap.py`

- [ ] **Step 1: Write authorization and one-time setup tests**

```python
import pytest
from agent_hub.auth.models import Role
from agent_hub.auth.service import PermissionDenied


def test_operator_cannot_publish_configuration(authorizer) -> None:
    with pytest.raises(PermissionDenied):
        authorizer.require(Role.OPERATOR, "config:publish")


async def test_bootstrap_code_is_single_use(auth_service) -> None:
    code = await auth_service.issue_bootstrap_code(ttl_seconds=900)
    await auth_service.consume_bootstrap_code(code, "owner", "correct horse battery staple")
    with pytest.raises(PermissionDenied):
        await auth_service.consume_bootstrap_code(code, "other", "correct horse battery staple")
```

- [ ] **Step 2: Run and observe failure**

Run: `uv run pytest tests/unit/auth tests/integration/auth -q`

Expected: FAIL because auth types do not exist.

- [ ] **Step 3: Implement roles, Argon2id, JWT, and bootstrap hashes**

Define stable roles `super_admin`, `admin`, `operator`, and `viewer`. Store only a SHA-256 hash of the random 32-byte bootstrap code with `expires_at` and `consumed_at`. Passwords use `argon2.PasswordHasher`; access tokens contain `sub`, `tenant_id`, `role`, `iat`, and `exp` and are signed with the configured private key.

- [ ] **Step 4: Implement the permission matrix**

```python
PERMISSIONS: dict[Role, frozenset[str]] = {
    Role.SUPER_ADMIN: frozenset({"*"}),
    Role.ADMIN: frozenset({"config:*", "agent:*", "skill:*", "mcp:*", "run:*", "audit:read"}),
    Role.OPERATOR: frozenset({"run:create", "run:read", "run:pause", "run:resume", "run:cancel", "config:read"}),
    Role.VIEWER: frozenset({"run:read", "config:read", "audit:read"}),
}
```

Permission matching must support exact values and a trailing `*` namespace only; it must not use substring matching.

- [ ] **Step 5: Run tests and commit**

Run: `uv run pytest tests/unit/auth tests/integration/auth -q`

Expected: RBAC, password, JWT expiry, bootstrap expiry, and one-time consumption tests pass.

```bash
git add src/agent_hub/auth src/agent_hub/db alembic tests
git commit -m "feat: add bootstrap authentication and rbac"
```

### Task 7: Expose foundation APIs and finish the phase

**Files:**
- Modify: `src/agent_hub/app.py`
- Create: `src/agent_hub/api/dependencies.py`
- Create: `src/agent_hub/api/routers/system.py`
- Create: `src/agent_hub/api/routers/auth.py`
- Create: `src/agent_hub/api/routers/config.py`
- Create: `tests/api/test_foundation_api.py`

- [ ] **Step 1: Write API acceptance tests**

```python
def test_setup_login_and_publish(client, bootstrap_code) -> None:
    setup = client.post("/api/v1/setup", json={"code": bootstrap_code, "username": "owner", "password": "correct horse battery staple"})
    assert setup.status_code == 201
    token = setup.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    draft = client.post("/api/v1/config/drafts", headers=headers, json={"models": {}, "agents": []})
    assert draft.status_code == 201
    published = client.post(f"/api/v1/config/drafts/{draft.json()['id']}/publish", headers=headers)
    assert published.status_code == 200
    assert published.json()["version"] == 1
```

- [ ] **Step 2: Confirm endpoints return 404**

Run: `uv run pytest tests/api/test_foundation_api.py -q`

Expected: FAIL with the first `/api/v1/setup` request returning 404.

- [ ] **Step 3: Add routers and dependency wiring**

Expose `/health/live`, `/health/ready`, `/api/v1/setup`, `/api/v1/auth/login`, `/api/v1/config/current`, draft creation, validation, publish, history, diff, and rollback. Pydantic response models must omit secret ciphertext and fingerprints.

- [ ] **Step 4: Run the phase verification suite**

Run: `uv run alembic upgrade head && uv run ruff check . && uv run mypy src && uv run pytest tests/unit tests/integration tests/api -q`

Expected: all checks exit `0`; setup is single-use; an admin can publish and rollback config; an operator gets HTTP 403 on publish.

- [ ] **Step 5: Commit the phase checkpoint**

```bash
git add src tests alembic pyproject.toml uv.lock
git commit -m "feat: complete platform foundation"
```
