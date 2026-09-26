# Active Run Queue Control Design

Date: 2026-09-26
Status: Proposed

## Intent

When a conversation already has a queued or running Agent run, a new user message must become a durable follow-up queue item instead of starting concurrently. The composer and queue item expose four clear behaviors:

- **Queue**: sending while a run is active adds the message behind that run.
- **Change direction**: stop the active direction at a safe boundary and promote this queued item to run next.
- **Edit queued message**: change the queued instruction in place without losing its position, attachments, or references.
- **Cancel queue**: withdraw the queued item before execution.

The behavior must be real backend state, tenant-scoped, auditable, idempotent, and consistent across desktop and mobile. Existing completed work, artifacts, and conversation history are preserved.

## Non-goals

- Do not inject text into a model request already in flight.
- Do not expose an `Interrupt` action.
- Do not start a second run concurrently in the same conversation.
- Do not kill an irreversible tool operation mid-call.
- Do not add manual workflow or model routing controls; Main Agent routing remains automatic.

## Chosen Architecture

Add a durable **conversation queue item** backed by a successor run. A queue item belongs to one tenant, conversation, active predecessor run, and successor run. The successor is created immediately with status `queued` and `blocked_by_run_id=active_run_id`, but workers cannot lease it until the dependency is released.

A PostgreSQL advisory lock keyed by tenant and conversation serializes queue creation, editing, cancellation, direction changes, and predecessor completion. This makes browser refreshes, multiple tabs, retries, and worker restarts converge on one state.

## Data Model

Add `agent_hub_conversation_queue_items` with:

- `id`, `tenant_id`, `conversation_id`
- `predecessor_run_id`, `successor_run_id`
- `message` plus the same safe attachment/reference fields accepted by ordinary run creation
- `position` and `idempotency_key`
- `status`: `queued`, `redirecting`, `released`, `cancelled`, `running`, `completed`, or `failed`
- `version` for optimistic editing
- `created_by`, `created_at`, `updated_at`, `released_at`
- redacted failure code/details suitable for operators

Add nullable `blocked_by_run_id` to runs. The run repository must exclude blocked runs from worker leasing. This is an execution dependency, not a visual-only status.

Constraints:

- idempotency is unique per tenant;
- queue position is unique within a conversation's active queue;
- records cannot cross tenant or conversation boundaries;
- a redirect may target only one queued item at a time;
- message and attachments follow existing validation and sensitive-data controls.

## API

### Add to queue

`POST /api/v1/admin/conversations/{conversation_id}/queue`

The request carries `active_run_id`, `message`, attachment/reference fields, and a client-generated `idempotency_key`. The server locks the conversation, confirms the referenced run is still the latest non-terminal run, and creates the queue item and blocked successor in one transaction. Replaying the key returns the original result.

### Edit queued message

`PATCH /api/v1/admin/conversation-queue/{queue_item_id}`

The request carries revised message/attachments and the current optimistic `version`. Editing is allowed only while status is `queued`. The queue item and successor run request update in the same transaction. Its position, ids, predecessor, project/workspace, sandbox policy, and creation audit remain unchanged.

A stale version returns `409 queue_item_changed`; an item already redirecting or released returns `409 queue_item_not_editable`.

### Change direction

`POST /api/v1/admin/conversation-queue/{queue_item_id}/redirect`

The server marks the item `redirecting`, requests cooperative cancellation of the predecessor, and makes this item the first eligible successor. Other queued items remain behind it in their existing order.

### Cancel queue

`DELETE /api/v1/admin/conversation-queue/{queue_item_id}`

Cancellation is allowed only before release. It marks both queue item and successor run cancelled in one transaction, compacts later queue positions, and leaves the conversation message visible as cancelled history. Repeated cancellation is idempotent.

Conversation detail includes non-terminal and recently terminal queue items so the UI can restore cards after refresh.

## Execution Semantics

### Normal queue release

When the active predecessor becomes completed, failed, or cancelled, the repository releases exactly the first queue item. Its successor loses `blocked_by_run_id` and becomes leaseable. When that successor becomes terminal, the next queue item is released. Only one run in a conversation owns the execution lease at a time.

### Change direction

At the next safe runtime boundary, the predecessor records `direction.changed`, saves a compatible checkpoint where supported, and becomes `cancelled` with reason `superseded_by_user`. The selected successor is released with durable conversation history, completed artifact references, the last compatible checkpoint when available, the revised queued message as controlling instruction, and automatic Main Agent routing.

