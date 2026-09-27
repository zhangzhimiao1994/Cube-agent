# Persistent Layered Memory Design

Date: 2026-09-27

## Goal

Close the gap between the production memory management API and runtime context. A memory saved or approved in the Web UI must be durably stored, scoped to the correct actor/project/conversation, retrieved deterministically, and supplied to every runtime mode as bounded guidance.

## Existing Problem

The repository has two memory paths:

- `MemoryService` implements rich layer/category/search behavior over an in-memory repository and is not wired into production.
- The admin API persists `memory` payloads in `AdminResourceRow`, while `PersistentHermesRunAdvisor` primarily reads `hermes` rows.

As a result, manually managed long-term memories can survive restarts and appear in the UI but are not a reliable runtime context source.

## Architecture

Keep `AdminResourceRow(kind="memory")` as the production source of truth. Do not add a second table or copy production data into the in-memory repository.

Add a focused persistent memory reader in the memory package. It converts backward-compatible admin payloads into a small runtime memory projection and performs:

1. tenant and actor ownership filtering;
2. project and conversation scope filtering;
3. keyword relevance scoring;
4. layer, lock, heat, confidence, and scope weighting;
5. bounded top-N selection;
6. recall-count, heat, and last-recalled updates in the same database transaction.

Add a dedicated runtime memory recall dependency to `RunService`. Explicit memory recall remains available when Hermes learning is disabled; the learning policy only controls learned recommendations. Recall is attached as `routing_decision.memory.items` without changing the selected mode solely because a memory was found. Runtime adapters temporarily keep reading the legacy `hermes.injected_memories` envelope for already queued runs.

## Data Contract

Extend the existing admin memory payload with backward-compatible defaults:

- `layer`: `core`, `episodic`, or `working`; default `core` for legacy explicit rules.
- `category`: `preference`, `fact`, `task`, `summary`, `decision`, `lesson`, or `other`; default `other`.
- `confidence`: `0..1`; default `1.0` for explicit user-created memory.
- `owner_actor_id`: actor UUID string or null. New user-scoped records use the current actor.
- existing `scope`, `project_id`, `conversation_id`, `heat`, `locked`, summary, and recall fields remain.

Scopes:

- `user:<actor-id>`: visible only to that actor.
- `tenant`: shared within the tenant and writable only through the existing governed admin API.
- project/conversation fields further narrow applicability; they never broaden actor or tenant access.

Hermes-approved memories keep their immutable `hermes-rule-*` lifecycle and are read through the same persistent memory reader after promotion.

## Runtime Flow

1. A run submission supplies actor, message, conversation ID, and project ID to the advisor.
2. The persistent reader retrieves applicable memory rows.
3. Core locked memories may match by scope without keyword overlap; episodic and working memories require relevance.
4. At most three memories are injected, with bounded summaries only.
5. The selected records receive recall reinforcement transactionally.
6. Runtime adapters consume the safe `memory.items` envelope and retain legacy Hermes-envelope compatibility.

Memory text remains guidance, never an authorization grant. It cannot select unavailable providers, bypass approvals, expand tool allowlists, or enable a capability.

## UI

The merged Hermes memory view exposes layer and category controls, project/conversation scope, and confidence. Human-readable Chinese labels explain the distinction between long-term core rules, episodic project facts, and short-lived working notes. Existing records render with safe defaults.

## Failure Behavior

Memory lookup is bounded and fail-open for run submission: a timeout or database read failure records a server log and continues without memory. Invalid or cross-user payloads are ignored. Recall reinforcement failure must not block the run and must not expose raw memory content in logs.

## Verification

- Unit tests for scope, ranking, legacy defaults, and bounded recall.
- PostgreSQL integration tests for persistence, actor isolation, project/conversation filtering, and recall reinforcement.
- Run-service tests proving explicit modes and auto mode receive memory without mode changes.
- API tests for the extended schema and protected Hermes records.
- Frontend tests for layer/category/scope controls and legacy rendering.
- Desktop/mobile browser acceptance, production API/runtime probe, full relevant suites, deployment, GitHub checks, and server cleanup.

## Non-goals

- No vector database or embedding dependency.
- No arbitrary model-written memory promotion.
- No cross-tenant or implicit cross-user sharing.
- No replacement of current conversation history/checkpoint storage.
