# Subagent Task Detail UI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Convert run/process details into a compact subagent workforce UI with per-agent outputs, activity tracks, and form-factor-specific desktop/mobile layouts.

**Architecture:** Keep this frontend-only by deriving an `AgentWorkItem` display model from the existing `RunDetail.events`, `RunDetail.artifacts`, and `explicit_details`. Reuse existing `RunsPage` process summary/drawer entry points, but split display into compact inline summary, selected-agent output, selected-agent activity, and raw detail drill-down.

**Tech Stack:** React, TypeScript, existing CSS in `web/src/styles.css`, Vitest/Testing Library, existing API schemas in `web/src/api/client.ts`.

---

## File structure

- Modify `web/src/pages/RunsPage.tsx`
  - Add derived subagent work item helpers near the current process/event helpers.
  - Replace the bulky process summary/drawer rendering with workforce-aware components.
  - Keep current API contracts and current run detail fetching.
- Modify `web/src/styles.css`
  - Add desktop side-panel/workforce styles and mobile bottom-sheet/member-card styles.
  - Keep existing responsive breakpoints.
- Modify `web/src/pages/OperationalPages.test.tsx`
  - Add tests for completed subagents showing `已下班`.
  - Add tests for per-agent output visibility and member switching.
  - Add tests that the view switcher is hidden when only `活动轨迹` is available.
  - Update existing Kimi/process tests to match the less bulky process display.
- Optionally modify `web/e2e/responsive-layout.spec.ts`
  - Only if the layout change needs an additional mobile/desktop regression assertion.
- Update `HANDOFF.md`, `progress.md`, and `task_plan.md`
  - Record the UI change, verification performed, and remaining risks.

## Task 1: Add failing frontend tests

**Files:**
- Modify: `web/src/pages/OperationalPages.test.tsx`

- [ ] **Step 1: Add a run detail fixture with two subagents and outputs**

Add or extend a fixture so it contains:

```ts
events: [
  { sequence: 1, kind: 'step.started', actor: 'planner', step_id: 'planner_step', payload: { role: 'Planner' } },
  { sequence: 2, kind: 'artifact.created', actor: 'planner', artifact_id: 'artifact-plan', step_id: 'planner_step' },
  { sequence: 3, kind: 'step.completed', actor: 'planner', step_id: 'planner_step' },
  { sequence: 4, kind: 'step.started', actor: 'reviewer', step_id: 'reviewer_step', payload: { role: 'Reviewer' } },
  { sequence: 5, kind: 'artifact.created', actor: 'reviewer', artifact_id: 'artifact-review', step_id: 'reviewer_step' },
  { sequence: 6, kind: 'step.completed', actor: 'reviewer', step_id: 'reviewer_step' },
  { sequence: 7, kind: 'checkpoint.saved', actor: 'reviewer', step_id: 'reviewer_step' },
],
artifacts: [
  { id: 'artifact-plan', type: 'text', producer: 'planner', content: { text: '计划输出：先拆解，再验证。' } },
  { id: 'artifact-review', type: 'text', producer: 'reviewer', content: { text: '审查输出：流程可执行。' } },
]
```

Use the actual fixture shape already present in the file; do not invent schema keys that conflict with existing tests.

- [ ] **Step 2: Test completed agent cards show `已下班`**

Add a test equivalent to:

```ts
expect(await screen.findByText('Planner')).toBeInTheDocument();
expect(screen.getAllByText('已下班').length).toBeGreaterThanOrEqual(2);
```

- [ ] **Step 3: Test selected agent output is visible and switchable**

Add a test equivalent to:

```ts
expect(screen.getByText(/计划输出：先拆解/)).toBeInTheDocument();
await userEvent.click(screen.getByRole('button', { name: /Reviewer/ }));
expect(screen.getByText(/审查输出：流程可执行/)).toBeInTheDocument();
```

- [ ] **Step 4: Test fake computer view is not shown**

Add a test equivalent to:

```ts
expect(screen.queryByText('电脑视图')).not.toBeInTheDocument();
expect(screen.getByText('活动轨迹')).toBeInTheDocument();
```

- [ ] **Step 5: Test noisy checkpoints are not primary rows**

Add a test equivalent to:

```ts
expect(screen.queryByText(/checkpoint.saved/)).not.toBeInTheDocument();
```

- [ ] **Step 6: Run tests and verify RED**

Run:

```powershell
npm --prefix web test -- --run src/pages/OperationalPages.test.tsx --reporter=dot
```

Expected: FAIL because the workforce UI, `已下班`, and output switching are not implemented yet.

## Task 2: Implement derived subagent work model and UI

**Files:**
- Modify: `web/src/pages/RunsPage.tsx`
- Modify: `web/src/styles.css`

