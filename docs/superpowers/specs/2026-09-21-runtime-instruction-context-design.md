# Runtime Instruction Context

## Intent

Load actual authorized project guidance before model execution and expose
metadata proving which bytes were read and which content entered a model
request. User-approved continuation does not require another approval round.
This is not evidence that the model followed instructions or finished a project.

## First Deliverable

Support direct execution with the current session's root `AGENTS.md` and
`SKILL.md`. Reuse persisted run authorization and secure file handles from
`read_scoped_file`. Never search host parent directories. `SKILL.md` here is
project guidance, not approval or activation of an installed skill package.

Use a dedicated immutable TaskContext field, loaded by RunService and wired in
both API and worker construction. Do not source it from routing flags, history,
generated artifacts, provider responses, or API request parameters. Keep raw
guidance out of repr and ordinary event logs.

Reading and injection are separate facts. Loading records source path, run and
tenant, load identity, byte counts and content digests. A prefix digest is never
called a full-file digest. Missing/denied/unavailable inputs must not be counted
as reads; generic redacted unavailable is acceptable when missing cannot be
distinguished safely. Invalid UTF-8 is not silently treated as exact text.

Render guidance as bounded subordinate project context, below current user and
system instructions. It cannot grant tools, change sandbox/model/actor, or
override approval. Confirm injection at the final direct model request boundary,
after request validation and budget checks. Emit metadata, not content, tied to
the same load identity and actual submitted request. Replayed completions and
fixture shortcuts must not invent model requests or injected events.

Optional missing guidance must not break normal chat. Every execution refreshes
authorization; restored completed calls must not be silently replayed with new
guidance. Keep the current checkpoint lifecycle behavior intact.

## Follow-On Work

Linked repository attachment rules, dispatch/discuss/hybrid role requests,
approved installed skill versions, and plan-before-implementation evidence
remain explicit separate acceptance items. The capability process gate remains
closed until its full criteria are actually proven.

## Verification

Use real temporary files for loading and gateway capture for injection. Cover
tenant/session isolation, read permission revocation, links, missing files,
invalid text, bounded multibyte truncation, forged routing/history metadata,
concurrent scopes, budget rejection, fixture shortcuts, and checkpoint replay.
Repeat feature checks against production code with an isolated database and a
capturing gateway, then run one real direct provider task if authorized settings
allow it. Do not equate a capturing-gateway probe with real provider acceptance.