Safe boundaries are after a model response is durably recorded, before or after a tool call, and between dispatch/discussion steps. If cooperative cancellation times out, the item remains visibly `redirecting`; it must not run concurrently. The existing Stop control remains a separate emergency action.

### Edit and race behavior

Editing does not create a new run and does not move the item. The worker reloads the successor request only after the item is released, under the conversation lock. If release wins the race, editing receives a conflict and the UI preserves the proposed text so it can be sent as a later queue item.

### Cancel behavior

Cancellation never deletes history or artifacts. It prevents the successor from being leased and records who cancelled it. If execution has already started, cancellation returns a conflict and the existing run Stop action is used instead.

## Frontend Interaction

The flow under test is: conversation with an active run -> user enters a message -> the send button reads `排队` -> submission creates a queue card -> the card supports change direction, edit, and cancel.

### Composer

- With no active run, the primary action remains `发送`.
- With an active or blocked run, it becomes `排队` with tooltip `当前任务完成后执行`.
- Submitting immediately creates the queue item; no extra chooser interrupts typing.
- A concise notice confirms its queue position.

### Queue card

Each queued message appears near the composer and at the matching point in conversation history. It shows queue order, a two-line message preview, attachment count, and status. Actions are:

- `改变方向`
- edit icon with tooltip `编辑排队信息`
- cancel icon with tooltip `取消排队`

Use familiar Lucide icons for edit and cancel; do not add text-filled rounded controls where an icon is sufficient.

Editing opens the queued text and attachments in a compact inline editor. `保存` sends the optimistic version; `取消编辑` restores the card without changing the queue. The card stays in place while editing. Errors keep the editor content.

Changing direction requires one explicit confirmation because it stops active work. Cancelling a queued item also requires a concise confirmation because it discards a pending instruction, but neither action navigates away from the conversation.

On mobile, the queue card and editor are full-width composer bands with at least 44px touch targets. Actions do not overlap the input, attachments, approvals, or Stop control. Desktop uses the same information hierarchy in a compact horizontal layout.

Keyboard behavior:

- Enter/Send while active queues the message.
- Escape closes the inline editor without discarding the queued item.
- Tab order follows Change direction, Edit, Cancel.

## Visibility and Audit

Conversation history shows Chinese queue status labels. Agent workbench shows the detailed dependency and direction-change events. Audit records include actor, tenant, conversation, predecessor, successor, queue item, action, position, and terminal status, but never raw message text.

## Error Handling

- `active_run_changed`: refresh and retry as an ordinary send or against the new active run; preserve the draft.
- `queue_item_changed`: reload the card and preserve proposed edits.
- `queue_item_not_editable`: show current state and preserve proposed text for a new queue item.
- `redirect_already_pending`: show the existing redirecting item.
- duplicate idempotency key: return the original queue result.
- worker crash: blocked/released state remains durable and lease recovery does not run two conversation turns concurrently.
- archived conversation: reject queue creation and all mutations.

## Testing

Backend tests cover tenant/conversation isolation, idempotent concurrent queue submissions, ordered release for all predecessor terminal states, blocked-run leasing, edit-in-place preservation, optimistic edit/release races, cancel compaction, redirect cancellation, execution lease exclusivity, audit redaction, and migration upgrade/downgrade.

Frontend tests are written first and cover ordinary `发送`, active-run `排队`, queue API routing, queue cards, in-place editing with preserved failures, direction-change confirmation, cancel confirmation, refresh restoration, keyboard behavior, and mobile layout.

Rendered acceptance covers desktop 1280x720 and mobile 390x844: no horizontal overflow, readable queue cards, stable composer height, console health, and visible state transitions for queue, edit, redirect, and cancel.

Production acceptance creates controlled long-running runs, verifies database/API state and artifacts for all four behaviors, then removes test conversations. GitHub checks must pass before deployment, and old server releases/temp packages are removed after acceptance.

## Rollout

1. Add schema, repository, ordered release, and API behind `active_run_queue_enabled=false`.
2. Add cooperative redirect handling and conversation execution lease.
3. Add frontend queue button, cards, editor, and confirmations.
4. Enable after all runtime modes pass ordered queue and redirect acceptance.

When disabled, the current Send and Stop behavior remains. No placeholder queue controls are shown.

