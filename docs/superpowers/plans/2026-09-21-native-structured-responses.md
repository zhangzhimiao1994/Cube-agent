# Native Structured Responses Plan

Implement against the matching spec with subagents and TDD. Existing root task
authorizes continuation; this plan does not require another user confirmation.

1. Configuration owner: add typed structured_output_api to deployment/config,
   propagate through construction and admin read/edit/probe/fingerprints; test
   legacy compatibility and preservation when an edit omits the field.
2. Transport owner: implement native Responses encoding/parsing and explicit
   selection only for schema-bearing requests. Support tools/continuation,
   strict validation, usage, cancellation, redaction and close. Keep Chat paths.
3. Independent review: validate exact wire contract and configuration isolation;
   fix any findings without weakening user requirements.
4. Main verification: full unit/API/contracts and static checks, isolated server
   PG/Crew test, deployment and audited protocol-setting publication.
5. Run identical actual-provider dispatch acceptance, then authenticated regression;
   push and inspect CI only after passing. Clean unneeded releases/test resources.
6. Continue strict handoff/reviewer correction, then discuss/hybrid guidance and
   durability. The real multi-scale project capability gates remain unfulfilled.

Do not mix unrelated UI, memory, framework upgrades or protocol guessing into
this repair. Report limitations honestly, including structured Responses streaming
if not implemented, rather than sending the request via the wrong decoder.
