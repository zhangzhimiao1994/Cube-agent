# Subagent Task Detail UI Design

## Goal

Remodel the run/process details UI into a readable agent-workforce view inspired by the provided Kimi screenshots, while keeping desktop and mobile layouts appropriate to their form factors.

## Scope

This change is frontend-only. It does not change scheduling, routing, runtime execution, or backend event generation.

The UI must make three things clear:

1. Which subagents participated in the task.
2. What each subagent produced.
3. What key activity happened without flooding the main chat with raw runtime events.

## Views

Each selected agent can expose one or two views:

- `活动轨迹`: the default view. Shows a compact timeline of meaningful activity such as step start, tool run, artifact creation, discussion, completion, or failure.
- `电脑视图`: optional. Only appears when the selected agent has real computer/screen/code-execution evidence. Current non-coding agents do not show this option.

If only `活动轨迹` is available, the UI must not show a fake view switcher.

## Desktop layout

Desktop must not copy the mobile bottom sheet. It should use available horizontal space:

- Left/center content remains the chat and task narrative.
- A compact process summary remains inline in the chat stream.
- Selecting a process item opens a side detail panel/drawer.
- The detail panel contains:
  - subagent work roster,
  - selected agent status,
  - selected agent output,
  - selected agent activity timeline,
  - raw event details only behind expansion.

This reduces the current bulky scheduling/runtime display by making outputs primary and process details secondary.

## Mobile layout

Mobile follows the Kimi-style model:

- Bottom sheet with large rounded corners for process details.
- Bottom horizontal subagent cards inside the sheet.
- Each card shows avatar/icon, role/name, short task, and status.
- Completed agents display `已下班`.
- Selecting a card switches the detail content above it.

## Data derivation

The frontend currently receives `RunDetail.events`, `RunDetail.artifacts`, and `explicit_details`. The new UI should derive a display model from those existing fields:

- `AgentWorkItem`
  - `id`
  - `label`
  - `role`
  - `status`: `working | waiting | done | failed`
  - `statusLabel`: `工作中 | 等待中 | 已下班 | 失败`
  - `summary`
  - `outputs`
  - `activity`
  - `availableViews`

Primary derivation rules:

- Group by event `actor`, `step_id`, `role`, or artifact producer-like fields when present.
- If a run has no clear subagent identity, create a single `主Agent` work item.
- Treat completed steps and final artifacts as outputs.
- Treat noisy infrastructure events such as frequent checkpoints as hidden-by-default activity.
- Keep raw details accessible from individual activity rows.

## Main display rules

The inline chat process summary should show:

- a concise overall status,
- a small roster count,
- key outputs,
- a limited number of activity rows,
- a button to open full details.

It should not show every raw event by default.

## Interaction

- Click subagent card: select that agent and show its output/activity.
- Click activity row: open event detail.
- Click output card: open artifact/detail.
- View switch menu appears only when the selected agent has more than one available view.
- The selected agent card uses a clear border.
- Completed agents show `已下班`.

## Testing requirements

Frontend tests must verify:

- completed subagents render `已下班`,
- selected subagent output is visible,
- selecting another subagent changes output/activity content,
- the view switcher is hidden when only `活动轨迹` is available,
- process summary no longer renders noisy raw checkpoint-heavy output as the main content,
- mobile/detail drawer still opens and remains usable.

No backend test is required unless the frontend schema changes.
