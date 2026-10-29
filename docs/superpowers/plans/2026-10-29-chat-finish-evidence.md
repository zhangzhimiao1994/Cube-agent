# Chat completion evidence and recovery

The ordinary-user Hybrid continuation reached successful native tools, then failed at
Crew implementer call 3 with `invalid_tool`, known paid usage, no text and output
status `unknown`. The historical private receipt did not retain finish_reason;
its exact original termination reason cannot now be asserted or upgraded.

The Chat parser currently infers completion from retained text. That loses a
confirmed `tool_calls`/`stop` outcome when text is absent or deliberately omitted.
It also parses tool JSON before checking explicit provider `length`, so a
truncated tool batch can become an unknown malformed-tool result.

## Design

- Separate confirmed finish state from optional private text. Completed malformed
  tools use the existing single, same-role, no-tool correction slot only after
  known usage, single received transport and quiet native tool history qualify.
- Explicit `length` rejects the entire response as incomplete before any tool
  can execute. Extend the existing post-tool recovery to explicitly enabled
  native workspace text without a JSON schema. Validate the full causal prefix
  and the closed correction boundary; later delivery rounds must still pass
  their original artifact graph checks. Preserve original deadline, budget,
  scope, accounting and native evidence.
- Unknown/refusal/filter outcomes never become correction authority. Invalid or
  missing usage retains its accounting guard. Never execute valid prefixes of
  rejected batches, reuse historical FAILED runs or synthesize final delivery.

## Implementation and verification

1. Add parser/SDK regressions; observe failure, then change only Chat parsing.
2. Exercise actual SDK parser -> gateway -> Crew correction with tool-only
   rejection and existing successful tool receipt; verify no duplicate tool.
3. Check explicit truncation, unknown/filter/usage/cancellation and restoration
   boundaries with focused adjacent tests; independent review before release.
4. Deploy a new sole-parent release; verify installed source and service health.
   Create a genuinely fresh ordinary-user acceptance after read-only scope
   binding. Only native completed Main/ZIP plus independent product checks count.

No provider/model/permission changes, GitHub/CI, generated source edits or
historical run resets. The separate host resource action may allocate 1 GiB
instead of 512 MiB to the environment cache only after measured free disk,
native admission lock, idle-run and exact configuration checks; retain its
1 GiB disk reserve, 24-hour TTL, entry limit and all UNKNOWN environments.
Bind the actual configuration again before the next ordinary-user submission.
This slice does not itself establish whole-user acceptance.
