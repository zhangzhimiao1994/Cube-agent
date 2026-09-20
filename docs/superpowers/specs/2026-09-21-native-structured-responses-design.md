# Native Structured Responses

## Evidence And Goal

Real dispatch now produces valid worker handoffs, but DeepSeek Chat Completions
rejects response_format=json_schema with HTTP 400 ("This response_format type is
unavailable now"). The same configured deployment accepted a native Responses
request with text.format=json_schema and returned a completed, metered JSON result.
Reference: https://api-docs.deepseek.com/api/create-response/.

Use an explicit, provider-neutral deployment setting, not model-name detection or
error-triggered protocol fallback. Preserve the user's model and exact schema.

## Contract

- Add structured_output_api: chat_completions | responses, default chat_completions.
- Only schema-bearing requests with responses selected use client.responses.create.
  Ordinary requests, tools without a schema, and ordinary Chat streaming retain
  existing behavior. Keep api_base as the API root; do not append duplicate paths.
- Configuration, admin projections/edits, runtime deployment construction and
  fingerprints must preserve the field. Existing configs remain compatible.
- No new host, key, role, tool authority, model, approval or runtime scheduling.
- Encode full ordered messages, original schema, tools, timeout and output budget.
  Use text.format with the original name/schema. No json_object downgrade, schema
  deletion, synthesized fields, automatic 400 protocol retries or thinking changes.
- Support schema plus tools and actual tool-result continuation, not only a no-tool
  probe. Responses function calls use call_id, not the output item's id.
- Reject unsupported input/stream combinations before sending. Never silently
  remove multimodal parts, schemas or tools. Plain Chat paths remain unchanged.
- Accept only complete, well-formed output. Ignore reasoning as private provider
  data, not user-facing handoff text. Reject refusal, incomplete/failed output,
  invalid calls and schema-invalid final text. Use existing JSON schema dependency
  with bounded, non-network validation; no coercion or parsing prose as JSON.
- Preserve exact usage and safe API-protocol metadata, cancellation and close.
  Failed responses must not be represented as successful zero-usage artifacts.
  Existing failure-usage persistence and DB-ack guarantees remain explicit separate
  work; this transport slice alone does not complete project capability acceptance.
- Existing /messages schema rejection stays fail-closed.

## Validation And Release

TDD wire tests: default Chat compatibility, explicit Responses selection, exact
schema/message/tool encoding, usage, function-call continuation, refusal and
malformed/schema-invalid responses, cancellation, close, redaction and streaming.
Configuration round-trip must preserve protocol choice through unrelated edits.

Run full unit/API/contracts, static checks and real PG/Crew capture. Deploy and
set only the tested DeepSeek deployment's structured_output_api through the
normal audited configuration publication path, preserving all other fields.
Run the unchanged actual dispatch probe and authenticated acceptance before push.
No pass if the final marker is missing, a review was skipped, or model fallback
was needed. Retain previous release/config for rollback; inspect CI after push.
