# New-task durable split execution clock

Baseline: a35833c1e72a7ecc8f0f9843d7b391463bae26f7. This private candidate preserves Main's integrated consumer diagnostics. No shared source, deployment, Root, credentials or paid execution is changed here.

Ordinary authenticated API/UI submissions automatically select v2 when the server explicitly configures AGENT_HUB_RUNTIME_AUTHORIZATION_TIMEOUT_SECONDS. Default None leaves v2 disabled. No deployment value is selected by this patch. This setting is passed independently into the API and worker RunService and ReviewAssemblyBroker. Deployment must explicitly choose an authorized finite H; installing code with None does not activate the fix.

A remains the existing project assessment/default or explicit active task budget, never above 3600 seconds. H is an independent finite whole-second server-policy authorization horizon and may exceed 3600. The actual live broker signs H only within its configured server bound. Its original legacy submission_profile retains the 3600 limit. The authenticated service rechecks the live profile before the SQL transaction creates RunRow and its sealed clock. Public routing flags cannot grant v2 or increase H. Existing profile version2 still means activation continuity; its exact shape/hash is unchanged.

H is fixed to post-lock database creation time plus the approved duration. A, initial soft S and progress increments are sealed separately; S can grow only to A. Durable remaining intersects soft, active and authorization remaining. Each operation also intersects original lease/per-attempt limits. Increasing H never increases tokens, reviews or cost. A3600/H7200 is a controlled regression example, not a selected production policy. A3600/H3600 still has no extra waiting runway.

The clock row is atomically linked to original authenticated input and a parent witness. Run/clock locks and post-lock database time serialize claim, checkpoint, park and terminal close. The seal binds identity, created_at, H and policy/profile hashes. Restore must match exact checkpoint ID/hash. Crashes remain charged. Legacy rows have no marker and never receive a new clock or grant during recovery.

Parking requires actual preparation/generator cleanup, heartbeat cleanup and worker release plus a durable matching approval checkpoint. WAITING status alone, lease expiry, UNKNOWN model/tool attempts and uncertain subprocess cleanup never pause accounting. Confirmed terminal cleanup closes its active interval before controlled same-run repair waiting without resetting A, H or pool. Expired H rejects resume even while parked.

Consumers connect real submit/claim/checkpoint/resume, execution contexts, adaptive soft deadlines, physical model admission, original/generated Python preparation and Root claim guards. Before any longer-H pool, the broker validates private run/worker clock binding, durable marker, original profile hash, A<=3600 and created_at+H. Resume reuses the same pool and original token grant. Serialized action continuation retains historical total paired with cumulative elapsed while execution uses actual current remaining.

Migration 0037 adds the nullable legacy witness and clock table/composite identity constraints; downgrade removes child first. Actual DDL and new authority modules enter the 97-path runtime source fingerprint. Old native qualification proof cannot cover this new source.

Verification distinguishes controlled-clock SQL/service/broker tests and SQLite migration from real PostgreSQL. Actual PG 0036->0037/lifecycle execution is pending Main/Bohr; SQLite or fingerprint results do not replace it. Aristotle reviews exact final source before Main integrates. Old fc723 and its hard cutoff, authorization and terminal state are permanently excluded.