- [ ] **Step 1: Add display model types**

Add local types in `RunsPage.tsx`:

```ts
type AgentWorkStatus = 'working' | 'waiting' | 'done' | 'failed';

type AgentWorkActivity = {
  id: string;
  title: string;
  kind: string;
  summary: string;
  event?: RunEvent;
  artifact?: RunArtifact;
};

type AgentWorkItem = {
  id: string;
  label: string;
  role: string;
  status: AgentWorkStatus;
  statusLabel: string;
  summary: string;
  outputs: AgentWorkActivity[];
  activity: AgentWorkActivity[];
  availableViews: Array<'activity' | 'computer'>;
};
```

Use existing imported frontend types if their names differ.

- [ ] **Step 2: Add helper to derive a stable agent id**

Create a helper that uses the first available value from event/artifact fields:

```ts
function agentKeyFromEvent(event: RunEvent): string {
  return safeString(event.actor) || safeString(event.step_id) || 'main-agent';
}
```

Use existing safe string helpers if present. If not present, add a local `safeString(value: unknown): string` that returns trimmed strings only.

- [ ] **Step 3: Add `buildAgentWorkItems(detail)`**

Implement:

- group events by agent key,
- attach artifacts to the matching agent via `producer`, `actor`, `step_id`, or artifact id referenced by an event,
- convert text artifacts into output activities,
- convert meaningful events into activity rows,
- hide checkpoint events from primary rows,
- mark status:
  - `failed` if any grouped event kind includes `failed`,
  - `working` if the run is running and the agent has a started event without completed/failed,
  - `done` if step completed or run completed,
  - otherwise `waiting`.
- return one `主Agent` item if no subagent can be derived.

- [ ] **Step 4: Replace process drawer content with workforce layout**

Update `RunProcessDrawer` so it:

- renders a roster of agent cards,
- stores selected agent id in React state,
- shows selected agent output first,
- shows selected agent activity second,
- renders raw details only when an activity row is opened or expanded,
- shows `活动轨迹`,
- only renders `电脑视图` when `availableViews` contains `computer`.

- [ ] **Step 5: Compact inline process summary**

Update `RunProcessSummary` so it:

- shows overall status and agent count,
- shows up to three key output/activity rows,
- opens the drawer for full details,
- does not list every checkpoint or raw event.

- [ ] **Step 6: Add CSS**

In `web/src/styles.css`, add classes for:

```css
.agent-workforce-layout
.agent-workforce-roster
.agent-workforce-card
.agent-workforce-card.is-active
.agent-workforce-status
.agent-workforce-output
.agent-workforce-activity
.agent-workforce-view-switch
```

Desktop behavior:

- roster and details use side-by-side grid.
- detail panel has a clear output-first hierarchy.

Mobile behavior under existing mobile breakpoint:

- roster becomes horizontal cards at the bottom of the drawer.
- drawer keeps rounded, Kimi-like card spacing.

- [ ] **Step 7: Run tests and verify GREEN**

Run:

```powershell
npm --prefix web test -- --run src/pages/OperationalPages.test.tsx --reporter=dot
```

Expected: PASS.

## Task 3: Broader frontend verification

**Files:**
- Modify tests only if required by legitimate behavior changes.

- [ ] **Step 1: Run full frontend tests**

Run:

```powershell
npm --prefix web test -- --run --reporter=dot
```

Expected: PASS.

- [ ] **Step 2: Run frontend type/lint**

Run:

```powershell
npm --prefix web run lint
```

Expected: PASS.

- [ ] **Step 3: Run frontend build**

Run:

```powershell
npm --prefix web run build
```

Expected: PASS. Existing Vite large chunk warning is acceptable if no new error is introduced.

- [ ] **Step 4: Run responsive Playwright if affected**

Run:

```powershell
npm --prefix web exec playwright test e2e/responsive-layout.spec.ts
```

Expected: PASS. If browser dependencies are unavailable, report the exact blocker and keep Vitest/build evidence.

## Task 4: Review and handoff

**Files:**
- Modify: `HANDOFF.md`
- Modify: `progress.md`
- Modify: `task_plan.md`

- [ ] **Step 1: Request independent review**

Reviewer must inspect diff and run at least:

```powershell
npm --prefix web test -- --run src/pages/OperationalPages.test.tsx --reporter=dot
npm --prefix web run lint
```

- [ ] **Step 2: Fix review issues**

Critical and Important issues must be fixed within scope before completion.

- [ ] **Step 3: Update handoff docs**

Record:

- changed files,
- tests run,
- desktop/mobile behavior,
- remaining risks.

- [ ] **Step 4: Final status check**

Run:

```powershell
git status --short
git diff --check
```

Expected: only intended files changed before commit, and no whitespace errors.

