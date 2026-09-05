# Project Workspace And Sandbox Permission Design

## Goal

Add explicit project workspace and sandbox permission selection to run submission without changing the harness execution contract.

## Contract

- Each run may carry a project id, project label, session id, sandbox profile, and requested permission ids.
- The values are stored in `routing_decision` so harness scheduling, details, audit logs, and future workspace tools share one source of truth.
- Workspace paths are logical storage paths, not host OS paths.
- Runtime tool escalation remains approval-gated through the existing capability approval flow.

## Non-goals

- Do not expose host absolute paths in the UI.
- Do not bypass per-tool approval.
- Do not change README or local handoff material in this feature slice.
