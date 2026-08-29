# Mofang Agent Open Harness / DeepSeek Harness Refactor Design

Date: 2026-08-29

## Context

The user asked to continue the handoff plan, integrate kiwi-mem ideas into Hermes+, and produce a future refactor plan that combines:

- OpenAI's 2026-08-19 article, "Codex as a platform: build on the open agent harness": https://developers.openai.com/blog/codex-as-a-platform
- The local handoff's DeepSeek harness note, especially the idea that provider-specific protocol behavior should live in a reusable adapter/contract layer.
- The decision to keep Hermes+ work in the current/original repository first, while reserving the open harness / DeepSeek harness refactor for the later `https://github.com/zhangzhimiao1994/Cube-agent` repository.

## Design position

Do not rewrite the current/original repository as part of the Hermes+ delivery. Treat this document as the future `Cube-agent` repository route. After the original repository reaches a verified Hermes+ state and is pushed to `Cube-agent`, refactor it incrementally into a reusable harness architecture.

The compatibility goal is not a lowest-common-denominator merge. The two harness directions should raise capability together:

- OpenAI/Codex-style harness improves platform execution: long-running state, approvals, sandbox policy, tool orchestration, event streaming, resumability, and operational records.
- DeepSeek-style harness improves provider execution: reasoning stream normalization, streamed tool-call aggregation, context-window protection, prefix-cache-friendly prompt layout, and low-cost/high-throughput routing.

Mofang should keep a shared protocol for safety and observability, then let provider adapters declare enhanced capabilities so the scheduler can use each model where it is strongest.

1. Keep current production behavior stable.
2. Put a narrow, typed harness protocol around the existing run loop.
3. Move model/provider quirks behind provider adapters.
4. Route all capabilities through a policy-aware tool gateway.
5. Consume the already-completed Hermes+ memory layer as a first-class context provider for the harness.
6. Add Codex-style entrypoints over the same core behavior.

This gives Mofang Agent the same kind of separation OpenAI describes: product surfaces own records, dashboards, approvals, and business context; the harness owns conversation state, execution, streaming, tools, boundaries, and resumability.

## Proposed architecture

### 1. Harness Kernel

Owns the generic agent execution loop:

- Thread/run lifecycle: create, resume, cancel, archive, retry.
- Turn state: input, model call, tool-call loop, result synthesis.
- Event timeline: model delta, reasoning delta, tool requested, approval requested, tool result, warning, error, final.
- Checkpoints: recover from worker restart, failed model call, interrupted tool, or user approval wait.
- Context compaction: keep the working set inside model limits while preserving the important run state.

Future implementation in `Cube-agent` should wrap the current `RunService` lineage instead of replacing it.

### 2. Harness Protocol Contract

Create stable typed schemas for:

- `RunStart`
- `RunEvent`
- `ToolCallRequest`
- `ApprovalRequest`
- `ToolCallResult`
- `RunCheckpoint`
- `RunFinal`
- `ProviderUsage`

These schemas should become the source of truth for Web UI, API, worker, CLI/MCP entrypoints, replay tests, and production observability.

### 3. Provider Adapter Contract

Each model provider implements the same interface:

- Build request from normalized conversation, tools, and harness policy.
- Stream normalized events.
- Aggregate streamed tool calls.
- Report token usage and context-window risk.
- Normalize errors without leaking secrets.
- Expose provider feature flags.

Adapters should also expose a capability profile, for example:

- `supports_reasoning_delta`
- `supports_streamed_tool_call_delta`
- `supports_parallel_tool_calls`
- `supports_prefix_cache`
- `max_context_tokens`
- `preferred_prompt_layout`
- `cost_latency_tier`
- `tool_schema_strictness`

The scheduler can then route tasks by capability instead of flattening all providers into the same behavior.

DeepSeek-specific behavior belongs here, not in workflows/UI:

- thinking defaults
- `reasoning_content`
- streamed tool-call delta aggregation
- token-window validation
- prefix-cache estimation
- cache-friendly prompt layout
- sensitive header/error redaction

DeepSeek should be the first adapter hardened because the handoff explicitly called it out and provider behavior has high blast radius. The goal is to preserve DeepSeek's advantages inside the adapter rather than forcing it to behave like an OpenAI clone.

OpenAI/Codex-style execution should likewise preserve its platform strengths in the harness kernel rather than being reduced to only a chat-completions adapter.

### 4. Capability-Aware Scheduler

The scheduler should be defined by Mofang Harness Core, not by any single provider adapter, UI page, or workflow. Its job is to choose the best available execution path from explicit inputs:

