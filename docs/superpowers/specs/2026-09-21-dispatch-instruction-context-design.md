# Dispatch Instruction Context

## Scope

Extend the deployed direct session-guidance contract to dispatch execution
roles, tool continuation calls and reviewers. The same immutable load bundle
must reach each actual gateway request. This does not activate attachment rules,
installed skill packages, discuss/hybrid or planning approvals.

## Design

RunService loads guidance for direct and dispatch using the existing status,
lease, permission and scope checks. Crew uses TaskContext.validated_internal_clone
to preserve private content; public serialization remains metadata-only.

StepBridge and ReviewBridge append one bounded subordinate guidance block after
normalizing their messages and before constructing the request. Existing request
budget checks include the added bytes. Reviewer schema and tool restrictions,
role models, harness actor identity and scheduling behavior are unchanged.

Every real request with guidance emits context.injected at the gateway submission
boundary, using the existing emitter/sequence. Evidence records the load identity,
trusted actor and stage, step/attempt/call identity and the existing ledger key and
ledger request digest. Keep the ledger digest algorithm intact and distinguish
it from a full request digest. Never place load_id in prompts: it must not cause
recovery digest drift. Replayed successful entries, refused uncertain entries,
budget rejection, cancellation before submission and fixtures produce no new
injection evidence. All created tasks are collected on cancellation and timeout.

Source text and event data remain scoped to the run-local bridge. Do not mutate
global variables, environment, workflow/role configuration or routing to share
guidance. A changed source causing an existing ledger digest mismatch must fail
closed rather than silently reissue an uncertain model call.

## Public Evidence

Use stage values session_load, direct, dispatch_step and dispatch_review. Actors
come from runtime constants or validated AgentSpec identities, never model text.
Projection rejects unknown stage and untyped identity fields; legacy direct
events remain readable. Continue rejecting unknown payload keys and source text.

## Acceptance

Capture actual worker/reviewer/tool-continuation requests; correlate their source
digest and ledger digest. Test concurrent scopes, changed-source replay, paused
or revoked tasks, pre-submit cancellation and request budgets. Run Crew contract
and integration suites, existing direct regressions, strict projection tests and
server-specific feature probes. A fixture pass is not real provider evidence.
