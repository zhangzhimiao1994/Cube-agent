# Split-clock merge slice

1. Preserve a35833c1 and integrated diagnostics in a private candidate; produce only clock deltas.
2. Persist sealed authorization/identity and durable active segments with migration 0037. Wire ordinary submit, claim, checkpoint, approval park/resume and confirmed terminal close.
3. Derive A/S from task policy; issue independent H only through explicit server policy and live submission profile. Keep legacy/TaskContext 3600 and original token policy.
4. Connect adaptive, repeated action continuation, resolution claims, broker and admission pool to the same sealed clock. Preserve UNKNOWN/replay/cleanup guards.
5. Record actual RED/GREEN for A3600/H7200 persisted broker lifecycle, cleanup parking, same-pool remainder, repeated approvals, seal mutation and migration. Rerun only changed/failing boundaries.
6. Strict and ruff all 19 changed production files. Keep test static evidence and actual PG separate; PG remains pending until Main/Bohr provides its receipt.
7. Freeze exact source/test pins and patch against a35833c1 for Aristotle review. Main chooses integration and explicit authorized deployment H. No Root, paid run, commit or shared edits here.