1. Product policy: tenant configuration, cost/latency preference, safety posture, approval requirements, allowed providers, and admin overrides.
2. Task requirements: requested mode, tool intensity, context length, reasoning depth, streaming needs, privacy sensitivity, and deadline.
3. Provider capability profiles: features declared by adapters, such as reasoning deltas, streamed tool-call support, prefix cache, context window, tool schema strictness, cost tier, and reliability tier.
4. Runtime feedback: recent errors, queue saturation, rate limits, observed latency, budget usage, and fallback health.
5. Hermes+ context hints: project/conversation memory, prior successful modes, and confirmed lessons; Hermes+ may influence ranking but must not bypass policy.

The scheduler should output a transparent decision envelope:

- selected provider/deployment
- selected runtime mode
- capability reasons
- policy constraints applied
- fallbacks considered
- whether user approval is required

Provider adapters can declare strengths, but they cannot select themselves. UI/admin configuration can set policy, but it should not duplicate selection logic. Workflows can request requirements, but the scheduler decides within policy.

This keeps OpenAI/Codex-style platform strengths and DeepSeek-style provider strengths additive: the core chooses by capability, while adapters preserve their own advantages behind a common contract.

### 5. Tool / Capability Gateway

All operational capabilities should pass through one gateway:

- local tools
- MCP tools
- memory tools
- OpenClaw/open-browser style tools
- shell/file/deployment tools
- future plugin tools

The gateway enforces:

- tenant/user/project scope
- allow/deny policy
- approval requirements
- sandbox boundaries
- audit logging
- replay-safe tool result envelopes

This matches the open-harness idea that the product owns its tools and operational boundaries while the harness coordinates calls.

### 6. Hermes Memory+

Keep Hermes as the native memory system. Borrow kiwi-mem concepts without copying AGPL code:

- memory heat/reinforcement score
- decay and archival
- Dream-like consolidation jobs
- day/week/month summaries
- project/conversation isolation
- safe memory tools: search, save, get recent, lock, unlock, trigger digest

Hermes Memory+ should be completed in the original repository first. The future harness should then consume it through a context-provider interface, not as a special case buried in prompts.

### 7. Entry Points

Expose the same harness through multiple hosts:

- current Web UI/API
- background worker/scheduler
- MCP server
- CLI/non-interactive execution
- channel adapters such as Feishu/Telegram
- optional Codex app-server-compatible bridge later

The key rule: entrypoints are thin. They should not duplicate model/tool/memory logic.

## Test strategy

Before broad implementation, add provider/harness contract tests:

- multi-turn conversations
- tool calls and streamed tool-call aggregation
- provider reasoning content normalization
- context-window guardrails
- cache-friendly prompt consistency
- tool failure and retry
- approval pause/resume
- memory retrieval and memory write events
- secret redaction in provider errors
- replay from event log/checkpoint
- capability-aware model routing
- provider-specific enhanced behavior remaining available through normalized events
- scheduler decision envelopes include policy reasons, provider capability reasons, and safe fallbacks

CI should run the contract suite on mocked providers first, then allow optional real-provider tests when credentials/quota are explicitly available.

## Phased rollout

### Phase 0: Complete original-repository Hermes+

Finish the current CI closeout and Hermes+ implementation/deployment in the original repository.

### Phase 1: Push source to `Cube-agent`

Push the completed source to `https://github.com/zhangzhimiao1994/Cube-agent`. That repository becomes the source for harness refactor work.

### Phase 2: Design lock in `Cube-agent`

Review this harness design and write a DeepSeek provider contract checklist inside the new repository.

### Phase 3: Provider contract extraction

Extract provider request/stream/error normalization. Harden DeepSeek first, then OpenAI/Anthropic/Qwen/Kimi.

### Phase 4: Harness event protocol

Wrap current `RunService` with normalized event types, checkpoints, and replay tests.

### Phase 5: Tool gateway

Move tool invocation and approval policy into a single gateway.

### Phase 6: Hermes+ context provider

Connect completed Hermes+ to the harness context-provider interface.

### Phase 7: Entry points and operations

Add CLI/MCP/channel entrypoints over the same harness protocol, then update UI observability and deployment probes.

## Main risks

- Big-bang rewrite risk: avoid by wrapping existing behavior first.
- Provider regressions: mitigate with contract tests and replay fixtures.
- Lowest-common-denominator risk: avoid by keeping a shared protocol plus provider capability profiles, rather than erasing provider advantages.
- Memory privacy and prompt-injection risk: keep current Hermes filtering/confirmation/audit rules.
- AGPL contamination risk from kiwi-mem: borrow ideas and protocols, not implementation code, unless license obligations are explicitly accepted.
- CI/production drift: resolve current CI failure before merging broader changes.

## Recommendation

Proceed with an A+ path:

1. In the original repository, fix the current GitHub Actions red run and complete Hermes+ first.
2. Deploy Hermes+ incrementally to `prod-web-01` with real probes.
3. Push the completed source to `Cube-agent`.
4. In `Cube-agent`, start with DeepSeek provider contract hardening, then harness event protocol, then tool gateway, then entrypoints.
