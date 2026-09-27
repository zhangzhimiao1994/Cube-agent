# Hermes Learning and Memory

Hermes is the system's governed learning and memory layer. It is not online model training and it does not modify model weights.

## Memory layers

Runtime context is assembled from bounded, persisted memory records:

- `working`: short-lived task context.
- `episodic`: outcomes and experience from earlier runs.
- `core`: reviewed facts, preferences, and durable rules.

Records are isolated by tenant and actor, with optional project and conversation scope. Unknown legacy scopes fail closed. Retrieved memory is treated as context only: it cannot grant permissions, select a more privileged execution mode, or bypass an approval.

## Learning loop

Completed tasks can produce evidence-backed learning candidates. Each candidate records its source run, category, summary, evidence, and proposed memory layer. A candidate remains inactive until an authorized reviewer approves it.

- Approval promotes the candidate into a locked, scoped memory record.
- Rejection keeps the decision and provenance without adding recallable memory.
- Deletion or forget operations revoke the promoted memory consistently.
- Promotion, rejection, deletion, and forgetting are serialized and audited.

Obvious secrets and unsafe values are rejected before storage. Generic memory mutation cannot rewrite locked Hermes rules.

## Product surfaces

The `Hermes 学习与记忆` page combines:

- pending, approved, and rejected learning candidates;
- working, episodic, and core memories;
- source task and source conversation links;
- the learning journey, including who approved a lesson and when;
- search and filters for reviewing accumulated knowledge.

Conversation slash commands provide quick access to memory and session controls. User-question checkpoints and full-history conversation search complement memory recall: old dialogue stays searchable without copying entire transcripts into the active prompt.

## Safety properties

Hermes can recommend dispatch mode, model candidates, Skill candidates, and risk or approval hints. It never executes actions directly, installs capabilities without an approved plan, or turns remembered text into authorization.
